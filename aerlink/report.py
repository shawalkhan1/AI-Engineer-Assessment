"""Writing the record out, and summarising a batch.

Case records are written atomically -- rendered to a temporary file in the same
directory and then replaced -- so an interrupted run never leaves a half-written
record that looks parseable.

The batch summary deliberately includes a reconciliation against the operations
API's own `GET /_audit`: every write we claim, and every write the server holds.
A run that exits cleanly is not evidence that the right things happened.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from .schemas import ActionState, ActionType, CaseRecord, CaseStatus

# The identifier fields the operations API returns on a write (API.md S12-S18).
_ID_FIELDS = (
    "rebooking_id",
    "voucher_id",
    "payment_id",
    "refund_id",
    "escalation_id",
)


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False, default=_default)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp"
    )
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)


def write_case_record(record: CaseRecord, output_dir: Path) -> Path:
    path = Path(output_dir) / "{}.json".format(record.case_id)
    _atomic_write_json(path, record.model_dump(mode="json"))
    return path


def build_batch_summary(
    records: list[CaseRecord],
    *,
    run_id: str,
    dry_run: bool,
    usage_totals: dict[str, Any],
    audit: dict[str, Any] | None,
    config_description: dict[str, Any],
    notes: list[str],
    server_was_reset: bool = False,
) -> dict[str, Any]:
    statuses = Counter(r.status.value for r in records)
    action_states: Counter[str] = Counter()
    money_minor = 0
    claimed_ids: set[str] = set()

    for record in records:
        for action in record.actions:
            action_states["{}:{}".format(action.action_type.value, action.state.value)] += 1
            if action.state == ActionState.SUCCEEDED and action.amount is not None:
                if action.action_type in {
                    ActionType.COMPENSATION_PAYMENT,
                    ActionType.GOODWILL_PAYMENT,
                    ActionType.REFUND,
                }:
                    money_minor += action.amount.amount_minor
            if action.state == ActionState.SUCCEEDED:
                # Only the identifier fields. `returned_ids` also carries timestamps
                # and statuses, and matching on "looks like it has a hyphen" pulled
                # ISO dates in and reported them as writes missing from the server.
                for key in _ID_FIELDS:
                    value = (action.returned_ids or {}).get(key)
                    if isinstance(value, str) and value:
                        claimed_ids.add(value)

    reconciliation: dict[str, Any] = {"performed": False}
    if audit is not None:
        server_ids: set[str] = set()
        server_money = Decimal("0")
        writes = audit.get("writes", {})
        for collection, rows in writes.items():
            for row in rows:
                for key in _ID_FIELDS:
                    if row.get(key):
                        server_ids.add(str(row[key]))
                if collection in {"payments", "refunds"} and row.get("amount_gbp") is not None:
                    server_money += Decimal(str(row["amount_gbp"]))
        reconciliation = {
            "performed": True,
            "server_totals": audit.get("totals", {}),
            "ids_we_recorded": sorted(claimed_ids),
            "ids_in_server_audit": sorted(server_ids),
            # Writes we claim that the server does not hold are always a problem.
            "recorded_but_absent_from_server": sorted(claimed_ids - server_ids),
            # Writes the server holds that this run did not make are only meaningful
            # when the run started from a reset server. Otherwise they are simply
            # earlier history, and flagging them buries the real signal.
            "in_server_but_not_recorded_by_us": (
                sorted(server_ids - claimed_ids) if server_was_reset else []
            ),
            "server_writes_predating_this_run": (
                0 if server_was_reset else len(server_ids - claimed_ids)
            ),
            "server_was_reset_for_this_run": server_was_reset,
            "money_we_recorded_gbp": "{}.{:02d}".format(money_minor // 100, money_minor % 100),
            "money_in_server_audit_gbp": str(server_money),
            "note": (
                "POST /_reset clears this log, so it is the state since the last "
                "reset rather than permanent history."
            ),
        }

    return {
        "run_id": run_id,
        "dry_run": dry_run,
        "cases": len(records),
        "statuses": dict(sorted(statuses.items())),
        "status_note": (
            "These are counts of outcomes, not an accuracy score. A case that was "
            "correctly handed to a human is a correct outcome, and a resolved case is "
            "only correct if the money and the journey were right."
        ),
        "actions": dict(sorted(action_states.items())),
        "model_usage": usage_totals,
        "operations_api_reconciliation": reconciliation,
        "configuration": config_description,
        "notes": notes,
        "per_case": [
            {
                "case_id": r.case_id,
                "status": r.status.value,
                "booking_ref": r.identity_resolution.get("verified_booking_ref"),
                "identity_confirmed": r.identity_resolution.get("confirmed"),
                "actions": [
                    {
                        "type": a.action_type.value,
                        "state": a.state.value,
                        "amount": str(a.amount) if a.amount else None,
                        "ids": a.returned_ids,
                    }
                    for a in r.actions
                ],
                "human_handover_required": r.human_handover.required,
                "escalation_ids": r.human_handover.escalation_ids,
                "uncertainties": len(r.uncertainties),
                "errors": len(r.errors),
                "cost_usd": r.usage.get("case_calculated_cost_usd"),
            }
            for r in records
        ],
    }


def write_batch_summary(summary: dict[str, Any], output_dir: Path) -> Path:
    path = Path(output_dir) / "batch-summary.json"
    _atomic_write_json(path, summary)
    return path


def render_console_summary(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("")
    lines.append("run {}{}".format(summary["run_id"], "  [DRY RUN]" if summary["dry_run"] else ""))
    lines.append("-" * 72)
    for case in summary["per_case"]:
        done = [
            a
            for a in case["actions"]
            if a["state"] in {"succeeded", "would_execute"} and a["type"] != "escalation"
        ]
        summary_bits = ", ".join(
            "{}{}".format(a["type"], " " + a["amount"] if a["amount"] else "")
            for a in done
        )
        lines.append(
            "{:<10} {:<20} {:<12} {}".format(
                case["case_id"],
                case["status"],
                case["booking_ref"] or "-",
                summary_bits or ("referred" if case["human_handover_required"] else ""),
            )
        )
    lines.append("-" * 72)
    lines.append("statuses: " + json.dumps(summary["statuses"]))
    usage = summary["model_usage"]
    lines.append(
        "model: {} calls, {} in / {} out tokens, calculated ${}".format(
            usage.get("calls"),
            usage.get("tokens", {}).get("input"),
            usage.get("tokens", {}).get("output"),
            usage.get("calculated_cost_usd"),
        )
    )
    if Decimal(usage.get("unresolved_reservation_upper_bound_usd", "0")) > 0:
        lines.append(
            "  plus ${} of unresolved reservations across {} call(s) -- worst case "
            "${}".format(
                usage.get("unresolved_reservation_upper_bound_usd"),
                usage.get("unresolved_calls"),
                usage.get("worst_case_total_usd"),
            )
        )
    rec = summary.get("operations_api_reconciliation", {})
    if rec.get("performed"):
        lines.append(
            "operations API: {} money paid, our records show {}".format(
                rec.get("money_in_server_audit_gbp"), rec.get("money_we_recorded_gbp")
            )
        )
        if rec.get("recorded_but_absent_from_server"):
            lines.append(
                "  WARNING: recorded but absent from the server audit: "
                + ", ".join(rec["recorded_but_absent_from_server"])
            )
        if rec.get("in_server_but_not_recorded_by_us"):
            lines.append(
                "  WARNING: the server was reset for this run, yet it holds writes "
                "this run did not make: "
                + ", ".join(rec["in_server_but_not_recorded_by_us"])
            )
        elif rec.get("server_writes_predating_this_run"):
            lines.append(
                "  ({} earlier write(s) already on the server; it was not reset for "
                "this run)".format(rec["server_writes_predating_this_run"])
            )
    return "\n".join(lines)
