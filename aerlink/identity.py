"""Establishing who is writing and which booking they mean (APCP-2026-04 S2).

S2.1 gives exactly three confirmation standards and no others:

  (a) a valid booking reference together with a surname matching a passenger on
      that booking;
  (b) an email address matching, character for character, the contact email held on
      exactly one booking;
  (c) a telephone number matching, in normalised international form, the contact
      telephone held on exactly one booking.

A name on its own is not one of them, however distinctive it looks and however
confident a model is about it. S2.2: "A partial or probable match is not a match.
Similarity of name is not identity." Where identity is not confirmed, S16 prohibits
*any* action on *any* booking, and the case is referred under S12.5.

Every plausible candidate that was inspected is recorded, including the ones that
were rejected, so the record shows the ambiguity rather than hiding it behind the
first match.
"""

from __future__ import annotations

import re
from email.parser import Parser
from dataclasses import dataclass, field
from typing import Any

from .ops_client import OpsClient, OpsError
from .schemas import Extraction

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_ADDRESS_RE = re.compile(r"^\s*(?P<name>.*?)\s*<(?P<email>[^>]+)>\s*$")
# Sweep of the raw message, used as a backstop when the model did not run. Kept
# tight to the Aerlink reference shape so that the airline's own name in a To: header
# is not spent as a lookup. A reference the model quoted is looked up whatever its
# shape, so "that reference does not exist" stays a checked statement rather than an
# assumption -- see `candidate_refs`.
_REF_CANDIDATE_RE = re.compile(r"\bAER-[A-Z0-9]{6}\b")


@dataclass
class IdentityResult:
    confirmed: bool
    standard: str | None
    booking_ref: str | None
    booking: dict[str, Any] | None
    customer_id: str | None
    sender_email: str | None
    sender_display_name: str | None
    reason: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    unresolved_checks: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    third_party_blocked: bool = False

    def to_record(self) -> dict[str, Any]:
        return {
            "confirmed": self.confirmed,
            "confirmation_standard": self.standard,
            "verified_booking_ref": self.booking_ref,
            "customer_id": self.customer_id,
            "sender_email": self.sender_email,
            "sender_display_name": self.sender_display_name,
            "reason": self.reason,
            "candidates_considered": self.candidates,
            "unresolved_checks": self.unresolved_checks,
            "evidence_source_ids": self.evidence,
            "third_party_contact_blocked": self.third_party_blocked,
            "rule": (
                "APCP-2026-04 S2.1 permits confirmation only by (a) reference plus "
                "matching surname, (b) an exact contact email on exactly one "
                "booking, or (c) a normalised telephone match on exactly one "
                "booking. S2.2: a partial or probable match is not a match."
            ),
        }


def parse_address(value: str | None) -> tuple[str | None, str | None]:
    """Split 'Name <addr@example>' into a display name and an address."""
    if not value:
        return None, None
    match = _ADDRESS_RE.match(value)
    if match:
        name = match.group("name").strip().strip('"') or None
        return name, match.group("email").strip()
    found = _EMAIL_RE.search(value)
    if found:
        return None, found.group(0)
    return value.strip() or None, None


def normalise_phone(value: str | None) -> str:
    digits = re.sub(r"[^\d]", "", value or "")
    return digits[2:] if digits.startswith("00") else digits


def _surname_pool(
    extraction: Extraction | None, sender_display_name: str | None
) -> set[str]:
    """Surnames the contact has supplied, for the S2.1(a) test."""
    pool: set[str] = set()
    if extraction:
        for surname in extraction.surnames_claimed:
            if surname.strip():
                pool.add(surname.strip().casefold())
        if extraction.sender_display_name:
            parts = extraction.sender_display_name.split()
            if parts:
                pool.add(parts[-1].casefold())
    if sender_display_name:
        parts = sender_display_name.replace(",", " ").split()
        if parts:
            pool.add(parts[-1].casefold())
            pool.add(parts[0].casefold())
    return {p for p in pool if len(p) >= 2}


def candidate_refs(extraction: Extraction | None, inbound_text: str) -> list[str]:
    """Booking references to look up, most trusted first.

    References the model quoted verbatim come first; a light regex sweep of the
    message catches any it missed. Both are only *candidates* -- the lookup decides.
    """
    ordered: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        cleaned = value.strip().upper()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            ordered.append(cleaned)

    if extraction:
        for ref in extraction.booking_refs:
            # A real quote alone does not prove the model copied the reference
            # correctly. The identifier itself must occur in the raw contact.
            if ref.value.strip().casefold() in inbound_text.casefold():
                add(ref.value)
    for match in _REF_CANDIDATE_RE.finditer(inbound_text.upper()):
        add(match.group(0))
    return ordered[:5]


def resolve_identity(
    ops: OpsClient,
    *,
    extraction: Extraction | None,
    inbound_text: str,
    meta_from: str | None,
) -> IdentityResult:
    display_name, sender_email = parse_address(meta_from)
    if not sender_email:
        # Fall back to the message's own From: header if transport metadata is absent.
        headers = Parser().parsestr(inbound_text, headersonly=True).get_all("From", [])
        if len(headers) == 1:
            display_name, sender_email = parse_address(headers[0])

    candidates: list[dict[str, Any]] = []
    unresolved: list[str] = []
    surnames = _surname_pool(extraction, display_name)
    identity_text = (inbound_text + " " + (display_name or "")).casefold()
    surnames = {name for name in surnames if re.search(
        r"(?<!\w)" + re.escape(name) + r"(?!\w)", identity_text
    )}

    # Inspect every supplied reference before choosing a booking. S2.2 prohibits
    # turning an ambiguous or contradictory contact into the first plausible match.
    refs = candidate_refs(extraction, inbound_text)
    ref_matches: list[IdentityResult] = []
    invalid_reference = False
    # --- S2.1(a): reference plus a matching surname --------------------------
    for ref in refs:
        try:
            booking = ops.get_booking(ref)
        except OpsError as exc:
            if exc.status == 404:
                invalid_reference = True
                candidates.append(
                    {
                        "booking_ref": ref,
                        "source": "reference supplied by the contact",
                        "outcome": "no such booking in the operational record",
                    }
                )
                continue
            unresolved.append(
                "lookup of reference {} failed: {} {}".format(ref, exc.status, exc.error_code)
            )
            continue

        booking_surnames = {
            p["surname"].casefold() for p in booking.get("passengers", []) if p.get("surname")
        }
        matched = sorted(booking_surnames & surnames)
        if matched:
            result = IdentityResult(
                confirmed=True,
                standard="S2.1(a) reference plus matching surname",
                booking_ref=booking["booking_ref"],
                booking=booking,
                customer_id=booking.get("customer_id"),
                sender_email=sender_email,
                sender_display_name=display_name,
                reason=(
                    "Reference {} exists and carries passenger surname '{}', which the "
                    "contact supplied.".format(booking["booking_ref"], matched[0])
                ),
                candidates=candidates
                + [
                    {
                        "booking_ref": booking["booking_ref"],
                        "source": "reference supplied by the contact",
                        "outcome": "exists; surname matched",
                        "matched_surname": matched[0],
                    }
                ],
                unresolved_checks=unresolved,
                evidence=[s.source_id for s in ops.sources],
            )
            ref_matches.append(result)
            candidates = result.candidates
            continue

        invalid_reference = True
        candidates.append(
            {
                "booking_ref": booking["booking_ref"],
                "source": "reference supplied by the contact",
                "outcome": (
                    "exists, but carries no passenger with a surname the contact "
                    "supplied (S2.2)"
                ),
                "booking_surnames": sorted(booking_surnames),
                "surnames_supplied": sorted(surnames),
            }
        )

    if refs:
        if len(ref_matches) == 1 and not invalid_reference and not unresolved:
            result = ref_matches[0]
            result.candidates = candidates
            _apply_third_party_check(result)
            return result
        return IdentityResult(
            confirmed=False, standard=None, booking_ref=None, booking=None,
            customer_id=None, sender_email=sender_email,
            sender_display_name=display_name,
            reason=("S2.2: the supplied references are ambiguous, invalid, mismatched, "
                    "or could not all be verified. No booking was selected; confirm "
                    "the intended reference and passenger details."),
            candidates=candidates, unresolved_checks=unresolved,
            evidence=[s.source_id for s in ops.sources],
        )

    # --- S2.1(b): exact contact email on exactly one booking -----------------
    if sender_email:
        try:
            found = ops.search_bookings(sender_email)
            exact = [
                r
                for r in found.get("results", [])
                if "contact_email_exact" in (r.get("matched_on") or [])
                and r.get("contact_email") == sender_email
            ]
            for row in found.get("results", []):
                candidates.append(
                    {
                        "booking_ref": row.get("booking_ref"),
                        "source": "search by sender email address",
                        "matched_on": row.get("matched_on"),
                        "outcome": "candidate",
                    }
                )
            if len(exact) == 1:
                booking = ops.get_booking(exact[0]["booking_ref"])
                result = IdentityResult(
                    confirmed=True,
                    standard="S2.1(b) exact contact email on exactly one booking",
                    booking_ref=booking["booking_ref"],
                    booking=booking,
                    customer_id=booking.get("customer_id"),
                    sender_email=sender_email,
                    sender_display_name=display_name,
                    reason=(
                        "The sender's address matches the contact email held on "
                        "exactly one booking, {}.".format(booking["booking_ref"])
                    ),
                    candidates=candidates,
                    unresolved_checks=unresolved,
                    evidence=[s.source_id for s in ops.sources],
                )
                _apply_third_party_check(result)
                return result
            if len(exact) > 1:
                unresolved.append(
                    "sender email matches the contact email on {} bookings; S2.2 "
                    "treats more than one match as not confirmed".format(len(exact))
                )
        except OpsError as exc:
            unresolved.append(
                "booking search by email failed: {} {}".format(exc.status, exc.error_code)
            )

    # --- S2.1(c): normalised telephone on exactly one booking ----------------
    # The model cannot introduce a phone number absent from the message. Require
    # explicit international notation and refuse multiple distinct numbers here.
    phones = list(dict.fromkeys(normalise_phone(p) for p in re.findall(
        r"(?:\+|00)\d[\d ()-]{7,}\d", inbound_text)))
    if len(phones) > 1:
        unresolved.append("Multiple contact telephone numbers supplied; confirm which belongs to the sender.")
        phones = []
    for phone in phones:
        digits = normalise_phone(phone)
        if len(digits) < 9:
            continue
        try:
            found = ops.search_bookings(phone)
        except OpsError as exc:
            unresolved.append(
                "booking search by telephone failed: {} {}".format(
                    exc.status, exc.error_code
                )
            )
            continue
        exact = [
            r
            for r in found.get("results", [])
            if "contact_phone_match" in (r.get("matched_on") or [])
        ]
        for row in found.get("results", []):
            candidates.append(
                {
                    "booking_ref": row.get("booking_ref"),
                    "source": "search by telephone number in the message",
                    "matched_on": row.get("matched_on"),
                    "outcome": "candidate",
                }
            )
        if len(exact) == 1:
            booking = ops.get_booking(exact[0]["booking_ref"])
            if normalise_phone(booking.get("contact_phone")) != digits:
                unresolved.append("The retrieved booking does not confirm the exact telephone match.")
                continue
            result = IdentityResult(
                confirmed=True,
                standard="S2.1(c) telephone match on exactly one booking",
                booking_ref=booking["booking_ref"],
                booking=booking,
                customer_id=booking.get("customer_id"),
                sender_email=sender_email,
                sender_display_name=display_name,
                reason=(
                    "The telephone number given matches the contact telephone held "
                    "on exactly one booking, {}.".format(booking["booking_ref"])
                ),
                candidates=candidates,
                unresolved_checks=unresolved,
                evidence=[s.source_id for s in ops.sources],
            )
            _apply_third_party_check(result)
            return result

    # --- Not confirmed. Enumerate name candidates for the referral -----------
    name_queries: list[str] = []
    if extraction and extraction.sender_display_name:
        name_queries.append(extraction.sender_display_name)
    elif display_name:
        name_queries.append(display_name)
    for surname in sorted(surnames):
        if len(name_queries) >= 2:
            break
        if surname not in " ".join(name_queries).casefold():
            name_queries.append(surname)

    name_matches: list[dict[str, Any]] = []
    for query in name_queries[:2]:
        if ops.attempts_remaining() < 2:
            unresolved.append(
                "name search for {!r} skipped: per-case attempt budget nearly "
                "spent".format(query)
            )
            break
        try:
            found = ops.search_bookings(query)
        except OpsError as exc:
            unresolved.append(
                "name search failed: {} {}".format(exc.status, exc.error_code)
            )
            continue
        if not found.get("results"):
            unresolved.append(
                "name search for {!r} returned no booking at all".format(query)
            )
        for row in found.get("results", []):
            name_matches.append(row)
            candidates.append(
                {
                    "booking_ref": row.get("booking_ref"),
                    "source": "search by name {!r}".format(query),
                    "matched_on": row.get("matched_on"),
                    "passengers": row.get("passengers"),
                    "segments": row.get("segments"),
                    "outcome": (
                        "name match only -- S2.1 does not accept a name as a "
                        "confirmation standard"
                    ),
                }
            )
    if not name_queries:
        unresolved.append(
            "no name was available to search on: the contact supplied no display "
            "name and no passenger surname"
        )

    distinct = {c.get("booking_ref") for c in name_matches if c.get("booking_ref")}
    if not candidates:
        reason = (
            "Nothing the contact supplied matches any booking in the operational "
            "record: no usable reference, no matching contact email, no matching "
            "telephone. S2.2 applies."
        )
    elif name_matches:
        reason = (
            "The contact can only be matched by name, and {} booking(s) carry that "
            "name. S2.1 does not accept a name as a confirmation standard, and S2.2 "
            "treats more than one match as not confirmed.".format(len(distinct))
        )
    else:
        reason = (
            "The details supplied partially match the record but do not meet any "
            "S2.1 standard, so identity is not confirmed (S2.2)."
        )

    return IdentityResult(
        confirmed=False,
        standard=None,
        booking_ref=None,
        booking=None,
        customer_id=None,
        sender_email=sender_email,
        sender_display_name=display_name,
        reason=reason,
        candidates=candidates,
        unresolved_checks=unresolved,
        evidence=[s.source_id for s in ops.sources],
    )


def _apply_third_party_check(result: IdentityResult) -> None:
    """S2.3: a person who is neither a named passenger nor the booker of record.

    "Booker of record" is read as the holder of the booking's contact email, since
    that is the only booker identifier the record carries. Documented as an
    assumption in DECISIONS.md.
    """
    booking = result.booking or {}
    contact_email = booking.get("contact_email") or ""
    sender = result.sender_email or ""
    if sender and contact_email and sender == contact_email:
        return

    names = {
        "{} {}".format(p.get("given_name", ""), p.get("surname", "")).strip().casefold()
        for p in booking.get("passengers", [])
    }
    display = (result.sender_display_name or "").casefold().strip()
    if display and display in names:
        return

    result.third_party_blocked = True
    result.confirmed = False
    result.reason = (
        "The contact is neither the booking's contact email holder nor, on the "
        "evidence available, a named passenger. S2.3 permits no information and no "
        "action at their request. " + result.reason
    )
