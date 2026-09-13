"""Configuration and the verified model price table.

Secrets are read from the environment and never logged, printed or written into a
case record. `Config.describe()` returns a redacted view suitable for the console.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- Operational limits ------------------------------------------------------
MAX_MODEL_REQUESTS_PER_CASE = 3        # includes the single permitted schema repair
MAX_MODEL_REPAIRS_PER_CALL = 1
MAX_OPS_ATTEMPTS_PER_CASE = 30         # includes retries
OPS_ATTEMPTS_RESERVED_FOR_HANDOVER = 4
MAX_READ_ATTEMPTS = 3                  # per distinct read request
MAX_WRITE_ATTEMPTS = 1                 # mutations are never retried automatically
REQUEST_TIMEOUT_S = 30.0
CASE_TIMEOUT_S = 180.0

# Client-side throttle, kept under the server's documented 30 requests / 10 seconds
# (API.md "Rate limit") so we do not spend attempts on 429s.
THROTTLE_MAX_REQUESTS = 25
THROTTLE_WINDOW_S = 10.0

# --- Spending ceilings (USD) -------------------------------------------------
SWEEP_ADMISSION_CEILING_USD = Decimal("1.90")   # margin below the brief's $2.00
DEV_SESSION_CEILING_USD = Decimal("5.00")


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1,000,000 tokens, with the source it was read from and when.

    Selecting a model with no entry here is a configuration error: we will not run
    against a model whose price we have not verified.

    These are the **short-context** standard rates. The pricing page lists a
    long-context tier at roughly double (gpt-5.6-luna: $0.40 / $0.04 / $1.80). Every
    call this system makes is capped at MAX_INPUT_TOKENS_PER_CALL (24,000) and runs at
    about 5,000, so the short-context tier applies with a wide margin. If that cap is
    ever raised past the provider's long-context threshold these figures stop being
    correct, which is why the cap and the rates live in the same file.
    """

    model: str
    input_usd_per_mtok: Decimal
    cached_input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    source_url: str
    verified_on: str
    is_reasoning_model: bool
    reasoning_effort: str | None


VERIFIED_MODEL_PRICES: dict[str, ModelPrice] = {
    "gpt-5.6-luna": ModelPrice(
        model="gpt-5.6-luna",
        input_usd_per_mtok=Decimal("0.20"),
        cached_input_usd_per_mtok=Decimal("0.02"),
        output_usd_per_mtok=Decimal("1.20"),
        source_url="https://developers.openai.com/api/docs/pricing",
        # Read 2026-09-13 and re-verified against the same page after the corrective
        # audit; `models.retrieve` confirms the id is live for the key in use.
        verified_on="2026-09-13 (re-verified)",
        is_reasoning_model=True,
        # "none" is accepted by this model (developers.openai.com/api/docs/guides/
        # reasoning). It keeps reasoning tokens at or near zero; max_output_tokens
        # bounds reasoning + visible + formatting tokens regardless, so the billable
        # upper bound stays accountable either way.
        reasoning_effort="none",
    ),
    "gpt-5-nano": ModelPrice(
        model="gpt-5-nano",
        input_usd_per_mtok=Decimal("0.05"),
        cached_input_usd_per_mtok=Decimal("0.005"),
        output_usd_per_mtok=Decimal("0.40"),
        source_url="https://developers.openai.com/api/docs/pricing",
        verified_on="2026-09-13",
        is_reasoning_model=True,
        reasoning_effort="none",
    ),
    "gpt-5-mini": ModelPrice(
        model="gpt-5-mini",
        input_usd_per_mtok=Decimal("0.25"),
        cached_input_usd_per_mtok=Decimal("0.025"),
        output_usd_per_mtok=Decimal("2.00"),
        source_url="https://developers.openai.com/api/docs/pricing",
        verified_on="2026-09-13",
        is_reasoning_model=True,
        reasoning_effort="none",
    ),
}

DEFAULT_MODEL = "gpt-5.6-luna"

# Output caps. These bound what we can be billed for on each call.
EXTRACT_MAX_OUTPUT_TOKENS = 2200
NARRATE_MAX_OUTPUT_TOKENS = 1400
# Refuse to send an oversized prompt rather than silently paying for it.
MAX_INPUT_TOKENS_PER_CALL = 24000
# tiktoken has no encoding registered for this model id; o200k_base is used as an
# approximation and inflated by this margin before the spend is reserved. It is an
# estimate with headroom, not a guarantee -- the authoritative figure is the usage
# the API reports afterwards.
TOKEN_ESTIMATE_SAFETY_MARGIN = Decimal("1.20")

MAX_INBOUND_BYTES = 200_000


class ConfigError(Exception):
    """Raised for a configuration problem. Maps to CLI exit code 2."""


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal .env reader. Avoids a dependency for fifteen lines of parsing."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key.strip()] = value
    return out


@dataclass
class Config:
    ops_base_url: str
    ops_api_key: str
    openai_api_key: str
    openai_model: str
    price: ModelPrice
    journal_path: Path
    journal_namespace: str
    allow_non_loopback_ops: bool
    authority_level: str
    notes: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        """Redacted view. Never includes a key or any part of one."""
        return {
            "ops_base_url": self.ops_base_url,
            "ops_api_key": "set" if self.ops_api_key else "MISSING",
            "openai_api_key": "set" if self.openai_api_key else "MISSING",
            "openai_model": self.openai_model,
            "model_price_source": self.price.source_url,
            "model_price_verified_on": self.price.verified_on,
            "journal_path": str(self.journal_path),
            "journal_namespace": self.journal_namespace,
            "authority_level": self.authority_level,
            "notes": self.notes,
        }


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_config(
    *,
    env_file: Path | None = None,
    journal_namespace: str = "default",
    require_openai_key: bool = True,
) -> Config:
    """Build a validated Config from .env plus the process environment.

    The process environment wins over the file, so a reviewer can override a single
    value without editing anything.
    """
    notes: list[str] = []
    env_path = env_file if env_file is not None else REPO_ROOT / ".env"
    file_env = parse_env_file(env_path)

    def get(key: str, default: str | None = None) -> str | None:
        return os.environ.get(key) or file_env.get(key) or default

    # .env.example is the documented source for the operations API settings. The
    # supplied .env may legitimately carry only the OpenAI key, so fall back to the
    # example's values rather than failing, and say that we did.
    example_env = parse_env_file(REPO_ROOT / ".env.example")

    ops_base_url = get("OPS_BASE_URL")
    if not ops_base_url:
        ops_base_url = example_env.get("OPS_BASE_URL", "http://127.0.0.1:8642")
        notes.append(
            "OPS_BASE_URL not set in {}; using the .env.example value {}.".format(
                env_path.name, ops_base_url
            )
        )
    ops_base_url = ops_base_url.rstrip("/")

    ops_api_key = get("OPS_API_KEY")
    if not ops_api_key:
        ops_api_key = example_env.get("OPS_API_KEY", "")
        if ops_api_key:
            notes.append(
                "OPS_API_KEY not set in {}; using the .env.example value.".format(
                    env_path.name
                )
            )
    if not ops_api_key:
        raise ConfigError(
            "OPS_API_KEY is not set and no fallback is available in .env.example. "
            "Copy .env.example to .env and fill it in."
        )

    host_match = re.match(r"^https?://([^/:]+)(?::(\d+))?$", ops_base_url)
    if not host_match:
        raise ConfigError(
            "OPS_BASE_URL must look like http://host:port, got {!r}.".format(ops_base_url)
        )
    host = host_match.group(1)
    allow_non_loopback = _truthy(get("AERLINK_ALLOW_NON_LOOPBACK_OPS", "0"))
    if host not in _LOOPBACK_HOSTS and not allow_non_loopback:
        raise ConfigError(
            "OPS_BASE_URL points at {!r}, which is not loopback. This system performs "
            "real payments, refunds and re-bookings. Set "
            "AERLINK_ALLOW_NON_LOOPBACK_OPS=1 only if you genuinely intend to point "
            "mutation-capable execution at a non-local service.".format(host)
        )

    openai_api_key = get("OPENAI_API_KEY", "") or ""
    if require_openai_key and not openai_api_key:
        raise ConfigError(
            "OPENAI_API_KEY is not set. Put the supplied key in .env as "
            "OPENAI_API_KEY=... (the file is already in .gitignore)."
        )

    model = get("OPENAI_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL
    price = VERIFIED_MODEL_PRICES.get(model)
    if price is None:
        raise ConfigError(
            "OPENAI_MODEL={!r} has no verified price entry. Changing the model "
            "requires adding its verified input / cached-input / output prices, the "
            "source URL and the date they were read to VERIFIED_MODEL_PRICES in "
            "aerlink/config.py. Known models: {}".format(
                model, ", ".join(sorted(VERIFIED_MODEL_PRICES))
            )
        )

    # The journal deliberately does NOT live under --output: changing the output
    # directory must not re-enable an action that has already been taken.
    journal_path = Path(
        get("AERLINK_JOURNAL", str(REPO_ROOT / "state" / "journal.sqlite3"))
    )

    # Which level of the S12.1 table this desk operates at. That is a deployment
    # fact about the deployment, not something this code may decide, so it is
    # configuration with the most conservative default. Raising it does not unlock
    # everything: S12.1 lists no compensation row at any level.
    level = (get("AERLINK_AUTHORITY_LEVEL", "representative") or "").strip().lower()
    if level not in {"representative", "supervisor", "manager"}:
        raise ConfigError(
            "AERLINK_AUTHORITY_LEVEL={!r} is not one of the three levels S12.1 "
            "defines: representative, supervisor, manager.".format(level)
        )
    if level != "representative":
        notes.append(
            "Operating at {} level under S12.1, not the default representative level. "
            "This widens what may be actioned without referral.".format(level)
        )

    return Config(
        ops_base_url=ops_base_url,
        ops_api_key=ops_api_key,
        openai_api_key=openai_api_key,
        openai_model=model,
        price=price,
        journal_path=journal_path,
        journal_namespace=journal_namespace,
        allow_non_loopback_ops=allow_non_loopback,
        authority_level=level,
        notes=notes,
    )


def redact(text: str, *secrets: str) -> str:
    """Strip anything secret out of a string before it reaches a log or a record."""
    out = text
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "[redacted]")
    # Catch key-shaped tokens and auth headers we did not explicitly pass in.
    out = re.sub(r"sk-[A-Za-z0-9_\-]{16,}", "[redacted]", out)
    out = re.sub(r"(?i)(authorization|x-ops-key)(\s*[:=]\s*)\S+", r"\1\2[redacted]", out)
    return out
