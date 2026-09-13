"""Identity resolution (APCP-2026-04 S2).

The property that matters most: where identity is not confirmed, nothing is
actionable. `test_executor.py` checks the other half -- that no write happens.
"""

from __future__ import annotations

from aerlink.identity import normalise_phone, parse_address, resolve_identity


def _extraction(**overrides):
    """A minimal Extraction stand-in; only the fields identity reads are set."""
    from aerlink.schemas import Extraction, Preferences

    base = dict(
        language="en",
        sender_display_name=None,
        booking_refs=[],
        emails_in_body=[],
        phone_numbers_in_body=[],
        surnames_claimed=[],
        flight_refs=[],
        requests=[],
        preferences=Preferences(
            explicitly_asked_for_earliest_available=False,
            arrive_by_local=None,
            travel_date_iso=None,
            depart_not_before_local=None,
            alternative_origin_airports=[],
            quote=None,
        ),
        passenger_fact_claims=[],
        embedded_instructions=[],
        unclear_points=[],
    )
    base.update(overrides)
    return Extraction(**base)


def _ref(value: str, quote: str = "my booking reference"):
    from aerlink.schemas import BookingRefClue

    return BookingRefClue(value=value, quote=quote)


def test_reference_plus_surname_confirms(ops):
    result = resolve_identity(
        ops,
        extraction=_extraction(
            booking_refs=[_ref("TST-000001")], surnames_claimed=["Lovelace"]
        ),
        inbound_text="My booking is TST-000001, name Lovelace.",
        meta_from="Ada Lovelace <ada.lovelace@example.test>",
    )
    assert result.confirmed
    assert result.standard.startswith("S2.1(a)")
    assert result.booking_ref == "TST-000001"


def test_reference_without_a_matching_surname_does_not_confirm(ops):
    """S2.2: a reference that carries no passenger with the supplied surname."""
    result = resolve_identity(
        ops,
        extraction=_extraction(
            booking_refs=[_ref("TST-000001")], surnames_claimed=["Babbage"]
        ),
        inbound_text="Booking TST-000001, name Babbage.",
        meta_from="Charles Babbage <charles.babbage@example.test>",
    )
    assert not result.confirmed
    assert any(
        "carries no passenger" in c.get("outcome", "") for c in result.candidates
    )


def test_nonexistent_reference_is_looked_up_and_reported(ops):
    """"That reference does not exist" must be a checked statement, not an assumption."""
    result = resolve_identity(
        ops,
        extraction=_extraction(booking_refs=[_ref("BA-99201")], surnames_claimed=["Lindqvist"]),
        inbound_text="reference BA-99201",
        meta_from="Peter Lindqvist <p.lindqvist@example.test>",
    )
    assert not result.confirmed
    assert any(c["booking_ref"] == "BA-99201" for c in result.candidates)
    assert any("no such booking" in c["outcome"] for c in result.candidates)


def test_exact_contact_email_on_exactly_one_booking_confirms(ops):
    result = resolve_identity(
        ops,
        extraction=_extraction(),
        inbound_text="No reference to hand.",
        meta_from="Grace Hopper <grace.hopper@example.test>",
    )
    assert result.confirmed
    assert result.standard.startswith("S2.1(b)")
    assert result.booking_ref == "TST-000002"


def test_a_name_alone_never_confirms_even_when_unique(ops):
    """S2.1 lists three standards and a name is not one of them."""
    result = resolve_identity(
        ops,
        extraction=_extraction(
            sender_display_name="Ada Lovelace", surnames_claimed=["Lovelace"]
        ),
        inbound_text="I am Ada Lovelace, I have lost my reference.",
        meta_from="ada.personal@elsewhere.test",
    )
    assert not result.confirmed
    assert result.booking_ref is None
    assert any("name match only" in c.get("outcome", "") for c in result.candidates)


def test_same_name_on_two_bookings_does_not_confirm(ops):
    """S2.2: more than one match is not a match."""
    result = resolve_identity(
        ops,
        extraction=_extraction(sender_display_name="Jo Doe", surnames_claimed=["Doe"]),
        inbound_text="My flight was late on Tuesday. Name is Jo Doe.",
        meta_from="jo.doe.personal@elsewhere.test",
    )
    assert not result.confirmed
    refs = {c["booking_ref"] for c in result.candidates}
    assert {"TST-000005", "TST-000006"} <= refs, "both candidates must be inspected"
    assert "more than one match" in result.reason or "2 booking" in result.reason


def test_nothing_matching_at_all_is_reported_honestly(ops):
    result = resolve_identity(
        ops,
        extraction=_extraction(sender_display_name="Nobody Here"),
        inbound_text="Please help.",
        meta_from="nobody@elsewhere.test",
    )
    assert not result.confirmed
    assert "Nothing the contact supplied matches" in result.reason


def test_third_party_contact_is_blocked(ops):
    """S2.3: not a named passenger and not the booker of record."""
    result = resolve_identity(
        ops,
        extraction=_extraction(
            booking_refs=[_ref("TST-000003")], surnames_claimed=["Clarke"]
        ),
        inbound_text="I am calling about my aunt Joan Clarke, booking TST-000003.",
        meta_from="Nosy Nephew <nephew@elsewhere.test>",
    )
    assert not result.confirmed
    assert result.third_party_blocked
    assert "S2.3" in result.reason


def test_named_passenger_writing_from_another_address_is_not_blocked(ops):
    result = resolve_identity(
        ops,
        extraction=_extraction(
            booking_refs=[_ref("TST-000003")], surnames_claimed=["Turing"]
        ),
        inbound_text="Booking TST-000003, Turing.",
        meta_from="Alan Turing <alan.work@elsewhere.test>",
    )
    assert result.confirmed
    assert not result.third_party_blocked


def test_the_airline_name_in_a_header_is_not_spent_as_a_lookup(ops):
    """A backstop regex sweep must not burn attempts on "Aerlink Customer Care"."""
    before = ops.attempts_used
    resolve_identity(
        ops,
        extraction=None,
        inbound_text="To: Aerlink Customer Care <care@aerlink.example>\nHello.",
        meta_from="someone@elsewhere.test",
    )
    # One search by email; no lookup of a reference called "AERLINK".
    assert ops.attempts_used - before <= 2


def test_address_and_phone_parsing():
    assert parse_address("Ada Lovelace <a@b.test>") == ("Ada Lovelace", "a@b.test")
    assert parse_address("a@b.test") == (None, "a@b.test")
    assert parse_address(None) == (None, None)
    assert normalise_phone("+44 7700 900001") == "447700900001"
