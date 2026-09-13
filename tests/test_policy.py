"""Policy arithmetic and the S12.1 authority gate.

Expected figures here are derived from env/data/policy.md, not copied from any
supplied answer.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aerlink import policy
from aerlink.policy import Money, gbp
from tests.conftest import BOOKING_DOWNGRADE, ENTITLEMENTS


# --- S1.3 banding. The policy says boundaries are absolute and must not be rounded.


@pytest.mark.parametrize(
    "distance_km, expected_band",
    [
        (1, "A"),
        (1500, "A"),   # "A journey of 1,500 km is Band A"
        (1501, "B"),   # "A journey of 1,501 km is Band B"
        (3500, "B"),
        (3501, "C"),
        (None, None),
    ],
)
def test_band_boundaries_are_absolute(distance_km, expected_band):
    assert policy.band_for_distance_km(distance_km) == expected_band


# --- S5: the arrival-delay test, the amounts, and the re-routing reduction.


def _entitlement(band_distance, cause, arrival_delay, rerouted, *, status="ASSESSED"):
    """Build a service response shaped like GET /entitlements/calculate."""
    band = policy.band_for_distance_km(band_distance)
    extraordinary = cause in policy.EXTRAORDINARY_CAUSES
    if extraordinary:
        comp_status, amount = "NOT_PAYABLE", 0.0
    elif arrival_delay is None:
        comp_status, amount = "INSUFFICIENT_DATA", 0.0
    elif arrival_delay < 180:
        comp_status, amount = "NOT_PAYABLE", 0.0
    else:
        pence = policy.COMPENSATION_PENCE[band]
        if rerouted and arrival_delay < policy.REROUTE_REDUCTION_BELOW_MIN[band]:
            pence //= 2
        comp_status, amount = "PAYABLE", pence / 100
    return {
        "booking_ref": "TST-X",
        "status": status,
        "journey": {
            "great_circle_distance_km": band_distance,
            "band": band,
            "cause_code": cause,
            "cause_is_extraordinary": extraordinary,
            "arrival_delay_minutes_at_final_destination": arrival_delay,
            "rerouted_onto": rerouted,
        },
        "compensation": {"amount_gbp": amount, "status": comp_status, "reasoning": []},
        "passengers": [],
    }


@pytest.mark.parametrize(
    "arrival_delay, expected_status, expected_gbp",
    [
        (179, "NOT_PAYABLE", 0.0),      # S5.1(c): below the 3 hour threshold
        (180, "PAYABLE", 350.0),        # S5.1(c): "3 hours or more"
        (299, "PAYABLE", 350.0),        # not re-routed, so S5.4 cannot apply
        (300, "PAYABLE", 350.0),
    ],
)
def test_band_b_threshold_without_rerouting(arrival_delay, expected_status, expected_gbp):
    ent = _entitlement(1585, "TECHNICAL", arrival_delay, None)
    assert ent["compensation"]["status"] == expected_status
    assert ent["compensation"]["amount_gbp"] == expected_gbp
    assert policy.cross_check_entitlement(ent, {"segments": [], "passengers": []}).agrees


@pytest.mark.parametrize(
    "arrival_delay, expected_gbp",
    [
        (180, 175.0),   # re-routed and below the Band B threshold of 300 -> halved
        (299, 175.0),
        (300, 350.0),   # at the threshold, so no reduction
        (420, 350.0),
    ],
)
def test_band_b_rerouting_reduction_boundary(arrival_delay, expected_gbp):
    """S5.4 halves the amount only *below* the band's figure, not at it."""
    ent = _entitlement(1585, "TECHNICAL", arrival_delay, "ZZ104:2026-08-01")
    assert ent["compensation"]["amount_gbp"] == expected_gbp
    assert policy.cross_check_entitlement(ent, {"segments": [], "passengers": []}).agrees


def test_technical_is_never_extraordinary():
    """S3.4 spells this out because representatives get it wrong."""
    assert "TECHNICAL" not in policy.EXTRAORDINARY_CAUSES
    ent = _entitlement(1585, "TECHNICAL", 400, None)
    assert ent["compensation"]["status"] == "PAYABLE"


def test_weather_blocks_compensation_but_not_care():
    """S5.1(b) blocks compensation; S4.3 keeps care owed regardless of cause."""
    ent = _entitlement(1585, "WEATHER", 400, None)
    assert ent["compensation"]["status"] == "NOT_PAYABLE"
    assert ent["compensation"]["amount_gbp"] == 0.0
    # Care is assessed on cancellation/departure delay, never on cause.
    assert policy.CARE_TRIGGER_DEPARTURE_DELAY_MIN == {"A": 120, "B": 180, "C": 240}


def test_departure_delay_is_not_the_compensation_test():
    """S5.2: a flight that departs five hours late but arrives 2h40 late pays nothing."""
    ent = _entitlement(1585, "TECHNICAL", 160, "ZZ104:2026-08-01")
    assert ent["compensation"]["status"] == "NOT_PAYABLE"


# --- S9.4: the downgrade basis is the affected segment fare, nothing else.


def test_downgrade_is_computed_on_the_segment_fare_not_the_booking_total():
    ent = ENTITLEMENTS["TST-000001"]
    check = policy.cross_check_entitlement(ent, BOOKING_DOWNGRADE)
    assert check.agrees, check.disagreements

    pax = ent["passengers"][0]
    assert pax["downgrade_reimbursement_gbp"] == 240.0     # 50% of the GBP 480 segment
    assert pax["downgrade_reimbursement_gbp"] != 620.0     # NOT 50% of the GBP 1,240 total
    # S9.3: both remedies are paid, one is not instead of the other.
    assert pax["compensation_gbp"] == 175.0
    assert ent["total_payable_gbp"] == 415.0


def test_cross_check_detects_a_wrong_downgrade_basis():
    """S10.4: we must notice a disagreement, and never silently substitute our figure."""
    ent = dict(ENTITLEMENTS["TST-000001"])
    ent["passengers"] = [dict(ent["passengers"][0], downgrade_reimbursement_gbp=620.0)]
    check = policy.cross_check_entitlement(ent, BOOKING_DOWNGRADE)
    assert not check.agrees
    assert any("S9.4" in d for d in check.disagreements)


def test_cross_check_detects_a_wrong_band():
    ent = _entitlement(1585, "TECHNICAL", 400, None)
    ent["journey"]["band"] = "A"
    check = policy.cross_check_entitlement(ent, {"segments": [], "passengers": []})
    assert not check.agrees
    assert any("S1.3" in d for d in check.disagreements)


# --- Money handling.


def test_money_never_goes_through_a_float():
    assert gbp(415.0).amount_minor == 41500
    assert gbp("0.1").amount_minor == 10
    assert gbp(0.81).amount_minor == 81
    # 1.005 as a float is 1.00499...; going via Decimal(str(x)) keeps it exact.
    assert gbp(1.005).amount_minor == 101
    assert gbp(None).amount_minor == 0
    assert Money(amount_minor=5, currency="GBP").as_decimal_str == "0.05"


# --- S12.1 authority gate.


def test_own_carrier_same_cabin_with_no_additional_fare_is_representative_level():
    auth = policy.authority_for_rebooking(
        own_carrier=True,
        cabin_matches_booked=True,
        additional_fare_payable=gbp(0.00),
    )
    assert auth.allowed and auth.required_level == policy.REPRESENTATIVE


@pytest.mark.parametrize("fare", [0.01, 0.81, 12.50, 108.67, 314.46])
def test_any_additional_fare_needs_a_supervisor(fare):
    """S12.1 draws the line at "no fare difference" versus "cabin change or fare
    difference". API.md S3 defines `fare_gbp` as "the additional fare payable", so a
    non-zero value is a fare difference.

    This is the regression that matters most in this file. An earlier version passed a
    hard-coded `passenger_charged=0` to this gate on the theory that the listed figure
    was a commercial sell fare the passenger never sees. Nothing in the supplied
    sources says that, and it put five passengers onto fares of GBP 108.67 to GBP
    314.46 at representative level.
    """
    auth = policy.authority_for_rebooking(
        own_carrier=True,
        cabin_matches_booked=True,
        additional_fare_payable=gbp(fare),
    )
    assert not auth.allowed
    assert auth.required_level == policy.SUPERVISOR


def test_a_supervisor_may_action_a_fare_difference():
    """The level is a real gate with a real effect, not decoration."""
    auth = policy.authority_for_rebooking(
        own_carrier=True,
        cabin_matches_booked=True,
        additional_fare_payable=gbp(108.67),
        operating_level=policy.SUPERVISOR,
    )
    assert auth.allowed


def test_cabin_change_needs_a_supervisor():
    auth = policy.authority_for_rebooking(
        own_carrier=True,
        cabin_matches_booked=False,
        additional_fare_payable=gbp(0.00),
    )
    assert not auth.allowed and auth.required_level == policy.SUPERVISOR


@pytest.mark.parametrize(
    "fare, expected_level",
    [(410.0, policy.SUPERVISOR), (600.0, policy.SUPERVISOR), (600.01, policy.MANAGER)],
)
def test_partner_metal_is_never_automatic(fare, expected_level):
    """S8.2: at or below GBP 600 supervisor, above it manager, never automatic."""
    auth = policy.authority_for_rebooking(
        own_carrier=False,
        cabin_matches_booked=True,
        additional_fare_payable=gbp(fare),
    )
    assert not auth.allowed
    assert auth.required_level == expected_level
    # S8.2 forbids actioning partner metal automatically, so holding the level does
    # not make it allowed either.
    at_manager = policy.authority_for_rebooking(
        own_carrier=False,
        cabin_matches_booked=True,
        additional_fare_payable=gbp(fare),
        operating_level=policy.MANAGER,
    )
    assert not at_manager.allowed


# --- Compensation: entitlement and authority are different questions.


@pytest.mark.parametrize(
    "level", [policy.REPRESENTATIVE, policy.SUPERVISOR, policy.MANAGER]
)
def test_compensation_payment_is_not_authorised_at_any_level(level):
    """S12.1 lists no compensation row, and says an unlisted action "is not authorised
    without referral". S16 makes exceeding a S12.1 limit prohibited without exception.

    The policy preamble is what settles it: the document is the authoritative statement
    of what Aerlink owes "AND of what an Aerlink representative is permitted to do
    about it", and it prevails over any other document or system message. So S5.1's
    "is payable" and S10.2's fixed figure establish the debt, not permission to settle
    it -- and API.md's worked example cannot confer authority the policy withholds.
    """
    auth = policy.authority_for_compensation_payment(gbp(415.00), operating_level=level)
    assert not auth.allowed
    assert "S12.1" in auth.clause


def test_the_compensation_referral_still_carries_the_authoritative_figure():
    """Refusing to pay is not refusing to assess. S12.5 needs the recommendation."""
    auth = policy.authority_for_compensation_payment(gbp(415.00))
    assert "415.00" in auth.reason
    assert "referred" in auth.reason


@pytest.mark.parametrize(
    "amount, allowed", [(2000.00, True), (2000.01, False), (1.00, True)]
)
def test_refund_ceiling(amount, allowed):
    assert policy.authority_for_refund(gbp(amount)).allowed is allowed


@pytest.mark.parametrize(
    "amount, allowed, level",
    [
        (150.00, True, policy.REPRESENTATIVE),
        (150.01, False, policy.SUPERVISOR),
        (1000.00, False, policy.SUPERVISOR),
        (1000.01, False, policy.MANAGER),
    ],
)
def test_goodwill_ceilings(amount, allowed, level):
    auth = policy.authority_for_goodwill(gbp(amount), repeat_claimant=False)
    assert auth.allowed is allowed
    assert auth.required_level == level


def test_authority_levels_are_ordered():
    assert policy.permits(policy.MANAGER, policy.SUPERVISOR)
    assert policy.permits(policy.SUPERVISOR, policy.SUPERVISOR)
    assert not policy.permits(policy.REPRESENTATIVE, policy.SUPERVISOR)
    assert not policy.permits(policy.SUPERVISOR, policy.MANAGER)


def test_repeat_goodwill_claimant_is_referred_not_paid():
    """S11.5, even for an amount a representative could otherwise authorise."""
    auth = policy.authority_for_goodwill(gbp(10.00), repeat_claimant=True)
    assert not auth.allowed
    assert "S11.5" in auth.clause


def test_repeat_claimant_detection_reads_flags_and_history():
    customer = {
        "flags": ["REPEAT_GOODWILL_CLAIMANT"],
        "history": [
            {"case_id": "C1", "opened": "2026-01-01", "actions": ["goodwill_paid:150.00"]}
        ],
    }
    repeat, evidence = policy.repeat_goodwill_claimant(customer)
    assert repeat and len(evidence) == 2


@pytest.mark.parametrize(
    "rate, rooms, nights, allowed",
    [
        (165.0, 5, 1, True),
        (180.0, 5, 1, True),      # exactly at the S4.2 cap
        (180.01, 5, 1, False),
        (165.0, 0, 1, False),     # S4.5/S16: allocation exhausted
        (165.0, 5, 4, False),     # more than 3 nights needs referral
    ],
)
def test_hotel_gate(rate, rooms, nights, allowed):
    auth = policy.authority_for_hotel(
        rate=gbp(rate), rooms_remaining=rooms, nights_requested=nights
    )
    assert auth.allowed is allowed


def test_ytp_and_assistance_detection_read_the_record_not_the_age():
    """S13.1 identifies YTP by passenger_type; a child with an adult is not YTP."""
    from tests.conftest import BOOKING_GROUP, BOOKING_YTP

    assert policy.ytp_passengers(BOOKING_YTP) == ["P1"]
    assert policy.ytp_passengers(BOOKING_GROUP) == []
    assert policy.assistance_passengers(BOOKING_GROUP) == ["P2"]


def test_rule_table_covers_every_implemented_area():
    """The rule table is the documented map from behaviour to clause."""
    expected = {
        "precedence",
        "identity",
        "cause",
        "duty_of_care",
        "compensation",
        "rerouting",
        "refund",
        "partner_rerouting",
        "downgrade",
        "entitlement_service",
        "goodwill",
        "authority",
        "ytp",
        "special_assistance",
        "handling",
    }
    assert set(policy.RULES_BY_ID) == expected
    for rule in policy.RULE_TABLE:
        # Every rule names where it came from: a section number, or the preamble.
        assert rule.clause.startswith("S") or rule.clause.startswith("preamble"), (
            rule.rule_id
        )
