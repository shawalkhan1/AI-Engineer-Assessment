"""Adversarial policy regressions: conflicting identity and unsafe action scope."""

from copy import deepcopy

import pytest

from aerlink.identity import resolve_identity
from aerlink.planner import Planner, live_requests, match_passengers
from aerlink.policy import authority_for_hotel, cross_check_entitlement, gbp
from aerlink.schemas import ActionState, ActionType, RequestType
from tests.conftest import BOOKING_DOWNGRADE, BOOKING_GROUP, BOOKING_STRANDED, ENTITLEMENTS
from tests.test_identity import _extraction, _ref
from tests.test_planner import FakeInventory, make_extraction, make_facts, make_request, plan_for


def test_two_valid_booking_references_do_not_select_first(ops):
    result = resolve_identity(
        ops, extraction=_extraction(
            booking_refs=[_ref("TST-000005"), _ref("TST-000006")],
            surnames_claimed=["Doe"],
        ),
        inbound_text="I am Jo Doe. TST-000005 or TST-000006; I am unsure which booking.",
        meta_from="Jo Doe <jo@elsewhere.test>",
    )
    assert not result.confirmed
    assert {r["booking_ref"] for r in result.candidates} == {"TST-000005", "TST-000006"}


@pytest.mark.parametrize("reference,surname", [("BAD-999999", "Hopper"), ("TST-000001", "Hopper")])
def test_conflicting_reference_cannot_fall_back_to_contact_email(ops, reference, surname):
    result = resolve_identity(
        ops, extraction=_extraction(booking_refs=[_ref(reference)], surnames_claimed=[surname]),
        inbound_text=f"My reference is {reference}. Grace {surname}.",
        meta_from="Grace Hopper <grace.hopper@example.test>",
    )
    assert not result.confirmed
    assert result.booking_ref is None


def test_same_surname_relative_is_not_named_passenger(ops):
    result = resolve_identity(
        ops, extraction=_extraction(booking_refs=[_ref("TST-000001")], surnames_claimed=["Lovelace"]),
        inbound_text="Refund my sister Ada Lovelace's booking TST-000001.",
        meta_from="Byron Lovelace <byron@elsewhere.test>",
    )
    assert not result.confirmed
    assert result.third_party_blocked


def test_email_confirmation_requires_character_exact_match(ops):
    result = resolve_identity(
        ops, extraction=_extraction(), inbound_text="Please help with my booking.",
        meta_from="Grace Hopper <Grace.Hopper@example.test>",
    )
    assert not result.confirmed


def test_invented_reference_and_surname_cannot_confirm(ops):
    result = resolve_identity(
        ops, extraction=_extraction(booking_refs=[_ref("TST-000001")], surnames_claimed=["Lovelace"]),
        inbound_text="Please refund my booking.", meta_from="Outsider <other@elsewhere.test>",
    )
    assert not result.confirmed
    assert not any(r.get("source") == "reference supplied by the contact" for r in result.candidates)


def test_passenger_substring_and_shared_surname_are_not_matches():
    booking = deepcopy(BOOKING_GROUP)
    booking["passengers"][0].update(given_name="Ann", surname="Smith")
    booking["passengers"][1].update(given_name="Anna", surname="Smith")
    assert match_passengers(booking, ["An"]) == ([], ["An"])
    assert match_passengers(booking, ["Smith"]) == ([], ["Smith"])
    assert match_passengers(booking, ["Ann Smith"]) == (["P1"], [])


@pytest.mark.parametrize("kind", [RequestType.REFUND, RequestType.HOTEL_ACCOMMODATION])
def test_unverified_request_cannot_move_money(config, kind):
    extraction = make_extraction([make_request(kind)])
    plan = Planner(config, FakeInventory(hotel={"rate_gbp": 100, "rooms_remaining": 1})).plan(
        make_facts(BOOKING_STRANDED), extraction,
        requests=live_requests(extraction, set()), injection_indicators=[],
    )
    assert not any(p.state == ActionState.PROPOSED for p in plan.proposals)
    assert plan.needs_passenger_input


def test_short_delay_does_not_authorise_rebooking(config):
    facts = make_facts(deepcopy(BOOKING_STRANDED))
    facts.flight = {**facts.flight, "status": "DELAYED", "departure_delay_minutes": 60}
    extraction = make_extraction([make_request(RequestType.REBOOKING, authorised=True)], earliest=True)
    plan = Planner(config, FakeInventory()).plan(
        facts, extraction, requests=live_requests(extraction, {0}), injection_indicators=[],
    )
    assert not any(p.action_type == ActionType.REBOOKING for p in plan.proposals)
    assert any(h.blocking_clause == "S6.1" for h in plan.handovers)


def test_hotel_request_applies_only_to_named_passenger(config):
    plan, _ = plan_for(
        BOOKING_GROUP,
        [make_request(RequestType.HOTEL_ACCOMMODATION, names=["Joan Clarke"])],
        config, inventory=FakeInventory(hotel={"rate_gbp": 100, "rooms_remaining": 2}),
    )
    hotels = [p for p in plan.proposals if p.action_type == ActionType.HOTEL_VOUCHER]
    assert len(hotels) == 1
    assert hotels[0].passenger_ids == ["P2"]


def test_refund_after_travel_is_referred(config):
    plan, _ = plan_for(BOOKING_DOWNGRADE, [make_request(RequestType.REFUND)], config)
    assert not any(p.action_type == ActionType.REFUND for p in plan.proposals)
    assert any("unused" in h.requested_decision for h in plan.handovers)


def test_unconfirmed_hotel_allocation_is_not_authorised():
    assert not authority_for_hotel(rate=gbp(100), rooms_remaining=None, nights_requested=1).allowed


def test_advance_cancellation_notice_is_included_in_cross_check():
    booking = deepcopy(BOOKING_DOWNGRADE)
    booking["disruption"]["informed_days_before"] = 14
    entitlement = deepcopy(ENTITLEMENTS[booking["booking_ref"]])
    entitlement["compensation"].update(status="NOT_PAYABLE", amount_gbp=0)
    result = cross_check_entitlement(entitlement, booking)
    assert result.agrees, result.disagreements
