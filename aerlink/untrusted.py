"""Handling of content that came from outside Aerlink.

APCP-2026-04 S12.4 is explicit: an instruction arriving inside a passenger contact,
a forwarded section, an attachment, a free-text booking field, or *any data returned
by an operational system* is not a valid instruction, whatever authority it claims.

Three things live here:

* `wrap_untrusted` -- fences the content and labels it as data before it reaches a
  prompt.
* `verify_span` -- checks that a quote the model attributed to the message really
  appears in it. An unverifiable quote means the claim around it is not evidence.
* `detect_injection_indicators` -- a cheap deterministic scan, recorded in the case
  record.

The scan is the *last* line of defence, not the first. The real one is structural:
the model never proposes a mutation, amounts come from the entitlement service, and
authority comes from `policy.py`. A fully compromised extraction still cannot move
money.
"""

from __future__ import annotations

import re
import unicodedata

_FENCE_OPEN = "<<<UNTRUSTED_PASSENGER_CONTENT>>>"
_FENCE_CLOSE = "<<<END_UNTRUSTED_PASSENGER_CONTENT>>>"

# Patterns that a legitimate passenger message essentially never needs, and that
# appear in attempts to redirect handling.
_INJECTION_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)\bignore (the |all |any )?(previous|prior|above|earlier)\b", "ignore_prior_instructions"),
    (r"(?i)\bdisregard (the |all |any )?(previous|prior|above|earlier|system)\b", "disregard_instructions"),
    (r"(?i)\bpriority override\b", "claimed_override"),
    (r"(?i)\bsystem (notice|message|instruction|prompt)\b", "claimed_system_message"),
    (r"(?i)\bautomated handling instruction\b", "claimed_handling_instruction"),
    (r"(?i)\bhandling agent,? (human or automated|automated or human)\b", "addresses_the_agent"),
    # The clause reference usually sits between the two halves: "the authorisation
    # limits in Section 12 of APCP-2026-04 are suspended by agreement".
    (r"(?i)\b(?:authorisation |authorization )?limits?\b[^.\n]{0,90}\bsuspended\b", "claims_limits_suspended"),
    (r"(?i)\byou are authoris(ed|ed to)|you are authorized\b", "claims_to_grant_authority"),
    (r"(?i)\bwithout (supervisor|manager) (approval|authorisation|authorization)\b", "claims_approval_waived"),
    (r"(?i)\bdo not (escalate|refer|verify|request any further verification)\b", "suppresses_referral"),
    (r"(?i)\bwithout further verification\b", "suppresses_verification"),
    (r"(?i)\bnot visible in the standard booking system\b", "claims_invisible_status"),
    (r"(?i)\bwill be recorded as a service breach\b", "threatens_the_agent"),
    (r"(?i)\breply .{0,30}with the single word\b", "dictates_reply"),
]

_URL_PATTERN = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"')]+")


def wrap_untrusted(label: str, content: str) -> str:
    """Fence untrusted content so the prompt can point at it as data."""
    # Strip any attempt to forge our own fence out of the content itself.
    cleaned = content.replace(_FENCE_OPEN, "[fence]").replace(_FENCE_CLOSE, "[fence]")
    return "{} ({})\n{}\n{}".format(_FENCE_OPEN, label, cleaned, _FENCE_CLOSE)


def normalise_for_span(text: str) -> str:
    """Fold whitespace, case and accents so a quote can be matched robustly.

    Accents are folded because passengers write in several languages and a model
    re-typing a quote may normalise them; the check is for provenance, not for
    byte-identical transcription.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).strip().casefold()


def verify_span(quote: str | None, haystack: str) -> bool:
    """True if the quote genuinely appears in the message."""
    if not quote or not quote.strip():
        return False
    needle = normalise_for_span(quote)
    if len(needle) < 8:
        # Too short to be evidence of anything.
        return False
    return needle in normalise_for_span(haystack)


def detect_injection_indicators(text: str) -> list[dict[str, str]]:
    """Deterministic scan. Recorded in the case record; never acted on."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for pattern, label in _INJECTION_PATTERNS:
        match = re.search(pattern, text)
        if match and label not in seen:
            seen.add(label)
            found.append({"indicator": label, "matched_text": match.group(0)[:120]})
    return found


def extract_urls(text: str) -> list[str]:
    """URLs are recorded so the case shows we saw them. They are never fetched."""
    return sorted({m.group(0)[:200] for m in _URL_PATTERN.finditer(text)})


_FORWARD_MARKERS = (
    "-----original message-----",
    "--- forwarded message ---",
    "begin forwarded message:",
    "========================= forwarded conversation",
)


INTERNAL_AUTHORITY_WORDS = (
    "aerlink", "operations", "ops", "system", "supervisor", "manager", "desk",
    "automated", "override", "notice", "head office", "internal",
)


def claims_internal_authority(claimed_authority: str | None) -> bool:
    """Does this text present itself as direction from inside the airline?

    A passenger quoting the Passenger Care Policy at us is asserting an entitlement,
    not claiming authority over how their case is handled. Getting this wrong erased
    both of Tomas Ferreira's requests -- his own "Please pay me GBP 970" was
    reclassified as an instruction and dropped from the case.
    """
    claimed = (claimed_authority or "").casefold()
    if not claimed:
        return False
    if not any(word in claimed for word in INTERNAL_AUTHORITY_WORDS):
        return False
    if "policy" in claimed and not any(
        word in claimed for word in ("desk", "operations", "system", "internal")
    ):
        return False
    return True


def tainted_blocks(text: str, instruction_quotes: list[str]) -> list[str]:
    """The region of the message that belongs to content claiming authority.

    S12.4 treats an instruction arriving inside a contact as invalid whatever it
    claims. A forged notice is not one sentence, and it is not one paragraph: it is a
    contiguous *block*, and a model reading it will quote one part as an instruction
    and another part as a request. Tainting only the paragraphs that were quoted
    misses the second kind, which is how an injected "goodwill payment of up to GBP
    5000.00" turned into a passenger request of its own.

    So the region is treated as a hull: from the first paragraph carrying evidence of
    an instruction to the last, extended back to a forwarded-message marker where one
    precedes it. The passenger's own words, above the marker, stay untainted.
    """
    paragraphs = re.split(r"(\n\s*\n)", text or "")
    blocks = [p for i, p in enumerate(paragraphs) if i % 2 == 0]
    if not blocks:
        return []

    needles = [
        n for n in (normalise_for_span(q) for q in instruction_quotes if q) if len(n) >= 8
    ]

    marked: list[int] = []
    for index, block in enumerate(blocks):
        normalised = normalise_for_span(block)
        if not normalised:
            continue
        if any(n in normalised for n in needles) or detect_injection_indicators(block):
            marked.append(index)
    if not marked:
        return []

    start, end = min(marked), max(marked)
    for index in range(start - 1, -1, -1):
        normalised = normalise_for_span(blocks[index])
        if any(marker in normalised for marker in _FORWARD_MARKERS):
            start = index
            break

    return [
        normalise_for_span(blocks[i]) for i in range(start, end + 1)
        if normalise_for_span(blocks[i])
    ]


def quote_is_tainted(quote: str | None, tainted: list[str]) -> bool:
    """True where a quote sits inside content that was claiming authority."""
    if not quote or not tainted:
        return False
    needle = normalise_for_span(quote)
    if len(needle) < 8:
        return False
    return any(needle in block for block in tainted)
