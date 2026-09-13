"""OpenAI access, with the spend accounted for before the money is spent.

Every call goes through `parse()`, which:

1. estimates the input size and refuses anything oversized;
2. reserves a conservative upper bound -- estimated input at the input price plus the
   full `max_output_tokens` at the output price -- and refuses the call if that
   reservation would breach a ceiling;
3. sends exactly one request (the SDK's own retries are disabled so nothing
   multiplies the per-case limit), with at most one schema-repair attempt;
4. records the usage the API actually reported, and derives the cost from the
   verified price table.

If a call fails in a way that may still have been billed, its reservation is kept as
an unresolved upper bound rather than being written off as zero.

Token counting uses tiktoken's ``o200k_base`` because no encoding is registered for
this model id. It is an estimate with a margin, used to decide whether to *risk* a
call. The authoritative figure is always the usage the API returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .config import (
    DEV_SESSION_CEILING_USD,
    MAX_INPUT_TOKENS_PER_CALL,
    MAX_MODEL_REQUESTS_PER_CASE,
    REQUEST_TIMEOUT_S,
    TOKEN_ESTIMATE_SAFETY_MARGIN,
    Config,
    ModelPrice,
    redact,
)
from .journal import Journal

T = TypeVar("T", bound=BaseModel)


class BudgetExhausted(Exception):
    """A ceiling would be breached. The case must continue without the model."""


class ModelCallLimitReached(Exception):
    """The per-case model request limit is spent."""


class ModelOutputInvalid(Exception):
    """The model did not return output matching the schema, after one repair."""


@dataclass
class CallResult:
    parsed: Any
    usage: dict[str, Any]


def _encoder():
    import tiktoken

    return tiktoken.get_encoding("o200k_base")


class LLMClient:
    def __init__(
        self,
        config: Config,
        journal: Journal,
        run_id: str,
        *,
        client: Any | None = None,
        run_ceiling_usd: Decimal | None = None,
        session_ceiling_usd: Decimal = DEV_SESSION_CEILING_USD,
    ) -> None:
        self.config = config
        self.price: ModelPrice = config.price
        self.journal = journal
        self.run_id = run_id
        self.run_ceiling = run_ceiling_usd
        self.session_ceiling = session_ceiling_usd
        self.case_id: str | None = None
        self.calls_this_case = 0
        self.case_usage: list[dict[str, Any]] = []
        self._encoding = None
        self._model_verified = False

        if client is not None:
            self._client = client
        else:
            from openai import OpenAI

            # max_retries=0: a hidden SDK retry would quietly multiply the per-case
            # request limit and the spend it was meant to bound.
            self._client = OpenAI(
                api_key=config.openai_api_key,
                max_retries=0,
                timeout=REQUEST_TIMEOUT_S,
            )

    # -- lifecycle ---------------------------------------------------------

    def begin_case(self, case_id: str) -> None:
        self.case_id = case_id
        self.calls_this_case = 0
        self.case_usage = []

    def verify_model_available(self) -> dict[str, Any]:
        """Confirm the configured model exists for this key before spending on it."""
        model = self._client.models.retrieve(self.config.openai_model)
        self._model_verified = True
        return {
            "model": getattr(model, "id", self.config.openai_model),
            "owned_by": getattr(model, "owned_by", None),
        }

    # -- token accounting --------------------------------------------------

    def estimate_tokens(self, text: str) -> int:
        if self._encoding is None:
            self._encoding = _encoder()
        return len(self._encoding.encode(text))

    def reserve_for(self, estimated_input_tokens: int, max_output_tokens: int) -> Decimal:
        """Conservative upper bound on what this call can cost.

        Input is inflated by the safety margin and priced at the uncached rate (we
        never assume a cache hit). Output is priced at `max_output_tokens` in full,
        which for a reasoning model bounds reasoning, visible and formatting tokens
        together.
        """
        padded_input = (
            Decimal(estimated_input_tokens) * TOKEN_ESTIMATE_SAFETY_MARGIN
        )
        million = Decimal(1_000_000)
        return (
            padded_input * self.price.input_usd_per_mtok / million
            + Decimal(max_output_tokens) * self.price.output_usd_per_mtok / million
        ).quantize(Decimal("0.000001"))

    def cost_from_usage(self, usage: Any) -> tuple[Decimal, dict[str, int]]:
        """Derive cost from reported usage without double-counting token categories.

        `reasoning_tokens` is a subset of `output_tokens`, and `cached_tokens` is a
        subset of `input_tokens`, so each billed token is priced exactly once.
        """
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        details_in = getattr(usage, "input_tokens_details", None)
        details_out = getattr(usage, "output_tokens_details", None)
        cached = int(getattr(details_in, "cached_tokens", 0) or 0) if details_in else 0
        reasoning = (
            int(getattr(details_out, "reasoning_tokens", 0) or 0) if details_out else 0
        )
        uncached = max(0, input_tokens - cached)
        million = Decimal(1_000_000)
        cost = (
            Decimal(uncached) * self.price.input_usd_per_mtok / million
            + Decimal(cached) * self.price.cached_input_usd_per_mtok / million
            + Decimal(output_tokens) * self.price.output_usd_per_mtok / million
        ).quantize(Decimal("0.000001"))
        counts = {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning,
        }
        return cost, counts

    # -- budget ------------------------------------------------------------

    def _admit(self, reservation: Decimal, purpose: str) -> None:
        session_spent = self.journal.committed_spend()
        if session_spent + reservation > self.session_ceiling:
            raise BudgetExhausted(
                "Development session ceiling ${} would be breached by a ${} "
                "reservation for '{}' (${} already committed).".format(
                    self.session_ceiling, reservation, purpose, session_spent
                )
            )
        if self.run_ceiling is not None:
            run_totals = self.journal.usage_totals(run_id=self.run_id)
            run_spent = Decimal(run_totals["worst_case_total_usd"])
            if run_spent + reservation > self.run_ceiling:
                raise BudgetExhausted(
                    "Run ceiling ${} would be breached by a ${} reservation for "
                    "'{}' (${} already committed on this run).".format(
                        self.run_ceiling, reservation, purpose, run_spent
                    )
                )

    # -- the one call path -------------------------------------------------

    def parse(
        self,
        *,
        purpose: str,
        instructions: str,
        user_content: str,
        text_format: type[T],
        max_output_tokens: int,
    ) -> CallResult:
        if self.calls_this_case >= MAX_MODEL_REQUESTS_PER_CASE:
            raise ModelCallLimitReached(
                "Per-case model request limit of {} reached.".format(
                    MAX_MODEL_REQUESTS_PER_CASE
                )
            )

        estimated = self.estimate_tokens(instructions) + self.estimate_tokens(
            user_content
        )
        if estimated > MAX_INPUT_TOKENS_PER_CALL:
            raise BudgetExhausted(
                "Estimated input of {} tokens exceeds the per-call cap of {}.".format(
                    estimated, MAX_INPUT_TOKENS_PER_CALL
                )
            )

        attempt_inputs = [user_content]
        last_error: Exception | None = None

        for attempt in range(2):  # one initial call plus at most one repair
            if attempt == 1:
                # The repair is a fresh request and costs money, so it is admitted,
                # counted and capped exactly like the first.
                if self.calls_this_case >= MAX_MODEL_REQUESTS_PER_CASE:
                    break
            content = attempt_inputs[-1]
            estimated = self.estimate_tokens(instructions) + self.estimate_tokens(content)
            reservation = self.reserve_for(estimated, max_output_tokens)
            self._admit(reservation, purpose)

            self.calls_this_case += 1
            usage_row: dict[str, Any] = {
                "purpose": purpose if attempt == 0 else purpose + "_repair",
                "model": self.config.openai_model,
                "request_id": None,
                "input_tokens": None,
                "cached_input_tokens": None,
                "output_tokens": None,
                "reasoning_tokens": None,
                "calculated_cost_usd": None,
                "reserved_upper_bound_usd": str(reservation),
                "reservation_resolved": False,
                "note": None,
            }

            kwargs: dict[str, Any] = {
                "model": self.config.openai_model,
                "instructions": instructions,
                "input": content,
                "text_format": text_format,
                "max_output_tokens": max_output_tokens,
                "store": False,
                "timeout": REQUEST_TIMEOUT_S,
            }
            if self.price.is_reasoning_model and self.price.reasoning_effort:
                kwargs["reasoning"] = {"effort": self.price.reasoning_effort}

            try:
                response = self._client.responses.parse(**kwargs)
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                usage_row["note"] = (
                    "call failed before usage could be established; reservation kept "
                    "as an unresolved upper bound: "
                    + redact(str(exc), self.config.openai_api_key)[:300]
                )
                self._persist_usage(usage_row, reservation, resolved=False)
                last_error = exc
                raise ModelOutputInvalid(
                    redact(str(exc), self.config.openai_api_key)[:300]
                ) from exc

            usage = getattr(response, "usage", None)
            if usage is not None:
                cost, counts = self.cost_from_usage(usage)
                usage_row.update(counts)
                usage_row["calculated_cost_usd"] = str(cost)
                usage_row["reservation_resolved"] = True
                usage_row["request_id"] = getattr(response, "id", None)
                self._persist_usage(usage_row, reservation, resolved=True, cost=cost)
            else:
                usage_row["note"] = (
                    "API returned no usage object; reservation kept as an unresolved "
                    "upper bound."
                )
                self._persist_usage(usage_row, reservation, resolved=False)

            status = getattr(response, "status", None)
            if status == "incomplete":
                reason = getattr(
                    getattr(response, "incomplete_details", None), "reason", "unknown"
                )
                last_error = ModelOutputInvalid(
                    "response incomplete ({}) at max_output_tokens={}".format(
                        reason, max_output_tokens
                    )
                )
                attempt_inputs.append(
                    content
                    + "\n\nYour previous reply was cut off before it was complete. "
                    "Reply again, much more briefly. Keep every quote short."
                )
                continue

            parsed = getattr(response, "output_parsed", None)
            if parsed is None:
                refusal = _first_refusal(response)
                last_error = ModelOutputInvalid(
                    "no parsed output" + (" (refusal: {})".format(refusal) if refusal else "")
                )
                attempt_inputs.append(
                    content
                    + "\n\nYour previous reply did not match the required schema. "
                    "Reply again, filling in every field of the schema exactly."
                )
                continue

            try:
                validated = text_format.model_validate(parsed.model_dump())
            except ValidationError as exc:
                last_error = exc
                attempt_inputs.append(
                    content
                    + "\n\nYour previous reply failed validation:\n"
                    + str(exc)[:800]
                    + "\nReply again, matching the schema exactly."
                )
                continue

            return CallResult(parsed=validated, usage=usage_row)

        raise ModelOutputInvalid(
            "structured output could not be obtained after one repair: {}".format(
                str(last_error)[:300]
            )
        )

    def _persist_usage(
        self,
        row: dict[str, Any],
        reservation: Decimal,
        *,
        resolved: bool,
        cost: Decimal | None = None,
    ) -> None:
        self.case_usage.append(dict(row))
        self.journal.record_usage(
            run_id=self.run_id,
            case_id=self.case_id,
            purpose=row["purpose"],
            model=row["model"],
            request_id=row["request_id"],
            input_tokens=row["input_tokens"],
            cached_tokens=row["cached_input_tokens"],
            output_tokens=row["output_tokens"],
            reasoning_tokens=row["reasoning_tokens"],
            cost_usd=cost,
            reserved_usd=reservation,
            resolved=resolved,
        )


def _first_refusal(response: Any) -> str | None:
    for item in getattr(response, "output", []) or []:
        for part in getattr(item, "content", []) or []:
            refusal = getattr(part, "refusal", None)
            if refusal:
                return str(refusal)[:200]
    return None
