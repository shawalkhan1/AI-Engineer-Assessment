"""Command line entry point.

Exit codes:
  0  the batch completed and every case produced a record
  2  configuration failure (missing key, unpriced model, non-loopback target)
  3  execution failure (a case could not produce a record, or a run-level fault)

A case that was correctly handed to a human is **not** a failure and exits 0.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

from .config import (
    DEV_SESSION_CEILING_USD,
    SWEEP_ADMISSION_CEILING_USD,
    ConfigError,
    load_config,
    redact,
)
from .journal import Journal, JournalLocked
from .llm import LLMClient
from .ops_client import OpsClient
from .pipeline import CaseInputError, load_case, load_case_dir, run_case
from .report import (
    build_batch_summary,
    render_console_summary,
    write_batch_summary,
    write_case_record,
    _atomic_write_json,
    validate_case_id,
)
from .schemas import CaseStatus

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_EXECUTION = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aerlink",
        description="Aerlink disruption desk: works inbound passenger disruption cases.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--cases",
        type=Path,
        help="Directory of case sub-directories, each with inbound.txt and optional "
        "meta.json. Processed sequentially in filename order.",
    )
    source.add_argument(
        "--inbound",
        type=Path,
        help="A single inbound message file, anywhere on disk.",
    )
    parser.add_argument(
        "--meta",
        type=Path,
        help="Optional metadata JSON to accompany --inbound.",
    )
    parser.add_argument("--case-id", help="Identifier for a single --inbound case.")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Directory for case records and the batch summary.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Take no operations API write of any kind, including handovers. Model "
        "calls still happen and still cost money.",
    )
    parser.add_argument(
        "--journal",
        type=Path,
        help="Override the execution journal path. Defaults to state/journal.sqlite3 "
        "and is deliberately independent of --output.",
    )
    parser.add_argument(
        "--journal-namespace",
        default="default",
        help="Journal namespace. Use a distinct one for test runs so stale local "
        "rows cannot suppress a clean test.",
    )
    parser.add_argument(
        "--reset-ops",
        action="store_true",
        help="Deliberately POST /_reset before the run. This clears the server's "
        "writes AND its audit log. Never happens implicitly.",
    )
    parser.add_argument(
        "--run-ceiling-usd",
        type=Decimal,
        default=SWEEP_ADMISSION_CEILING_USD,
        help="Hard ceiling on calculated model spend for this run.",
    )
    parser.add_argument(
        "--session-ceiling-usd",
        type=Decimal,
        default=DEV_SESSION_CEILING_USD,
        help="Hard ceiling on cumulative model spend recorded in this journal.",
    )
    parser.add_argument(
        "--no-model",
        action="store_true",
        help="Run the deterministic path only, with no OpenAI calls. Cases will hand "
        "over; used for plumbing checks.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run and args.reset_ops:
        print("configuration error: --dry-run cannot be combined with --reset-ops", file=sys.stderr)
        return EXIT_CONFIG
    for ceiling in (args.run_ceiling_usd, args.session_ceiling_usd):
        if not ceiling.is_finite() or ceiling <= 0:
            print("configuration error: spending ceilings must be finite positive amounts", file=sys.stderr)
            return EXIT_CONFIG

    try:
        config = load_config(
            journal_namespace=args.journal_namespace,
            require_openai_key=not args.no_model,
        )
    except ConfigError as exc:
        print("configuration error: {}".format(exc), file=sys.stderr)
        return EXIT_CONFIG
    if args.journal:
        config.journal_path = args.journal

    try:
        cases = _collect_cases(args)
    except CaseInputError as exc:
        print("input error: {}".format(exc), file=sys.stderr)
        return EXIT_CONFIG
    if not cases:
        print("no cases found", file=sys.stderr)
        return EXIT_CONFIG

    run_id = "run-{}".format(uuid.uuid4().hex[:10])
    output_dir = Path(args.output)
    try:
        identifiers = [validate_case_id(case.case_id).casefold() for case in cases]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("duplicate case IDs would overwrite case records")
        if output_dir.exists() and any(output_dir.iterdir()):
            raise ValueError("output directory is not empty; choose a new --output to preserve prior records")
        output_dir.mkdir(parents=True, exist_ok=True)
        journal = Journal(config.journal_path, namespace=args.journal_namespace)
    except (OSError, ValueError, JournalLocked) as exc:
        print("configuration error: {}".format(exc), file=sys.stderr)
        return EXIT_CONFIG
    ops = OpsClient(config)
    notes: list[str] = list(config.notes)

    try:
        health = ops.health()
        if health.get("service") != "aerlink-ops":
            print(
                "the service at {} is not the Aerlink operations API (it reports "
                "{!r}). Refusing to send anything to it.".format(
                    config.ops_base_url, health.get("service")
                ),
                file=sys.stderr,
            )
            ops.close()
            journal.close()
            return EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001
        print(
            "could not reach the operations API at {}: {}".format(
                config.ops_base_url, redact(str(exc), config.openai_api_key, config.ops_api_key)[:200]
            ),
            file=sys.stderr,
        )
        ops.close()
        journal.close()
        return EXIT_CONFIG

    if args.reset_ops:
        if args.journal_namespace == "default":
            print(
                "--reset-ops requires a distinct --journal-namespace so that stale "
                "local journal rows cannot suppress actions in the clean run.",
                file=sys.stderr,
            )
            ops.close()
            journal.close()
            return EXIT_CONFIG
        ops.reset()
        # Record the reset in the journal. This is the only thing that makes an
        # earlier local "succeeded" row safely stale: without it, a journal row the
        # server cannot confirm is a disagreement to reconcile, not permission to
        # repeat the action.
        reset_at = journal.record_reset(
            ops_base_url=config.ops_base_url, run_id=run_id
        )
        notes.append(
            "POST /_reset was called deliberately before this run, under journal "
            "namespace {!r}. This clears the server's writes and its audit log; it "
            "does not and cannot erase records written outside it.".format(
                args.journal_namespace
            )
        )
        print("operations API reset (deliberate, --reset-ops) recorded at {}".format(reset_at))

    llm: LLMClient | None = None
    if not args.no_model:
        llm = LLMClient(
            config,
            journal,
            run_id,
            run_ceiling_usd=args.run_ceiling_usd,
            session_ceiling_usd=args.session_ceiling_usd,
        )
        try:
            verified = llm.verify_model_available()
            notes.append(
                "Model {} verified available for this key before any billed "
                "call.".format(verified["model"])
            )
        except Exception as exc:  # noqa: BLE001
            print(
                "Could not verify model {!r}; check connectivity, credentials and model access: {}".format(
                    config.openai_model, redact(str(exc), config.openai_api_key, config.ops_api_key)[:200]
                ),
                file=sys.stderr,
            )
            ops.close()
            journal.close()
            return EXIT_CONFIG

    if args.dry_run:
        notes.append(
            "DRY RUN: no operations API write was sent, including handovers. Model "
            "calls were still made and still cost money."
        )

    records = []
    exit_code = EXIT_OK
    try:
        for case in cases:
            print("[{}] working...".format(case.case_id), flush=True)
            outcome = run_case(
                case,
                config=config,
                ops=ops,
                journal=journal,
                llm=llm,
                run_id=run_id,
                dry_run=args.dry_run,
            )
            path = write_case_record(outcome.record, output_dir)
            records.append(outcome.record)
            print(
                "[{}] {} in {:.1f}s -> {}".format(
                    case.case_id, outcome.record.status.value, outcome.duration_s, path.name
                ),
                flush=True,
            )
            if outcome.record.status == CaseStatus.FAILED:
                exit_code = EXIT_EXECUTION

        audit = None
        if not args.dry_run:
            try:
                ops.begin_case()
                audit = ops.audit()
            except Exception as exc:  # noqa: BLE001
                notes.append(
                    "Final reconciliation against GET /_audit failed: {}".format(
                        redact(str(exc), config.openai_api_key, config.ops_api_key)[:200]
                    )
                )

        if audit is not None:
            _atomic_write_json(output_dir / "ops-audit.json", audit)
        summary = build_batch_summary(
            records,
            run_id=run_id,
            dry_run=args.dry_run,
            usage_totals=journal.usage_totals(run_id=run_id),
            audit=audit,
            config_description=config.describe(),
            notes=notes,
            server_was_reset=args.reset_ops,
        )
        rec = summary["operations_api_reconciliation"]
        if not args.dry_run and (not rec.get("performed") or rec.get("recorded_but_absent_from_server")
                                 or rec.get("in_server_but_not_recorded_by_us")):
            exit_code = EXIT_EXECUTION
        # A successful process is insufficient: validate the resulting decisions and replies.
        audit_check = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "tools/inspect_run.py"), str(output_dir)],
            capture_output=True, text=True, timeout=120,
        )
        summary["quality_audit"] = {"passed": audit_check.returncode == 0,
                                    "details": audit_check.stdout + audit_check.stderr}
        if audit_check.returncode:
            exit_code = EXIT_EXECUTION
            print(audit_check.stdout, file=sys.stderr)
        summary_path = write_batch_summary(summary, output_dir)
        print(render_console_summary(summary))
        print("\nrecords: {}\nsummary: {}".format(output_dir, summary_path))
    finally:
        ops.close()
        journal.close()

    return exit_code


def _collect_cases(args: argparse.Namespace) -> list:
    if args.inbound:
        return [
            load_case(inbound=args.inbound, meta=args.meta, case_id=args.case_id)
        ]
    root = Path(args.cases)
    if not root.is_dir():
        raise CaseInputError("--cases must be a directory, got {}".format(root))
    cases = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (directory / "inbound.txt").is_file():
            continue
        cases.append(load_case_dir(directory))
    return cases


if __name__ == "__main__":
    raise SystemExit(main())
