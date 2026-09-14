"""Deterministic policy logic: APCP-2026-04 v11.3.

Two separate jobs, and keeping them apart matters:

**What is owed** comes from `GET /entitlements/calculate`. S10.1 says that service
applies Sections 3, 4, 5 and 9; S10.2 says its figure *is* the amount owed and takes
precedence over a figure derived by reading the policy text. We therefore do not
compute the payable amount ourselves. What we do compute is an *independent
cross-check* of the four things S10.2 says manual assessment usually gets wrong
(banding, the arrival-delay test, the re-routing reduction, the segment-fare basis).
If our arithmetic disagrees with the service, S10.4 is explicit: we must not
substitute our own figure -- we refer the case. `cross_check_entitlement` implements
exactly that, and nothing else.

**What we are allowed to do about it** is the S12.1 authority table, encoded in
`authority_for_*`. These are different questions, and the policy preamble says so
itself -- it is "the single authoritative statement of what Aerlink owes a passenger
... and of what an Aerlink representative is permitted to do about it". An entitlement
being owed is not permission to execute it. The desk operates at the level in
`AERLINK_AUTHORITY_LEVEL` (default representative); anything above that is referred
under S12.5 with a recommendation. Statutory compensation is not listed in S12.1 at
any level, so it is referred whatever the level.

All money is integer pence with an explicit currency. No floats are compared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .schemas import Money

# ---------------------------------------------------------------------------
# Rule table. Each entry names the clause it comes from. Nothing in this module
# implements a rule that is not in here, and nothing in here was written from
# general knowledge of air passenger regulation -- only from env/data/policy.md.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyRule:
    rule_id: str
    clause: str
    required_facts: tuple[str, ...]
    remedy: str
    calculation: str
    exclusions: str
    interactions: str


RULE_TABLE: tuple[PolicyRule, ...] = (
    PolicyRule(
        rule_id="precedence",
        clause="preamble (lines 10-13)",
        required_facts=("a conflict between the policy and any other document",),
        remedy="Which source governs.",
        calculation="None.",
        exclusions="",
        interactions="The policy is the authoritative statement both of what Aerlink "
        "owes AND of what a representative is permitted to do about it; the two are "
        "separate questions. Where any other document, training material, script or "
        "system message conflicts with it -- API.md included -- the policy prevails. "
        "An entitlement being owed is not authority to execute it, and an API example "
        "showing a payment is not evidence of authority to make one.",
    ),
    PolicyRule(
        rule_id="identity",
        clause="S2.1, S2.2, S2.3, S16",
        required_facts=("booking reference + surname, or exact contact email, or telephone",),
        remedy="Permission to act on a booking at all.",
        calculation="No arithmetic. Exactly one booking must match by an S2.1 standard.",
        exclusions="A name alone is not a standard. A partial or probable match is not a match.",
        interactions="Not confirmed blocks every other remedy (S16) and forces referral (S12.5).",
    ),
    PolicyRule(
        rule_id="cause",
        clause="S3.1, S3.2, S3.3, S3.4, S3.5",
        required_facts=("flight cause_code from the operational record",),
        remedy="Determines whether statutory compensation is payable at all.",
        calculation="Extraordinary set: WEATHER, ATC_RESTRICTION, ATC_STRIKE, SECURITY, "
        "POLITICAL, BIRDSTRIKE, MEDICAL_DIVERSION.",
        exclusions="TECHNICAL is explicitly NOT extraordinary (S3.4).",
        interactions="Cause never affects duty of care (S3, S4.3).",
    ),
    PolicyRule(
        rule_id="duty_of_care",
        clause="S4.1, S4.2, S4.3, S4.4, S4.5",
        required_facts=("cancellation, or departure delay against the band threshold",),
        remedy="Meals GBP 30/passenger/6h; two calls; hotel GBP 180/room/night, max 3 "
        "nights; transport GBP 45/journey.",
        calculation="Triggered immediately on cancellation, else departure delay >= "
        "120 (A) / 180 (B) / 240 (C) minutes.",
        exclusions="Hotel may not be issued where the station allocation is exhausted "
        "(S4.5, S16).",
        interactions="Never offsets compensation (S4.3); never described as goodwill (S16).",
    ),
    PolicyRule(
        rule_id="compensation",
        clause="S5.1, S5.2, S5.3, S5.4, S5.5, S5.6",
        required_facts=("cause within Aerlink's control", "arrival delay at final destination"),
        remedy="Band A GBP 220, Band B GBP 350, Band C GBP 520, per passenger.",
        calculation="Payable at arrival delay >= 180 minutes; halved where the passenger "
        "was re-routed and arrival delay is below 240 (A) / 300 (B) / 360 (C) minutes.",
        exclusions="Not payable for extraordinary causes; not measured on departure "
        "delay; not varied by ticket price; infants not occupying a seat attract none.",
        interactions="Separate from, and not reduced by, a refund (S5.6) or care (S4.3).",
    ),
    PolicyRule(
        rule_id="rerouting",
        clause="S6.1, S6.2, S6.3, S6.4",
        required_facts=("cancellation or delay expected over 5 hours", "passenger's own election"),
        remedy="Re-route at the earliest opportunity, or later at the passenger's "
        "convenience, or a full refund.",
        calculation="No arithmetic. Same cabin where a seat exists in it (S6.2).",
        exclusions="Must not be confirmed before the passenger expresses a preference, "
        "except where they explicitly asked for the earliest service (S6.4).",
        interactions="Own carrier, same cabin, no cost to the passenger needs no "
        "referral (S6.3, S12.1).",
    ),
    PolicyRule(
        rule_id="refund",
        clause="S7.1, S7.3",
        required_facts=("cancellation, or departure delay >= 5 hours with an election not to travel",),
        remedy="Refund of the ticket price for the parts of the journey not made.",
        calculation="Requires a per-passenger ticket price. Not derivable where the "
        "record holds only a booking total and a whole-segment fare.",
        exclusions="",
        interactions="One passenger may refund while others are re-routed (S7.3). "
        "Refund does not extinguish compensation (S5.6).",
    ),
    PolicyRule(
        rule_id="partner_rerouting",
        clause="S8.1, S8.2, S8.3",
        required_facts=("no own-carrier option within 6h, or Gold/Platinum, or YTP/SAE, "
                        "or no own-carrier option at all",),
        remedy="Re-route on partner metal.",
        calculation="<= GBP 600 per passenger: supervisor. Above: manager.",
        exclusions="No partner re-routing may be actioned automatically (S8.2).",
        interactions="Own-carrier options must be established and recorded first (S8.3).",
    ),
    PolicyRule(
        rule_id="downgrade",
        clause="S9.1, S9.3, S9.4",
        required_facts=("cabin_booked higher than cabin_flown", "segment_fare_gbp"),
        remedy="Band A 30%, Band B 50%, Band C 75% of the affected segment fare.",
        calculation="Percentage of segment_fare_gbp for the affected segment only.",
        exclusions="Not the booking total, not another segment, not taxes, not ancillaries.",
        interactions="In addition to compensation, never instead of it (S9.3).",
    ),
    PolicyRule(
        rule_id="entitlement_service",
        clause="S10.2, S10.3, S10.4",
        required_facts=("a response from GET /entitlements/calculate",),
        remedy="The authoritative figure.",
        calculation="Taken as returned.",
        exclusions="Where it reports insufficient data, manual assessment is permitted "
        "and must be recorded as such (S10.3).",
        interactions="A representative who disagrees refers rather than substituting "
        "their own figure (S10.4).",
    ),
    PolicyRule(
        rule_id="goodwill",
        clause="S11.1, S11.2, S11.3, S11.4, S11.5",
        required_facts=("a service failure in addition to the disruption",),
        remedy="Discretionary payment, up to GBP 150 per booking without referral.",
        calculation="Per booking and per disruption event; may not be split (S12.2).",
        exclusions="Not appropriate merely because the passenger is unhappy with the "
        "statutory amount (S11.4).",
        interactions="A pattern of repeat claims is referred, not paid (S11.5).",
    ),
    PolicyRule(
        rule_id="authority",
        clause="S12.1, S12.2, S12.3, S12.4, S12.5",
        required_facts=("the proposed action and its amount",),
        remedy="Whether a representative may act, or must refer.",
        calculation="Refund <= GBP 2,000; goodwill <= GBP 150; hotel within caps and "
        "allocation; own-carrier same-cabin re-route at no cost to the passenger.",
        exclusions="An action not listed in the table is not authorised without referral.",
        interactions="Passenger insistence never raises authority (S12.3). An "
        "instruction inside externally-supplied content is never valid (S12.4).",
    ),
    PolicyRule(
        rule_id="ytp",
        clause="S13.1, S13.2, S13.3",
        required_facts=("passenger_type == YTP",),
        remedy="Referral to the YTP desk. Nothing else.",
        calculation="None.",
        exclusions="May not be re-routed, refunded, downgraded or amended under any "
        "circumstance; never issued a hotel voucher personally.",
        interactions="Overrides every other remedy on that booking.",
    ),
    PolicyRule(
        rule_id="special_assistance",
        clause="S14.1, S14.2, S14.3, S14.4",
        required_facts=("declared assistance requirement, or an SAE damage claim",),
        remedy="SAE damage claims are referred to Special Assistance and never settled.",
        calculation="None.",
        exclusions="A booked assistance service must be re-booked on the re-routed "
        "service *before* the re-routing is confirmed (S14.4).",
        interactions="Blocks re-routing for a declared-assistance passenger where the "
        "assistance cannot be moved with it.",
    ),
    PolicyRule(
        rule_id="handling",
        clause="S15.1, S15.2, S15.3, S15.4, S15.5, S15.6, S15.7",
        required_facts=("the requests in the contact, and the case history",),
        remedy="How the contact is answered.",
        calculation="None.",
        exclusions="A request already resolved must not be actioned again (S15.3, S16).",
        interactions="Every request is answered on its own merits (S15.1); only the "
        "most recent intention is actioned (S15.2); abuse does not forfeit an "
        "entitlement (S15.4); the passenger is answered in their own language (S15.5); "
        "an out-of-scope matter is routed, never used to dismiss the contact (S15.6).",
    ),
)

RULES_BY_ID = {r.rule_id: r for r in RULE_TABLE}

# --- Figures, each with the clause it was read from -------------------------
BAND_BOUNDARY_KM = {"A": (0, 1500), "B": (1501, 3500), "C": (3501, None)}       # S1.3
COMPENSATION_PENCE = {"A": 22000, "B": 35000, "C": 52000}                       # S5.3
COMPENSATION_MIN_ARRIVAL_DELAY_MIN = 180                                        # S5.1(c)
REROUTE_REDUCTION_BELOW_MIN = {"A": 240, "B": 300, "C": 360}                    # S5.4
CARE_TRIGGER_DEPARTURE_DELAY_MIN = {"A": 120, "B": 180, "C": 240}               # S4.1
DOWNGRADE_PERCENT = {"A": Decimal("0.30"), "B": Decimal("0.50"), "C": Decimal("0.75")}  # S9.1
HOTEL_CAP_PENCE_PER_ROOM_NIGHT = 18000                                          # S4.2
HOTEL_MAX_NIGHTS_WITHOUT_REFERRAL = 3                                           # S4.2
MEALS_CAP_PENCE_PER_PASSENGER_PER_6H = 3000                                     # S4.2
TRANSPORT_CAP_PENCE_PER_JOURNEY = 4500                                          # S4.2
GOODWILL_REPRESENTATIVE_CEILING_PENCE = 15000                                   # S11.2, S12.1
GOODWILL_SUPERVISOR_CEILING_PENCE = 100000                                      # S11.2
REFUND_REPRESENTATIVE_CEILING_PENCE = 200000                                    # S12.1
PARTNER_SUPERVISOR_CEILING_PENCE = 60000                                        # S8.2
EXTRAORDINARY_CAUSES = frozenset(                                               # S3.2
    {
        "WEATHER",
        "ATC_RESTRICTION",
        "ATC_STRIKE",
        "SECURITY",
        "POLITICAL",
        "BIRDSTRIKE",
        "MEDICAL_DIVERSION",
    }
)
CABIN_RANK = {"ECONOMY": 0, "PREMIUM": 1, "BUSINESS": 2, "FIRST": 3}


# ---------------------------------------------------------------------------
# Money helpers
# ---------------------------------------------------------------------------


def gbp(amount: Any) -> Money:
    """Convert an API figure to integer pence without ever going through a float."""
    if amount is None:
        return Money(amount_minor=0, currency="GBP")
    value = Decimal(str(amount))
    pence = (value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return Money(amount_minor=int(pence), currency="GBP")


def pence_to_api_amount(money: Money) -> float:
    """The API takes a decimal number of pounds. Built from pence, never rounded."""
    return float(Decimal(money.amount_minor) / Decimal(100))


def band_for_distance_km(distance_km: int | None) -> str | None:
    """S1.3. Boundaries are absolute; 1500 is Band A and 1501 is Band B."""
    if distance_km is None:
        return None
    if distance_km <= 1500:
        return "A"
    if distance_km <= 3500:
        return "B"
    return "C"


# ---------------------------------------------------------------------------
# Cross-check of the authoritative service (S10.4)
# ---------------------------------------------------------------------------


@dataclass
class CrossCheck:
    agrees: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        return {
            "agrees_with_authoritative_service": self.agrees,
            "checks": self.checks,
            "disagreements": self.disagreements,
            "rule": (
                "S10.2 makes the entitlement service's figure the amount owed. This "
                "check never replaces that figure. Under S10.4, a disagreement is "
                "referred with both numbers stated, not resolved locally."
            ),
        }


def cross_check_entitlement(
    entitlement: dict[str, Any], booking: dict[str, Any]
) -> CrossCheck:
    """Re-derive the four figures S10.2 says manual assessment usually gets wrong.

    Banding (S1.3), the arrival-delay test (S5.2), the re-routing reduction (S5.4)
    and the segment-fare basis (S9.4). Used only to detect disagreement.
    """
    result = CrossCheck(agrees=True)
    journey = entitlement.get("journey") or {}
    if entitlement.get("status") != "ASSESSED" or not journey:
        result.checks.append(
            {
                "check": "not applicable",
                "detail": "Service status is {}; nothing to cross-check.".format(
                    entitlement.get("status")
                ),
            }
        )
        return result

    # S1.3 banding
    distance = journey.get("great_circle_distance_km")
    expected_band = band_for_distance_km(distance)
    service_band = journey.get("band")
    _record(
        result,
        "S1.3 band",
        expected_band,
        service_band,
        "distance {} km".format(distance),
    )

    # S3.2 / S3.3 cause classification
    cause = journey.get("cause_code")
    expected_extraordinary = cause in EXTRAORDINARY_CAUSES
    _record(
        result,
        "S3.2 cause is extraordinary",
        expected_extraordinary,
        bool(journey.get("cause_is_extraordinary")),
        "cause_code {}".format(cause),
    )

    # S5.1(c) / S5.4 compensation
    comp = entitlement.get("compensation") or {}
    arrival_delay = journey.get("arrival_delay_minutes_at_final_destination")
    rerouted = journey.get("rerouted_onto")
    informed_days = (booking.get("disruption") or {}).get("informed_days_before")
    if expected_extraordinary or (informed_days is not None and informed_days >= 14):
        expected_status = "NOT_PAYABLE"
        expected_pence = 0
    elif arrival_delay is None:
        expected_status = "INSUFFICIENT_DATA"
        expected_pence = 0
    elif arrival_delay < COMPENSATION_MIN_ARRIVAL_DELAY_MIN:
        expected_status = "NOT_PAYABLE"
        expected_pence = 0
    else:
        expected_status = "PAYABLE"
        base = COMPENSATION_PENCE.get(expected_band or "", 0)
        if rerouted and arrival_delay < REROUTE_REDUCTION_BELOW_MIN.get(
            expected_band or "", 0
        ):
            base = base // 2
        expected_pence = base
    _record(
        result,
        "S5 compensation status",
        expected_status,
        comp.get("status"),
        "arrival delay {} min, re-routed onto {}".format(arrival_delay, rerouted),
    )
    _record(
        result,
        "S5.3/S5.4 compensation amount (pence)",
        expected_pence,
        gbp(comp.get("amount_gbp")).amount_minor
        if comp.get("status") == "PAYABLE"
        else 0,
        "band {}".format(expected_band),
    )

    # S9.4 downgrade basis
    disruption = booking.get("disruption") or {}
    affected_id = disruption.get("affected_segment")
    segment = next(
        (s for s in booking.get("segments", []) if s.get("segment_id") == affected_id),
        None,
    )
    if segment is not None:
        segment_fare = gbp(segment.get("segment_fare_gbp"))
        for pax in entitlement.get("passengers", []):
            booking_pax = next(
                (
                    p
                    for p in booking.get("passengers", [])
                    if p.get("passenger_id") == pax.get("passenger_id")
                ),
                None,
            )
            if not booking_pax:
                continue
            booked = booking_pax.get("cabin_booked")
            flown = booking_pax.get("cabin_flown")
            downgraded = bool(
                booked
                and flown
                and CABIN_RANK.get(flown, 0) < CABIN_RANK.get(booked, 0)
            )
            expected = 0
            if downgraded and expected_band:
                expected = int(
                    (
                        Decimal(segment_fare.amount_minor)
                        * DOWNGRADE_PERCENT[expected_band]
                    ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                )
            _record(
                result,
                "S9.4 downgrade reimbursement for {} (pence)".format(
                    pax.get("passenger_id")
                ),
                expected,
                gbp(pax.get("downgrade_reimbursement_gbp")).amount_minor,
                "segment {} fare {} pence, NOT the booking total of {} pence".format(
                    affected_id,
                    segment_fare.amount_minor,
                    gbp(booking.get("total_paid_gbp")).amount_minor,
                ),
            )
    return result


def _record(
    result: CrossCheck, name: str, expected: Any, actual: Any, basis: str
) -> None:
    agrees = expected == actual
    result.checks.append(
        {
            "check": name,
            "independently_derived": expected,
            "service_returned": actual,
            "basis": basis,
            "agrees": agrees,
        }
    )
    if not agrees:
        result.agrees = False
        result.disagreements.append(
            "{}: policy text gives {!r}, the entitlement service returned {!r} "
            "({}).".format(name, expected, actual, basis)
        )


# ---------------------------------------------------------------------------
# S12.1 authority gate
# ---------------------------------------------------------------------------


@dataclass
class Authority:
    allowed: bool
    required_level: str
    clause: str
    reason: str

    def to_record(self) -> dict[str, Any]:
        return {
            "allowed_at_representative_level": self.allowed,
            "required_level": self.required_level,
            "clause": self.clause,
            "reason": self.reason,
        }


REPRESENTATIVE = "representative"
SUPERVISOR = "supervisor"
MANAGER = "manager"

# The S12.1 table defines three levels. Which one a given desk operates at is a
# deployment fact, not something this code may decide, so it is configuration with a
# conservative default. Note that no level in the table authorises a statutory
# compensation payment -- raising the level does not unlock everything, which is the
# point of it being a gate rather than a dial.
AUTHORITY_RANK = {REPRESENTATIVE: 0, SUPERVISOR: 1, MANAGER: 2}


def permits(operating_level: str, required_level: str) -> bool:
    """Does a desk operating at `operating_level` hold `required_level` authority?"""
    return AUTHORITY_RANK.get(operating_level, 0) >= AUTHORITY_RANK.get(required_level, 99)


def authority_for_rebooking(
    *,
    own_carrier: bool,
    cabin_matches_booked: bool,
    additional_fare_payable: Money,
    operating_level: str = REPRESENTATIVE,
) -> Authority:
    """S6.3 and the first four rows of the S12.1 table.

    `additional_fare_payable` is the option's own `fare_gbp`. API.md S3 defines that
    field as "the additional fare payable", and S12.1 draws the representative /
    supervisor line at "no fare difference" versus "cabin change or fare difference".
    A non-zero value is therefore a fare difference and needs a supervisor.

    An earlier version of this function took a `passenger_charged` argument and every
    caller passed zero, on the theory that the listed figure was a commercial sell
    fare that the passenger never sees. No supplied source says that. It was invented
    to work around the fact that the supplied inventory generator
    (`round(rng.uniform(0, 320), 2)`) effectively never emits 0.00, and it had the
    effect of booking five passengers onto fares of GBP 108.67 to GBP 314.46 at
    representative level. Whether the figure is read as cost to the passenger (S6.3,
    API.md S3) or cost to Aerlink (which is what the S8.2 partner rows gate on), it is
    a fare difference either way. The argument is gone.
    """
    if not own_carrier:
        level = (
            SUPERVISOR
            if additional_fare_payable.amount_minor <= PARTNER_SUPERVISOR_CEILING_PENCE
            else MANAGER
        )
        return Authority(
            allowed=False,   # S8.2: never actioned automatically, at any level
            required_level=level,
            clause="S8.2, S12.1",
            reason=(
                "Partner-carrier re-routing at GBP {} per passenger requires {} "
                "authorisation, and S8.2 forbids actioning any partner re-routing "
                "automatically.".format(additional_fare_payable.as_decimal_str, level)
            ),
        )
    if not cabin_matches_booked:
        return Authority(
            allowed=permits(operating_level, SUPERVISOR),
            required_level=SUPERVISOR,
            clause="S6.2, S12.1",
            reason="Re-routing into a different cabin requires supervisor authorisation.",
        )
    if additional_fare_payable.amount_minor > 0:
        return Authority(
            allowed=permits(operating_level, SUPERVISOR),
            required_level=SUPERVISOR,
            clause="S12.1, S6.3",
            reason=(
                "The option carries an additional fare payable of GBP {} (API.md S3). "
                "S12.1 places 're-route on own carrier, cabin change or fare "
                "difference' at supervisor level, and S6.3 grants a representative "
                "authority only where there is no additional cost to the "
                "passenger.".format(additional_fare_payable.as_decimal_str)
            ),
        )
    return Authority(
        allowed=True,
        required_level=REPRESENTATIVE,
        clause="S6.3, S12.1",
        reason=(
            "Own-carrier re-routing in the cabin originally booked with no additional "
            "fare payable may be actioned without referral."
        ),
    )


def authority_for_refund(
    amount: Money, *, operating_level: str = REPRESENTATIVE
) -> Authority:
    if amount.amount_minor <= REFUND_REPRESENTATIVE_CEILING_PENCE:
        return Authority(
            allowed=True,
            required_level=REPRESENTATIVE,
            clause="S12.1",
            reason="Refund of GBP {} is within the representative limit of GBP "
            "2,000.".format(amount.as_decimal_str),
        )
    return Authority(
        allowed=permits(operating_level, SUPERVISOR),
        required_level=SUPERVISOR,
        clause="S12.1",
        reason="Refund of GBP {} exceeds GBP 2,000.".format(amount.as_decimal_str),
    )


def authority_for_goodwill(
    amount: Money, *, repeat_claimant: bool, operating_level: str = REPRESENTATIVE
) -> Authority:
    if repeat_claimant:
        return Authority(
            allowed=False,
            required_level=SUPERVISOR,
            clause="S11.5",
            reason=(
                "The customer record shows a pattern of previous goodwill payments. "
                "S11.5 requires the case to be referred rather than paid."
            ),
        )
    if amount.amount_minor <= GOODWILL_REPRESENTATIVE_CEILING_PENCE:
        return Authority(
            allowed=True,
            required_level=REPRESENTATIVE,
            clause="S11.2, S12.1",
            reason="Goodwill of GBP {} is within the representative limit of GBP "
            "150 per booking.".format(amount.as_decimal_str),
        )
    level = (
        SUPERVISOR
        if amount.amount_minor <= GOODWILL_SUPERVISOR_CEILING_PENCE
        else MANAGER
    )
    return Authority(
        allowed=permits(operating_level, level),
        required_level=level,
        clause="S11.2, S12.1",
        reason="Goodwill of GBP {} requires {} authorisation.".format(
            amount.as_decimal_str, level
        ),
    )


def authority_for_hotel(
    *,
    rate: Money,
    rooms_remaining: int | None,
    nights_requested: int,
    operating_level: str = REPRESENTATIVE,
) -> Authority:
    if rooms_remaining is None or rate.amount_minor <= 0 or nights_requested < 1:
        return Authority(
            allowed=False,
            required_level=SUPERVISOR,
            clause="S4.2, S4.5",
            reason="The allocation, positive room rate, or requested stay length is not established.",
        )
    if rooms_remaining is not None and rooms_remaining <= 0:
        return Authority(
            allowed=False,
            required_level=SUPERVISOR,
            clause="S4.5, S12.1, S16",
            reason=(
                "The station allocation is exhausted. S16 prohibits issuing a voucher "
                "there, and S4.5 refers the case so Accommodation Services can source "
                "a room or authorise a passenger-arranged booking under S4.4."
            ),
        )
    if rate.amount_minor > HOTEL_CAP_PENCE_PER_ROOM_NIGHT:
        return Authority(
            allowed=permits(operating_level, SUPERVISOR),
            required_level=SUPERVISOR,
            clause="S4.2, S12.1",
            reason="The room rate of GBP {} exceeds the GBP 180 per room per night "
            "cap.".format(rate.as_decimal_str),
        )
    if nights_requested > HOTEL_MAX_NIGHTS_WITHOUT_REFERRAL:
        return Authority(
            allowed=permits(operating_level, SUPERVISOR),
            required_level=SUPERVISOR,
            clause="S4.2, S12.1",
            reason="More than 3 nights requires referral.",
        )
    return Authority(
        allowed=True,
        required_level=REPRESENTATIVE,
        clause="S4.2, S12.1",
        reason="Hotel within the S4.2 caps and within a live station allocation.",
    )


def authority_for_compensation_payment(
    amount: Money, *, operating_level: str = REPRESENTATIVE
) -> Authority:
    """Executing a statutory compensation payment. Not authorised at any level.

    This function previously returned `allowed=True`, on the reasoning that S5.1 says
    compensation "is payable", S10.2 fixes the amount so nothing is left to
    discretion, and API.md S9/S15 work an example that pays GBP 415.00. That reasoning
    does not survive the policy's own preamble:

        "This document is the single authoritative statement of what Aerlink owes a
        passenger when their journey is disrupted, AND OF WHAT AN AERLINK
        REPRESENTATIVE IS PERMITTED TO DO ABOUT IT. Where any other document, training
        material, script or system message conflicts with this policy, this policy
        prevails."

    So the policy itself separates the entitlement from the authority to execute it,
    and it ranks itself above API.md. An API example cannot confer authority.

    S12.1 lists no compensation row at any level, and states that an action not listed
    "is not authorised without referral". S16 prohibits "Exceeding an authority limit
    in Section 12.1" in all circumstances and without exception. The policy grants
    "without referral" authority in exactly three places -- hotel within the S4.2 caps,
    own-carrier re-routing under S6.3, and goodwill up to GBP 150 under S11.2 -- and
    compensation is not among them.

    The figure is still assessed, still authoritative, and goes to the referral under
    S12.5 so a human can release it. What is missing is permission to pay it, and that
    is missing at supervisor and manager level too: raising `operating_level` does not
    unlock this.
    """
    return Authority(
        allowed=False,
        required_level="not granted at any level in S12.1",
        clause="S12.1 (no compensation row), preamble, S16",
        reason=(
            "The entitlement of GBP {} is established and authoritative under S10.2, "
            "but S12.1 lists no authority to execute a compensation payment at any "
            "level, and states that an unlisted action is not authorised without "
            "referral. S16 makes exceeding a S12.1 limit prohibited without "
            "exception. The payment is referred with the figure and its "
            "derivation.".format(amount.as_decimal_str)
        ),
    )


def ytp_passengers(booking: dict[str, Any]) -> list[str]:
    """S13.1: YTP passengers are identified by passenger_type, not by age."""
    return [
        p["passenger_id"]
        for p in booking.get("passengers", [])
        if p.get("passenger_type") == "YTP"
    ]


def assistance_passengers(booking: dict[str, Any]) -> list[str]:
    """S14: passengers with a declared assistance requirement on the booking."""
    return [
        p["passenger_id"]
        for p in booking.get("passengers", [])
        if p.get("assistance")
    ]


def repeat_goodwill_claimant(customer: dict[str, Any]) -> tuple[bool, list[str]]:
    """S11.5: previous goodwill payments in the customer record."""
    evidence: list[str] = []
    for flag in customer.get("flags", []) or []:
        if "GOODWILL" in str(flag).upper():
            evidence.append("customer flag: {}".format(flag))
    for entry in customer.get("history", []) or []:
        for action in entry.get("actions", []) or []:
            if str(action).startswith("goodwill_paid"):
                evidence.append(
                    "{} ({}): {}".format(
                        entry.get("case_id"), entry.get("opened"), action
                    )
                )
    return (len(evidence) > 0, evidence)


def prior_benefits(customer: dict[str, Any], booking_ref: str) -> list[dict[str, Any]]:
    """S15.3: what the record already shows was done, so it is not done twice."""
    out: list[dict[str, Any]] = []
    for entry in customer.get("history", []) or []:
        out.append(
            {
                "case_id": entry.get("case_id"),
                "opened": entry.get("opened"),
                "status": entry.get("status"),
                "booking_ref": entry.get("booking_ref"),
                "same_booking": entry.get("booking_ref") == booking_ref,
                "actions": entry.get("actions", []),
                "summary": entry.get("summary"),
            }
        )
    return out
