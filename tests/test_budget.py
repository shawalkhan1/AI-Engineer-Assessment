"""Token accounting, cost arithmetic and the spending ceilings.

No network: the OpenAI client is a stub whose usage numbers we control, so the
arithmetic can be checked against figures worked out by hand.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aerlink.config import MAX_MODEL_REQUESTS_PER_CASE
from aerlink.llm import BudgetExhausted, LLMClient, ModelCallLimitReached, ModelOutputInvalid
from aerlink.schemas import PassengerReply


class Usage:
    def __init__(self, input_tokens, output_tokens, cached=0, reasoning=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.input_tokens_details = type("D", (), {"cached_tokens": cached})()
        self.output_tokens_details = type("D", (), {"reasoning_tokens": reasoning})()


class StubResponse:
    def __init__(self, parsed, usage, status="completed", reason=None):
        self.id = "resp_test_1"
        self.output_parsed = parsed
        self.usage = usage
        self.status = status
        self.incomplete_details = type("I", (), {"reason": reason})()
        self.output = []


class StubClient:
    """Stands in for openai.OpenAI. Records calls; returns scripted responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        outer = self

        class Responses:
            def parse(self, **kwargs):
                outer.calls.append(kwargs)
                item = outer._responses.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        self.responses = Responses()
        self.models = type("M", (), {"retrieve": lambda _s, m: type("X", (), {"id": m})()})()


def make_llm(config, journal, responses, **kwargs):
    return LLMClient(
        config, journal, "run-test", client=StubClient(responses), **kwargs
    )


REPLY = PassengerReply(language="en", subject="s", body="b")


# ---------------------------------------------------------------------------
# Cost arithmetic
# ---------------------------------------------------------------------------


def test_cost_is_derived_from_reported_usage_without_double_counting(config, journal):
    """cached is a subset of input; reasoning is a subset of output."""
    llm = make_llm(config, journal, [])
    usage = Usage(input_tokens=10_000, output_tokens=2_000, cached=4_000, reasoning=1_500)
    cost, counts = llm.cost_from_usage(usage)

    # 6,000 uncached @ $0.20/M = 0.0012
    # 4,000 cached   @ $0.02/M = 0.00008
    # 2,000 output   @ $1.20/M = 0.0024   (the 1,500 reasoning tokens are inside this)
    assert cost == Decimal("0.003680")
    assert counts == {
        "input_tokens": 10_000,
        "cached_input_tokens": 4_000,
        "cache_write_tokens": 0,
        "output_tokens": 2_000,
        "reasoning_tokens": 1_500,
    }


def test_reservation_is_an_upper_bound_with_a_margin(config, journal):
    llm = make_llm(config, journal, [])
    # 1,000 input tokens * 1.20 margin @ $0.20/M = 0.00024
    # 500 output tokens (the cap, in full) @ $1.20/M = 0.0006
    assert llm.reserve_for(1_000, 500) == Decimal("0.000840")


def test_reservation_exceeds_the_eventual_cost_for_a_typical_call(config, journal):
    llm = make_llm(config, journal, [StubResponse(REPLY, Usage(2_000, 600))])
    reservation = llm.reserve_for(2_000, 1_400)
    llm.begin_case("case-1")
    result = llm.parse(
        purpose="narration",
        instructions="x" * 10,
        user_content="y" * 10,
        text_format=PassengerReply,
        max_output_tokens=1_400,
    )
    assert Decimal(result.usage["calculated_cost_usd"]) < reservation


# ---------------------------------------------------------------------------
# Ceilings
# ---------------------------------------------------------------------------


def test_a_call_that_would_breach_the_session_ceiling_is_refused(config, journal):
    llm = make_llm(
        config, journal, [StubResponse(REPLY, Usage(100, 10))],
        session_ceiling_usd=Decimal("0.0000001"),
    )
    llm.begin_case("case-1")
    with pytest.raises(BudgetExhausted, match="session ceiling"):
        llm.parse(
            purpose="extraction",
            instructions="x",
            user_content="y",
            text_format=PassengerReply,
            max_output_tokens=100,
        )
    # Nothing was sent, so nothing was billed.
    assert journal.usage_totals()["calls"] == 0


def test_a_call_that_would_breach_the_run_ceiling_is_refused(config, journal):
    llm = make_llm(
        config,
        journal,
        [StubResponse(REPLY, Usage(100_000, 1_000))],
        run_ceiling_usd=Decimal("1.90"),
    )
    llm.begin_case("case-1")
    llm.parse(
        purpose="extraction",
        instructions="x" * 40,
        user_content="y" * 40,
        text_format=PassengerReply,
        max_output_tokens=100,
    )
    # Now pretend the run has nearly spent its ceiling.
    llm.run_ceiling = Decimal("0.02")
    llm.begin_case("case-2")
    with pytest.raises(BudgetExhausted, match="Run ceiling"):
        llm.parse(
            purpose="extraction",
            instructions="x" * 40,
            user_content="y" * 40,
            text_format=PassengerReply,
            max_output_tokens=100_000,
        )


def test_an_oversized_prompt_is_refused_rather_than_paid_for(config, journal):
    llm = make_llm(config, journal, [])
    llm.begin_case("case-1")
    with pytest.raises(BudgetExhausted, match="exceeds the per-call cap"):
        llm.parse(
            purpose="extraction",
            instructions="word " * 30_000,
            user_content="y",
            text_format=PassengerReply,
            max_output_tokens=100,
        )


def test_the_per_case_call_limit_is_enforced(config, journal):
    responses = [StubResponse(REPLY, Usage(100, 10)) for _ in range(5)]
    llm = make_llm(config, journal, responses)
    llm.begin_case("case-1")
    for _ in range(MAX_MODEL_REQUESTS_PER_CASE):
        llm.parse(
            purpose="extraction",
            instructions="x",
            user_content="y",
            text_format=PassengerReply,
            max_output_tokens=100,
        )
    with pytest.raises(ModelCallLimitReached):
        llm.parse(
            purpose="extraction",
            instructions="x",
            user_content="y",
            text_format=PassengerReply,
            max_output_tokens=100,
        )


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


def test_a_billed_call_whose_usage_is_unknown_keeps_its_reservation(config, journal):
    """Never reported as zero: it is an unresolved upper bound."""
    llm = make_llm(config, journal, [RuntimeError("connection reset")])
    llm.begin_case("case-1")
    with pytest.raises(ModelOutputInvalid):
        llm.parse(
            purpose="extraction",
            instructions="x" * 100,
            user_content="y" * 100,
            text_format=PassengerReply,
            max_output_tokens=500,
        )
    totals = journal.usage_totals()
    assert totals["unresolved_calls"] == 1
    assert Decimal(totals["calculated_cost_usd"]) == 0
    assert Decimal(totals["unresolved_reservation_upper_bound_usd"]) > 0
    assert Decimal(totals["worst_case_total_usd"]) == Decimal(
        totals["unresolved_reservation_upper_bound_usd"]
    )


def test_one_repair_is_attempted_and_both_calls_are_billed(config, journal):
    llm = make_llm(
        config,
        journal,
        [
            StubResponse(None, Usage(100, 10)),          # no parsed output
            StubResponse(REPLY, Usage(120, 12)),         # the repair succeeds
        ],
    )
    llm.begin_case("case-1")
    result = llm.parse(
        purpose="extraction",
        instructions="x",
        user_content="y",
        text_format=PassengerReply,
        max_output_tokens=100,
    )
    assert result.parsed.subject == "s"
    totals = journal.usage_totals()
    assert totals["calls"] == 2, "the repair costs money and is recorded"
    assert llm.calls_this_case == 2


def test_only_one_repair_is_attempted(config, journal):
    llm = make_llm(
        config,
        journal,
        [StubResponse(None, Usage(100, 10)), StubResponse(None, Usage(100, 10))],
    )
    llm.begin_case("case-1")
    with pytest.raises(ModelOutputInvalid, match="after one repair"):
        llm.parse(
            purpose="extraction",
            instructions="x",
            user_content="y",
            text_format=PassengerReply,
            max_output_tokens=100,
        )
    assert journal.usage_totals()["calls"] == 2


def test_an_incomplete_response_is_retried_once_with_a_shorter_instruction(config, journal):
    llm = make_llm(
        config,
        journal,
        [
            StubResponse(None, Usage(100, 100), status="incomplete", reason="max_output_tokens"),
            StubResponse(REPLY, Usage(100, 50)),
        ],
    )
    llm.begin_case("case-1")
    result = llm.parse(
        purpose="narration",
        instructions="x",
        user_content="y",
        text_format=PassengerReply,
        max_output_tokens=100,
    )
    assert result.parsed.subject == "s"
    assert "cut off" in llm._client.calls[1]["input"]


def test_the_sdk_never_retries_behind_our_back(config, journal, monkeypatch):
    """A hidden SDK retry would silently multiply both the call limit and the spend."""
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.responses = type("R", (), {"parse": lambda *a, **k: None})()
            self.models = type("M", (), {"retrieve": lambda *a, **k: None})()

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    LLMClient(config, journal, "run-test")
    assert captured["max_retries"] == 0


def test_usage_totals_separate_calculated_cost_from_unresolved_bounds(config, journal):
    journal.record_usage(
        run_id="r", case_id="c", purpose="extraction", model="m", request_id="x",
        input_tokens=1000, cached_tokens=0, output_tokens=100,
        reasoning_tokens=0, cost_usd=Decimal("0.001"), reserved_usd=Decimal("0.002"),
        resolved=True,
    )
    journal.record_usage(
        run_id="r", case_id="c", purpose="narration", model="m", request_id=None,
        input_tokens=None, cached_tokens=None, output_tokens=None,
        reasoning_tokens=None, cost_usd=None, reserved_usd=Decimal("0.005"),
        resolved=False,
    )
    totals = journal.usage_totals()
    assert totals["calculated_cost_usd"] == "0.001000"
    assert totals["unresolved_reservation_upper_bound_usd"] == "0.005000"
    assert totals["worst_case_total_usd"] == "0.006000"
    assert totals["tokens"]["input"] == 1000      # the unresolved call adds no tokens
