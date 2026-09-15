"""Working one case, end to end.

Ingest -> read the contact (model call 1) -> confirm identity -> gather the
operational facts -> evaluate deterministically -> execute what is authorised ->
refer what is not -> draft the reply (model call 2) -> write the record.

The order matters. Nothing is executed before identity is confirmed, no amount is
chosen outside `policy`/`planner`, and the reply is drafted last, from outcomes that
have already happened.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import policy
from .config import CASE_TIMEOUT_S, MAX_INBOUND_BYTES, Config
from .executor import Executor, escalation_proposal, referral_key
from .extract import apply_span_verification, run_extraction
from .identity import resolve_identity
from .journal import Journal
from .llm import BudgetExhausted, LLMClient, ModelCallLimitReached, ModelOutputInvalid
from .narrate import fallback_reply, run_narration
from .report import validate_case_id
from .ops_client import OpsClient, OpsError, OpsTransportError, AttemptBudgetExhausted
from .planner import (
    CaseFacts,
    HandoverItem,
    Planner,
    live_requests,
)
from .schemas import (
    ActionState,
    ActionType,
    CaseRecord,
    CaseStatus,
    Extraction,
    HumanHandover,
    PlannedAction,
    SourceRef,
    Uncertainty,
)
from .timeutil import UnknownCaseTime, case_now
from .untrusted import detect_injection_indicators, extract_urls

MAX_ESCALATIONS_PER_CASE = 3


class CaseInputError(Exception):
    """The case could not be read or validated."""


@dataclass
class CaseInput:
    case_id: str
    inbound_path: Path
    meta_path: Path | None
    text: str
    meta: dict[str, Any] | None
    sha256: str

    @property
    def meta_from(self) -> str | None:
        return (self.meta or {}).get("from")


def load_case(
    *, inbound: Path, meta: Path | None, case_id: str | None
) -> CaseInput:
    """Read and validate one inbound message and its optional metadata."""
    inbound = Path(inbound)
    if not inbound.is_file():
        raise CaseInputError("No inbound message at {}".format(inbound))
    raw = inbound.read_bytes()
    if not raw.strip():
        raise CaseInputError("Inbound message at {} is empty.".format(inbound))
    if len(raw) > MAX_INBOUND_BYTES:
        raise CaseInputError(
            "Inbound message at {} is {} bytes, above the {} byte limit.".format(
                inbound, len(raw), MAX_INBOUND_BYTES
            )
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CaseInputError(
            "Inbound message at {} is not valid UTF-8: {}".format(inbound, exc)
        ) from exc

    meta_data: dict[str, Any] | None = None
    if meta is not None:
        meta = Path(meta)
        if not meta.is_file():
            raise CaseInputError("No metadata file at {}".format(meta))
        try:
            meta_data = json.loads(meta.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CaseInputError(
                "Metadata at {} is not readable JSON: {}".format(meta, exc)
            ) from exc
        if not isinstance(meta_data, dict):
            raise CaseInputError(
                "Metadata at {} must be a JSON object, got {}.".format(
                    meta, type(meta_data).__name__
                )
            )
        for name in ("from", "received_at", "case_id"):
            if name in meta_data and not isinstance(meta_data[name], str):
                raise CaseInputError(
                    "Metadata field {!r} must be a string, got {}.".format(
                        name, type(meta_data[name]).__name__
                    )
                )

    resolved_id = case_id or (meta_data or {}).get("case_id") or inbound.parent.name
    try:
        resolved_id = validate_case_id(str(resolved_id))
    except ValueError as exc:
        raise CaseInputError(str(exc)) from exc
    return CaseInput(
        case_id=str(resolved_id),
        inbound_path=inbound,
        meta_path=meta,
        text=text,
        meta=meta_data,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def load_case_dir(directory: Path, case_id: str | None = None) -> CaseInput:
    directory = Path(directory)
    meta = directory / "meta.json"
    return load_case(
        inbound=directory / "inbound.txt",
        meta=meta if meta.is_file() else None,
        case_id=case_id,
    )


class InventoryAdapter:
    """The planner's window onto live inventory, with failures made explicit."""

    def __init__(self, ops: OpsClient) -> None:
        self.ops = ops

    def own_availability(
        self, origin: str, destination: str, date_iso: str, booking_ref: str
    ) -> dict[str, Any]:
        try:
            return self.ops.availability(
                origin, destination, date_iso, booking_ref=booking_ref
            )
        except (OpsError, OpsTransportError, AttemptBudgetExhausted) as exc:
            return {"unavailable": True, "error": str(exc)[:200], "results": []}

    def partner_availability(
        self, origin: str, destination: str, date_iso: str, booking_ref: str
    ) -> dict[str, Any]:
        try:
            return self.ops.availability(
                origin, destination, date_iso, booking_ref=booking_ref, partners=True
            )
        except (OpsError, OpsTransportError, AttemptBudgetExhausted) as exc:
            return {"unavailable": True, "error": str(exc)[:200], "results": []}

    def hotel_allocation(self, iata: str, night_iso: str) -> dict[str, Any] | None:
        try:
            return self.ops.hotel_allocation(iata, night_iso)
        except OpsError as exc:
            if exc.status == 404:
                return None
            return None
        except (OpsTransportError, AttemptBudgetExhausted):
            return None

    def existing_hotel_voucher(
        self, booking_ref: str | None, station: str, night_iso: str
    ) -> dict[str, Any] | None:
        """A voucher this booking already holds for that station and night, if any.

        There is no documented read endpoint for vouchers by booking, so the committed
        writes in `GET /_audit` are the only live source -- the same source the
        executor's duplicate detection already trusts.

        Returns None both for "no such voucher" and for "could not tell". That
        conflation is deliberate and safe in this direction: not knowing sends the
        case to a human, which costs a colleague a minute. The opposite default would
        risk issuing a second room, which S12.2 forbids.
        """
        if not booking_ref or not station:
            return None
        try:
            audit = self.ops.audit()
        except (OpsError, OpsTransportError, AttemptBudgetExhausted):
            return None
        vouchers = ((audit or {}).get("writes") or {}).get("hotel_vouchers") or []
        for voucher in vouchers:
            if not isinstance(voucher, dict):
                continue
            if (
                str(voucher.get("booking_ref", "")).upper() == booking_ref.upper()
                and str(voucher.get("station", "")).upper() == str(station).upper()
                and str(voucher.get("night", "")) == str(night_iso)
            ):
                return voucher
        return None


@dataclass
class CaseOutcome:
    record: CaseRecord
    duration_s: float
    fatal: bool = False
    notes: list[str] = field(default_factory=list)


def run_case(
    case: CaseInput,
    *,
    config: Config,
    ops: OpsClient,
    journal: Journal,
    llm: LLMClient | None,
    run_id: str,
    dry_run: bool,
) -> CaseOutcome:
    started_monotonic = time.monotonic()
    started_at = _utcnow()
    ops.begin_case()
    if llm is not None:
        llm.begin_case(case.case_id)

    errors: list[dict[str, Any]] = []
    uncertainties: list[Uncertainty] = []
    actions: list[PlannedAction] = []
    handovers: list[HandoverItem] = []
    needs_input: list[str] = []
    model_usage: list[dict[str, Any]] = []
    status = CaseStatus.FAILED
    extraction: Extraction | None = None
    span_report: dict[str, Any] = {}
    plan = None
    facts: CaseFacts | None = None
    identity = None
    reply: Any = None
    reply_was_model = False
    executor: Executor | None = None
    recovery_needed = False

    injection_indicators = detect_injection_indicators(case.text)
    urls = extract_urls(case.text)

    provenance = {
        "inbound_path": str(case.inbound_path),
        "inbound_sha256": case.sha256,
        "inbound_bytes": len(case.text.encode("utf-8")),
        "metadata_path": str(case.meta_path) if case.meta_path else None,
        "metadata_origin": (
            "supplied meta.json, treated as trusted transport metadata"
            if case.meta
            else "none supplied; identity attempted from the message alone"
        ),
        "metadata_fields": sorted(case.meta) if case.meta else [],
        "trust_model": (
            "meta.from is transport metadata from the mail system. Everything inside "
            "inbound.txt, including any header it quotes, is passenger-supplied and "
            "untrusted (S12.4)."
        ),
        "urls_in_message_not_followed": urls,
        "injection_indicators": injection_indicators,
    }

    try:
        now, now_source = case_now((case.meta or {}).get("received_at"), case.text)
        provenance["case_now"] = now.isoformat()
        provenance["case_now_source"] = now_source

        # --- 1. read the contact (model call 1) --------------------------
        if llm is not None:
            try:
                result = run_extraction(
                    llm,
                    inbound_text=case.text,
                    meta=case.meta,
                    received_at=(case.meta or {}).get("received_at"),
                )
                extraction = result.parsed
                model_usage.append(result.usage)
                span_report = apply_span_verification(extraction, case.text)
            except (BudgetExhausted, ModelCallLimitReached, ModelOutputInvalid) as exc:
                errors.append(
                    {
                        "stage": "extraction",
                        "error": str(exc)[:300],
                        "effect": (
                            "The contact was not read into structured form. Identity "
                            "is attempted from the message and transport metadata "
                            "alone, and the case is handed to a human."
                        ),
                    }
                )
                model_usage.extend(llm.case_usage)
        else:
            errors.append(
                {
                    "stage": "extraction",
                    "error": "no model client configured",
                    "effect": "Deterministic path only; the case is handed over.",
                }
            )

        # --- 2. identity (S2) ---------------------------------------------
        identity = resolve_identity(
            ops,
            extraction=extraction,
            inbound_text=case.text,
            meta_from=case.meta_from,
        )

        if not identity.confirmed or identity.booking is None:
            handovers.append(
                HandoverItem(
                    queue="SUPERVISOR",
                    summary=(
                        "Identity could not be confirmed for a contact from {}. {} "
                        "Candidates inspected: {}".format(
                            identity.sender_email or "an unknown address",
                            identity.reason,
                            json.dumps(identity.candidates)[:900] or "none",
                        )
                    ),
                    requested_decision=(
                        "Establish who this contact is and which booking they mean, or "
                        "close the contact."
                    ),
                    recommendation=(
                        "S16 prohibits any action on any booking before identity is "
                        "confirmed under S2, so nothing has been actioned. The reply "
                        "asks the passenger for the details that would confirm it."
                    ),
                    blocking_clause="S2.2, S12.1",
                    passenger_note=(
                        "We have not been able to match your message to a booking, so "
                        "we have not done anything on any booking. We need the "
                        "booking reference and the surname it is held in."
                    ),
                )
            )
            needs_input.append(
                "We need the booking reference and the surname it is held in before we "
                "can look at this. We have not taken any action on any booking."
            )
            uncertainties.append(
                Uncertainty(
                    issue="Identity is not confirmed: " + identity.reason,
                    effect_on_decision=(
                        "Every remedy is blocked (S16). The case is referred and the "
                        "passenger is asked for identifying details."
                    ),
                )
            )
        else:
            facts = _gather_facts(ops, identity, extraction, now, now_source, errors)
            planner = Planner(config, InventoryAdapter(ops))
            verified_indexes = set(span_report.get("verified_request_indexes", []))
            requests = (
                live_requests(extraction, verified_indexes, case.text)
                if extraction
                else []
            )
            if not requests:
                needs_input.append(
                    "We could not establish what you are asking us to do from this "
                    "message. Could you tell us what you would like us to do?"
                )
            plan = planner.plan(
                facts,
                extraction,
                requests=requests,
                injection_indicators=injection_indicators,
                inbound_text=case.text,
            )
            handovers.extend(plan.handovers)
            needs_input.extend(plan.needs_passenger_input)
            uncertainties.extend(
                Uncertainty(issue=u["issue"], effect_on_decision=u["effect_on_decision"])
                for u in plan.uncertainties
            )
            _add_cause_dispute_referral(plan, facts, extraction, handovers)
            # The cause-dispute check appends to the plan after the copy above, so
            # take anything it added rather than silently dropping it.
            seen = {(u.issue, u.effect_on_decision) for u in uncertainties}
            for extra in plan.uncertainties:
                key = (extra["issue"], extra["effect_on_decision"])
                if key not in seen:
                    uncertainties.append(
                        Uncertainty(
                            issue=extra["issue"],
                            effect_on_decision=extra["effect_on_decision"],
                        )
                    )

        # --- 3. execute ----------------------------------------------------
        # Two passes, deliberately. The remedies go first and the referrals second, so
        # a referral can describe what actually happened -- including a write whose
        # outcome could not be established, which is only known once it is attempted.
        executor = Executor(
            ops, journal, config, case_id=case.case_id, run_id=run_id, dry_run=dry_run
        )
        inventory = InventoryAdapter(ops)
        scope = facts.disruption_scope if facts else "{}:unidentified".format(case.case_id)

        report = executor.execute(list(plan.proposals) if plan else [], inventory)
        actions = list(report.actions)
        errors.extend(report.errors)

        handovers.extend(
            _handovers_from_execution(
                report,
                booking_ref=identity.booking_ref if identity else None,
                preblocked_action_ids={
                    p.action_id for p in (plan.proposals if plan else [])
                    if p.state == ActionState.BLOCKED
                },
            )
        )
        for unknown in report.unknown_outcomes:
            uncertainties.append(
                Uncertainty(
                    issue=unknown,
                    effect_on_decision=(
                        "Not retried. Dependent writes were blocked and the case is "
                        "handed to a human to settle it against the audit log."
                    ),
                )
            )

        escalation_report = executor.execute(
            _build_escalations(
                handovers,
                booking_ref=identity.booking_ref if identity else None,
                disruption_scope=scope,
                ops_base_url=config.ops_base_url,
                contact_key=case.sha256[:16],
            ),
            inventory,
        )
        actions.extend(escalation_report.actions)
        errors.extend(escalation_report.errors)

        # --- 4. status -----------------------------------------------------
        status = _determine_status(
            actions=actions,
            handovers=handovers,
            needs_input=needs_input,
            identity_confirmed=bool(identity and identity.confirmed),
            dry_run=dry_run,
        )

        # --- 5. draft the reply (model call 2) -----------------------------
        brief = _build_brief(
            case=case,
            identity=identity,
            facts=facts,
            plan=plan,
            actions=actions,
            handovers=handovers,
            needs_input=needs_input,
            extraction=extraction,
            status=status,
            dry_run=dry_run,
        )
        if llm is not None:
            try:
                result = run_narration(llm, brief)
                reply = result.parsed
                reply_was_model = True
                model_usage.append(result.usage)
            except (BudgetExhausted, ModelCallLimitReached, ModelOutputInvalid) as exc:
                errors.append(
                    {
                        "stage": "narration",
                        "error": str(exc)[:300],
                        "effect": "The reply was produced from a deterministic template "
                        "instead. The decision and the actions are unaffected.",
                    }
                )
                reply = fallback_reply(brief)
        else:
            reply = fallback_reply(brief)

    except UnknownCaseTime as exc:
        recovery_needed = True
        errors.append(
            {
                "stage": "ingest",
                "error": str(exc)[:300],
                "effect": "The case cannot be worked without a reliable date. A handover is required.",
            }
        )
        handovers.append(
            HandoverItem(
                queue="GENERAL",
                summary="A contact arrived with no usable date, so nothing "
                "time-relative in it could be resolved.",
                requested_decision="Establish when this was sent and re-run the case.",
                recommendation="Nothing was actioned.",
                blocking_clause="local rule: the host clock is never used as the "
                "incident date",
            )
        )
        status = CaseStatus.FAILED
    except Exception as exc:  # noqa: BLE001 - a case must always leave a record
        recovery_needed = True
        errors.append(
            {
                "stage": "case",
                "error": "{}: {}".format(type(exc).__name__, str(exc)[:300]),
                "effect": "Case processing stopped. Completed writes are preserved and a handover is attempted.",
            }
        )
        handovers.append(
            HandoverItem(
                queue="SUPERVISOR",
                summary="Processing case {} stopped unexpectedly: {}.".format(
                    case.case_id, type(exc).__name__
                ),
                requested_decision=(
                    "Review this case record and the operations audit, reconcile any "
                    "uncertain writes, then complete the outstanding requests."
                ),
                recommendation="Preserve completed actions; do not repeat any uncertain write.",
                blocking_clause="case processing error",
            )
        )
        status = CaseStatus.FAILED

    if recovery_needed:
        # execute() can fail after an earlier action has committed. Keep the
        # executor's durable per-action history even if that pass never returned.
        if executor is None:
            executor = Executor(
                ops, journal, config, case_id=case.case_id, run_id=run_id, dry_run=dry_run
            )
        actions = list(executor.report.actions)
        try:
            executor.execute(
                _build_escalations(
                    handovers,
                    booking_ref=identity.booking_ref if identity else None,
                    disruption_scope=facts.disruption_scope if facts else "unidentified",
                    ops_base_url=config.ops_base_url,
                    contact_key=case.sha256[:16],
                ),
                InventoryAdapter(ops),
            )
        except Exception as exc:  # even an unavailable handover must leave a record
            errors.append({
                "stage": "recovery_handover",
                "error": "{}: {}".format(type(exc).__name__, str(exc)[:300]),
                "effect": "The handover could not be completed. A human must pick up this local case record.",
            })
        actions = list(executor.report.actions)
        for error in executor.report.errors:
            if error not in errors:
                errors.append(error)
        if any(
            a.action_type == ActionType.ESCALATION
            and a.state in {ActionState.SUCCEEDED, ActionState.SKIPPED_DUPLICATE, ActionState.WOULD_EXECUTE}
            for a in actions
        ):
            status = (
                CaseStatus.PARTIALLY_RESOLVED
                if any(a.action_type != ActionType.ESCALATION and a.state == ActionState.SUCCEEDED for a in actions)
                else CaseStatus.HANDED_OVER
            )

    if reply is None:
        reply = fallback_reply(
            {
                "booking_ref": identity.booking_ref if identity else None,
                "reply_language": extraction.language if extraction else "en",
                "we_need_from_you": needs_input,
                "human_handover": {
                    "required": bool(handovers),
                    "what_to_tell_the_passenger": [
                        "This needs a colleague to review it. " + (
                            "It has been passed to them; no decision has been made."
                            if status != CaseStatus.FAILED and not dry_run else
                            "The handover has not been completed; no decision has been made."
                        )
                    ],
                },
            }
        )

    duration = time.monotonic() - started_monotonic
    if duration > CASE_TIMEOUT_S:
        errors.append(
            {
                "stage": "case",
                "error": "case took {:.0f}s, above the {:.0f}s limit".format(
                    duration, CASE_TIMEOUT_S
                ),
                "effect": "Recorded. The work completed, but the limit was exceeded.",
            }
        )

    # A referral counts as reaching a human whether this pass raised it or found it
    # already open. On a repeat of an unchanged case every escalation is correctly
    # skipped as a duplicate, and reporting "no escalation reached the API" for a case
    # whose referral is sitting open in the queue would be false.
    escalation_ids: list[str] = []
    for action in actions:
        if action.action_type != ActionType.ESCALATION:
            continue
        ids = action.returned_ids or {}
        if action.state == ActionState.SUCCEEDED and ids.get("escalation_id"):
            escalation_ids.append(str(ids["escalation_id"]))
        elif action.state == ActionState.SKIPPED_DUPLICATE:
            escalation_ids.extend(str(x) for x in (ids.get("existing") or []))
    passenger_facing = [h for h in handovers if not h.internal_only]

    record = CaseRecord(
        case_id=case.case_id,
        run_id=run_id,
        started_at=started_at,
        finished_at=_utcnow(),
        dry_run=dry_run,
        status=status,
        input_provenance=provenance,
        identity_resolution=(
            identity.to_record()
            if identity
            else {"confirmed": False, "reason": "identity resolution did not run"}
        ),
        passenger_requests=_requests_record(extraction, plan, span_report, needs_input),
        verified_facts=_facts_record(facts),
        policy_evaluation=_policy_record(facts, plan),
        decision=_decision_record(plan, actions, status, handovers, identity),
        sources_consulted=list(ops.sources),
        actions=actions,
        uncertainties=uncertainties,
        human_handover=HumanHandover(
            required=bool(passenger_facing),
            reason=(
                "; ".join(h.blocking_clause for h in passenger_facing)
                if passenger_facing
                else None
            ),
            escalation_ids=escalation_ids,
            api_handover_succeeded=bool(escalation_ids),
            next_steps=[h.requested_decision for h in handovers],
        ),
        passenger_response={
            "language": getattr(reply, "language", "en"),
            "subject": getattr(reply, "subject", ""),
            "body": getattr(reply, "body", ""),
            "status": "draft",
            "note": (
                "Draft only. The operations API exposes no send operation, so this "
                "text has not reached the passenger."
            ),
            "generated_by": (
                "model" if reply_was_model else "deterministic template"
            ),
        },
        usage=_usage_record(model_usage, config, journal, run_id, case.case_id),
        errors=errors,
    )
    return CaseOutcome(record=record, duration_s=duration, fatal=status == CaseStatus.FAILED)


# ---------------------------------------------------------------------------
# Fact gathering
# ---------------------------------------------------------------------------


def _gather_facts(
    ops: OpsClient,
    identity: Any,
    extraction: Extraction | None,
    now: Any,
    now_source: str,
    errors: list[dict[str, Any]],
) -> CaseFacts:
    booking = identity.booking
    disruption = booking.get("disruption") or {}
    segment = next(
        (
            s
            for s in booking.get("segments", [])
            if s.get("segment_id") == disruption.get("affected_segment")
        ),
        None,
    )

    flight = None
    if segment:
        try:
            flight = ops.get_flight(segment["flight_no"], segment["date"])
        except (OpsError, OpsTransportError) as exc:
            errors.append(
                {
                    "stage": "facts",
                    "error": str(exc)[:200],
                    "effect": "The flight's operational record could not be read; the "
                    "entitlement service's view of it is used instead.",
                }
            )

    try:
        entitlement = ops.entitlements(booking["booking_ref"])
    except (OpsError, OpsTransportError) as exc:
        entitlement = {
            "status": "SERVICE_ERROR",
            "error": str(exc)[:200],
            "passengers": [],
        }
        errors.append(
            {
                "stage": "facts",
                "error": str(exc)[:200],
                "effect": "The authoritative entitlement figure is unavailable, so no "
                "payment is proposed (S10.3).",
            }
        )

    try:
        customer = ops.customer_history(booking["customer_id"])
    except (OpsError, OpsTransportError) as exc:
        customer = {"customer_id": booking.get("customer_id"), "history": []}
        errors.append(
            {
                "stage": "facts",
                "error": str(exc)[:200],
                "effect": "Case history is unavailable, so previously supplied "
                "benefits cannot be fully checked.",
            }
        )

    advisories: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    if _passenger_disputes_cause(extraction):
        # Only fetched where it earns its attempt: the feed corroborates or
        # contradicts a passenger's account of why the flight went wrong.
        try:
            feed = ops.disruption_feed()
            flight_no = (segment or {}).get("flight_no")
            date_iso = (segment or {}).get("date")
            events = [
                e
                for e in feed.get("flight_events", [])
                if e.get("flight_no") == flight_no and e.get("date") == date_iso
            ]
            stations = {(segment or {}).get("origin"), (segment or {}).get("destination")}
            advisories = [
                a
                for a in feed.get("network_advisories", [])
                if stations & set(a.get("stations", []))
            ]
        except (OpsError, OpsTransportError):
            pass

    cross_check = policy.cross_check_entitlement(entitlement, booking)
    return CaseFacts(
        booking=booking,
        flight=flight,
        entitlement=entitlement,
        customer=customer,
        cross_check=cross_check,
        advisories=advisories,
        flight_events=events,
        now=now,
        now_source=now_source,
    )


_CAUSE_WORDS = {
    "CREW": ("crew", "staff did not", "tripulaci", "personal de cabina"),
    "WEATHER": ("weather", "fog", "storm", "wind", "snow", "niebla", "tormenta"),
    "TECHNICAL": ("technical", "fault", "engineer", "mechanical", "averia", "avería"),
    "ATC_RESTRICTION": ("air traffic", "atc", "slot", "flow restriction"),
    "OPERATIONAL": ("rotation", "late inbound", "commercial decision"),
    "SECURITY": ("security",),
    "GROUND_HANDLING": ("ground handler", "handling agent", "baggage handler"),
}


def _passenger_disputes_cause(extraction: Extraction | None) -> bool:
    if not extraction:
        return False
    return any(c.topic == "cause_of_disruption" for c in extraction.passenger_fact_claims)


def _claimed_cause(extraction: Extraction | None) -> tuple[str | None, str | None]:
    """Map the passenger's account of the cause onto a cause code, if we can."""
    if not extraction:
        return None, None
    for claim in extraction.passenger_fact_claims:
        if claim.topic != "cause_of_disruption":
            continue
        haystack = (claim.claim + " " + claim.quote).casefold()
        for code, words in _CAUSE_WORDS.items():
            if any(word in haystack for word in words):
                return code, claim.quote
    return None, None


def _add_cause_dispute_referral(
    plan: Any, facts: CaseFacts, extraction: Extraction | None, handovers: list[HandoverItem]
) -> None:
    """S3.1: a disputed cause goes to Operations Liaison, and is never amended here."""
    recorded = (facts.flight or {}).get("cause_code") or (
        facts.entitlement.get("journey") or {}
    ).get("cause_code")
    claimed, quote = _claimed_cause(extraction)
    if not recorded or not claimed or claimed == recorded:
        return

    corroboration = "; ".join(
        e.get("detail", "") for e in facts.flight_events[:2]
    ) or "no additional detail in the network operations feed"
    item = HandoverItem(
        queue="OPS_LIAISON",
        summary=(
            "Passenger on booking {} disputes the recorded cause of {}. The "
            "operational record shows {}; the passenger's account points to {}. They "
            "quote: {!r}. Feed detail: {}".format(
                facts.booking_ref,
                (facts.affected_segment or {}).get("flight_no"),
                recorded,
                claimed,
                (quote or "")[:200],
                corroboration[:300],
            )
        ),
        requested_decision="Review the recorded cause and confirm or amend it.",
        recommendation=(
            "S3.1 requires the case to proceed on the operational record and the "
            "discrepancy to be stated plainly to the passenger, which the reply does. "
            "A representative may not amend a cause code (S12.1, S16), so the "
            "entitlement has been assessed on the recorded cause of {}.".format(recorded)
        ),
        blocking_clause="S3.1",
        passenger_note=(
            "You and our record do not agree on why the flight was disrupted. We have "
            "to work from the record, and we have set out what it says. The "
            "difference has been passed to the team that can review it."
        ),
    )
    handovers.append(item)
    plan.handovers.append(item)
    plan.rationale.append(
        "S3.1: the passenger's account of the cause differs from the operational "
        "record. The record governs, the difference is stated in the reply, and the "
        "dispute is referred to Operations Liaison."
    )
    plan.add_uncertainty(
        "The passenger's account of the cause ({}) differs from the recorded cause "
        "({}).".format(claimed, recorded),
        "Assessed on the recorded cause. The dispute is referred; a representative "
        "may not amend a cause code.",
    )


# ---------------------------------------------------------------------------
# Escalations
# ---------------------------------------------------------------------------


def _handovers_from_execution(
    report, *, booking_ref: str | None,
    preblocked_action_ids: set[str] | None = None,
) -> list[HandoverItem]:
    """Referrals that only become necessary once the writes have been attempted.

    The unknown outcome is the important one. It is not a failure we can shrug off,
    and it must not be retried, so the only safe move left is to put it in front of a
    person together with the exact request that was sent.
    """
    items: list[HandoverItem] = []
    blocked = [
        a for a in report.actions
        if a.state == ActionState.BLOCKED
        and a.action_type != ActionType.ESCALATION
        and a.action_id not in (preblocked_action_ids or set())
    ]
    if blocked:
        items.append(
            HandoverItem(
                queue="SUPERVISOR",
                summary=(
                    "A proposed remedy on booking {} was stopped at execution: {}".format(
                        booking_ref or "an unidentified booking",
                        "; ".join(
                            "{} ({}): {}".format(a.action_id, a.action_type.value, a.blocked_reason)
                            for a in blocked
                        ),
                    )
                ),
                requested_decision=(
                    "Reconcile any prior or uncertain writes, recheck live availability "
                    "and authority, then complete the outstanding remedy or agree an alternative."
                ),
                recommendation=(
                    "These blocked actions were not sent. Preserve any separately "
                    "confirmed actions and inspect each blocking reason before acting."
                ),
                blocking_clause="execution safety gate: precondition or duplicate check blocked the write",
                passenger_note=(
                    "One part of your request needs a colleague to check before it can be completed."
                ),
            )
        )
    unknown = [a for a in report.actions if a.state == ActionState.UNKNOWN]
    if unknown:
        detail = "; ".join(
            "{} {}".format(a.action_type.value, str(a.amount) if a.amount else "").strip()
            for a in unknown
        )
        items.append(
            HandoverItem(
                queue="SUPERVISOR",
                summary=(
                    "A write to the operations API on booking {} did not return an "
                    "outcome we could establish, and GET /_audit does not show it. "
                    "Affected: {}. The exact request sent is in the case record.".format(
                        booking_ref or "an unidentified booking", detail
                    )
                ),
                requested_decision=(
                    "Establish from the audit log whether this committed, then either "
                    "complete it or confirm it did not happen."
                ),
                recommendation=(
                    "It was deliberately NOT retried: a blind retry here risks a "
                    "duplicate payment or a duplicate journey. Any dependent write on "
                    "this booking was blocked for the same reason."
                ),
                blocking_clause="local rule: an unestablished write is never retried",
                passenger_note=(
                    "One part of this is still being checked at our end. We have not "
                    "confirmed it either way, and someone is settling it now."
                ),
            )
        )
    failed = [a for a in report.actions if a.state == ActionState.FAILED]
    if failed:
        items.append(
            HandoverItem(
                queue="SUPERVISOR",
                summary=(
                    "A remedy on booking {} was authorised but rejected by the "
                    "operations API: {}".format(
                        booking_ref or "an unidentified booking",
                        "; ".join(
                            "{}: {}".format(a.action_type.value, a.error) for a in failed
                        ),
                    )
                ),
                requested_decision="Complete the remedy by another route.",
                recommendation=(
                    "The entitlement was assessed and the action authorised; only the "
                    "write failed. Nothing was retried automatically."
                ),
                blocking_clause="operations API rejected the write",
                passenger_note=(
                    "We were not able to put one part of this through. It has been "
                    "passed to a colleague to complete."
                ),
            )
        )
    return items


def _build_escalations(
    handovers: list[HandoverItem],
    *,
    booking_ref: str | None,
    disruption_scope: str,
    ops_base_url: str,
    contact_key: str,   # the inbound message's sha256; stable across re-runs
) -> list[Any]:
    """One escalation per queue, so routing is preserved without flooding the desk.

    Each carries a stable `referral-key` derived from the inbound message's own hash,
    the queue and the disruption event. Re-run the same message and the key matches, so
    the referral already open is reused; a genuinely new issue arrives as a different
    message, hashes differently, and reaches a human on its own.
    """
    grouped: dict[str, list[HandoverItem]] = {}
    for item in handovers:
        grouped.setdefault(item.queue, []).append(item)

    # Never drop queue four and beyond: route overflow to a coordinating supervisor.
    if len(grouped) > MAX_ESCALATIONS_PER_CASE:
        ordered = sorted(grouped, key=lambda q: (q not in {"YTP", "SPECIAL_ASSISTANCE"}, q))
        keep = [q for q in ordered if q != "SUPERVISOR"][:MAX_ESCALATIONS_PER_CASE - 1]
        overflow = [item for q, items in grouped.items() if q not in keep for item in items]
        grouped = {**{q: grouped[q] for q in keep}, "SUPERVISOR": overflow}
    proposals = []
    for index, (queue, items) in enumerate(sorted(grouped.items())):
        if index >= MAX_ESCALATIONS_PER_CASE:
            break
        proposals.append(
            escalation_proposal(
                action_id="ACT-esc-{:02d}-{}".format(index + 1, queue.lower()),
                booking_ref=booking_ref,
                queue=queue,
                summary=" || ".join("[{}] {}".format(i.queue, i.summary) for i in items),
                requested_decision=" || ".join(i.requested_decision for i in items),
                recommendation=" || ".join(i.recommendation for i in items),
                blocking_clause="; ".join(
                    sorted({i.blocking_clause for i in items})
                )[:300],
                disruption_scope=disruption_scope,
                key=referral_key(
                    ops_base_url=ops_base_url,
                    case_sha=contact_key,
                    queue=queue,
                    disruption_scope=disruption_scope,
                ),
            )
        )
    return proposals


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def _determine_status(
    *,
    actions: list[PlannedAction],
    handovers: list[HandoverItem],
    needs_input: list[str],
    identity_confirmed: bool,
    dry_run: bool,
) -> CaseStatus:
    """Local terminal statuses, decided on what happened to the passenger's requests.

    In a dry run nothing is executed, so `would_execute` stands in for `succeeded`;
    the record still labels every action `would_execute` and never `succeeded`.
    """
    done_states = {ActionState.SUCCEEDED, ActionState.SKIPPED_DUPLICATE}
    if dry_run:
        done_states = done_states | {ActionState.WOULD_EXECUTE}

    passenger_actions = [a for a in actions if a.action_type != ActionType.ESCALATION]
    completed = [a for a in passenger_actions if a.state in done_states]
    outstanding = [
        a
        for a in passenger_actions
        if a.state in {ActionState.BLOCKED, ActionState.FAILED, ActionState.UNKNOWN}
    ]
    passenger_facing_handovers = [h for h in handovers if not h.internal_only]

    referrals = [a for a in actions if a.action_type == ActionType.ESCALATION]
    if handovers and (not referrals or any(
        a.state not in done_states or (
            not dry_run and not (a.returned_ids or {}).get(
                "existing" if a.state == ActionState.SKIPPED_DUPLICATE else "escalation_id"
            )
        ) for a in referrals
    )):
        return CaseStatus.FAILED

    if not identity_confirmed:
        return CaseStatus.HANDED_OVER

    if completed:
        if outstanding or passenger_facing_handovers or needs_input:
            return CaseStatus.PARTIALLY_RESOLVED
        return CaseStatus.RESOLVED

    if needs_input and not passenger_facing_handovers:
        return CaseStatus.NEEDS_CLARIFICATION
    if passenger_facing_handovers:
        return CaseStatus.HANDED_OVER
    if outstanding:
        return CaseStatus.PARTIALLY_RESOLVED
    # Nothing to do, and nothing outstanding: the contact was answered outright.
    return CaseStatus.RESOLVED


# ---------------------------------------------------------------------------
# Record assembly
# ---------------------------------------------------------------------------


def _requests_record(
    extraction: Extraction | None,
    plan: Any,
    span_report: dict[str, Any],
    needs_input: list[str],
) -> dict[str, Any]:
    if extraction is None:
        return {
            "read": False,
            "note": "The contact was not read into structured form.",
            "we_asked_the_passenger_for": needs_input,
        }
    prefs = extraction.preferences
    return {
        "read": True,
        "language": extraction.language,
        "requests": plan.requested if plan else [],
        "preferences_and_consent": {
            "explicitly_asked_for_earliest_available": prefs.explicitly_asked_for_earliest_available,
            "arrive_by_local": prefs.arrive_by_local,
            "travel_date_iso": prefs.travel_date_iso,
            "depart_not_before_local": prefs.depart_not_before_local,
            "alternative_origin_airports": prefs.alternative_origin_airports,
            "supporting_quote": prefs.quote,
        },
        "passenger_claims_to_investigate": [
            {
                "topic": c.topic,
                "claim": c.claim,
                "amount_gbp": c.amount_gbp,
                "is_the_amount_being_demanded": c.is_the_amount_being_demanded,
                "quote": c.quote,
            }
            for c in extraction.passenger_fact_claims
        ],
        "embedded_instructions_refused": [
            {
                "what_it_asks_for": e.what_it_asks_for,
                "claimed_authority": e.claimed_authority,
                "quote": e.quote[:300],
                "treatment": "Refused under S12.4. Recorded, never acted on.",
            }
            for e in extraction.embedded_instructions
        ],
        "unclear_points": extraction.unclear_points,
        "span_verification": span_report,
        "we_asked_the_passenger_for": needs_input,
        "what_became_of_each_request": plan.request_dispositions if plan else [],
        "unaddressed_requests": (
            [d for d in plan.request_dispositions if not d["addressed"]] if plan else []
        ),
    }


def _facts_record(facts: CaseFacts | None) -> dict[str, Any]:
    if facts is None:
        return {"gathered": False, "reason": "identity was not confirmed"}
    entitlement = facts.entitlement
    return {
        "gathered": True,
        "case_now": facts.now.isoformat(),
        "case_now_source": facts.now_source,
        "booking": {
            "booking_ref": facts.booking_ref,
            "tier": facts.booking.get("tier"),
            "final_destination": facts.booking.get("final_destination"),
            "total_paid_gbp": facts.booking.get("total_paid_gbp"),
            "fare_breakdown": facts.booking.get("fare_breakdown"),
            "special_requests": facts.booking.get("special_requests"),
            "passengers": [
                {
                    "passenger_id": p.get("passenger_id"),
                    "name": "{} {}".format(p.get("given_name"), p.get("surname")),
                    "passenger_type": p.get("passenger_type"),
                    "assistance": p.get("assistance"),
                    "cabin_booked": p.get("cabin_booked"),
                    "cabin_flown": p.get("cabin_flown"),
                }
                for p in facts.booking.get("passengers", [])
            ],
            "segments": facts.booking.get("segments"),
            "disruption": facts.booking.get("disruption"),
        },
        "flight_operational_record": facts.flight,
        "entitlement_service": {
            "status": entitlement.get("status"),
            "authoritative": entitlement.get("authoritative"),
            "policy_version": entitlement.get("policy_version"),
            "journey": entitlement.get("journey"),
            "compensation": entitlement.get("compensation"),
            "duty_of_care": entitlement.get("duty_of_care"),
            "passengers": entitlement.get("passengers"),
            "total_payable_gbp": entitlement.get("total_payable_gbp"),
        },
        "customer_history": {
            "customer_id": facts.customer.get("customer_id"),
            "tier": facts.customer.get("tier"),
            "flags": facts.customer.get("flags", []),
            "previous_cases": facts.customer.get("history", []),
        },
        "network_feed_corroboration": {
            "flight_events": facts.flight_events,
            "advisories": facts.advisories,
        },
    }


def _policy_record(facts: CaseFacts | None, plan: Any) -> dict[str, Any]:
    if facts is None or plan is None:
        return {"evaluated": False, "reason": "identity was not confirmed"}
    return {
        "evaluated": True,
        "policy_document": "APCP-2026-04 v11.3",
        "authoritative_source": (
            "GET /entitlements/calculate. S10.2 makes its figure the amount owed; "
            "S10.4 forbids substituting a locally derived one."
        ),
        "eligible_remedies": plan.eligible,
        "already_provided": plan.already_provided,
        "independent_cross_check": facts.cross_check.to_record(),
        "rule_references": sorted(
            {clause for p in plan.proposals for clause in p.policy_basis}
        ),
        "answers_given_without_action": plan.answers,
    }


def _decision_record(
    plan: Any,
    actions: list[PlannedAction],
    status: CaseStatus,
    handovers: list[HandoverItem],
    identity: Any = None,
) -> dict[str, Any]:
    """The decision block. `rationale` must never be empty.

    When identity fails there is no plan, so this came back as `rationale: []` and the
    most-read field in the record said nothing about why nothing happened. The reasoning
    was in `uncertainties` and `policy_evaluation`, but a reader who opens `decision`
    first should not have to go looking for it.
    """
    completed = [
        a
        for a in actions
        if a.state in {ActionState.SUCCEEDED, ActionState.WOULD_EXECUTE}
        and a.action_type != ActionType.ESCALATION
    ]
    rationale = list(plan.rationale) if plan else []
    if not rationale:
        confirmed = bool(getattr(identity, "confirmed", False)) if identity else False
        if not confirmed:
            reason = (
                getattr(identity, "reason", None)
                or "identity resolution did not run"
            )
            rationale = [
                "S2.2: identity is not confirmed, so no remedy may be actioned on any "
                "booking (S16). {}".format(reason)
            ]
        else:
            rationale = [
                "No action was authorised at this level; every remedy owed is recorded "
                "in the referral rather than actioned here."
            ]

    return {
        "status": status.value,
        "rationale": rationale,
        "what_the_passenger_gets": [
            {
                "action": a.action_type.value,
                "state": a.state.value,
                "amount": str(a.amount) if a.amount else None,
                "passenger_ids": a.passenger_ids,
                "itinerary": a.itinerary,
            }
            for a in completed
        ],
        "rejected_alternatives": plan.rejected_alternatives if plan else [],
        "referred_to_humans": [
            {
                "queue": h.queue,
                "blocking_clause": h.blocking_clause,
                "requested_decision": h.requested_decision,
                "internal_only": h.internal_only,
            }
            for h in handovers
        ],
    }


def _usage_record(
    model_usage: list[dict[str, Any]],
    config: Config,
    journal: Journal,
    run_id: str,
    case_id: str,
) -> dict[str, Any]:
    from decimal import Decimal

    calculated = sum(
        (Decimal(u["calculated_cost_usd"]) for u in model_usage if u.get("calculated_cost_usd")),
        Decimal("0"),
    )
    unresolved = sum(
        (
            Decimal(u["reserved_upper_bound_usd"])
            for u in model_usage
            if not u.get("reservation_resolved")
        ),
        Decimal("0"),
    )
    return {
        "model": config.openai_model,
        "price_source": config.price.source_url,
        "price_verified_on": config.price.verified_on,
        "prices_usd_per_mtok": {
            "input": str(config.price.input_usd_per_mtok),
            "cached_input": str(config.price.cached_input_usd_per_mtok),
            "cache_write": str(config.price.cache_write_rate),
            "output": str(config.price.output_usd_per_mtok),
        },
        "calls": model_usage,
        "case_calculated_cost_usd": str(calculated),
        "case_unresolved_reservation_upper_bound_usd": str(unresolved),
        "note": (
            "Cost is derived from the usage the API reported, priced from the table "
            "above. Cached input and reasoning tokens are subsets of input and output "
            "tokens respectively and are each counted once. This is a calculated "
            "figure, not a billing statement from OpenAI."
        ),
    }


def _build_brief(
    *,
    case: CaseInput,
    identity: Any,
    facts: CaseFacts | None,
    plan: Any,
    actions: list[PlannedAction],
    handovers: list[HandoverItem],
    needs_input: list[str],
    extraction: Extraction | None,
    status: CaseStatus,
    dry_run: bool,
) -> dict[str, Any]:
    """Everything the reply may be built from, and nothing else."""
    corrections: list[str] = []
    record_facts: dict[str, Any] = {}
    entitlement_summary: dict[str, Any] = {}

    if facts is not None:
        flight = facts.flight or {}
        journey = facts.entitlement.get("journey") or {}
        record_facts = {
            "flight": flight.get("flight_no") or journey.get("affected_flight"),
            "date": flight.get("date"),
            "status": flight.get("status") or journey.get("flight_status"),
            "cause_code": flight.get("cause_code") or journey.get("cause_code"),
            "cause_note": flight.get("cause_note"),
            "arrival_delay_minutes_at_final_destination": journey.get(
                "arrival_delay_minutes_at_final_destination"
            ),
            "departure_delay_minutes": journey.get("departure_delay_minutes"),
            "distance_km": journey.get("great_circle_distance_km"),
            "band": journey.get("band"),
        }
        claimed, quote = _claimed_cause(extraction)
        recorded = record_facts.get("cause_code")
        if claimed and recorded and claimed != recorded:
            corrections.append(
                "The passenger believes the cause was {}. Our operational record for "
                "{} records the cause as {}: {}. Say this plainly.".format(
                    claimed,
                    record_facts.get("flight"),
                    recorded,
                    (record_facts.get("cause_note") or "")[:200],
                )
            )
        for claim in (extraction.passenger_fact_claims if extraction else []):
            if claim.topic == "delay_length" and journey.get(
                "arrival_delay_minutes_at_final_destination"
            ) is not None:
                corrections.append(
                    "The passenger describes the delay they experienced. Compensation "
                    "rests on arrival delay at the final destination, which our record "
                    "puts at {} minutes.".format(
                        journey["arrival_delay_minutes_at_final_destination"]
                    )
                )
                break
        if any(
            c.topic == "money_owed" for c in (extraction.passenger_fact_claims if extraction else [])
        ):
            # The passenger has done their own arithmetic. Where it differs from the
            # authoritative figure, they are owed the specific reason, not a lower
            # number quoted back at them.
            workings: list[str] = []
            for pax in facts.entitlement.get("passengers", []):
                workings.extend(pax.get("downgrade_reasoning", []))
                for flag in pax.get("flags", []):
                    workings.append(flag)
            workings.extend((facts.entitlement.get("compensation") or {}).get("reasoning", []))
            if workings:
                corrections.append(
                    "The passenger has calculated a figure of their own. Explain "
                    "precisely where it differs, using these workings, in plain "
                    "language and without clause numbers: "
                    + " ".join(workings)
                )
        comp = facts.entitlement.get("compensation") or {}
        care = facts.entitlement.get("duty_of_care") or {}
        assessable = comp.get("status") not in {"INSUFFICIENT_DATA", None}
        entitlement_summary = {
            "service_status": facts.entitlement.get("status"),
            "compensation_status": comp.get("status"),
            # Amounts are withheld entirely where the assessment could not be made,
            # so there is no figure available to be quoted as if it were a decision.
            "compensation_amount_gbp": comp.get("amount_gbp") if assessable else None,
            "total_payable_gbp": (
                facts.entitlement.get("total_payable_gbp") if assessable else None
            ),
            "amounts_withheld_because": (
                None
                if assessable
                else "Compensation is not assessable yet. Do not state any amount, "
                "including zero, and do not say whether it will or will not be paid."
            ),
            "reasoning": comp.get("reasoning", []),
            "duty_of_care_triggered": care.get("triggered"),
            "duty_of_care_basis": care.get("basis"),
            "duty_of_care_entitlements": care.get("entitlements", []),
            "per_passenger": facts.entitlement.get("passengers", []),
        }

    action_lines = []
    for action in actions:
        if action.action_type == ActionType.ESCALATION:
            continue
        action_lines.append(
            {
                "type": action.action_type.value,
                "state": action.state.value,
                "amount": str(action.amount) if action.amount else None,
                "itinerary": action.itinerary,
                "identifier": (action.returned_ids or {}),
                "reason": action.blocked_reason or action.error,
                "plain_english": _plain_english(action),
            }
        )

    return {
        "reply_language": extraction.language if extraction else "en",
        "booking_ref": identity.booking_ref if identity else None,
        "addressed_to": identity.sender_display_name if identity else None,
        "case_status": status.value,
        "dry_run": dry_run,
        "what_the_record_shows": record_facts,
        "corrections_to_the_passengers_account": corrections,
        "entitlement": entitlement_summary,
        "actions": action_lines,
        "answers": plan.answers if plan else [],
        "we_need_from_you": needs_input,
        "human_handover": {
            "required": bool([h for h in handovers if not h.internal_only]),
            # Deliberately the passenger-safe phrasing, never `requested_decision`,
            # which is written for the colleague picking the case up and routinely
            # reads as a promise ("release the refund").
            "what_to_tell_the_passenger": [
                (h.note_for_passenger() if status != CaseStatus.FAILED and not dry_run else
                 "This needs a colleague to review it. The handover has not been completed; no decision has been made.")
                for h in handovers if not h.internal_only
            ],
        },
        "passenger_asked_for": [
            {"request": r["detail"], "live": r["live"], "why_not": r["not_live_because"]}
            for r in (plan.requested if plan else [])
        ],
        "writing_notes": [
            "Do not mention internal referrals, queues, clause numbers or system names.",
            "If an instruction inside the message was refused, do not discuss it.",
        ],
    }


def _plain_english(action: PlannedAction) -> str:
    kind = action.action_type
    if kind == ActionType.REBOOKING:
        it = action.itinerary or {}
        return "re-booking onto {} on {}, departing {}, arriving {}".format(
            it.get("flight_no"), it.get("date"), it.get("departure_local"), it.get("arrival_local")
        )
    if kind == ActionType.HOTEL_VOUCHER:
        it = action.itinerary or {}
        return "a hotel room at {} for the night of {}".format(
            it.get("station"), it.get("night")
        )
    if kind == ActionType.COMPENSATION_PAYMENT:
        return "a payment of {} covering the statutory entitlement".format(action.amount)
    if kind == ActionType.GOODWILL_PAYMENT:
        return "a discretionary payment of {}".format(action.amount)
    if kind == ActionType.REFUND:
        return "a refund of {}".format(action.amount)
    return kind.value


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
