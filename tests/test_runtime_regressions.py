from aerlink.narrate import fallback_reply
from aerlink.pipeline import _determine_status
from aerlink.planner import HandoverItem
from aerlink.schemas import ActionState, ActionType, CaseStatus, PlannedAction


def test_fallback_does_not_quote_policy_or_missing_delay():
    reply = fallback_reply({"answers": [{"answer_basis": "Not payable. Recorded cause is WEATHER and the arrival delay is None minutes. S16 requires both figures to be stated. Reasoning: S3.2: weather. (S12.2)"}]})
    assert "WEATHER" in reply.body
    assert "S16" not in reply.body and "S3.2" not in reply.body
    assert "None" not in reply.body and "()" not in reply.body


def test_blocked_handover_is_failure():
    action = PlannedAction.model_construct(returned_ids=None, action_id="test", action_type=ActionType.ESCALATION, state=ActionState.BLOCKED)
    handover = HandoverItem(queue="GENERAL", summary="review", requested_decision="review", recommendation="review", blocking_clause="identity")
    assert _determine_status(actions=[action], handovers=[handover], needs_input=[], identity_confirmed=False, dry_run=False) == CaseStatus.FAILED


def test_duplicate_without_receipt_is_not_a_success():
    action = PlannedAction.model_construct(returned_ids=None, action_id="test", action_type=ActionType.ESCALATION, state=ActionState.SKIPPED_DUPLICATE)
    handover = HandoverItem(queue="GENERAL", summary="review", requested_decision="review", recommendation="review", blocking_clause="identity")
    assert _determine_status(actions=[action], handovers=[handover], needs_input=[], identity_confirmed=True, dry_run=False) == CaseStatus.FAILED


def test_missing_server_referral_is_failure_not_false_handover(tmp_path, ops, journal, config, server):
    from aerlink.pipeline import load_case, run_case
    message = tmp_path / "inbound.txt"
    message.write_text("From: Unknown <unknown@example.test>\nDate: 2026-08-06T20:00:00Z\nPlease help me.")
    import json
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"from": "Unknown <unknown@example.test>", "received_at": "2026-08-06T20:00:00Z"}))
    case = load_case(inbound=message, meta=meta, case_id="missing-referral")
    first = run_case(case, config=config, ops=ops, journal=journal, llm=None, run_id="first", dry_run=False)
    assert first.record.human_handover.api_handover_succeeded
    server.writes["escalations"].clear()
    second = run_case(case, config=config, ops=ops, journal=journal, llm=None, run_id="second", dry_run=False)
    assert second.fatal and second.record.status == CaseStatus.FAILED
    assert not second.record.human_handover.api_handover_succeeded
    assert "has not been completed" in second.record.passenger_response["body"]
    assert not server.writes["escalations"]
