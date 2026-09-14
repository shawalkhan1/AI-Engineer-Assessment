"""The only place in this system that writes.

A proposal becomes a request only after every one of these has passed, and all of it
is journalled first:

1. the proposal was produced by `planner`, not by a model;
2. it is not a duplicate -- checked against the local journal *and* the server's own
   `GET /_audit`, with the server winning;
3. its mutable precondition has been re-read a moment ago (the seat still exists, the
   room is still there), not at planning time;
4. no earlier write in this case came back `unknown`, which would make later money
   movements unsafe;
5. the intent is committed to disk before the request leaves.

`unknown` is a first-class outcome. A write that timed out is never retried and never
written off as a failure: we go back to `/_audit` and try to settle it, and if we
cannot, we block what depends on it and hand the case over saying so.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Config
from .journal import Journal, benefit_keys_for, canonical_fingerprint
from .ops_client import OpsClient, WriteOutcome, audit_collection_for
from .planner import ActionProposal
from .schemas import ActionState, ActionType, Money, PlannedAction

# The order writes are attempted in. Re-routing first because a later remedy may
# depend on the passenger actually having a journey; escalations last so they can
# describe what did and did not happen.
EXECUTION_ORDER = {
    ActionType.REBOOKING: 0,
    ActionType.HOTEL_VOUCHER: 1,
    ActionType.COMPENSATION_PAYMENT: 2,
    ActionType.REFUND: 3,
    ActionType.GOODWILL_PAYMENT: 4,
    ActionType.ESCALATION: 5,
}

_POST_PATHS = {
    ActionType.REBOOKING: "/rebooking",
    ActionType.HOTEL_VOUCHER: "/vouchers/hotel",
    ActionType.COMPENSATION_PAYMENT: "/payments/compensation",
    ActionType.GOODWILL_PAYMENT: "/payments/goodwill",
    ActionType.REFUND: "/refunds",
    ActionType.ESCALATION: "/escalations",
}

_ID_FIELD = {
    ActionType.REBOOKING: "rebooking_id",
    ActionType.HOTEL_VOUCHER: "voucher_id",
    ActionType.COMPENSATION_PAYMENT: "payment_id",
    ActionType.GOODWILL_PAYMENT: "payment_id",
    ActionType.REFUND: "refund_id",
    ActionType.ESCALATION: "escalation_id",
}


@dataclass
class ExecutionReport:
    actions: list[PlannedAction] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    unknown_outcomes: list[str] = field(default_factory=list)
    escalation_ids: list[str] = field(default_factory=list)
    any_write_succeeded: bool = False


class Executor:
    def __init__(
        self,
        ops: OpsClient,
        journal: Journal,
        config: Config,
        *,
        case_id: str,
        run_id: str,
        dry_run: bool,
    ) -> None:
        self.ops = ops
        self.journal = journal
        self.config = config
        self.case_id = case_id
        self.run_id = run_id
        self.dry_run = dry_run
        self.report = ExecutionReport()
        self._audit_cache: dict[str, Any] | None = None
        self._audit_available: bool | None = None
        self._blocked_by_unknown = False

    # -- audit access ------------------------------------------------------

    def _audit(self, *, refresh: bool = False) -> dict[str, Any] | None:
        """The server's write log, or **None** if we could not read it.

        Returning an empty log on failure was a live double-payment path: an empty log
        is indistinguishable from "the server holds nothing", and the duplicate check
        below used to read that as licence to perform the action again. An audit
        outage could therefore re-pay every previously successful action. Absence of
        evidence is never evidence of absence here.
        """
        if self._audit_cache is None or refresh:
            try:
                audit = self.ops.audit()
                if not isinstance(audit, dict) or not isinstance(audit.get("writes"), dict):
                    raise ValueError("operations audit response has no usable write log")
                if any(
                    not isinstance(audit["writes"].get(name), list)
                    for name in ("rebookings", "refunds", "payments", "hotel_vouchers", "escalations")
                ):
                    raise ValueError("operations audit response has incomplete write collections")
                self._audit_cache = audit
                self._audit_available = True
            except Exception as exc:  # noqa: BLE001 - recorded, never fatal
                self.report.errors.append(
                    {
                        "stage": "audit_read",
                        "error": str(exc)[:300],
                        "effect": (
                            "The server's write log could not be read. Duplicate "
                            "detection cannot establish that an action is new. "
                            "Booking and money writes are blocked; a human handover "
                            "may still be raised."
                        ),
                    }
                )
                self._audit_cache = None
                self._audit_available = False
        return self._audit_cache

    # -- duplicate detection ----------------------------------------------

    def _server_equivalents(
        self, proposal: ActionProposal, *, refresh: bool = False
    ) -> list[dict[str, Any]]:
        """Equivalent writes the server itself already holds.

        `GET /_audit` is the authority. If the journal says we did something and the
        server has no record of it, the server was reset and the action has *not*
        happened -- so we do it rather than silently skipping it.
        """
        collection = audit_collection_for(_POST_PATHS[proposal.action_type])
        if not collection:
            return []
        audit = self._audit(refresh=refresh)
        if audit is None:
            return []          # unknown, not empty -- callers check _audit_available
        return self.ops.find_matching_writes(
            audit, collection, _matcher_for(proposal)
        )

    # -- precondition refresh ---------------------------------------------

    def _refresh_precondition(
        self, proposal: ActionProposal, inventory: Any
    ) -> tuple[bool, dict[str, Any]]:
        """Re-read the mutable state this write depends on, immediately before it.

        The operations API does not validate `option_id`, seat counts, cabin or fare
        on POST /rebooking (env/ops_server.py), so a 201 is not evidence that a seat
        existed. This check is the only thing standing between a stale option and a
        confirmation the passenger cannot use.
        """
        spec = proposal.refresh
        if not spec:
            return True, {"refreshed": False, "reason": "no mutable precondition"}

        if spec["kind"] == "availability":
            fresh = inventory.own_availability(
                spec["origin"], spec["destination"], spec["date"], proposal.booking_ref or ""
            )
            if fresh.get("unavailable"):
                return False, {
                    "refreshed": False,
                    "reason": "inventory system did not respond on re-check: "
                    + str(fresh.get("error")),
                }
            row = next(
                (
                    r
                    for r in fresh.get("results", [])
                    if r.get("option_id") == spec["option_id"]
                ),
                None,
            )
            if row is None:
                return False, {
                    "refreshed": True,
                    "reason": "the selected option is no longer in inventory",
                    "option_id": spec["option_id"],
                }
            seats = int(row.get("seats_available") or 0)
            if seats < spec["seats_needed"]:
                return False, {
                    "refreshed": True,
                    "reason": "seats fell from planning to execution",
                    "seats_available_now": seats,
                    "seats_needed": spec["seats_needed"],
                }
            if row.get("cabin") != spec["cabin"]:
                return False, {
                    "refreshed": True,
                    "reason": "the option's cabin changed since planning",
                    "cabin_now": row.get("cabin"),
                }
            # A stable option_id does not guarantee stable commercial terms or
            # times. The original authority and passenger consent apply to the
            # option we assessed, so any material change needs a fresh decision.
            body = proposal.request_body or {}
            itinerary = proposal.itinerary or {}
            expected = {
                "flight_no": body.get("flight_no"),
                "date": body.get("date"),
                "origin": spec.get("origin"),
                "destination": spec.get("destination"),
                "operated_by": itinerary.get("operated_by", "Aerlink"),
                **{
                    name: itinerary[name]
                    for name in ("departure_local", "arrival_local", "arrival_delay_vs_original_minutes")
                    if itinerary.get(name) is not None
                },
            }
            for name, value in expected.items():
                if value is not None and row.get(name) != value:
                    return False, {
                        "refreshed": True,
                        "reason": "the option's {} changed since planning".format(name),
                        "planned": value,
                        "current": row.get(name),
                    }
            if _pence(row.get("fare_gbp")) != _pence(body.get("fare_gbp")):
                return False, {
                    "refreshed": True,
                    "reason": "the option's additional fare changed since planning",
                    "fare_gbp_planned": body.get("fare_gbp"),
                    "fare_gbp_now": row.get("fare_gbp"),
                }
            return True, {
                "refreshed": True,
                "seats_available_now": seats,
                "fare_gbp_now": row.get("fare_gbp"),
                "cabin_now": row.get("cabin"),
            }

        if spec["kind"] == "hotel":
            allocation = inventory.hotel_allocation(spec["station"], spec["night"])
            if allocation is None:
                return False, {
                    "refreshed": True,
                    "reason": "no allocation is held at this station for this night",
                }
            remaining = int(allocation.get("rooms_remaining") or 0)
            if remaining <= 0:
                return False, {
                    "refreshed": True,
                    "reason": "the station allocation was exhausted between planning "
                    "and execution (S4.5)",
                    "rooms_remaining_now": remaining,
                }
            if proposal.amount and _pence(allocation.get("rate_gbp")) != proposal.amount.amount_minor:
                return False, {
                    "refreshed": True,
                    "reason": "the hotel rate changed since planning; authority must be reassessed",
                    "rate_gbp_now": allocation.get("rate_gbp"),
                    "rate_minor_planned": proposal.amount.amount_minor,
                }
            return True, {"refreshed": True, "rooms_remaining_now": remaining}

        return True, {"refreshed": False, "reason": "unrecognised precondition kind"}

    # -- the write path ----------------------------------------------------

    def execute(
        self, proposals: list[ActionProposal], inventory: Any
    ) -> ExecutionReport:
        """Run one pass of proposals.

        The executor keeps cumulative state across passes -- notably
        `_blocked_by_unknown`, so an unknown write in the first pass still blocks
        money in the second -- but the report returned covers only this pass, so a
        caller that runs two passes does not count the same action twice.
        """
        first_new_action = len(self.report.actions)
        first_new_error = len(self.report.errors)
        first_new_unknown = len(self.report.unknown_outcomes)
        first_new_escalation = len(self.report.escalation_ids)

        ordered = sorted(proposals, key=lambda p: EXECUTION_ORDER[p.action_type])
        for proposal in ordered:
            self.report.actions.append(self._execute_one(proposal, inventory))
        self._verify_by_readback()

        return ExecutionReport(
            actions=self.report.actions[first_new_action:],
            errors=self.report.errors[first_new_error:],
            unknown_outcomes=self.report.unknown_outcomes[first_new_unknown:],
            escalation_ids=self.report.escalation_ids[first_new_escalation:],
            any_write_succeeded=self.report.any_write_succeeded,
        )

    def _execute_one(
        self, proposal: ActionProposal, inventory: Any
    ) -> PlannedAction:
        record = _to_record(proposal)

        if proposal.state == ActionState.BLOCKED:
            return record

        fingerprint = canonical_fingerprint(
            ops_base_url=self.config.ops_base_url,
            booking_ref=proposal.booking_ref or "NO-BOOKING",
            action_type=proposal.action_type.value,
            disruption_scope=proposal.disruption_scope,
            params=proposal.fingerprint_params,
        )
        record.fingerprint = fingerprint

        benefit_keys = benefit_keys_for(
            ops_base_url=self.config.ops_base_url,
            action_type=proposal.action_type.value,
            booking_ref=proposal.booking_ref or "NO-BOOKING",
            disruption_scope=proposal.disruption_scope,
            passenger_ids=proposal.passenger_ids,
            station=(proposal.itinerary or {}).get("station"),
            night=(proposal.itinerary or {}).get("night"),
        )
        # --- duplicate detection, server first ---------------------------
        prior = self.journal.find_prior_actions(fingerprint)
        server_matches = self._server_equivalents(proposal)
        provenance = self.journal.actions_for_booking(
            self.config.ops_base_url, proposal.booking_ref or "NO-BOOKING")
        current_keys = set(benefit_keys)
        by_id = {str(p["returned_id"]): p for p in provenance if p.get("returned_id")}
        # The API omits disruption IDs on payments/refunds. Equal amounts do not
        # establish equal events; known different events must not suppress a remedy.
        money_kinds = {ActionType.COMPENSATION_PAYMENT, ActionType.GOODWILL_PAYMENT, ActionType.REFUND}
        if proposal.action_type in money_kinds:
            server_matches = [w for w in server_matches
                if (p := by_id.get(str(w.get(_ID_FIELD[proposal.action_type]))))
                and p["fingerprint"] == fingerprint]
        if server_matches:
            record.state = ActionState.SKIPPED_DUPLICATE
            record.blocked_reason = (
                "An equivalent action already exists in the operations API's own "
                "write log ({}). S15.3 and S16 forbid actioning a remedy twice."
            ).format(
                ", ".join(
                    str(m.get(_ID_FIELD[proposal.action_type], "?"))
                    for m in server_matches[:3]
                )
            )
            record.returned_ids = {
                "existing": [
                    m.get(_ID_FIELD[proposal.action_type]) for m in server_matches[:3]
                ]
            }
            record.verification = "Confirmed against GET /_audit."
            return record

        if not self._audit_available and proposal.action_type != ActionType.ESCALATION:
            record.state = ActionState.BLOCKED
            record.blocked_reason = (
                "The server's write log could not be read, so we cannot establish "
                "whether this remedy already exists. A fresh local journal does not "
                "prove nothing was paid or booked elsewhere. No booking or money "
                "write is sent until the audit is available or a human reconciles it."
            )
            return record

        if proposal.action_type != ActionType.ESCALATION:
            reset_at = self.journal.last_reset_at(self.config.ops_base_url)
            from .journal import _parse_ts
            for prior_row in provenance:
                stale = reset_at and _parse_ts(prior_row["created_at"]) < _parse_ts(reset_at)
                if not stale and prior_row["state"] in {"unknown", "attempted"}:
                    record.state = ActionState.BLOCKED
                    record.blocked_reason = "A previous booking write has an unsettled outcome in the journal; reconcile before further remedies."
                    return record
            collection = audit_collection_for(_POST_PATHS[proposal.action_type])
            for write in (self._audit_cache or {}).get("writes", {}).get(collection, []):
                if str(write.get("booking_ref", "")).upper() != str(proposal.booking_ref).upper():
                    continue
                if proposal.action_type in {ActionType.COMPENSATION_PAYMENT, ActionType.GOODWILL_PAYMENT}:
                    expected_type = "COMPENSATION" if proposal.action_type == ActionType.COMPENSATION_PAYMENT else "GOODWILL"
                    if write.get("type") != expected_type:
                        continue
                if proposal.action_type == ActionType.HOTEL_VOUCHER and (
                    write.get("station") != (proposal.itinerary or {}).get("station") or
                    write.get("night") != (proposal.itinerary or {}).get("night")):
                    continue
                if proposal.action_type in {ActionType.REBOOKING, ActionType.REFUND, ActionType.HOTEL_VOUCHER}:
                    if write.get("passenger_ids") and not (set(write["passenger_ids"]) & set(proposal.passenger_ids)):
                        continue
                linked = by_id.get(str(write.get(_ID_FIELD[proposal.action_type])))
                if linked and linked["benefit_keys"] and not (current_keys & set(linked["benefit_keys"])):
                    continue
                record.state = ActionState.BLOCKED
                record.blocked_reason = "S12.2: a prior server remedy overlaps these passengers; its amount or event cannot establish a new benefit. Human reconciliation required."
                return record

        # --- benefit already granted? (S12.2, independent of the request) --
        record.preconditions_checked["benefit_scope_keys"] = benefit_keys
        granted = self.journal.find_benefit_grants(benefit_keys)
        if granted:
            record.state = ActionState.BLOCKED
            record.blocked_reason = (
                "A benefit of this kind has already been granted for this booking and "
                "this disruption event ({}), so granting another would be the "
                "circumvention S12.2 names -- the amount or the exact parameters "
                "differing does not make it a different benefit."
            ).format(
                ", ".join(
                    "{} on {} ({})".format(g["action_type"], g["created_at"], g["state"])
                    for g in granted[:3]
                )
            )
            return record

        if prior and not server_matches:
            unresolved = [p for p in prior if p.state in {"unknown", "attempted"}]
            if unresolved:
                record.state = ActionState.BLOCKED
                record.blocked_reason = (
                    "A previous attempt at this exact action (journal key {}, case {}, "
                    "state {}) never reached a settled outcome, and the server's audit "
                    "log does not show it either. Repeating it could double-pay, so it "
                    "is blocked and referred rather than retried."
                ).format(
                    unresolved[0].journal_key,
                    unresolved[0].case_id,
                    unresolved[0].state,
                )
                self.report.unknown_outcomes.append(record.blocked_reason)
                return record

            # The journal says this succeeded and the server does not show it. That is
            # a DISAGREEMENT, not permission to do it again. The only thing that makes
            # the local row safely stale is an explicit reset we recorded ourselves,
            # after the row was written.
            last_reset = self.journal.last_reset_at(self.config.ops_base_url)
            stale = all(p.predates(last_reset) for p in prior)
            if not self._audit_available:
                record.state = ActionState.BLOCKED
                record.blocked_reason = (
                    "The local journal records this action as already completed (case "
                    "{}), and the server's write log could not be read to confirm it. "
                    "An unreadable log is not an empty one, so the action is not "
                    "repeated."
                ).format(prior[0].case_id)
                return record
            if not stale:
                record.state = ActionState.BLOCKED
                record.blocked_reason = (
                    "The local journal records this action as completed on {} (case "
                    "{}, id {}), but the server's write log does not show it and no "
                    "operations-API reset has been recorded since. That disagreement "
                    "has to be settled by a human: repeating the action risks a "
                    "duplicate benefit, and assuming it never happened would be a "
                    "guess."
                ).format(prior[0].created_at, prior[0].case_id, prior[0].returned_id)
                self.report.unknown_outcomes.append(record.blocked_reason)
                return record
            record.preconditions_checked["journal_vs_server"] = (
                "The local journal holds a prior successful {} for this fingerprint "
                "(case {}, {}), the server's write log does not show it, and an "
                "explicit operations-API reset is recorded at {} -- after that row. "
                "The journal row is therefore known-stale and the action proceeds."
            ).format(
                proposal.action_type.value,
                prior[0].case_id,
                prior[0].created_at,
                last_reset,
            )

        # --- an earlier unknown write in this case makes money unsafe ------
        if self._blocked_by_unknown and proposal.action_type != ActionType.ESCALATION:
            record.state = ActionState.BLOCKED
            record.blocked_reason = (
                "An earlier write in this case ended with an outcome we could not "
                "establish. No further money is moved on this booking until that is "
                "reconciled."
            )
            return record

        # --- refresh the mutable precondition ------------------------------
        ok, refresh_detail = self._refresh_precondition(proposal, inventory)
        record.preconditions_checked["refreshed_immediately_before_write"] = refresh_detail
        if not ok:
            record.state = ActionState.BLOCKED
            record.blocked_reason = (
                "Precondition re-check failed immediately before the write: {}".format(
                    refresh_detail.get("reason")
                )
            )
            return record

        if self.dry_run:
            record.state = ActionState.WOULD_EXECUTE
            record.verification = (
                "Dry run: no request was sent to the operations API. This is a "
                "proposal, not a completed action."
            )
            return record

        # --- commit the intent, then send exactly once ---------------------
        path = _POST_PATHS[proposal.action_type]
        body = dict(proposal.request_body or {})
        journal_key = self.journal.record_intent(
            fingerprint=fingerprint,
            ops_base_url=self.config.ops_base_url,
            case_id=self.case_id,
            run_id=self.run_id,
            action_type=proposal.action_type.value,
            booking_ref=proposal.booking_ref,
            path=path,
            request_body=body,
        )
        self.journal.record_benefit_grants(
            benefit_keys=benefit_keys,
            ops_base_url=self.config.ops_base_url,
            booking_ref=proposal.booking_ref,
            action_type=proposal.action_type.value,
            journal_key=journal_key,
            state="attempted",
        )
        record.journal_key = str(journal_key)
        record.state = ActionState.ATTEMPTED
        record.request_summary = _summarise_request(proposal, body)

        outcome = self._post(proposal, body)
        record.response_status = outcome.status

        if outcome.state == "succeeded":
            returned_id = (outcome.body or {}).get(_ID_FIELD[proposal.action_type])
            record.state = ActionState.SUCCEEDED
            record.returned_ids = {
                _ID_FIELD[proposal.action_type]: returned_id,
                **{
                    k: v
                    for k, v in (outcome.body or {}).items()
                    if k in {"status", "rooms_remaining_after", "confirmed_at", "paid_at", "issued_at", "raised_at", "queue"}
                },
            }
            self.journal.update_outcome(
                journal_key,
                state="succeeded",
                response_status=outcome.status,
                returned_id=str(returned_id),
            )
            self.journal.update_benefit_state(journal_key, "succeeded")
            self.report.any_write_succeeded = True
            if proposal.action_type == ActionType.ESCALATION and returned_id:
                self.report.escalation_ids.append(str(returned_id))
            return record

        if outcome.state == "failed":
            record.state = ActionState.FAILED
            record.error = outcome.error
            self.journal.update_outcome(
                journal_key,
                state="failed",
                response_status=outcome.status,
                error=outcome.error,
            )
            # A rejected write granted nothing, so it must not block a later attempt.
            self.journal.update_benefit_state(journal_key, "failed")
            self.report.errors.append(
                {
                    "stage": "write",
                    "action": proposal.action_id,
                    "status": outcome.status,
                    "error": outcome.error,
                    "effect": "The action did not take effect; the case reports it as "
                    "outstanding.",
                }
            )
            return record

        # --- unknown: reconcile, never retry -------------------------------
        self.journal.update_outcome(
            journal_key,
            state="unknown",
            response_status=outcome.status,
            error=outcome.error,
        )
        self.journal.update_benefit_state(journal_key, "unknown")
        reconciled = self._reconcile(proposal)
        if reconciled is not None:
            returned_id = reconciled.get(_ID_FIELD[proposal.action_type])
            record.state = ActionState.SUCCEEDED
            record.returned_ids = {_ID_FIELD[proposal.action_type]: returned_id}
            record.verification = (
                "The request's outcome was not returned to us, but GET /_audit shows "
                "the server committed it. Reconciled, not retried."
            )
            self.journal.update_outcome(
                journal_key,
                state="succeeded",
                response_status=outcome.status,
                returned_id=str(returned_id),
                error=outcome.error,
            )
            self.journal.update_benefit_state(journal_key, "succeeded")
            self.report.any_write_succeeded = True
            if proposal.action_type == ActionType.ESCALATION and returned_id:
                self.report.escalation_ids.append(str(returned_id))
            return record

        record.state = ActionState.UNKNOWN
        record.error = outcome.error
        record.verification = (
            "The outcome could not be established and the server's audit log does not "
            "show the write. It was NOT retried: a blind retry here risks a duplicate "
            "payment or a duplicate journey."
        )
        self._blocked_by_unknown = True
        self.report.unknown_outcomes.append(
            "{} on booking {} has an unknown outcome.".format(
                proposal.action_type.value, proposal.booking_ref
            )
        )
        self.report.errors.append(
            {
                "stage": "write",
                "action": proposal.action_id,
                "status": outcome.status,
                "error": outcome.error,
                "effect": "Outcome unknown. Later money movements on this booking are "
                "blocked and the case is handed to a human.",
            }
        )
        return record

    def _post(self, proposal: ActionProposal, body: dict[str, Any]) -> WriteOutcome:
        sender: Callable[[dict[str, Any]], WriteOutcome] = {
            ActionType.REBOOKING: self.ops.post_rebooking,
            ActionType.HOTEL_VOUCHER: self.ops.post_hotel_voucher,
            ActionType.COMPENSATION_PAYMENT: self.ops.post_compensation,
            ActionType.GOODWILL_PAYMENT: self.ops.post_goodwill,
            ActionType.REFUND: self.ops.post_refund,
            ActionType.ESCALATION: self.ops.post_escalation,
        }[proposal.action_type]
        return sender(body)

    def _reconcile(self, proposal: ActionProposal) -> dict[str, Any] | None:
        """Ask the server whether it committed a write we lost the answer to."""
        matches = self._server_equivalents(proposal, refresh=True)
        return matches[-1] if matches else None

    def _verify_by_readback(self) -> None:
        """Confirm every id we were handed really exists in the server's write log."""
        if self.dry_run:
            return
        succeeded = [
            a for a in self.report.actions if a.state == ActionState.SUCCEEDED
        ]
        if not succeeded:
            return
        audit = self._audit(refresh=True)
        if audit is None:
            for action in succeeded:
                action.verification = (
                    (action.verification + " " if action.verification else "")
                    + "The server's write log could not be read, so confirmation "
                    "rests on the API response alone."
                )
            return
        writes = audit.get("writes", {})
        all_ids: set[str] = set()
        for rows in writes.values():
            for row in rows:
                for key in _ID_FIELD.values():
                    if row.get(key):
                        all_ids.add(str(row[key]))
        for action in succeeded:
            returned = (action.returned_ids or {}).get(
                _ID_FIELD[ActionType(action.action_type)]
            )
            if returned and str(returned) in all_ids:
                confirmation = "Confirmed by readback: {} appears in GET /_audit.".format(
                    returned
                )
                # Keep how we got here. An action that was reconciled after a lost
                # response is a materially different story from a clean 201, and the
                # audit trail should say so.
                action.verification = (
                    action.verification + " " + confirmation
                    if action.verification
                    else confirmation
                )
            elif returned:
                action.verification = (
                    "The API returned {} but it does not appear in GET /_audit. "
                    "Confirmation rests on the API response alone.".format(returned)
                )
            else:
                action.verification = (
                    "The API accepted the request but returned no identifier. "
                    "Confirmation rests on the API response alone."
                )


# ---------------------------------------------------------------------------


def _matcher_for(proposal: ActionProposal) -> Callable[[dict[str, Any]], bool]:
    """Equivalence, not equality: same remedy, same passengers, same event."""
    booking = (proposal.booking_ref or "").upper()
    params = proposal.fingerprint_params
    action = proposal.action_type

    def same_booking(write: dict[str, Any]) -> bool:
        return str(write.get("booking_ref", "")).upper() == booking

    def same_passengers(write: dict[str, Any]) -> bool:
        expected = set(params.get("passenger_ids") or [])
        actual = set(write.get("passenger_ids") or [])
        return expected == actual

    if action == ActionType.REBOOKING:
        return lambda w: (
            same_booking(w)
            and w.get("flight_no") == params.get("flight_no")
            and w.get("date") == params.get("date")
            and same_passengers(w)
            and w.get("status") == "CONFIRMED"
        )
    if action == ActionType.HOTEL_VOUCHER:
        return lambda w: (
            same_booking(w)
            and str(w.get("station", "")).upper() == str(params.get("station", "")).upper()
            and w.get("night") == params.get("night")
            and same_passengers(w)
        )
    if action == ActionType.COMPENSATION_PAYMENT:
        return lambda w: (
            same_booking(w)
            and w.get("type") == "COMPENSATION"
            and _pence(w.get("amount_gbp")) == params.get("amount_minor")
        )
    if action == ActionType.GOODWILL_PAYMENT:
        return lambda w: (
            same_booking(w)
            and w.get("type") == "GOODWILL"
            and _pence(w.get("amount_gbp")) == params.get("amount_minor")
        )
    if action == ActionType.REFUND:
        return lambda w: (
            same_booking(w)
            and _pence(w.get("amount_gbp")) == params.get("amount_minor")
            and same_passengers(w)
        )
    if action == ActionType.ESCALATION:
        # Matched on the stable referral key carried inside the summary, never on the
        # summary text itself -- that text is model-authored and drifts between runs,
        # which duplicated 8 of 20 referrals on an unchanged repeat.
        marker = "{}{}]".format(REFERRAL_KEY_PREFIX, params.get("referral_key", ""))
        return lambda w: (
            same_booking(w)
            and w.get("queue") == params.get("queue")
            and marker in str(w.get("summary", ""))
        )
    return lambda w: False


def _pence(value: Any) -> int:
    from decimal import Decimal, ROUND_HALF_UP

    if value is None:
        return -1
    return int(
        (Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def _summarise_request(
    proposal: ActionProposal, body: dict[str, Any]
) -> dict[str, Any]:
    """What was sent, without dragging a whole payload into the record."""
    keep = {
        "booking_ref",
        "passenger_ids",
        "option_id",
        "flight_no",
        "date",
        "cabin",
        "fare_gbp",
        "amount_gbp",
        "station",
        "night",
        "queue",
        "blocking_clause",
    }
    return {
        "path": _POST_PATHS[proposal.action_type],
        "fields": {k: v for k, v in body.items() if k in keep},
    }


def _to_record(proposal: ActionProposal) -> PlannedAction:
    return PlannedAction(
        action_id=proposal.action_id,
        action_type=proposal.action_type,
        state=proposal.state,
        booking_ref=proposal.booking_ref,
        passenger_ids=list(proposal.passenger_ids),
        amount=proposal.amount,
        itinerary=proposal.itinerary,
        policy_basis=list(proposal.policy_basis),
        preconditions_checked=dict(proposal.preconditions),
        consent_basis=proposal.consent_basis,
        blocked_reason=proposal.blocked_reason,
        fingerprint=None,
        journal_key=None,
        request_summary=None,
        response_status=None,
        returned_ids=None,
        verification=None,
        error=None,
    )


REFERRAL_KEY_PREFIX = "[referral-key: "


def referral_key(
    *,
    ops_base_url: str,
    case_sha: str,
    queue: str,
    disruption_scope: str,
) -> str:
    """A stable identity for "this referral, about this contact, for this queue".

    Anchored on the inbound message's own sha256. The same message re-processed
    produces the same key no matter how the model words its output that time; a
    different message produces a different key and reaches a human on its own.

    Two earlier designs both failed, and the reasons are worth keeping:

    * Matching on `summary` re-raised 8 of 20 referrals on an unchanged repeat, because
      the summary is assembled from model-authored text and drifts between runs.
    * Matching on the booking plus a multiset of blocking clauses drifted too. On a
      repeat of case-02 the model split the same assistance complaint into three items
      where it had found two, the clause multiset grew from {S14.4, S14.4} to three
      entries, and a duplicate SPECIAL_ASSISTANCE referral went out (ESC-00003 then
      ESC-00022). The multiset was meant to detect a genuinely new issue; what it
      actually tracked was how finely the model chose to slice the same complaint.

    The message hash has neither problem. A genuinely new issue arrives as a new
    inbound message, so it hashes differently and is never suppressed; re-running the
    same message is exactly the case that should reuse the referral already open.

    The known limit: two separate messages describing the same problem for the same
    booking raise two referrals. That is the safer direction -- a colleague sees a
    contact twice rather than a passenger going unanswered -- and every inbound contact
    arguably warrants acknowledgement under S15 in any case.
    """
    payload = "|".join(
        [ops_base_url.rstrip("/"), case_sha, queue, disruption_scope]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def escalation_proposal(
    *,
    action_id: str,
    booking_ref: str | None,
    queue: str,
    summary: str,
    requested_decision: str,
    recommendation: str,
    blocking_clause: str,
    disruption_scope: str,
    key: str,
) -> ActionProposal:
    """Build the typed proposal for POST /escalations (API.md S18).

    `key` is carried inside `summary`, which API.md S18 documents, rather than in an
    invented field. It stays visible to whoever reads the referral.
    """
    marked_summary = "{} {}{}]".format(summary, REFERRAL_KEY_PREFIX, key)
    body = {
        "summary": marked_summary,
        "requested_decision": requested_decision,
        "queue": queue,
        "recommendation": recommendation,
        "blocking_clause": blocking_clause,
    }
    if booking_ref:
        body["booking_ref"] = booking_ref
    return ActionProposal(
        action_id=action_id,
        action_type=ActionType.ESCALATION,
        state=ActionState.PROPOSED,
        booking_ref=booking_ref,
        passenger_ids=[],
        amount=None,
        itinerary=None,
        policy_basis=["S12.5", blocking_clause],
        consent_basis=None,
        preconditions={
            "referral_completeness": (
                "S12.5 requires what the passenger asked for, what the record shows, "
                "the recommended outcome, the blocking condition, and what the "
                "referring party must decide. All five are present."
            )
        },
        request_body=body,
        disruption_scope=disruption_scope,
        fingerprint_params={
            "queue": queue,
            "blocking_clause": blocking_clause,
            "referral_key": key,
        },
    )


__all__ = [
    "Executor",
    "ExecutionReport",
    "escalation_proposal",
    "referral_key",
    "REFERRAL_KEY_PREFIX",
    "Money",
]
