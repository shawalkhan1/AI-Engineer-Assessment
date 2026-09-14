from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from aerlink.cli import main
from aerlink.journal import Journal, JournalLocked
from aerlink.llm import BudgetExhausted, LLMClient
from aerlink.report import validate_case_id
from aerlink.schemas import PassengerReply
from aerlink.timeutil import (
    UnknownCaseTime, case_now, inventory_departure_utc,
    parse_local_deadline, parse_local_time, station_local_now,
)
from tests.test_budget import REPLY, StubClient, StubResponse, Usage


def test_reservation_is_durable_before_network_and_survives_process_interruption(config, journal):
    class CrashResponses:
        def parse(self, **kwargs):
            with sqlite3.connect(journal.path) as observer:
                row = observer.execute('SELECT resolved, reserved_usd FROM model_usage').fetchone()
            assert row[0] == 0 and Decimal(row[1]) > 0
            raise KeyboardInterrupt('simulated interruption after dispatch')

    client = StubClient([])
    client.responses = CrashResponses()
    llm = LLMClient(config, journal, 'crash', client=client)
    with pytest.raises(KeyboardInterrupt):
        llm.parse(purpose='test', instructions='x', user_content='y',
                  text_format=PassengerReply, max_output_tokens=100)
    assert journal.usage_totals()['unresolved_calls'] == 1


def test_new_namespace_does_not_reset_project_spend(config, tmp_path):
    path = tmp_path / 'shared.sqlite3'
    first = Journal(path, 'first')
    first.record_usage(run_id='r', case_id='c', purpose='test', model='m', request_id=None,
                       input_tokens=None, cached_tokens=None, output_tokens=None,
                       reasoning_tokens=None, cost_usd=None, reserved_usd=Decimal('4.99'), resolved=False)
    first.close()
    other = Journal(path, 'second')
    try:
        llm = LLMClient(config, other, 'r2', client=StubClient([]))
        with pytest.raises(BudgetExhausted):
            llm._admit(Decimal('.02'), 'test')
    finally:
        other.close()


def test_second_process_cannot_kill_or_bypass_lock(tmp_path):
    path = tmp_path / 'shared.sqlite3'
    owner = Journal(path)
    try:
        code = 'from aerlink.journal import Journal, JournalLocked; import sys\ntry:\n Journal(sys.argv[1], "other")\nexcept JournalLocked:\n sys.exit(7)'
        result = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True, timeout=15)
        assert result.returncode == 7
        assert owner.usage_totals()['calls'] == 0
    finally:
        owner.close()


def test_schema_size_is_included_in_model_admission(config, journal, monkeypatch):
    llm = LLMClient(config, journal, 'r', client=StubClient([]))
    monkeypatch.setattr(llm, 'estimate_tokens', lambda text: 24001 if 'properties' in text else 1)
    with pytest.raises(BudgetExhausted):
        llm.parse(purpose='test', instructions='x', user_content='y',
                  text_format=PassengerReply, max_output_tokens=100)
    assert journal.usage_totals()['calls'] == 0


@pytest.mark.parametrize('value', ['../outside', r'..\outside', '/absolute', 'CON', 'aux.txt',
                                  'batch-summary', 'ops-audit', 'trailing.', 'x:y'])
def test_untrusted_case_ids_cannot_escape_or_overwrite_special_records(value):
    with pytest.raises(ValueError):
        validate_case_id(value)


def test_dry_run_reset_rejected_before_any_configuration_or_write(monkeypatch):
    monkeypatch.setattr('aerlink.cli.load_config', lambda **kw: pytest.fail('configuration must not run'))
    assert main(['--inbound', 'x', '--output', 'unused', '--dry-run', '--reset-ops']) == 2


@pytest.mark.parametrize('amount', ['NaN', 'Infinity', '-1', '0'])
def test_invalid_budget_rejected_before_network(amount, monkeypatch):
    monkeypatch.setattr('aerlink.cli.load_config', lambda **kw: pytest.fail('configuration must not run'))
    assert main(['--inbound', 'x', '--output', 'unused', '--run-ceiling-usd', amount]) == 2


def test_email_offset_is_applied():
    stamp, source = case_now(None, 'Date: Thu, 6 Aug 2026 00:30:00 +0500\n\nHelp')
    assert stamp == datetime(2026, 8, 5, 19, 30, tzinfo=timezone.utc)
    assert 'offset applied' in source


@pytest.mark.parametrize('text', ['Help\n\nDate: Thu, 6 Aug 2026 00:30:00 +0500',
                                  'Date: 31 Feb 2026 00:00 +0000\n\nHelp',
                                  'Date: Thu, 6 Aug 2026 00:30:00\n\nHelp'])
def test_body_dates_invalid_dates_and_missing_offsets_do_not_supply_case_time(text):
    with pytest.raises(UnknownCaseTime):
        case_now(None, text)


def test_station_calendar_date_and_dst_are_used():
    stamp = datetime(2026, 8, 6, 23, 30, tzinfo=timezone.utc)
    assert station_local_now('LGW', stamp).date() == date(2026, 8, 7)
    row = {'origin': 'LHR', 'date': '2026-08-06', 'departure_local': '09:00'}
    assert inventory_departure_utc(row, date(2026, 8, 6)).hour == 8
    row.update(date='2026-12-06')
    assert inventory_departure_utc(row, date(2026, 12, 6)).hour == 9


def test_ambiguous_dst_inventory_time_is_not_guessed():
    assert inventory_departure_utc({'origin': 'LHR', 'date': '2026-10-25',
                                    'departure_local': '01:30'}, date(2026, 10, 25)) is None


def test_invalid_deadlines_cannot_silently_become_end_of_day():
    assert parse_local_deadline('2026-08-06T09:30') == datetime(2026, 8, 6, 9, 30)
    assert parse_local_deadline('2026-08-06T99:99') is None
    assert parse_local_time('24:90') is None


def test_cache_write_tokens_are_priced_separately(config, journal):
    from aerlink.config import VERIFIED_MODEL_PRICES
    config.price = VERIFIED_MODEL_PRICES['gpt-5.6-luna']
    llm = LLMClient(config, journal, 'r', client=StubClient([]))
    usage = Usage(10000, 2000, cached=4000)
    usage.input_tokens_details.cache_write_tokens = 2000
    cost, counts = llm.cost_from_usage(usage)
    assert cost == Decimal('.003780')
    assert counts['cache_write_tokens'] == 2000
    assert llm.reserve_for(1000, 500) == Decimal('.000900')


def test_ops_deadline_prevents_dispatch_but_keeps_recovery_available(ops, server):
    import time
    ops.deadline = time.monotonic() - 1
    result = ops.post_refund({'booking_ref': 'TST-000002', 'passenger_ids': ['P1'], 'amount_gbp': 1})
    assert result.state == 'failed' and result.error.startswith('Not sent:')
    assert not server.writes['refunds']
    referral = ops.post_escalation({'summary': 'deadline', 'requested_decision': 'review'})
    assert referral.state == 'succeeded'


def test_expired_model_deadline_sends_no_request(config, journal):
    import time
    llm = LLMClient(config, journal, 'r', client=StubClient([]))
    llm.deadline = time.monotonic() - 1
    with pytest.raises(BudgetExhausted, match='deadline'):
        llm.parse(purpose='test', instructions='x', user_content='y',
                  text_format=PassengerReply, max_output_tokens=100)
    assert journal.usage_totals()['calls'] == 0


def test_same_amount_different_event_is_not_suppressed_by_server(config, journal, ops, server):
    from tests.test_executor import make_executor, payment_proposal, Inventory
    first = payment_proposal()
    make_executor(ops, journal, config).execute([first], Inventory(ops))
    ops.begin_case()
    next_event = payment_proposal()
    next_event.disruption_scope = 'ZZ999:2026-08-02'
    report = make_executor(ops, journal, config).execute([next_event], Inventory(ops))
    assert report.actions[0].state.value == 'succeeded'
    assert len(server.writes['payments']) == 2


def test_changed_amount_in_fresh_namespace_cannot_duplicate_payment(config, journal, ops, server, tmp_path):
    from tests.test_executor import make_executor, payment_proposal, Inventory
    first = payment_proposal()
    make_executor(ops, journal, config).execute([first], Inventory(ops))
    # A completely new journal still sees the existing remedy in the server audit.
    other = Journal(tmp_path / 'other.sqlite3', 'new')
    try:
        proposal = payment_proposal(amount_gbp=100)
        ops.begin_case()
        report = make_executor(ops, other, config).execute([proposal], Inventory(ops))
        assert report.actions[0].state.value == 'blocked'
        assert len(server.writes['payments']) == 1
    finally:
        other.close()


def test_invented_phone_cannot_confirm_identity(ops):
    from aerlink.identity import resolve_identity
    from tests.test_identity import _extraction
    result = resolve_identity(ops, extraction=_extraction(phone_numbers_in_body=['+447700900001']),
        inbound_text='Please help me.', meta_from='Ada Lovelace <other@example.test>')
    assert not result.confirmed


def test_forwarded_from_line_cannot_become_the_sender(ops):
    from aerlink.identity import resolve_identity
    from tests.test_identity import _extraction
    result = resolve_identity(ops, extraction=_extraction(),
        inbound_text='Please help me.\n\nFrom: Ada Lovelace <ada.lovelace@example.test>', meta_from=None)
    assert not result.confirmed


def test_all_queues_survive_bounded_handover_fanout():
    from aerlink.pipeline import _build_escalations
    from aerlink.planner import HandoverItem
    queues = ['YTP', 'SPECIAL_ASSISTANCE', 'SUPERVISOR', 'LOST_PROPERTY', 'OPS_LIAISON']
    requests = [HandoverItem(q, q + ' issue', q + ' decision', q + ' recommendation', 'S12.5') for q in queues]
    proposals = _build_escalations(requests, booking_ref=None, disruption_scope='event',
                                  ops_base_url='http://127.0.0.1:8642', contact_key='unique')
    assert len(proposals) <= 3
    for queue in queues:
        assert any(queue + ' decision' in p.request_body['requested_decision'] for p in proposals)


def test_jsonl_scanner_understands_escaped_code_and_still_detects_credentials(tmp_path, capsys):
    import json
    from tools.check_transcript import main as check
    transcript = tmp_path / 'session.jsonl'
    transcript.write_text(json.dumps({'content': 'OPENAI_API_KEY=\nopenai_api_key: str\napi_key=config.openai_api_key\nX-Ops-Key: %s\n'}), encoding='utf-8')
    assert check(['check', str(transcript)]) == 0
    transcript.write_text(json.dumps({'content': 'SOME_SERVICE_TOKEN=abcdef0123456789\n'}), encoding='utf-8')
    assert check(['check', str(transcript)]) == 1
    assert 'abcdef0123456789' not in capsys.readouterr().out
