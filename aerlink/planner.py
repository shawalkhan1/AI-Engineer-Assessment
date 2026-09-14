"""Deciding what to do. Deterministic, and the only place remedies are chosen.

Nothing here consults a model. It takes verified facts and the structured reading of
the contact, and produces typed proposals plus the referrals that S12.5 requires.
Four things are kept deliberately apart throughout, because conflating them is how a
desk pays the wrong amount:

* what the passenger **asked for**;
* what they are **eligible** for;
* what has **already been provided**;
* what we are **proposing to do now**.

Option selection is arithmetic, not judgement: filter on hard constraints, sort by
arrival then fare, take the first. A model never picks a flight.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Protocol

from . import policy
from .config import Config
from .policy import Authority, Money, gbp, pence_to_api_amount
from .schemas import ActionState, ActionType, RequestType
from .untrusted import (
    claims_internal_authority,
    normalise_for_span,
    quote_is_tainted,
    tainted_blocks,
)
from .timeutil import (
    arrival_datetime,
    departure_local_minutes,
    parse_date,
    parse_local_deadline,
    parse_local_time,
    station_local_now,
    inventory_departure_utc,
)

# Queues the operations API documents for POST /escalations (API.md S18).
QUEUE_SUPERVISOR = "SUPERVISOR"
QUEUE_YTP = "YTP"
QUEUE_SPECIAL_ASSISTANCE = "SPECIAL_ASSISTANCE"
QUEUE_OPS_LIAISON = "OPS_LIAISON"
QUEUE_CUSTOMER_CONDUCT = "CUSTOMER_CONDUCT"
QUEUE_LOST_PROPERTY = "LOST_PROPERTY"
QUEUE_GENERAL = "GENERAL"


class Inventory(Protocol):
    """What the planner needs from the outside world, so it can be faked in tests."""

    def own_availability(
        self, origin: str, destination: str, date_iso: str, booking_ref: str
    ) -> dict[str, Any]: ...

    def partner_availability(
        self, origin: str, destination: str, date_iso: str, booking_ref: str
    ) -> dict[str, Any]: ...

    def hotel_allocation(self, iata: str, night_iso: str) -> dict[str, Any] | None: ...

    def existing_hotel_voucher(
        self, booking_ref: str | None, station: str, night_iso: str
    ) -> dict[str, Any] | None: ...


@dataclass
class HandoverItem:
    queue: str
    summary: str
    requested_decision: str
    recommendation: str
    blocking_clause: str
    # Internal referrals (a forged instruction, a service disagreement) are work for
    # Aerlink, not an unanswered passenger request, and do not by themselves stop a
    # case being resolved for the passenger.
    internal_only: bool = False
    # What the passenger may be told about this referral. `requested_decision` is
    # written for the colleague picking the case up and often reads as a promise
    # ("release the refund"); it must never reach the reply. This is the safe
    # phrasing, and it is the only one the narration brief ever sees.
    passenger_note: str = ""

    def note_for_passenger(self) -> str:
        return self.passenger_note or (
            "Part of this needs a colleague to look at, and it has been passed to "
            "them. No decision has been made on it yet."
        )


@dataclass
class ActionProposal:
    action_id: str
    action_type: ActionType
    state: ActionState
    booking_ref: str | None
    passenger_ids: list[str]
    amount: Money | None
    itinerary: dict[str, Any] | None
    policy_basis: list[str]
    consent_basis: str | None
    preconditions: dict[str, Any]
    request_body: dict[str, Any] | None
    blocked_reason: str | None = None
    disruption_scope: str = ""
    fingerprint_params: dict[str, Any] = field(default_factory=dict)
    # Re-checked immediately before the write; see executor.refresh_precondition.
    refresh: dict[str, Any] | None = None


@dataclass
class Plan:
    proposals: list[ActionProposal] = field(default_factory=list)
    handovers: list[HandoverItem] = field(default_factory=list)
    requested: list[dict[str, Any]] = field(default_factory=list)
    eligible: list[dict[str, Any]] = field(default_factory=list)
    already_provided: list[dict[str, Any]] = field(default_factory=list)
    uncertainties: list[dict[str, str]] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)
    rejected_alternatives: list[dict[str, Any]] = field(default_factory=list)
    answers: list[dict[str, str]] = field(default_factory=list)
    needs_passenger_input: list[str] = field(default_factory=list)
    # What became of each live request. S15.1 twice forbids answering the easy request
    # and leaving the others, so "nothing at all happened to this one" has to be
    # visible rather than inferred from a silence.
    request_dispositions: list[dict[str, Any]] = field(default_factory=list)

    def add_uncertainty(self, issue: str, effect: str) -> None:
        self.uncertainties.append({"issue": issue, "effect_on_decision": effect})

    def _tally(self) -> tuple[int, int, int, int]:
        return (
            len(self.proposals),
            len(self.handovers),
            len(self.answers),
            len(self.needs_passenger_input),
        )


@dataclass
class CaseFacts:
    booking: dict[str, Any]
    flight: dict[str, Any] | None
    entitlement: dict[str, Any]
    customer: dict[str, Any]
    cross_check: policy.CrossCheck
    advisories: list[dict[str, Any]]
    flight_events: list[dict[str, Any]]
    now: datetime
    now_source: str

    @property
    def booking_ref(self) -> str:
        return self.booking["booking_ref"]

    @property
    def affected_segment(self) -> dict[str, Any] | None:
        disruption = self.booking.get("disruption") or {}
        seg_id = disruption.get("affected_segment")
        return next(
            (s for s in self.booking.get("segments", []) if s.get("segment_id") == seg_id),
            None,
        )

    @property
    def disruption_scope(self) -> str:
        segment = self.affected_segment
        if segment:
            return "{}:{}".format(segment.get("flight_no"), segment.get("date"))
        return "{}:no-disruption".format(self.booking_ref)


# ---------------------------------------------------------------------------


def _name_tokens(value: str) -> list[str]:
    """Split a name into comparable tokens, dropping punctuation and titles."""
    cleaned = re.sub(r"[^\w\s-]", " ", (value or "").casefold())
    tokens = [t for t in cleaned.split() if t]
    return [t for t in tokens if t not in {"mr", "mrs", "ms", "miss", "dr", "prof"}]


def match_passengers(
    booking: dict[str, Any], names: list[str]
) -> tuple[list[str], list[str]]:
    """Map names as written in the message to passenger ids on the booking.

    People sign off differently from how they are ticketed. "K. Braithwaite" is
    Kenneth Braithwaite, and failing to see that dropped his request for a flight
    out of the reply entirely. So an initial is allowed to stand for a given name --
    but only where the surname matches exactly and only one passenger fits, because
    matching the wrong passenger is far worse than matching none.
    """
    passengers = booking.get("passengers", [])
    matched: list[str] = []
    unmatched: list[str] = []

    for raw in names:
        tokens = _name_tokens(raw)
        if not tokens:
            continue
        needle = " ".join(tokens)
        candidates = []
        for pax in passengers:
            given = (pax.get("given_name") or "").casefold()
            surname = (pax.get("surname") or "").casefold()
            full = "{} {}".format(given, surname).strip()
            if needle in (full, given, surname):
                candidates.append(pax["passenger_id"])

        hit = candidates[0] if len(candidates) == 1 else None

        if not candidates and len(tokens) >= 2:
            # "K. Braithwaite" / "Kenneth B" -- surname exact, given name by initial.
            surname_token = tokens[-1]
            lead = tokens[0]
            candidates = []
            for pax in passengers:
                given = (pax.get("given_name") or "").casefold()
                surname = (pax.get("surname") or "").casefold()
                if surname != surname_token:
                    continue
                if given == lead or (lead and given[:1] == lead and len(lead) == 1):
                    candidates.append(pax["passenger_id"])
            if len(candidates) == 1:
                hit = candidates[0]

        if hit and hit not in matched:
            matched.append(hit)
        elif not hit:
            unmatched.append(raw)
    return matched, unmatched


def live_requests(
    extraction: Any, verified_indexes: set[int], inbound_text: str = ""
) -> list[dict[str, Any]]:
    """Requests that are still open, with why each one is or is not live.

    S15.2: only the most recent stated intention is actioned. S15.3: a request the
    record shows was already resolved is not actioned again. S12.4: text inside
    content that claims authority over handling is not a request from the passenger,
    however it was categorised -- a forged notice demanding a GBP 5,000 goodwill
    payment must not become "the passenger is asking for GBP 5,000".
    """
    # Only content that actually claims authority over handling seeds the taint
    # region. The deterministic indicators inside `tainted_blocks` still mark a forged
    # block on their own, so a hostile message with no authority claim is still caught.
    tainted = tainted_blocks(
        inbound_text,
        [
            e.quote
            for e in extraction.embedded_instructions
            if claims_internal_authority(e.claimed_authority)
        ],
    )
    out: list[dict[str, Any]] = []
    for index, req in enumerate(extraction.requests):
        reasons: list[str] = []
        if quote_is_tainted(req.quote, tainted):
            reasons.append(
                "this text is part of content claiming authority over how the case is "
                "handled, not a request made by the passenger (S12.4)"
            )
        if req.superseded_by_later_message:
            reasons.append(
                "superseded by a later message in the same contact (S15.2)"
                + (": " + req.supersession_note if req.supersession_note else "")
            )
        if req.already_answered_in_thread:
            reasons.append(
                "the contact itself shows this was already dealt with (S15.3)"
            )
        out.append(
            {
                "index": index,
                "request_type": req.request_type.value,
                "detail": req.detail,
                "quote": req.quote,
                "quote_verified": index in verified_indexes,
                "for_passenger_names": list(req.for_passenger_names),
                "stated_at": req.stated_at,
                "passenger_has_authorised_booking": req.passenger_has_authorised_booking,
                "live": not reasons,
                "not_live_because": reasons,
            }
        )
    return out


# ---------------------------------------------------------------------------


class Planner:
    def __init__(self, config: Config, inventory: Inventory) -> None:
        self.config = config
        self.inventory = inventory
        self._seq = 0
        self._inbound_text = ""

    def _next_id(self, kind: str) -> str:
        self._seq += 1
        return "ACT-{:02d}-{}".format(self._seq, kind)

    # -- entry point -------------------------------------------------------

    def plan(
        self,
        facts: CaseFacts,
        extraction: Any,
        *,
        requests: list[dict[str, Any]],
        injection_indicators: list[dict[str, str]],
        inbound_text: str = "",
    ) -> Plan:
        self._inbound_text = inbound_text
        plan = Plan()
        plan.requested = requests
        plan.already_provided = policy.prior_benefits(facts.customer, facts.booking_ref)

        self._note_authoritative_facts(plan, facts)

        if extraction is None:
            # The contact could not be read into structured form -- the model was
            # unavailable, out of budget, or produced nothing usable. We do not guess
            # at what was asked for, and nothing is actioned. But an entitlement is a
            # fact about the booking and the operational record, not about the
            # message, so it is still assessed and still referred with its figure: a
            # passenger does not lose what they are owed because a request timed out.
            self._plan_compensation(plan, facts, request=None)
            self._record_eligibility_summary(plan, facts)
            plan.handovers.append(
                HandoverItem(
                    queue=QUEUE_GENERAL,
                    summary=(
                        "A contact about booking {} could not be read into structured "
                        "form, so what the passenger is asking for has not been "
                        "established. The operational facts and the entitlement "
                        "assessment are in the case record.".format(facts.booking_ref)
                    ),
                    requested_decision="Read the contact and work the case.",
                    recommendation=(
                        "Nothing was actioned. The entitlement service reports {} for "
                        "this booking.".format(facts.entitlement.get("status"))
                    ),
                    blocking_clause="local rule: no action without an established request",
                )
            )
            plan.add_uncertainty(
                "The inbound contact could not be read into structured form.",
                "No remedy was chosen or actioned; the case was handed to a human "
                "with the operational facts attached.",
            )
            return plan

        self._refuse_embedded_instructions(plan, extraction, injection_indicators)

        ytp = policy.ytp_passengers(facts.booking)
        if ytp:
            self._block_everything_for_ytp(plan, facts, ytp, requests)
            return plan

        if not facts.cross_check.agrees:
            plan.handovers.append(
                HandoverItem(
                    queue=QUEUE_SUPERVISOR,
                    summary=(
                        "The entitlement calculation service and the policy text "
                        "disagree on this booking: "
                        + "; ".join(facts.cross_check.disagreements[:3])
                    ),
                    requested_decision=(
                        "Confirm which figure is correct before anything is paid."
                    ),
                    recommendation=(
                        "S10.4 forbids substituting a locally derived figure. No "
                        "payment has been made on this booking."
                    ),
                    blocking_clause="S10.4",
                )
            )
            plan.add_uncertainty(
                "The authoritative entitlement service disagrees with the policy text.",
                "All payments on this booking are blocked pending the S10.4 referral.",
            )

        live = [r for r in requests if r["live"]]
        handled: set[str] = set()
        for request in live:
            kind = request["request_type"]
            if kind in handled and kind in {
                RequestType.COMPENSATION.value,
            }:
                continue
            handled.add(kind)
            before = plan._tally()
            self._dispatch(plan, facts, extraction, request)
            after = plan._tally()
            outcomes = [
                label
                for label, grew in zip(
                    ("action proposed", "referred to a human", "answered in the reply",
                     "passenger asked"),
                    (a > b for a, b in zip(after, before)),
                )
                if grew
            ]
            plan.request_dispositions.append(
                {
                    "request_type": request["request_type"],
                    "detail": request["detail"],
                    "addressed_by": outcomes,
                    "addressed": bool(outcomes),
                }
            )

        rebooks = [p for p in plan.proposals if p.action_type == ActionType.REBOOKING]
        refunds = [p for p in plan.proposals if p.action_type == ActionType.REFUND]
        conflicts = [p for p in rebooks + refunds if any(
            set(p.passenger_ids) & set(other.passenger_ids)
            for other in (refunds if p.action_type == ActionType.REBOOKING else rebooks))]
        if conflicts:
            for proposal in conflicts:
                proposal.state = ActionState.BLOCKED
                proposal.blocked_reason = "S7.3: refund and re-routing conflict for the same passengers; confirm their election."
            plan.handovers.append(HandoverItem(queue=QUEUE_SUPERVISOR,
                summary="The same passengers have conflicting refund and re-routing requests.",
                requested_decision="Confirm each passenger's latest choice before making changes.",
                recommendation="Neither conflicting remedy was executed.", blocking_clause="S7.3, S15.2"))

        # Compensation is an entitlement, not a favour: assess it whenever the record
        # shows a disruption, even where the passenger only asked for something else.
        if RequestType.COMPENSATION.value not in handled:
            self._plan_compensation(plan, facts, request=None)

        self._check_discretionary_demand(plan, facts, extraction)
        self._record_eligibility_summary(plan, facts)
        return plan

    # -- dispatch ----------------------------------------------------------

    def _dispatch(
        self,
        plan: Plan,
        facts: CaseFacts,
        extraction: Any,
        request: dict[str, Any],
    ) -> None:
        kind = request["request_type"]
        if kind in {RequestType.REBOOKING.value, RequestType.REFUND.value,
                    RequestType.HOTEL_ACCOMMODATION.value} and not request.get("quote_verified"):
            plan.needs_passenger_input.append(
                "We could not verify your request to change the booking or issue a "
                "benefit. Please confirm what you want us to arrange."
            )
            plan.add_uncertainty(
                "The request has no verified quote in the inbound message.",
                "No action was proposed for this request; confirmation is required.",
            )
            return
        if kind == RequestType.REBOOKING.value:
            self._plan_rebooking(plan, facts, extraction, request)
        elif kind == RequestType.REFUND.value:
            self._plan_refund(plan, facts, request)
        elif kind == RequestType.COMPENSATION.value:
            self._plan_compensation(plan, facts, request)
        elif kind == RequestType.HOTEL_ACCOMMODATION.value:
            self._plan_hotel(plan, facts, request)
        elif kind == RequestType.EXPENSE_REIMBURSEMENT.value:
            self._plan_expense_reimbursement(plan, facts, request)
        elif kind == RequestType.GOODWILL_OR_EXTRA_PAYMENT.value:
            self._plan_goodwill(plan, facts, request)
        elif kind == RequestType.ASSISTANCE_SERVICE.value:
            self._plan_assistance(plan, facts, request)
        elif kind == RequestType.LOST_PROPERTY.value:
            self._route_out_of_scope(plan, facts, request, QUEUE_LOST_PROPERTY)
        elif kind == RequestType.OUT_OF_SCOPE.value:
            self._route_out_of_scope(plan, facts, request, QUEUE_GENERAL)
        elif kind == RequestType.SERVICE_COMPLAINT.value:
            self._plan_service_complaint(plan, facts, request)
        elif kind == RequestType.INFORMATION_ONLY.value:
            plan.answers.append(
                {
                    "request": request["detail"],
                    "answer_basis": "Answered from the operational record in the reply.",
                }
            )
        elif kind == RequestType.CANCEL_PREVIOUS_REQUEST.value:
            plan.rationale.append(
                "The contact withdraws or qualifies an earlier request. S15.2 means "
                "the withdrawn request is not actioned: " + request["detail"]
            )
            # It must also be said back to the passenger. Recording it only in the
            # rationale meant a withdrawal was understood internally and never
            # acknowledged, which reads as having been ignored.
            plan.answers.append(
                {
                    "request": request["detail"],
                    "answer_basis": (
                        "Noted, and nothing has been actioned on it. Confirm this back "
                        "to the passenger in the reply so they know it was understood."
                    ),
                }
            )
        else:
            # A catch-all request is answered in the reply. It is not routed to another
            # team on its own: S15.6 is about matters outside this policy, and treating
            # "explain your reasoning" as one of those would refer a case that has
            # already been answered in full.
            plan.answers.append(
                {
                    "request": request["detail"],
                    "answer_basis": (
                        "Addressed in the reply from the operational record and the "
                        "entitlement assessment."
                    ),
                }
            )
            plan.add_uncertainty(
                "A request did not fit any recognised category: {}".format(
                    request["detail"]
                ),
                "It was answered in the reply rather than actioned or routed. Check "
                "the reply covers it.",
            )

    # -- individual remedies ----------------------------------------------

    def _plan_rebooking(
        self,
        plan: Plan,
        facts: CaseFacts,
        extraction: Any,
        request: dict[str, Any],
    ) -> None:
        segment = facts.affected_segment
        if segment is None:
            plan.handovers.append(
                HandoverItem(
                    queue=QUEUE_GENERAL,
                    summary="Re-routing requested but the record holds no disrupted "
                    "segment on booking {}.".format(facts.booking_ref),
                    requested_decision="Establish what the passenger is travelling on.",
                    recommendation="Do not re-route until the affected segment is known.",
                    blocking_clause="S6.1",
                )
            )
            return

        flight = facts.flight or {}
        delay = flight.get("departure_delay_minutes")
        if flight.get("status") != "CANCELLED" and not (
            flight.get("status") == "DELAYED" and delay is not None and delay > 300
        ):
            plan.handovers.append(HandoverItem(
                queue=QUEUE_GENERAL,
                summary="Re-routing requested; the operational record does not establish cancellation or an expected departure delay exceeding five hours.",
                requested_decision="Verify eligibility and the current journey before offering a change.",
                recommendation="No rebooking was made. Apply S6.1 using the verified flight status and expected departure delay.",
                blocking_clause="S6.1",
            ))
            return

        if not request["quote_verified"]:
            plan.needs_passenger_input.append(
                "We have not booked you onto another flight, because we want to be "
                "certain what you want before we confirm a seat. Tell us when you need "
                "to travel and we will arrange it."
            )
            plan.add_uncertainty(
                "The re-routing request could not be traced to a verbatim span of the "
                "message.",
                "No re-routing was confirmed; the passenger is asked to confirm.",
            )
            return

        passenger_ids, unmatched = self._resolve_request_passengers(
            plan, facts, request
        )
        if passenger_ids is None:
            return

        # S14.4: a booked assistance service must be re-booked onto the new service
        # before the re-routing is confirmed. The operations API has no endpoint that
        # can move an assistance booking, so this system cannot satisfy the condition.
        assistance = set(policy.assistance_passengers(facts.booking))
        blocked_assist = [p for p in passenger_ids if p in assistance]
        if blocked_assist:
            names = self._names_for(facts, blocked_assist)
            plan.handovers.append(
                HandoverItem(
                    queue=QUEUE_SPECIAL_ASSISTANCE,
                    summary=(
                        "{} on booking {} has a confirmed assistance requirement ({}) "
                        "and needs re-routing after {} was cancelled. The operations "
                        "API exposes no way to re-book the assistance service, so the "
                        "re-routing has not been confirmed.".format(
                            names,
                            facts.booking_ref,
                            facts.booking.get("special_requests") or "recorded on the booking",
                            segment.get("flight_no"),
                        )
                    ),
                    requested_decision=(
                        "Re-book the assistance onto the chosen service and then "
                        "confirm the re-routing, or arrange an alternative."
                    ),
                    recommendation=(
                        "S14.4 forbids confirming a re-routing for a declared-"
                        "assistance passenger without moving the assistance with it. "
                        "A colleague must check current flight options and arrange "
                        "the assistance before confirming the new journey."
                    ),
                    blocking_clause="S14.4",
                    passenger_note=(
                        "We have not moved {} onto another flight yet. Her booked "
                        "wheelchair assistance has to be arranged on the new flight "
                        "first, and a colleague who can do that has the case."
                    ).format(names),
                )
            )
            plan.add_uncertainty(
                "Assistance cannot be re-booked through the operations API.",
                "Re-routing for {} is blocked and referred, not attempted.".format(names),
            )
            passenger_ids = [p for p in passenger_ids if p not in assistance]
            if not passenger_ids:
                return

        consent = self._rebooking_consent(extraction, request)
        if consent is None:
            plan.needs_passenger_input.append(
                "We have not booked you onto another flight yet. Confirming a seat is "
                "hard to undo, so we would rather you told us what suits you first. "
                "Let us know when you need to travel and we will arrange it."
            )
            plan.add_uncertainty(
                "No explicit instruction to book was found in the contact, so S6.4 "
                "treats the passenger as not yet having expressed a preference.",
                "No seat confirmed. The reply asks the passenger what suits them.",
            )
            return

        target_date = self._target_date(extraction, facts, segment)
        deadline = parse_local_deadline(extraction.preferences.arrive_by_local)
        prefs = extraction.preferences
        rebooking_requests = [r for r in plan.requested if r.get("live") and r.get("request_type") == RequestType.REBOOKING.value]
        invalid_preferences = (
            (prefs.arrive_by_local and deadline is None)
            or (prefs.travel_date_iso and parse_date(prefs.travel_date_iso) is None)
            or (prefs.depart_not_before_local and parse_local_time(prefs.depart_not_before_local) is None)
        )
        from .untrusted import verify_span
        explicit_constraints = bool(prefs.arrive_by_local or prefs.travel_date_iso or
                                    prefs.depart_not_before_local or prefs.alternative_origin_airports)
        unverified_preferences = explicit_constraints and self._inbound_text and not verify_span(prefs.quote, self._inbound_text)
        if invalid_preferences or unverified_preferences or (len(rebooking_requests) > 1 and explicit_constraints):
            plan.handovers.append(HandoverItem(
                queue=QUEUE_SUPERVISOR,
                summary="Travel preferences cannot safely be assigned to this passenger group.",
                requested_decision="Confirm each traveller's date, deadline and acceptable airports before booking.",
                recommendation="No seat confirmed: the extracted preferences are invalid, unverified, or shared between distinct requests.",
                blocking_clause="S6.4, S15.1",
            ))
            return
        cabin = segment.get("cabin") or "ECONOMY"
        destination = facts.booking.get("final_destination") or segment.get("destination")

        origins = [segment.get("origin")]
        for alt in extraction.preferences.alternative_origin_airports[:1]:
            if alt and alt.upper() != segment.get("origin"):
                origins.append(alt.upper())

        searched: list[dict[str, Any]] = []
        chosen: dict[str, Any] | None = None
        chosen_origin = None
        for origin in origins:
            inventory = self.inventory.own_availability(
                origin, destination, target_date.isoformat(), facts.booking_ref
            )
            if inventory.get("unavailable"):
                searched.append(
                    {
                        "route": "{}-{}".format(origin, destination),
                        "date": target_date.isoformat(),
                        "outcome": "inventory system did not respond: "
                        + str(inventory.get("error")),
                    }
                )
                continue
            selection = select_option(
                inventory.get("results", []),
                cabin=cabin,
                seats_needed=len(passenger_ids),
                arrive_by=deadline,
                depart_not_before=extraction.preferences.depart_not_before_local,
                flight_date=target_date,
                now_utc=facts.now,
            )
            searched.append(
                {
                    "route": "{}-{}".format(origin, destination),
                    "date": target_date.isoformat(),
                    "total_options": inventory.get("total_results"),
                    "candidate_set_truncated": inventory.get("candidate_set_truncated"),
                    "passing_all_constraints": selection["passing"],
                    "rejection_counts": selection["rejections"],
                    "options_with_no_additional_fare": selection["free_option_count"],
                }
            )
            if selection["chosen"] and (chosen is None or
                    (arrival_datetime(selection["chosen"], target_date), gbp(selection["chosen"].get("fare_gbp")).amount_minor)
                    < (arrival_datetime(chosen, target_date), gbp(chosen.get("fare_gbp")).amount_minor)):
                chosen = selection["chosen"]
                chosen_origin = origin
                plan.rejected_alternatives.append(
                    {
                        "context": "re-routing option selection",
                        "chosen": _option_summary(chosen),
                        "runners_up": [_option_summary(o) for o in selection["runners_up"]],
                        "rule": "Earliest arrival meeting every constraint; ties broken "
                        "by the lower listed fare.",
                    }
                )
                # Check other explicitly accepted origins too; choose the earliest arrival.

        if chosen is None:
            self._plan_partner_fallback(
                plan, facts, request, passenger_ids, searched, target_date, cabin
            )
            return

        # API.md S3: this field is "the additional fare payable". It is passed to the
        # gate as exactly that. An earlier version asserted the passenger was charged
        # nothing regardless, which no supplied source supports.
        additional_fare = gbp(chosen.get("fare_gbp"))
        authority = policy.authority_for_rebooking(
            own_carrier=(chosen.get("operated_by") == "Aerlink"),
            cabin_matches_booked=(chosen.get("cabin") == cabin),
            additional_fare_payable=additional_fare,
            operating_level=self.config.authority_level,
        )

        body = {
            "booking_ref": facts.booking_ref,
            "passenger_ids": passenger_ids,
            "option_id": chosen["option_id"],
            "flight_no": chosen["flight_no"],
            "date": chosen["date"],
            "cabin": chosen["cabin"],
            "fare_gbp": pence_to_api_amount(additional_fare),
            "notes": "S6.1(a) re-accommodation after {} was cancelled.".format(
                segment.get("flight_no")
            ),
        }
        proposal = ActionProposal(
            action_id=self._next_id("rebooking"),
            action_type=ActionType.REBOOKING,
            state=ActionState.PROPOSED if authority.allowed else ActionState.BLOCKED,
            booking_ref=facts.booking_ref,
            passenger_ids=passenger_ids,
            amount=additional_fare if additional_fare.amount_minor else None,
            itinerary=_option_summary(chosen),
            policy_basis=[authority.clause, "S6.1(a)", "S6.2", "S6.4"],
            consent_basis=consent,
            preconditions={
                "authority": authority.to_record(),
                "origin_searched": chosen_origin,
                "searches": searched,
                "seats_available_at_selection": chosen.get("seats_available"),
                "additional_fare_payable_gbp": additional_fare.as_decimal_str,
                "passengers_needing_seats": len(passenger_ids),
                "cabin_booked": cabin,
                "deadline_applied_destination_local": (
                    deadline.isoformat() if deadline else None
                ),
            },
            request_body=body,
            blocked_reason=None if authority.allowed else authority.reason,
            disruption_scope=facts.disruption_scope,
            fingerprint_params={
                "passenger_ids": passenger_ids,
                "flight_no": chosen["flight_no"],
                "date": chosen["date"],
                "cabin": chosen["cabin"],
            },
            refresh={
                "kind": "availability",
                "origin": chosen_origin,
                "destination": destination,
                "date": chosen["date"],
                "option_id": chosen["option_id"],
                "seats_needed": len(passenger_ids),
                "cabin": cabin,
            },
        )
        plan.proposals.append(proposal)
        if not authority.allowed:
            plan.handovers.append(
                HandoverItem(
                    queue=QUEUE_SUPERVISOR,
                    summary=(
                        "Re-routing for booking {} onto {} on {} is correct but above "
                        "representative authority.".format(
                            facts.booking_ref, chosen["flight_no"], chosen["date"]
                        )
                    ),
                    requested_decision="Authorise the re-routing.",
                    recommendation=(
                        "Recommended: option {} ({} on {}, departing {}, arriving {}, "
                        "{} cabin, {} seats available), additional fare payable GBP {}. "
                        "Own-carrier options were checked and the full candidate set is "
                        "in the case record (S8.3). {}".format(
                            chosen["option_id"],
                            chosen["flight_no"],
                            chosen["date"],
                            chosen["departure_local"],
                            chosen["arrival_local"],
                            chosen["cabin"],
                            chosen["seats_available"],
                            additional_fare.as_decimal_str,
                            authority.reason,
                        )
                    ),
                    passenger_note=(
                        "We have found you a seat but we have not confirmed it. It "
                        "carries an additional fare, which someone senior has to "
                        "approve before we can book it. They have the details."
                    ),
                    blocking_clause=authority.clause,
                )
            )
        if unmatched:
            plan.add_uncertainty(
                "The contact names travellers not on the booking: {}.".format(
                    ", ".join(unmatched)
                ),
                "Those names were not included in any action.",
            )

    def _plan_partner_fallback(
        self,
        plan: Plan,
        facts: CaseFacts,
        request: dict[str, Any],
        passenger_ids: list[str],
        searched: list[dict[str, Any]],
        target_date: date,
        cabin: str,
    ) -> None:
        """S8: partner metal, which is never actioned automatically (S8.2)."""
        tier = (facts.booking.get("tier") or "NONE").upper()
        grounds: list[str] = []
        complete = bool(searched) and all(
            "passing_all_constraints" in item and not item.get("candidate_set_truncated")
            for item in searched
        )
        if complete and all((item.get("passing_all_constraints") or 0) == 0 for item in searched):
            grounds.append("S8.1(d): no own-carrier option meets the requirement.")
        if tier in {"GOLD", "PLATINUM"}:
            grounds.append("S8.1(b): passenger holds {} tier.".format(tier))
        partner_evidence = []
        segment = facts.affected_segment or {}
        if grounds:
            partner = self.inventory.partner_availability(
                segment.get("origin"), facts.booking.get("final_destination") or segment.get("destination"),
                target_date.isoformat(), facts.booking_ref,
            )
            partner_evidence = [_option_summary(row) for row in partner.get("results", [])
                                if row.get("cabin") == cabin and int(row.get("seats_available") or 0) >= len(passenger_ids)
                                and (inventory_departure_utc(row, target_date) or facts.now) > facts.now][:3]
            plan.rejected_alternatives.append({"context": "partner options for human review",
                "searched": searched, "candidate_options_not_booked": partner_evidence,
                "unavailable": bool(partner.get("unavailable")),
                "note": "Supervisor must verify all passenger constraints before authorising."})
        plan.handovers.append(
            HandoverItem(
                queue=QUEUE_SUPERVISOR,
                summary=(
                    "No suitable own-carrier option was established on {} for the passenger's stated "
                    "requirement for booking {}. Own-carrier inventory was checked and "
                    "the results are in the case record (S8.3).".format(
                        target_date.isoformat(), facts.booking_ref
                    )
                ),
                requested_decision=(
                    "Decide whether to authorise partner re-accommodation, or an "
                    "own-carrier option on a later date."
                ),
                recommendation=(
                    "Grounds considered: "
                    + (" ".join(grounds) if grounds else "none established.")
                    + " Partner candidates for review: {}. ".format(partner_evidence)
                    + "S8.2 requires supervisor authorisation and forbids actioning "
                    "any partner re-routing automatically, so none was attempted."
                ),
                blocking_clause="S8.2",
                passenger_note=(
                    "We have not booked you onto anything. We could not establish a suitable "
                    "own-flight option on that date, so a colleague has to "
                    "decide what to offer you next."
                ),
            )
        )
        plan.add_uncertainty(
            "No own-carrier seat matched the passenger's constraints in {} cabin on "
            "{}.".format(cabin, target_date.isoformat()),
            "Re-routing referred to a supervisor rather than booked.",
        )

    def _plan_refund(
        self, plan: Plan, facts: CaseFacts, request: dict[str, Any]
    ) -> None:
        flight = facts.flight or {}
        cancelled = flight.get("status") == "CANCELLED"
        dep_delay = flight.get("departure_delay_minutes")
        entitled = cancelled or (dep_delay is not None and dep_delay >= 300)
        if not entitled:
            plan.answers.append(
                {
                    "request": request["detail"],
                    "answer_basis": (
                        "S7.1 gives a refund on cancellation, or on a departure delay "
                        "of 5 hours or more where the passenger elects not to travel. "
                        "The record shows status {} and departure delay {}.".format(
                            flight.get("status"), dep_delay
                        )
                    ),
                }
            )
            return

        passenger_ids, _unmatched = self._resolve_request_passengers(
            plan, facts, request
        )
        if passenger_ids is None:
            return

        if any(p.get("cabin_flown") for p in facts.booking.get("passengers", [])
               if p.get("passenger_id") in passenger_ids):
            plan.handovers.append(HandoverItem(
                queue=QUEUE_SUPERVISOR,
                summary="A refund was requested for passengers whose record indicates they have travelled.",
                requested_decision="Establish which journey parts remain unused or became pointless under S7.1, and calculate any refund.",
                recommendation="Do not refund the entire ticket automatically after travel. Check the recorded journey and the passenger's election.",
                blocking_clause="S7.1",
            ))
            return

        amount, basis, determinable = self._refund_amount(facts, passenger_ids)
        if not determinable:
            plan.handovers.append(
                HandoverItem(
                    queue=QUEUE_SUPERVISOR,
                    summary=(
                        "Refund requested for {} on booking {} after {} was cancelled. "
                        "The booking record holds a total paid of GBP {} across {} "
                        "passengers and a whole-segment fare, but no per-passenger "
                        "ticket price, so the refundable amount cannot be derived from "
                        "the record.".format(
                            self._names_for(facts, passenger_ids),
                            facts.booking_ref,
                            (facts.affected_segment or {}).get("flight_no"),
                            gbp(facts.booking.get("total_paid_gbp")).as_decimal_str,
                            len(facts.booking.get("passengers", [])),
                        )
                    ),
                    requested_decision=(
                        "Confirm the refundable amount for this passenger and release "
                        "the refund."
                    ),
                    recommendation=(
                        "Entitlement under S7.1 is clear; only the figure is not. "
                        "An equal share of the total paid would be GBP {}. S7.3 "
                        "allows this passenger to refund while the others travel. No "
                        "refund has been issued.".format(amount.as_decimal_str)
                    ),
                    blocking_clause="S7.1 (amount not derivable from the record)",
                    passenger_note=(
                        "The refund has not been issued. The booking record does not "
                        "hold a separate ticket price for each passenger, so the exact "
                        "amount has to be confirmed by a colleague before any money "
                        "moves. The entitlement to a refund itself is not in doubt."
                    ),
                )
            )
            plan.add_uncertainty(
                "No per-passenger ticket price is recorded on this booking.",
                "The refund is referred with a recommended figure rather than paid on "
                "an assumed split.",
            )
            return

        authority = policy.authority_for_refund(amount, operating_level=self.config.authority_level)
        body = {
            "booking_ref": facts.booking_ref,
            "passenger_ids": passenger_ids,
            "amount_gbp": pence_to_api_amount(amount),
            "reason": "S7.1 refund following cancellation of {}. {}".format(
                (facts.affected_segment or {}).get("flight_no"), basis
            ),
        }
        plan.proposals.append(
            ActionProposal(
                action_id=self._next_id("refund"),
                action_type=ActionType.REFUND,
                state=ActionState.PROPOSED if authority.allowed else ActionState.BLOCKED,
                booking_ref=facts.booking_ref,
                passenger_ids=passenger_ids,
                amount=amount,
                itinerary=None,
                policy_basis=["S7.1", "S7.3", authority.clause],
                consent_basis="The contact explicitly asks for a refund: {!r}".format(
                    request["quote"][:120]
                ),
                preconditions={"authority": authority.to_record(), "basis": basis},
                request_body=body,
                blocked_reason=None if authority.allowed else authority.reason,
                disruption_scope=facts.disruption_scope,
                fingerprint_params={
                    "passenger_ids": passenger_ids,
                    "amount_minor": amount.amount_minor,
                },
            )
        )
        plan.rationale.append(
            "S5.6: taking a refund does not remove any compensation entitlement; the "
            "two are separate remedies and are assessed separately."
        )

    def _plan_compensation(
        self, plan: Plan, facts: CaseFacts, request: dict[str, Any] | None
    ) -> None:
        entitlement = facts.entitlement
        status = entitlement.get("status")
        comp = entitlement.get("compensation") or {}
        journey = entitlement.get("journey") or {}

        if status == "NO_DISRUPTION_RECORDED":
            plan.answers.append(
                {
                    "request": (request or {}).get("detail", "compensation"),
                    "answer_basis": entitlement.get("note", ""),
                }
            )
            return

        if status == "INSUFFICIENT_DATA" or comp.get("status") == "INSUFFICIENT_DATA":
            plan.handovers.append(
                HandoverItem(
                    queue=QUEUE_GENERAL,
                    summary=(
                        "Compensation on booking {} cannot yet be assessed: the "
                        "entitlement service reports insufficient data because the "
                        "arrival delay at the final destination is not known until the "
                        "passenger's re-routing is settled.".format(facts.booking_ref)
                    ),
                    requested_decision=(
                        "Re-run the entitlement assessment once the re-routed arrival "
                        "is recorded, and pay whatever falls due."
                    ),
                    recommendation=(
                        "Cause code is {} ({}). "
                        "S1.2 measures arrival delay from actual arrival, so nothing "
                        "has been paid on a projection.".format(
                            journey.get("cause_code"),
                            "extraordinary, so no compensation would be payable"
                            if journey.get("cause_is_extraordinary")
                            else "within Aerlink's control, so compensation is likely "
                            "to fall due",
                        )
                    ),
                    blocking_clause="S5.2, S10.3",
                    passenger_note=(
                        "We cannot work out compensation yet. It depends on how late "
                        "you actually arrive at your final destination, and that is "
                        "not known until your new flight has run. It will be assessed "
                        "then. Do not read this as a decision either way."
                    ),
                )
            )
            plan.add_uncertainty(
                "Arrival delay at the final destination is not yet established, so "
                "compensation is not assessable.",
                "No compensation paid. Referred for assessment once the passenger has "
                "actually travelled.",
            )
            return

        if comp.get("status") == "NOT_PAYABLE" and gbp(
            entitlement.get("total_payable_gbp")
        ).amount_minor == 0:
            plan.answers.append(
                {
                    "request": (request or {}).get("detail", "compensation"),
                    "answer_basis": (
                        "Not payable. Recorded cause is {} and the arrival delay at "
                        "the final destination is {} minutes. S16 requires both "
                        "figures to be stated. Reasoning: {}".format(
                            journey.get("cause_code"),
                            journey.get("arrival_delay_minutes_at_final_destination"),
                            " ".join(comp.get("reasoning", [])),
                        )
                    ),
                }
            )
            plan.rationale.append(
                "S4.3: duty of care is unaffected by the cause and is owed regardless."
            )
            return

        total = gbp(entitlement.get("total_payable_gbp"))
        if total.amount_minor <= 0:
            return
        if not facts.cross_check.agrees:
            return  # blocked by the S10.4 referral raised in plan()

        paid_already = [
            entry
            for entry in plan.already_provided
            if entry.get("same_booking")
            and any(str(a).startswith("compensation_paid") for a in entry.get("actions", []))
        ]
        if paid_already:
            plan.answers.append(
                {
                    "request": (request or {}).get("detail", "compensation"),
                    "answer_basis": (
                        "The customer record already shows compensation paid on this "
                        "booking ({}). S15.3 and S16 forbid actioning a remedy a "
                        "second time.".format(
                            "; ".join(e.get("case_id", "?") for e in paid_already)
                        )
                    ),
                }
            )
            return

        passenger_ids = [
            p["passenger_id"]
            for p in entitlement.get("passengers", [])
            if gbp(p.get("total_payable_gbp")).amount_minor > 0
        ]
        if not passenger_ids:
            return
        authority = policy.authority_for_compensation_payment(
            total, operating_level=self.config.authority_level
        )
        breakdown = [
            {
                "passenger_id": p.get("passenger_id"),
                "compensation_gbp": p.get("compensation_gbp"),
                "downgrade_reimbursement_gbp": p.get("downgrade_reimbursement_gbp"),
                "total_payable_gbp": p.get("total_payable_gbp"),
            }
            for p in entitlement.get("passengers", [])
        ]
        body = {
            "booking_ref": facts.booking_ref,
            "passenger_ids": passenger_ids,
            "amount_gbp": pence_to_api_amount(total),
            "reason": (
                "APCP-2026-04 {} entitlement for {} ({}). Figure taken from the "
                "entitlement calculation service under S10.2.".format(
                    entitlement.get("policy_version"),
                    journey.get("affected_flight"),
                    journey.get("cause_code"),
                )
            ),
        }
        preconditions = {
            "authority": authority.to_record(),
            "entitlement_service_status": status,
            "authoritative": entitlement.get("authoritative"),
            "policy_version": entitlement.get("policy_version"),
            "per_passenger": breakdown,
            "independent_cross_check": facts.cross_check.to_record(),
            "reasoning": comp.get("reasoning", []),
        }
        plan.proposals.append(
            ActionProposal(
                action_id=self._next_id("compensation"),
                action_type=ActionType.COMPENSATION_PAYMENT,
                state=(
                    ActionState.PROPOSED if authority.allowed else ActionState.BLOCKED
                ),
                booking_ref=facts.booking_ref,
                passenger_ids=passenger_ids,
                amount=total,
                itinerary=None,
                policy_basis=[
                    "S5.1", "S5.3", "S5.4", "S9.1", "S9.3", "S10.2", authority.clause
                ],
                consent_basis=None,
                preconditions=preconditions,
                # Kept even when blocked: S12.5 wants a recommendation the referring
                # party can act on, and the exact request is the clearest form of one.
                # `executor` returns early on a BLOCKED proposal and never sends it.
                request_body=body,
                blocked_reason=None if authority.allowed else authority.reason,
                disruption_scope=facts.disruption_scope,
                fingerprint_params={
                    "passenger_ids": passenger_ids,
                    "amount_minor": total.amount_minor,
                    "kind": "statutory",
                },
            )
        )
        if not authority.allowed:
            # S12.5: what was asked for, what the record shows, the recommended
            # outcome, the blocking condition, and what the referring party decides.
            plan.handovers.append(
                HandoverItem(
                    queue=QUEUE_SUPERVISOR,
                    summary=(
                        "Statutory compensation of GBP {} is assessed and payable on "
                        "booking {} for {} ({}), but no authority to execute the "
                        "payment exists. Per passenger: {}.".format(
                            total.as_decimal_str,
                            facts.booking_ref,
                            journey.get("affected_flight"),
                            journey.get("cause_code"),
                            "; ".join(
                                "{} GBP {}".format(
                                    b["passenger_id"], b["total_payable_gbp"]
                                )
                                for b in breakdown
                            ),
                        )
                    ),
                    requested_decision=(
                        "Establish who may authorise payment of the assessed GBP {}, "
                        "and release it. S12.1 names no level for this, so this is a "
                        "request for a policy decision as well as for the "
                        "payment.".format(total.as_decimal_str)
                    ),
                    recommendation=(
                        "Pay GBP {} to {}. The figure is the entitlement calculation "
                        "service's own, which S10.2 makes authoritative, and an "
                        "independent re-derivation of the banding, the arrival-delay "
                        "test, the S5.4 reduction and the S9.4 segment-fare basis "
                        "agrees with it. NOTE: S12.1 lists no compensation row at "
                        "representative, supervisor OR manager level, so this referral "
                        "is not simply asking a supervisor to exercise an authority "
                        "they hold -- it is flagging that the authority table appears "
                        "to have no route for a payment the policy plainly requires. "
                        "Service reasoning: {}. Blocking condition: {}".format(
                            total.as_decimal_str,
                            ", ".join(passenger_ids),
                            " ".join(comp.get("reasoning", []))[:600],
                            authority.reason,
                        )
                    ),
                    blocking_clause=authority.clause,
                    passenger_note=(
                        "We have worked out what you are owed and we agree you are "
                        "owed it. Releasing the payment is not something this desk "
                        "can do on its own, so it has gone to someone who can. No "
                        "money has moved yet."
                    ),
                )
            )
            plan.add_uncertainty(
                "Compensation of GBP {} is assessed and payable, but S12.1 grants no "
                "authority to execute a compensation payment at any level.".format(
                    total.as_decimal_str
                ),
                "Nothing was paid. The assessed figure and its derivation are in the "
                "referral so a human can release it.",
            )

    def _plan_hotel(
        self, plan: Plan, facts: CaseFacts, request: dict[str, Any]
    ) -> None:
        care = facts.entitlement.get("duty_of_care") or {}
        if not care.get("triggered"):
            plan.answers.append(
                {
                    "request": request["detail"],
                    "answer_basis": "Duty of care is not triggered: {}".format(
                        care.get("basis")
                    ),
                }
            )
            return

        passenger_ids, _unmatched = self._resolve_request_passengers(plan, facts, request)
        if passenger_ids is None:
            return

        segment = facts.affected_segment or {}
        station = segment.get("origin")
        local_now = station_local_now(station, facts.now)
        if local_now is None:
            plan.handovers.append(HandoverItem(
                queue=QUEUE_GENERAL, summary="The station-local date for accommodation cannot be established.",
                requested_decision="Confirm the passenger's location and required accommodation night.",
                recommendation="No hotel voucher issued until the station and night are verified.",
                blocking_clause="S4.2",
            ))
            return
        night = local_now.date().isoformat()

        # Has this booking already been given a room for this night? Ask before
        # looking at the allocation, because our own earlier voucher is what consumed
        # it. Re-running case-09 found the allocation exhausted, and referred the case
        # to Accommodation Services saying no room could be sourced -- for a passenger
        # already holding voucher HTL-00012 for that exact station and night. The
        # remedy is discharged; unavailability is only a problem when it is standing
        # between the passenger and something they do not already have.
        existing = (
            self.inventory.existing_hotel_voucher(facts.booking_ref, station, night)
            if station
            else None
        )
        if existing is not None and set(passenger_ids).issubset(
            set(existing.get("passenger_ids") or [])
        ):
            plan.answers.append(
                {
                    "request": request["detail"],
                    "answer_basis": (
                        "Accommodation is already in place: voucher {} covers {} at "
                        "{} for the night of {} (S4.2). Nothing further is owed on "
                        "this request and no second room was issued (S12.2).".format(
                            existing.get("voucher_id", "on file"),
                            facts.booking_ref,
                            station,
                            night,
                        )
                    ),
                }
            )
            return

        if existing is not None:
            plan.handovers.append(HandoverItem(
                queue=QUEUE_SUPERVISOR,
                summary="An existing hotel voucher does not establish accommodation for every passenger in this request.",
                requested_decision="Check room capacity and which passengers the existing voucher covers; arrange any remaining care.",
                recommendation="Do not treat one passenger's voucher as satisfying another passenger's request or issue an overlapping voucher automatically.",
                blocking_clause="S4.2, S15.1, S15.3",
            ))
            return

        allocation = self.inventory.hotel_allocation(station, night) if station else None

        if allocation is None:
            plan.handovers.append(
                HandoverItem(
                    queue=QUEUE_SUPERVISOR,
                    summary=(
                        "Overnight accommodation is owed to {} at {} for the night of "
                        "{} after {} was cancelled, but Aerlink holds no contracted "
                        "allocation there for that night.".format(
                            self._names_for(
                                facts,
                                [p["passenger_id"] for p in facts.booking["passengers"]],
                            ),
                            station,
                            night,
                            segment.get("flight_no"),
                        )
                    ),
                    requested_decision="Source a room or authorise a passenger-arranged "
                    "booking under S4.4.",
                    recommendation="Care is owed under S4.1 regardless of cause (S4.3).",
                    blocking_clause="S4.5",
                    passenger_note=(
                        "We could not confirm a hotel allocation at that airport for that night, "
                        "so we could not issue you a room directly. A colleague is "
                        "picking it up."
                    ),
                )
            )
            return

        rate = gbp(allocation.get("rate_gbp"))
        rooms = allocation.get("rooms_remaining")
        authority = policy.authority_for_hotel(
            rate=rate, rooms_remaining=rooms, nights_requested=1,
            operating_level=self.config.authority_level,
        )
        body = {
            "booking_ref": facts.booking_ref,
            "station": station,
            "night": night,
            "passenger_ids": passenger_ids,
            "notes": "S4.2 duty of care following cancellation of {}.".format(
                segment.get("flight_no")
            ),
        }
        plan.proposals.append(
            ActionProposal(
                action_id=self._next_id("hotel"),
                action_type=ActionType.HOTEL_VOUCHER,
                state=ActionState.PROPOSED if authority.allowed else ActionState.BLOCKED,
                booking_ref=facts.booking_ref,
                passenger_ids=passenger_ids,
                amount=rate,
                itinerary={"station": station, "night": night, "provider": allocation.get("provider")},
                policy_basis=["S4.1", "S4.2", "S4.3", "S4.5", authority.clause],
                consent_basis="The contact asks for somewhere to stay: {!r}".format(
                    request["quote"][:120]
                ),
                preconditions={
                    "authority": authority.to_record(),
                    "rooms_remaining_at_planning": rooms,
                    "rate_gbp": allocation.get("rate_gbp"),
                    "cap_gbp": 180.0,
                    "care_basis": care.get("basis"),
                },
                request_body=body,
                blocked_reason=None if authority.allowed else authority.reason,
                disruption_scope=facts.disruption_scope,
                fingerprint_params={
                    "station": station,
                    "night": night,
                    "passenger_ids": passenger_ids,
                },
                refresh={"kind": "hotel", "station": station, "night": night},
            )
        )
        if not authority.allowed:
            plan.handovers.append(
                HandoverItem(
                    queue=QUEUE_SUPERVISOR,
                    summary=(
                        "Overnight accommodation is owed at {} for the night of {} on "
                        "booking {}, but it cannot be issued at desk level: {}".format(
                            station, night, facts.booking_ref, authority.reason
                        )
                    ),
                    requested_decision="Source a room or authorise a passenger-arranged "
                    "booking under S4.4.",
                    recommendation=(
                        "Care is owed under S4.1 regardless of cause (S4.3). The "
                        "passenger is at the airport now."
                    ),
                    blocking_clause=authority.clause,
                    passenger_note=(
                        "We have not been able to issue you a room from our own "
                        "allocation tonight. A colleague is picking this up now. "
                        "Accommodation is something you are owed here, not a favour, "
                        "so do not let this drop."
                    ),
                )
            )
            plan.add_uncertainty(
                "The station hotel allocation could not satisfy this passenger.",
                "No voucher issued; referred so Accommodation Services can act tonight.",
            )

    def _plan_expense_reimbursement(
        self, plan: Plan, facts: CaseFacts, request: dict[str, Any]
    ) -> None:
        """S4.2/S4.4 reimbursement against receipts. No endpoint exists for this."""
        care = facts.entitlement.get("duty_of_care") or {}
        owed = bool(care.get("triggered"))
        plan.handovers.append(
            HandoverItem(
                queue=QUEUE_SUPERVISOR,
                summary=(
                    "Passenger on booking {} is claiming reimbursement of costs they "
                    "incurred themselves: {}. Duty of care {} triggered ({}).".format(
                        facts.booking_ref,
                        request["detail"],
                        "IS" if owed else "is NOT",
                        care.get("basis"),
                    )
                ),
                requested_decision=(
                    "Reimburse against receipts within the S4.2 caps, or explain why not."
                ),
                recommendation=(
                    "S4.4 entitles the passenger to reimbursement against receipts, "
                    "capped at GBP 30 per passenger per 6 hours for meals and GBP 180 "
                    "per room per night for accommodation. The operations API exposes "
                    "no reimbursement endpoint, and S16 prohibits paying a duty-of-care "
                    "entitlement as goodwill, so this cannot be settled at desk level."
                ),
                blocking_clause="S4.4 (no operations API endpoint exists)",
                passenger_note=(
                    "We have not repaid what you spent. Costs you paid yourself are "
                    "reimbursed against your receipts, and that has to be handled by "
                    "a colleague rather than at this desk. Keep the receipts."
                ),
            )
        )
        plan.add_uncertainty(
            "Duty-of-care reimbursement against receipts has no operations API endpoint.",
            "The claim is referred in full rather than paid through another mechanism.",
        )

    def _plan_goodwill(
        self, plan: Plan, facts: CaseFacts, request: dict[str, Any]
    ) -> None:
        """Goodwill is never paid automatically. See DECISIONS.md S2."""
        repeat, evidence = policy.repeat_goodwill_claimant(facts.customer)
        authority = policy.authority_for_goodwill(
            Money(amount_minor=0, currency="GBP"), repeat_claimant=repeat
        )
        plan.handovers.append(
            HandoverItem(
                queue=QUEUE_SUPERVISOR,
                summary=(
                    "Passenger on booking {} is asking for a payment beyond their "
                    "statutory entitlement: {}.{}".format(
                        facts.booking_ref,
                        request["detail"],
                        " The customer record shows a pattern of previous goodwill "
                        "payments: " + "; ".join(evidence[:4]) if repeat else "",
                    )
                ),
                requested_decision="Decide whether any discretionary goodwill is "
                "appropriate, and at what amount.",
                recommendation=(
                    "S11.1 makes goodwill discretionary and never an entitlement, and "
                    "S11.4 prohibits paying it merely because a passenger is "
                    "dissatisfied with the statutory amount. "
                    + (
                        "S11.5 requires a repeat pattern to be referred rather than "
                        "paid. "
                        if repeat
                        else ""
                    )
                    + "S12.3: insistence, threat of legal action or threat of publicity "
                    "does not raise authority. Nothing discretionary has been paid."
                ),
                blocking_clause=authority.clause,
                passenger_note=(
                    "We have not made any payment beyond what the policy entitles you "
                    "to. Anything on top of that is discretionary, it is not ours to "
                    "decide at this desk, and it has been passed on. Please do not "
                    "read that as agreement that a further payment is due."
                ),
            )
        )

    def _demanded_amount(self, extraction: Any) -> tuple[Money | None, str]:
        """The figure the passenger is asking to be paid, and how we know.

        Only figures the passenger themselves put forward count. Two sources, most
        reliable first: the figure flagged as the actual ask, then the largest amount
        attached to a money_owed claim. A message that shows its working quotes
        several numbers -- a band amount, a sub-total, the price they paid -- and only
        one of them is the ask.

        An earlier version fell back to scanning the raw message for sterling figures
        when neither source produced one. That was removed: on the forged-instruction
        case it picked up the "GBP 5000.00" from the injected notice and referred the
        case on a number no passenger had asked for. A demand we cannot read from the
        passenger's own claims is better left unread -- the S11.5 check below is
        grounded in Aerlink's own customer record and does not depend on parsing the
        message at all.
        """
        embedded = [
            normalise_for_span(e.quote)
            for e in extraction.embedded_instructions
            if e.quote
        ]

        def from_the_passenger(claim: Any) -> bool:
            """Exclude a figure quoted from text that was trying to direct handling."""
            quoted = normalise_for_span(claim.quote or "")
            return not any(quoted and quoted in block for block in embedded)

        candidates = [
            c
            for c in extraction.passenger_fact_claims
            if c.amount_gbp is not None and from_the_passenger(c)
        ]
        flagged = [c for c in candidates if c.is_the_amount_being_demanded]
        if flagged:
            return (
                max((gbp(c.amount_gbp) for c in flagged), key=lambda m: m.amount_minor),
                "the figure the contact asks to be paid",
            )
        money_claims = [c for c in candidates if c.topic == "money_owed"]
        if money_claims:
            return (
                max(
                    (gbp(c.amount_gbp) for c in money_claims),
                    key=lambda m: m.amount_minor,
                ),
                "the largest figure the contact mentions as owed",
            )
        return None, ""

    def _check_discretionary_demand(
        self, plan: Plan, facts: CaseFacts, extraction: Any
    ) -> None:
        """Anything above the authoritative figure is discretionary (S11, S12.3).

        Two independent triggers, because relying on one would be brittle:

        * the contact asks for more than the entitlement; and
        * the customer record already shows a pattern of goodwill payments, which
          S11.5 requires to be referred rather than paid, whatever the amount.

        The second is grounded in the operational record rather than in how the
        message happened to be phrased, so it holds even when the first misreads a
        forcefully worded demand.
        """
        if any("S11" in h.blocking_clause for h in plan.handovers):
            return  # already referred through the goodwill path

        payable = gbp(facts.entitlement.get("total_payable_gbp"))
        repeat, evidence = policy.repeat_goodwill_claimant(facts.customer)
        demanded, basis = self._demanded_amount(extraction)

        over = (
            demanded is not None and demanded.amount_minor > payable.amount_minor
        )
        disputes_money = any(
            c.topic == "money_owed" for c in extraction.passenger_fact_claims
        ) or any(
            r["request_type"]
            in {
                RequestType.COMPENSATION.value,
                RequestType.GOODWILL_OR_EXTRA_PAYMENT.value,
            }
            for r in plan.requested
            if r["live"]
        )
        # Money the passenger spent themselves and wants back is a duty-of-care
        # entitlement under S4.4, already referred through that route. Running it
        # through the goodwill test as well would tell a supervisor the passenger was
        # "asking for more than they are entitled to", which is the opposite of true
        # and is close to the mischaracterisation S16 prohibits.
        claiming_expenses = any(
            r["request_type"] == RequestType.EXPENSE_REIMBURSEMENT.value
            for r in plan.requested
            if r["live"]
        )
        if claiming_expenses:
            over = False

        # Both triggers require the passenger to actually be disputing money. A
        # figure mentioned in passing -- the price they paid, a hotel bill -- is not a
        # demand, and referring on it would bury the desk in noise.
        if not disputes_money or not (over or repeat):
            return

        if over:
            excess = Money(
                amount_minor=demanded.amount_minor - payable.amount_minor,
                currency="GBP",
            )
            summary = (
                "Passenger on booking {} is asking for GBP {} ({}) where the "
                "authoritative entitlement is GBP {}. The difference of GBP {} would "
                "be a discretionary payment.".format(
                    facts.booking_ref,
                    demanded.as_decimal_str,
                    basis,
                    payable.as_decimal_str,
                    excess.as_decimal_str,
                )
            )
        else:
            summary = (
                "Passenger on booking {} is disputing what they are owed, and the "
                "entitlement assessed is GBP {}.".format(
                    facts.booking_ref, payable.as_decimal_str
                )
            )
        if repeat:
            summary += " The customer record shows a pattern of previous goodwill payments: " + "; ".join(
                evidence[:4]
            )

        plan.handovers.append(
            HandoverItem(
                queue=QUEUE_SUPERVISOR,
                summary=summary,
                requested_decision=(
                    "Decide whether any discretionary payment above the statutory "
                    "entitlement is appropriate."
                ),
                recommendation=(
                    "The statutory entitlement has been settled in full. S11.4 "
                    "prohibits paying goodwill merely because a passenger is "
                    "dissatisfied with the statutory amount, and S12.3 is explicit "
                    "that insistence, distress, threat of legal action or threat of "
                    "publicity does not raise a representative's authority. "
                    + (
                        "S11.5 requires a repeat pattern of claims to be referred "
                        "rather than paid. "
                        if repeat
                        else ""
                    )
                    + "Nothing discretionary has been paid."
                ),
                blocking_clause="S11.4, S11.5, S12.3" if repeat else "S11.4, S12.3",
                passenger_note=(
                    "What the policy entitles you to is set out above. Anything beyond "
                    "that is discretionary, it is not ours to decide at this desk, and "
                    "it has been passed on. Please do not read that as agreement that "
                    "a further payment is due."
                ),
            )
        )
        plan.rationale.append(
            "S12.3: insistence does not raise authority. Anything above the "
            "authoritative entitlement is referred rather than paid."
        )

    def _plan_assistance(
        self, plan: Plan, facts: CaseFacts, request: dict[str, Any]
    ) -> None:
        # S14.1 defines Special Assistance Equipment by reference to a passenger "who
        # has DECLARED a requirement for assistance" -- a fact on the record, not an
        # inference from wording. Without one, "please sort this out" is an ordinary
        # care matter and routing it to Special Assistance only buries that desk.
        declared = policy.assistance_passengers(facts.booking) or (
            facts.booking.get("special_requests") or ""
        ).strip()
        if not declared:
            plan.answers.append(
                {
                    "request": request["detail"],
                    "answer_basis": (
                        "No assistance requirement is declared on this booking, so "
                        "this is handled as part of the duty of care owed under S4 "
                        "rather than referred to Special Assistance (S14.1)."
                    ),
                }
            )
            return
        plan.handovers.append(
            HandoverItem(
                queue=QUEUE_SPECIAL_ASSISTANCE,
                summary="Assistance matter on booking {}: {}".format(
                    facts.booking_ref, request["detail"]
                ),
                requested_decision="Arrange or re-book the assistance service.",
                recommendation=(
                    "Booking record holds: {}. S14.4 requires assistance to be "
                    "re-booked onto any re-routed service before that re-routing is "
                    "confirmed.".format(
                        facts.booking.get("special_requests") or "no assistance note"
                    )
                ),
                blocking_clause="S14.4",
            )
        )

    def _plan_service_complaint(
        self, plan: Plan, facts: CaseFacts, request: dict[str, Any]
    ) -> None:
        plan.answers.append(
            {
                "request": request["detail"],
                "answer_basis": (
                    "Answered on its substance. S15.4: the tone of a contact never "
                    "forfeits an entitlement, and the entitlement is assessed either way."
                ),
            }
        )

    def _route_out_of_scope(
        self, plan: Plan, facts: CaseFacts, request: dict[str, Any], queue: str
    ) -> None:
        plan.handovers.append(
            HandoverItem(
                queue=queue,
                summary="Matter outside the Passenger Care Policy raised on booking "
                "{}: {}".format(facts.booking_ref, request["detail"]),
                requested_decision="Take this on in the responsible team.",
                recommendation=(
                    "S15.6 routes an out-of-scope matter rather than dismissing the "
                    "contact. The in-scope parts of this contact have been assessed "
                    "and answered."
                ),
                blocking_clause="S15.6",
                # Naming the matter matters. Given only "this part is not something we
                # deal with", the model filled the gap itself and told a passenger we
                # could not help with his coat -- when we had in fact just routed it.
                passenger_note=(
                    "On {}: we have passed it to the team that handles it, because it "
                    "is not something this desk can settle. They will pick it up from "
                    "here.".format(_as_topic(request["detail"]))
                ),
            )
        )

    # -- helpers -----------------------------------------------------------

    def _resolve_request_passengers(
        self, plan: Plan, facts: CaseFacts, request: dict[str, Any]
    ) -> tuple[list[str] | None, list[str]]:
        names = request["for_passenger_names"]
        passengers = facts.booking.get("passengers", [])
        if names:
            matched, unmatched = match_passengers(facts.booking, names)
            if not matched or unmatched:
                plan.add_uncertainty(
                    "None of the travellers named for this request ({}) match a "
                    "passenger on booking {}.".format(
                        ", ".join(names), facts.booking_ref
                    ),
                    "No action taken for this request. The passenger is asked who it "
                    "covers rather than the request being dropped (S15.1).",
                )
                # Without this the request simply vanishes from the reply, which is
                # precisely what S15.1 forbids: answering the easy ones and leaving
                # the rest unaddressed.
                plan.needs_passenger_input.append(
                    "We could not tell which traveller on this booking your request "
                    "about {} is for, so we have not changed anything. Tell us the "
                    "name as it appears on the booking and we will sort it "
                    "out.".format(_as_topic(request["detail"]))
                )
                return None, unmatched
            return matched, unmatched
        if len(passengers) == 1:
            return [passengers[0]["passenger_id"]], []
        plan.needs_passenger_input.append(
            "There are {} passengers on this booking and we could not tell which of "
            "them this is for, so we have not changed anything. Tell us who it covers "
            "and we will sort it out.".format(len(passengers))
        )
        plan.add_uncertainty(
            "The request does not identify which passengers on a multi-passenger "
            "booking it covers.",
            "No action taken; S15.1 forbids applying one passenger's election to "
            "everyone on the booking.",
        )
        return None, []

    def _rebooking_consent(self, extraction: Any, request: dict[str, Any]) -> str | None:
        """S6.4: a seat is not confirmed until the passenger has expressed a preference.

        Consent is read per request, not per contact, because one message routinely
        carries different elections from different passengers on the same booking
        (S15.1). Stating an acceptance criterion -- "anything that gets us in by
        Friday evening is fine" -- is expressing a preference; asking what the options
        are is not.
        """
        if not request.get("passenger_has_authorised_booking"):
            return None
        prefs = extraction.preferences
        criteria: list[str] = []
        if prefs.explicitly_asked_for_earliest_available:
            criteria.append("asked for the earliest available service")
        if prefs.travel_date_iso:
            criteria.append("named {} as the travel date".format(prefs.travel_date_iso))
        if prefs.arrive_by_local:
            criteria.append(
                "set a deadline of {} at the destination".format(prefs.arrive_by_local)
            )
        if not criteria:
            criteria.append("stated an acceptance criterion for this request")
        return (
            "These passengers have expressed a preference under S6.4: they {}. "
            "Quoted: {!r}".format(
                " and ".join(criteria), (request["quote"] or prefs.quote or "")[:160]
            )
        )

    def _target_date(
        self, extraction: Any, facts: CaseFacts, segment: dict[str, Any]
    ) -> date:
        explicit = parse_date(extraction.preferences.travel_date_iso)
        if explicit:
            return explicit
        deadline = parse_local_deadline(extraction.preferences.arrive_by_local)
        local_now = station_local_now(segment.get("origin"), facts.now)
        disruption_date = max(parse_date(segment.get("date")) or facts.now.date(),
                              local_now.date() if local_now else facts.now.date())
        if deadline and deadline.date() >= disruption_date:
            # Prefer the disruption date itself when the deadline still allows it;
            # the passenger wants to travel as soon as they can.
            return disruption_date
        return disruption_date

    def _refund_amount(
        self, facts: CaseFacts, passenger_ids: list[str]
    ) -> tuple[Money, str, bool]:
        passengers = facts.booking.get("passengers", [])
        total = gbp(facts.booking.get("total_paid_gbp"))
        if len(passengers) == 1 and len(passenger_ids) == 1:
            segments = facts.booking.get("segments", [])
            if len(segments) == 1:
                return (
                    total,
                    "Single-passenger, single-segment booking: the ticket price for "
                    "the journey not made is the total paid, GBP {}.".format(
                        total.as_decimal_str
                    ),
                    True,
                )
        share = Money(
            amount_minor=total.amount_minor // max(1, len(passengers)), currency="GBP"
        )
        return (
            share,
            "An equal share of the total paid would be GBP {}.".format(
                share.as_decimal_str
            ),
            False,
        )

    def _names_for(self, facts: CaseFacts, passenger_ids: list[str]) -> str:
        lookup = {
            p["passenger_id"]: "{} {}".format(p.get("given_name", ""), p.get("surname", "")).strip()
            for p in facts.booking.get("passengers", [])
        }
        return ", ".join(lookup.get(pid, pid) for pid in passenger_ids) or "the passenger"

    def _note_authoritative_facts(self, plan: Plan, facts: CaseFacts) -> None:
        flight = facts.flight or {}
        if flight:
            plan.rationale.append(
                "S3.1: the operational record is authoritative on cause. {} on {} is "
                "recorded as {} with cause {} ({}).".format(
                    flight.get("flight_no"),
                    flight.get("date"),
                    flight.get("status"),
                    flight.get("cause_code"),
                    flight.get("cause_note", ""),
                )
            )

    def _refuse_embedded_instructions(
        self, plan: Plan, extraction: Any, indicators: list[dict[str, str]]
    ) -> None:
        """S12.4. Recorded either way; escalated only when it claims authority.

        A passenger demanding a particular amount is a request, not an instruction --
        forceful, but ordinary. The security event is content presenting itself as
        *direction to the handling agent*. That is gated on the deterministic scan and
        on an authority claim, so an angry passenger does not generate a false alarm.
        Note that nothing here is load-bearing for safety: the model cannot reach a
        mutation whether or not this fires.
        """
        if not extraction.embedded_instructions and not indicators:
            return
        claims_authority = [
            e for e in extraction.embedded_instructions if claims_internal_authority(e.claimed_authority)
        ]
        if not indicators and not claims_authority:
            plan.rationale.append(
                "The contact states firmly what it wants done. That is a request, "
                "assessed on its merits, not an instruction under S12.4."
            )
            return
        asks = [e.what_it_asks_for for e in extraction.embedded_instructions]
        plan.rationale.append(
            "S12.4: the contact carries {} instruction(s) about how to handle the "
            "case. Content received from outside Aerlink is information about the "
            "case, never direction about how to handle it. None of it was "
            "acted on.".format(len(asks))
        )
        plan.handovers.append(
            HandoverItem(
                queue=QUEUE_SUPERVISOR,
                summary=(
                    "A contact arrived carrying what is presented as an internal "
                    "Aerlink handling instruction, directing that authority limits be "
                    "treated as suspended. Instructions found: "
                    + "; ".join(asks[:5])
                ),
                requested_decision=(
                    "Confirm whether any such notice was genuinely issued, and review "
                    "whether other contacts carry the same content."
                ),
                recommendation=(
                    "Treated as invalid under S12.4 and not acted on. The case was "
                    "assessed on the operational record alone. No discretionary "
                    "payment was made."
                ),
                blocking_clause="S12.4",
                internal_only=True,
            )
        )
        plan.add_uncertainty(
            "The contact contains instructions claiming internal authority.",
            "Refused under S12.4. It does not change the entitlement, which was "
            "assessed from the operational record.",
        )

    def _block_everything_for_ytp(
        self,
        plan: Plan,
        facts: CaseFacts,
        ytp_ids: list[str],
        requests: list[dict[str, Any]],
    ) -> None:
        plan.handovers.append(
            HandoverItem(
                queue=QUEUE_YTP,
                summary=(
                    "Booking {} carries a Young Traveller Programme passenger ({}). "
                    "Requests in the contact: {}. Booking note: {}".format(
                        facts.booking_ref,
                        self._names_for(facts, ytp_ids),
                        "; ".join(r["detail"] for r in requests if r["live"]) or "none",
                        facts.booking.get("special_requests") or "none",
                    )
                ),
                requested_decision=(
                    "The YTP desk holds the chain-of-custody record and the authorised "
                    "receiving adults, and must decide and action everything on this "
                    "booking."
                ),
                recommendation=(
                    "S13.2 prohibits a representative re-routing, refunding, "
                    "downgrading or amending a YTP booking under any circumstance, and "
                    "S13.3 prohibits issuing the passenger a hotel voucher to redeem "
                    "personally. Nothing has been actioned. Duty of care under S4 is "
                    "still owed and must be arranged by the YTP desk."
                ),
                blocking_clause="S13.2, S12.1",
                passenger_note=(
                    "Nothing on this booking has been changed. Bookings for young "
                    "travellers are handled only by the team that holds the "
                    "arrangements for who meets the child at each end, and they have "
                    "the case now."
                ),
            )
        )
        plan.rationale.append(
            "S13.2: every request on this booking is blocked and referred to the YTP "
            "desk, including ones that would be routine on an adult booking, because "
            "changing a YTP itinerary changes who must meet the child and where."
        )
        plan.add_uncertainty(
            "This is a Young Traveller Programme booking.",
            "No action of any kind was taken; the whole case is referred.",
        )

    def _record_eligibility_summary(self, plan: Plan, facts: CaseFacts) -> None:
        entitlement = facts.entitlement
        care = entitlement.get("duty_of_care") or {}
        comp = entitlement.get("compensation") or {}
        plan.eligible.append(
            {
                "remedy": "statutory_compensation",
                "status": comp.get("status"),
                "amount_gbp": comp.get("amount_gbp"),
                "clause": "S5",
                "source": "entitlement calculation service (authoritative, S10.2)",
                "reasoning": comp.get("reasoning", []),
            }
        )
        plan.eligible.append(
            {
                "remedy": "duty_of_care",
                "status": "TRIGGERED" if care.get("triggered") else "NOT_TRIGGERED",
                "clause": "S4",
                "basis": care.get("basis"),
                "entitlements": care.get("entitlements", []),
                "note": care.get("note"),
            }
        )
        for pax in entitlement.get("passengers", []):
            if gbp(pax.get("downgrade_reimbursement_gbp")).amount_minor:
                plan.eligible.append(
                    {
                        "remedy": "downgrade_reimbursement",
                        "passenger_id": pax.get("passenger_id"),
                        "amount_gbp": pax.get("downgrade_reimbursement_gbp"),
                        "clause": "S9.1, S9.3, S9.4",
                        "reasoning": pax.get("downgrade_reasoning", []),
                    }
                )


# ---------------------------------------------------------------------------
# Option selection: arithmetic, not judgement.
# ---------------------------------------------------------------------------


def select_option(
    rows: list[dict[str, Any]],
    *,
    cabin: str,
    seats_needed: int,
    arrive_by: datetime | None,
    depart_not_before: str | None,
    flight_date: date,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Filter on hard constraints, then take the earliest arrival (S6.1(a)).

    `now_utc` excludes options that have already departed. S6.1(a) entitles the
    passenger to re-routing "at the earliest opportunity", and a departure in the past
    is not an opportunity. Without this the desk offered Priya Raghunathan a flight
    that had left at 05:25 when she wrote at 07:12, and the Okonkwo family one that
    had left at 06:20 when they wrote at 09:31 -- while 40 and 42 usable later options
    sat in the same result set.

    Origin-local times are converted using the supplied station time zones and IANA
    daylight-saving rules. Unknown or ambiguous zones/times are rejected, never
    compared to a UTC clock as though both were local.

    The additional fare is deliberately **not** a filter. It is an authority question
    (S12.1), not a suitability one, and filtering on it here would hide from the
    supervisor the option the passenger should actually be offered. An earlier version
    filtered against an invented ceiling; that is gone.

    `free_option_count` reports how many of the passing options carry no additional
    fare -- that is, how many a representative could action without referral.

    Every rejection is counted so the record can show the candidate set was really
    inspected rather than skimmed.
    """
    rejections = {
        "not_own_carrier": 0,
        "wrong_cabin": 0,
        "not_enough_seats": 0,
        "arrives_after_deadline": 0,
        "departs_too_early": 0,
        "already_departed": 0,
        "unparseable_times": 0,
    }
    cutoff = parse_local_time(depart_not_before)
    passing: list[tuple[datetime, int, dict[str, Any]]] = []

    for row in rows:
        if row.get("operated_by") != "Aerlink":
            rejections["not_own_carrier"] += 1
            continue
        if row.get("cabin") != cabin:
            rejections["wrong_cabin"] += 1
            continue
        if int(row.get("seats_available") or 0) < seats_needed:
            rejections["not_enough_seats"] += 1
            continue
        arrives = arrival_datetime(row, flight_date)
        departs = departure_local_minutes(row)
        if arrives is None or departs is None:
            rejections["unparseable_times"] += 1
            continue
        if cutoff is not None and departs < cutoff.absolute_minutes:
            rejections["departs_too_early"] += 1
            continue
        if now_utc is not None:
            departure_utc = inventory_departure_utc(row, flight_date)
            if departure_utc is None:
                rejections["unparseable_times"] += 1
                continue
            if departure_utc <= now_utc:
                rejections["already_departed"] += 1
                continue
        if arrive_by is not None and arrives > arrive_by:
            rejections["arrives_after_deadline"] += 1
            continue
        passing.append((arrives, gbp(row.get("fare_gbp")).amount_minor, row))

    passing.sort(key=lambda item: (item[0], item[1]))
    free = [row for _a, fare, row in passing if fare == 0]
    return {
        "chosen": passing[0][2] if passing else None,
        "runners_up": [row for _a, _f, row in passing[1:4]],
        "passing": len(passing),
        "rejections": rejections,
        "free_option_count": len(free),
        "earliest_option_with_no_additional_fare": free[0] if free else None,
    }


_THIRD_PERSON_OPENERS = (
    "the passenger wants ",
    "the passenger asks for ",
    "the passenger asks ",
    "the passenger is asking for ",
    "the sender wants ",
    "the sender asks for ",
    "the sender asks ",
    "he wants ",
    "she wants ",
    "they want ",
)


def _as_topic(detail: str) -> str:
    """Turn a third-person description into something that reads in a letter.

    The extraction describes requests as "The passenger wants help recovering a
    camel-coloured overcoat", which is right for the record and wrong dropped into a
    reply addressed to that passenger.
    """
    text = (detail or "").strip().rstrip(".")
    lowered = text.casefold()
    for opener in _THIRD_PERSON_OPENERS:
        if lowered.startswith(opener):
            return text[len(opener):].strip() or text
    return text[:1].lower() + text[1:] if text else text


def _option_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        k: row.get(k)
        for k in (
            "option_id",
            "flight_no",
            "operated_by",
            "origin",
            "destination",
            "date",
            "departure_local",
            "arrival_local",
            "cabin",
            "seats_available",
            "fare_gbp",
            "arrival_delay_vs_original_minutes",
        )
    }
