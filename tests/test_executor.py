"""The mutation gate.

These are the tests that matter most: everything here is about money that moves, or
must not.
"""

from __future__ import annotations

import httpx
import pytest

from aerlink.executor import Executor, escalation_proposal
from aerlink.journal import canonical_fingerprint
from aerlink.planner import ActionProposal
from aerlink.policy import gbp
from aerlink.schemas import ActionState, ActionType, Money
from tests.conftest import availability_rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def payment_proposal(
    amount_gbp=415.0, booking="TST-000001", passengers=("P1",), action_id=None
):
    amount = gbp(amount_gbp)
    return ActionProposal(
        action_id=action_id or "ACT-{:.0f}-compensation".format(amount_gbp),
        action_type=ActionType.COMPENSATION_PAYMENT,
        state=ActionState.PROPOSED,
        booking_ref=booking,
        passenger_ids=list(passengers),
        amount=amount,
        itinerary=None,
        policy_basis=["S5.1", "S10.2"],
        consent_basis=None,
        preconditions={},
        request_body={
            "booking_ref": booking,
            "passenger_ids": list(passengers),
            "amount_gbp": float(amount.amount_minor) / 100,
            "reason": "test",
        },
        disruption_scope="ZZ100:2026-08-01",
        fingerprint_params={
            "passenger_ids": list(passengers),
            "amount_minor": amount.amount_minor,
            "kind": "statutory",
        },
    )


def rebooking_proposal(option_id="OPT-EARLY", seats_needed=1):
    return ActionProposal(
        action_id="ACT-01-rebooking",
        action_type=ActionType.REBOOKING,
        state=ActionState.PROPOSED,
        booking_ref="TST-000002",
        passenger_ids=["P1"],
        amount=None,
        itinerary={"option_id": option_id, "flight_no": "ZZ900"},
        policy_basis=["S6.3"],
        consent_basis="asked for the earliest service",
        preconditions={},
        request_body={
            "booking_ref": "TST-000002",
            "passenger_ids": ["P1"],
            "option_id": option_id,
            "flight_no": "ZZ900",
            "date": "2026-08-07",
            "cabin": "ECONOMY",
            "fare_gbp": 12.50,
        },
        disruption_scope="ZZ200:2026-08-06",
        fingerprint_params={
            "passenger_ids": ["P1"],
            "flight_no": "ZZ900",
            "date": "2026-08-07",
            "cabin": "ECONOMY",
        },
        refresh={
            "kind": "availability",
            "origin": "LGW",
            "destination": "FCO",
            "date": "2026-08-07",
            "option_id": option_id,
            "seats_needed": seats_needed,
            "cabin": "ECONOMY",
        },
    )


def hotel_proposal(night="2026-08-06"):
    return ActionProposal(
        action_id="ACT-01-hotel",
        action_type=ActionType.HOTEL_VOUCHER,
        state=ActionState.PROPOSED,
        booking_ref="TST-000002",
        passenger_ids=["P1"],
        amount=gbp(165.0),
        itinerary={"station": "LGW", "night": night},
        policy_basis=["S4.2"],
        consent_basis="asked for somewhere to stay",
        preconditions={},
        request_body={
            "booking_ref": "TST-000002",
            "station": "LGW",
            "night": night,
            "passenger_ids": ["P1"],
        },
        disruption_scope="ZZ200:2026-08-06",
        fingerprint_params={"station": "LGW", "night": night, "passenger_ids": ["P1"]},
        refresh={"kind": "hotel", "station": "LGW", "night": night},
    )


class Inventory:
    """Inventory the tests can move under the executor's feet."""

    def __init__(self, ops, rows=None, hotel=...):
        self.ops = ops
        self.rows = rows
        self.hotel = hotel

    def own_availability(self, origin, destination, date_iso, booking_ref):
        rows = self.rows if self.rows is not None else availability_rows(
            origin, destination, date_iso
        )
        return {"results": rows, "total_results": len(rows)}

    def partner_availability(self, *a, **k):
        return {"results": [], "total_results": 0}

    def hotel_allocation(self, iata, night_iso):
        if self.hotel is ...:
            return self.ops.hotel_allocation(iata, night_iso)
        return self.hotel

    def existing_hotel_voucher(self, booking_ref, station, night_iso):
        return None


def make_executor(ops, journal, config, *, dry_run=False, case_id="case-x"):
    return Executor(
        ops, journal, config, case_id=case_id, run_id="run-test", dry_run=dry_run
    )


# ---------------------------------------------------------------------------
# The happy path must actually happen
# ---------------------------------------------------------------------------


def test_an_authorised_payment_is_executed_and_verified(ops, journal, config, server):
    report = make_executor(ops, journal, config).execute(
        [payment_proposal()], Inventory(ops)
    )
    action = report.actions[0]
    assert action.state == ActionState.SUCCEEDED
    assert action.returned_ids["payment_id"].startswith("CMP-")
    assert "Confirmed by readback" in action.verification
    assert server.writes["payments"][0]["amount_gbp"] == 415.0
    assert server.writes["payments"][0]["booking_ref"] == "TST-000001"


def test_the_intent_is_committed_before_the_request_leaves(ops, journal, config):
    make_executor(ops, journal, config).execute([payment_proposal()], Inventory(ops))
    fingerprint = canonical_fingerprint(
        ops_base_url=config.ops_base_url,
        booking_ref="TST-000001",
        action_type="compensation_payment",
        disruption_scope="ZZ100:2026-08-01",
        params={"passenger_ids": ["P1"], "amount_minor": 41500, "kind": "statutory"},
    )
    prior = journal.find_prior_actions(fingerprint)
    assert len(prior) == 1 and prior[0].state == "succeeded"


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


def test_the_same_action_twice_in_one_run_pays_once(ops, journal, config, server):
    executor = make_executor(ops, journal, config)
    executor.execute([payment_proposal()], Inventory(ops))
    second = make_executor(ops, journal, config).execute(
        [payment_proposal()], Inventory(ops)
    )
    assert second.actions[0].state == ActionState.SKIPPED_DUPLICATE
    assert len(server.writes["payments"]) == 1


def test_a_blocked_proposal_is_never_sent_even_though_it_carries_a_request(
    ops, journal, config, server
):
    """A blocked proposal keeps the request it would have made, as evidence for the
    referral. That evidence must never become a request."""
    proposal = payment_proposal()
    proposal.state = ActionState.BLOCKED
    proposal.blocked_reason = "no authority under S12.1"
    assert proposal.request_body is not None
    report = make_executor(ops, journal, config).execute([proposal], Inventory(ops))
    assert report.actions[0].state == ActionState.BLOCKED
    assert server.write_count == 0


def test_a_rerun_under_a_different_case_name_still_pays_once(ops, journal, config, server):
    """The fingerprint is built from the booking and the event, not the filename."""
    make_executor(ops, journal, config, case_id="case-08").execute(
        [payment_proposal()], Inventory(ops)
    )
    again = make_executor(ops, journal, config, case_id="renamed-copy-of-08").execute(
        [payment_proposal()], Inventory(ops)
    )
    assert again.actions[0].state == ActionState.SKIPPED_DUPLICATE
    assert len(server.writes["payments"]) == 1


def test_a_recorded_reset_makes_a_stale_journal_row_safe_to_ignore(
    ops, journal, config, server
):
    """Only a reset WE recorded makes a local success safely stale."""
    make_executor(ops, journal, config).execute([payment_proposal()], Inventory(ops))
    assert len(server.writes["payments"]) == 1

    for collection in server.writes.values():      # the server is reset...
        collection.clear()
    journal.record_reset(                          # ...and we recorded that we did it
        ops_base_url=config.ops_base_url, run_id="run-reset"
    )
    # The benefit grant belongs to the pre-reset world too.
    journal._conn.execute("DELETE FROM benefit_grants")

    report = make_executor(ops, journal, config).execute(
        [payment_proposal()], Inventory(ops)
    )
    action = report.actions[0]
    assert action.state == ActionState.SUCCEEDED
    assert "known-stale" in action.preconditions_checked["journal_vs_server"]
    assert len(server.writes["payments"]) == 1


def test_a_missing_audit_entry_is_not_permission_to_repeat_a_success(
    ops, journal, config, server
):
    """The defect this replaces was a live double-payment path.

    The journal said the payment succeeded; the server's log did not show it; the
    executor concluded "the server must have been reset" and paid again. Nothing had
    been reset. A disagreement between our record and the server's is something a
    human settles.
    """
    make_executor(ops, journal, config).execute([payment_proposal()], Inventory(ops))
    assert len(server.writes["payments"]) == 1
    server.writes["payments"].clear()              # vanished; no reset recorded
    journal._conn.execute("DELETE FROM benefit_grants")

    report = make_executor(ops, journal, config).execute(
        [payment_proposal()], Inventory(ops)
    )
    action = report.actions[0]
    assert action.state == ActionState.BLOCKED
    assert "no operations-API reset has been recorded" in action.blocked_reason
    assert server.writes["payments"] == [], "it must not be paid a second time"


def test_an_unreadable_audit_log_is_not_an_empty_one(ops, journal, config, server):
    """An audit outage used to re-authorise every previously successful action."""
    make_executor(ops, journal, config).execute([payment_proposal()], Inventory(ops))
    journal._conn.execute("DELETE FROM benefit_grants")

    executor = make_executor(ops, journal, config)

    def explode() -> dict:
        raise RuntimeError("audit unavailable")

    executor.ops.audit = explode                   # type: ignore[assignment]
    report = executor.execute([payment_proposal()], Inventory(ops))
    action = report.actions[0]
    assert action.state == ActionState.BLOCKED
    assert "could not be read" in action.blocked_reason
    assert len(server.writes["payments"]) == 1


def test_a_crash_before_the_outcome_is_recorded_leaves_an_unsettled_row(
    ops, journal, config, server
):
    """Success, then the process dies before the outcome reaches the journal.

    The intent row stays `attempted`. On the next run that is unsettled, not absent,
    and it must not be repeated.
    """
    proposal = payment_proposal()
    fingerprint = canonical_fingerprint(
        ops_base_url=config.ops_base_url,
        booking_ref="TST-000001",
        action_type="compensation_payment",
        disruption_scope="ZZ100:2026-08-01",
        params=proposal.fingerprint_params,
    )
    journal_key = journal.record_intent(
        fingerprint=fingerprint,
        ops_base_url=config.ops_base_url,
        case_id="case-crashed",
        run_id="run-crashed",
        action_type="compensation_payment",
        booking_ref="TST-000001",
        path="/payments/compensation",
        request_body=dict(proposal.request_body or {}),
    )
    # The POST went through; the process died here, before update_outcome.
    server._commit("/payments/compensation", dict(proposal.request_body or {}))
    assert len(server.writes["payments"]) == 1
    assert journal.find_prior_actions(fingerprint)[0].state == "attempted"

    report = make_executor(ops, journal, config).execute(
        [payment_proposal()], Inventory(ops)
    )
    # The server holds a matching write, so this is recognised as already done.
    assert report.actions[0].state == ActionState.SKIPPED_DUPLICATE
    assert len(server.writes["payments"]) == 1


def test_a_failed_post_does_not_block_a_later_legitimate_attempt(
    ops, journal, config, server
):
    """A rejected write granted nothing, so it must not consume the benefit scope."""
    proposal = hotel_proposal(night="2026-08-07")     # allocation is 0 that night
    first = make_executor(ops, journal, config).execute(
        [proposal],
        Inventory(ops, hotel={"station": "LGW", "night": "2026-08-07",
                              "rooms_remaining": 5, "rate_gbp": 165.0}),
    )
    assert first.actions[0].state == ActionState.FAILED
    assert first.actions[0].response_status == 409
    # The failed attempt is in the request log but granted nothing.
    audit = ops.audit()
    assert audit["writes"]["hotel_vouchers"] == []

    second = make_executor(ops, journal, config).execute(
        [hotel_proposal(night="2026-08-06")], Inventory(ops)
    )
    assert second.actions[0].state == ActionState.SUCCEEDED


def test_a_second_payment_for_one_event_is_blocked_whatever_the_amount(
    ops, journal, config, server
):
    """S12.2: limits are per booking and per disruption event, and may not be
    circumvented "by splitting an amount into several smaller payments".

    This replaces a test that asserted the opposite -- that GBP 415 followed by GBP 220
    on the same booking and event should both succeed, because the request fingerprints
    differ. They do differ. That is exactly why an exact request fingerprint cannot be
    the only protection against a duplicate benefit.
    """
    make_executor(ops, journal, config).execute([payment_proposal(415.0)], Inventory(ops))
    report = make_executor(ops, journal, config).execute(
        [payment_proposal(220.0)], Inventory(ops)
    )
    assert report.actions[0].state == ActionState.BLOCKED
    assert "S12.2" in report.actions[0].blocked_reason
    assert len(server.writes["payments"]) == 1


def test_a_different_disruption_event_is_not_a_duplicate(ops, journal, config, server):
    """The benefit key is per event, so a genuinely separate disruption still pays."""
    first = payment_proposal(415.0)
    second = payment_proposal(220.0, action_id="ACT-other-event")
    second.disruption_scope = "ZZ999:2026-09-01"
    make_executor(ops, journal, config).execute([first], Inventory(ops))
    report = make_executor(ops, journal, config).execute([second], Inventory(ops))
    assert report.actions[0].state == ActionState.SUCCEEDED
    assert len(server.writes["payments"]) == 2


def test_overlapping_remedies_with_different_parameters_are_blocked(
    ops, journal, config, server
):
    """Two re-bookings for overlapping passengers on one event is one journey twice."""
    first = rebooking_proposal(option_id="OPT-EARLY")
    make_executor(ops, journal, config).execute([first], Inventory(ops))

    second = rebooking_proposal(option_id="OPT-LATE")
    second.action_id = "ACT-02-rebooking"
    second.request_body = dict(second.request_body, option_id="OPT-LATE", flight_no="ZZ903")
    second.fingerprint_params = dict(second.fingerprint_params, flight_no="ZZ903")
    report = make_executor(ops, journal, config).execute([second], Inventory(ops))
    assert report.actions[0].state == ActionState.BLOCKED
    assert len(server.writes["rebookings"]) == 1


# ---------------------------------------------------------------------------
# Preconditions refreshed immediately before the write
# ---------------------------------------------------------------------------


def test_seats_disappearing_between_planning_and_execution_blocks_the_booking(
    ops, journal, config, server
):
    """POST /rebooking does not validate seats, so this check is the only guard."""
    sold_out = [dict(r, seats_available=0) for r in availability_rows("LGW", "FCO", "2026-08-07")]
    report = make_executor(ops, journal, config).execute(
        [rebooking_proposal()], Inventory(ops, rows=sold_out)
    )
    action = report.actions[0]
    assert action.state == ActionState.BLOCKED
    assert "seats fell" in action.blocked_reason
    assert server.writes["rebookings"] == [], "no false confirmation may be issued"


def test_an_option_vanishing_from_inventory_blocks_the_booking(ops, journal, config, server):
    report = make_executor(ops, journal, config).execute(
        [rebooking_proposal(option_id="OPT-GONE")], Inventory(ops)
    )
    assert report.actions[0].state == ActionState.BLOCKED
    assert "no longer in inventory" in report.actions[0].blocked_reason
    assert server.writes["rebookings"] == []


def test_an_allocation_exhausted_between_planning_and_execution_blocks_the_voucher(
    ops, journal, config, server
):
    report = make_executor(ops, journal, config).execute(
        [hotel_proposal()],
        Inventory(ops, hotel={"station": "LGW", "night": "2026-08-06", "rooms_remaining": 0, "rate_gbp": 165.0}),
    )
    assert report.actions[0].state == ActionState.BLOCKED
    assert "exhausted" in report.actions[0].blocked_reason
    assert server.writes["hotel_vouchers"] == []


def test_the_last_room_is_issued_to_the_first_case_and_the_second_is_refused(
    ops, journal, config, server
):
    """Shared inventory: LGW has one room, and two cases want it."""
    first = make_executor(ops, journal, config, case_id="case-a").execute(
        [hotel_proposal()], Inventory(ops)
    )
    assert first.actions[0].state == ActionState.SUCCEEDED

    second = hotel_proposal()
    second.booking_ref = "TST-000003"
    second.request_body = dict(second.request_body, booking_ref="TST-000003")
    second.fingerprint_params = dict(second.fingerprint_params, passenger_ids=["P2"])
    report = make_executor(ops, journal, config, case_id="case-b").execute(
        [second], Inventory(ops)
    )
    assert report.actions[0].state == ActionState.BLOCKED
    assert len(server.writes["hotel_vouchers"]) == 1


# ---------------------------------------------------------------------------
# Unknown outcomes
# ---------------------------------------------------------------------------


def test_a_lost_response_after_a_commit_is_reconciled_not_retried(
    ops, journal, config, server
):
    """The server committed; we never saw the answer. Exactly-once still holds."""
    server.silently_commit_then_fail = True
    server.fail_next_write_with = httpx.ReadTimeout("timed out")

    report = make_executor(ops, journal, config).execute(
        [payment_proposal()], Inventory(ops)
    )
    action = report.actions[0]
    assert action.state == ActionState.SUCCEEDED
    assert "Reconciled, not retried" in action.verification
    assert len(server.writes["payments"]) == 1, "must not have been sent twice"


def test_a_lost_response_with_no_commit_is_marked_unknown_and_not_retried(
    ops, journal, config, server
):
    server.silently_commit_then_fail = False
    server.fail_next_write_with = httpx.ReadTimeout("timed out")

    report = make_executor(ops, journal, config).execute(
        [payment_proposal()], Inventory(ops)
    )
    action = report.actions[0]
    assert action.state == ActionState.UNKNOWN
    assert "NOT retried" in action.verification
    assert server.writes["payments"] == []
    assert report.unknown_outcomes


def test_an_unknown_write_blocks_later_money_but_not_the_handover(
    ops, journal, config, server
):
    server.fail_next_write_with = httpx.ReadTimeout("timed out")
    proposals = [
        payment_proposal(415.0, action_id="ACT-first"),
        payment_proposal(220.0, action_id="ACT-second"),
        escalation_proposal(
            action_id="ACT-esc",
            booking_ref="TST-000001",
            queue="SUPERVISOR",
            summary="s",
            requested_decision="d",
            recommendation="r",
            blocking_clause="S12.5",
            disruption_scope="ZZ100:2026-08-01",
            key="deadbeef",
        ),
    ]
    report = make_executor(ops, journal, config).execute(proposals, Inventory(ops))
    states = {a.action_id: a.state for a in report.actions}
    assert states["ACT-first"] == ActionState.UNKNOWN
    assert states["ACT-second"] == ActionState.BLOCKED
    assert states["ACT-esc"] == ActionState.SUCCEEDED, "a case must still reach a human"
    blocked = [
        a
        for a in report.actions
        if a.state == ActionState.BLOCKED and a.action_type != ActionType.ESCALATION
    ]
    # Blocked either by the benefit-scope rule (S12.2) or by the unknown outcome --
    # both are correct refusals, and no second payment reaches the server.
    assert blocked
    assert len(server.writes["payments"]) == 0


def test_a_prior_unknown_blocks_the_same_action_on_a_later_run(
    ops, journal, config, server
):
    """Repeating an action whose outcome we never established risks double payment."""
    server.fail_next_write_with = httpx.ReadTimeout("timed out")
    make_executor(ops, journal, config, case_id="case-1").execute(
        [payment_proposal()], Inventory(ops)
    )
    report = make_executor(ops, journal, config, case_id="case-2").execute(
        [payment_proposal()], Inventory(ops)
    )
    action = report.actions[0]
    assert action.state == ActionState.BLOCKED
    assert server.writes["payments"] == []


def test_partial_success_is_preserved(ops, journal, config, server):
    """An earlier success is not rolled back when a later action goes wrong."""
    executor = make_executor(ops, journal, config)
    first = executor.execute([hotel_proposal()], Inventory(ops))
    assert first.actions[0].state == ActionState.SUCCEEDED

    server.fail_next_write_with = httpx.ReadTimeout("timed out")
    second = make_executor(ops, journal, config).execute(
        [payment_proposal()], Inventory(ops)
    )
    assert second.actions[0].state == ActionState.UNKNOWN
    assert len(server.writes["hotel_vouchers"]) == 1, "the voucher stands"


def test_a_structured_4xx_is_a_clean_failure_not_an_unknown(ops, journal, config, server):
    proposal = hotel_proposal(night="2026-08-07")     # allocation is 0 for that night
    report = make_executor(ops, journal, config).execute([proposal], Inventory(ops, hotel={
        "station": "LGW", "night": "2026-08-07", "rooms_remaining": 5, "rate_gbp": 165.0
    }))
    action = report.actions[0]
    assert action.state == ActionState.FAILED
    assert action.response_status == 409
    assert "allocation_exhausted" in action.error


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_sends_no_write_at_all_including_the_handover(
    ops, journal, config, server
):
    proposals = [
        payment_proposal(),
        rebooking_proposal(),
        hotel_proposal(),
        escalation_proposal(
            action_id="ACT-esc",
            booking_ref="TST-000001",
            queue="SUPERVISOR",
            summary="s",
            requested_decision="d",
            recommendation="r",
            blocking_clause="S12.5",
            disruption_scope="ZZ100:2026-08-01",
            key="deadbeef",
        ),
    ]
    report = make_executor(ops, journal, config, dry_run=True).execute(
        proposals, Inventory(ops)
    )
    assert all(a.state == ActionState.WOULD_EXECUTE for a in report.actions)
    assert server.write_count == 0
    assert not any(method == "POST" for method, _path in server.request_log)
    assert journal.usage_totals()["calls"] == 0


def test_dry_run_never_labels_an_action_as_completed(ops, journal, config):
    report = make_executor(ops, journal, config, dry_run=True).execute(
        [payment_proposal()], Inventory(ops)
    )
    assert report.actions[0].state != ActionState.SUCCEEDED
    assert "no request was sent" in report.actions[0].verification


def test_a_blocked_proposal_is_never_sent(ops, journal, config, server):
    proposal = payment_proposal()
    proposal.state = ActionState.BLOCKED
    proposal.blocked_reason = "above authority"
    report = make_executor(ops, journal, config).execute([proposal], Inventory(ops))
    assert report.actions[0].state == ActionState.BLOCKED
    assert server.write_count == 0


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


def test_fingerprints_ignore_spelling_and_ordering_but_not_substance():
    def fp(**kw):
        base = dict(
            ops_base_url="http://127.0.0.1:8642/",
            booking_ref="tst-000001",
            action_type="compensation_payment",
            disruption_scope="ZZ100:2026-08-01",
            params={"passenger_ids": ["P2", "P1"], "amount_minor": 41500},
        )
        base.update(kw)
        return canonical_fingerprint(**base)

    assert fp() == fp(
        ops_base_url="http://127.0.0.1:8642",
        booking_ref="TST-000001",
        params={"passenger_ids": ["P1", "P2"], "amount_minor": 41500},
    )
    assert fp() != fp(params={"passenger_ids": ["P1", "P2"], "amount_minor": 22000})
    assert fp() != fp(disruption_scope="ZZ200:2026-08-06")
    assert fp() != fp(booking_ref="TST-000002")


def _key(case_sha, queue, config, scope="ZZ100:2026-08-01"):
    from aerlink.executor import referral_key

    return referral_key(
        ops_base_url=config.ops_base_url,
        case_sha=case_sha,
        queue=queue,
        disruption_scope=scope,
    )


def _referral(booking_ref, key, summary="A referral.", queue="SUPERVISOR",
              clause="S12.5", scope="ZZ100:2026-08-01"):
    return escalation_proposal(
        action_id="ACT-esc",
        booking_ref=booking_ref,
        queue=queue,
        summary=summary,
        requested_decision="Decide.",
        recommendation="Recommended.",
        blocking_clause=clause,
        disruption_scope=scope,
        key=key,
    )


def test_two_different_unidentified_contacts_each_reach_a_human(
    ops, journal, config, server
):
    """Two contacts from different people that both fail identity verification share a
    booking_ref (none), a SUPERVISOR queue and an S2.2 clause. Deduping on those alone
    swallowed the second, and nobody ever saw that case.

    They are kept apart by their message hashes, which differ because the messages do.
    """
    scope = "case:unidentified"
    a = _key("sha-of-jo-doe", "SUPERVISOR", config, scope)
    b = _key("sha-of-lindqvist", "SUPERVISOR", config, scope)
    assert a != b

    first = make_executor(ops, journal, config, case_id="case-a").execute(
        [_referral(None, a, "Contact from jo.doe could not be matched.",
                   clause="S2.2, S12.1", scope=scope)],
        Inventory(ops),
    )
    second = make_executor(ops, journal, config, case_id="case-b").execute(
        [_referral(None, b, "Contact from p.lindqvist could not be matched.",
                   clause="S2.2, S12.1", scope=scope)],
        Inventory(ops),
    )
    assert first.actions[0].state == ActionState.SUCCEEDED
    assert second.actions[0].state == ActionState.SUCCEEDED
    assert len(server.writes["escalations"]) == 2


def test_the_same_referral_reworded_is_still_recognised(ops, journal, config, server):
    """The defect this fixes.

    An unchanged case re-run produces the same referral in slightly different words,
    because the summary is assembled from model-authored text. Matching on that text
    re-raised 8 of 20 referrals on a repeat run. The message hash does not move.
    """
    key = _key("sha-of-the-message", "SUPERVISOR", config)

    make_executor(ops, journal, config, case_id="case-a").execute(
        [_referral("TST-000001", key, "The passenger asks to be re-routed to Rome.")],
        Inventory(ops),
    )
    again = make_executor(ops, journal, config, case_id="case-a-rerun").execute(
        [_referral("TST-000001", key,
                   "The passenger is asking for a re-routing to Rome.")],
        Inventory(ops),
    )
    assert again.actions[0].state == ActionState.SKIPPED_DUPLICATE
    assert len(server.writes["escalations"]) == 1, "the referral is reused, not repeated"


def test_the_model_slicing_one_complaint_differently_is_not_a_new_referral(
    ops, journal, config, server
):
    """Regression for the second design that failed.

    Keying on a multiset of blocking clauses meant that when the model split the same
    assistance complaint into three items where it had found two, the key moved and a
    duplicate SPECIAL_ASSISTANCE referral went out. How finely the model slices a
    complaint is not a fact about the case.
    """
    key = _key("sha-of-case-02", "SPECIAL_ASSISTANCE", config)
    two_items = "Wheelchair not re-booked. || Nobody offered them a drink."
    three_items = (
        "Wheelchair not re-booked. || They want the wheelchair moved with her. "
        "|| Nobody has told them where to go."
    )

    make_executor(ops, journal, config, case_id="case-02").execute(
        [_referral("TST-000002", key, two_items, queue="SPECIAL_ASSISTANCE",
                   clause="S14.4")],
        Inventory(ops),
    )
    again = make_executor(ops, journal, config, case_id="case-02-rerun").execute(
        [_referral("TST-000002", key, three_items, queue="SPECIAL_ASSISTANCE",
                   clause="S14.4")],
        Inventory(ops),
    )
    assert again.actions[0].state == ActionState.SKIPPED_DUPLICATE
    assert len(server.writes["escalations"]) == 1


def test_a_genuinely_new_message_still_raises_its_own_referral(
    ops, journal, config, server
):
    """Reuse must not become suppression. A new issue arrives as a new message."""
    first = _key("sha-of-the-first-message", "SUPERVISOR", config)
    make_executor(ops, journal, config, case_id="case-a").execute(
        [_referral("TST-000001", first, "One issue.")], Inventory(ops)
    )

    later = _key("sha-of-a-later-message", "SUPERVISOR", config)
    assert later != first
    report = make_executor(ops, journal, config, case_id="case-a-later").execute(
        [_referral("TST-000001", later, "The hotel allocation has run out too.")],
        Inventory(ops),
    )
    assert report.actions[0].state == ActionState.SUCCEEDED
    assert len(server.writes["escalations"]) == 2


def test_different_queues_from_one_message_are_separate_referrals(config):
    """One message can owe work to two teams; each needs its own referral."""
    assert _key("sha", "SUPERVISOR", config) != _key("sha", "SPECIAL_ASSISTANCE", config)


def test_the_referral_key_is_visible_to_whoever_reads_the_referral(config):
    """Carried inside `summary`, which API.md S18 documents -- not an invented field."""
    from aerlink.executor import REFERRAL_KEY_PREFIX

    proposal = _referral("TST-000001", "abc12345", "Something happened.")
    body = proposal.request_body
    assert set(body) <= {
        "summary", "requested_decision", "queue", "recommendation",
        "blocking_clause", "booking_ref",
    }, "only fields API.md S18 defines"
    assert body["summary"].startswith("Something happened.")
    assert REFERRAL_KEY_PREFIX + "abc12345]" in body["summary"]


# ---------------------------------------------------------------------------
# Single-writer control
# ---------------------------------------------------------------------------


def test_a_second_run_cannot_share_a_journal_namespace(config, tmp_path):
    """Two CLI runs could otherwise both pass the duplicate check before either
    recorded its intent, and both pay. This is a local single-machine control, not a
    distributed exactly-once guarantee."""
    from aerlink.journal import Journal, JournalLocked

    path = tmp_path / "j.sqlite3"
    first = Journal(path, namespace="ns")
    try:
        with pytest.raises(JournalLocked, match="Another run is using"):
            Journal(path, namespace="ns")
        # A different namespace is independent and may proceed.
        other = Journal(path, namespace="other-ns")
        other.close()
    finally:
        first.close()
    # Released on close, so the next run starts cleanly.
    again = Journal(path, namespace="ns")
    again.close()


def test_a_lock_left_by_a_dead_process_is_reclaimed(config, tmp_path):
    from aerlink.journal import Journal

    path = tmp_path / "j.sqlite3"
    stale = path.with_suffix(".ns.lock")
    stale.write_text("999999", encoding="ascii")     # a pid that is not running
    journal = Journal(path, namespace="ns")
    journal.close()
    assert not stale.exists()
