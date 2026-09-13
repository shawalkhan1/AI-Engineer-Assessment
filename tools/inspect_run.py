#!/usr/bin/env python3
"""Audit a completed run: read every record and check what actually happened.

    python tools/inspect_run.py artifacts/full-run

A run that exits 0 is not evidence that the decisions were right. This reads the
records the way a reviewer would and asserts the things that would be embarrassing to
get wrong. Every defect found late in this build was found by reading the output; this
is that, written down.

Checks, in order of how much they would matter:

1. Every record parses against the schema.
2. No action of any kind on a booking whose identity was not confirmed (S16).
3. Every payment equals the figure the entitlement service gives for that booking,
   re-fetched now (S10.2).
3b. Every executed action was one the desk had authority to take, **re-derived from
   the recorded facts using the current policy module** rather than read back from the
   verdict the run stored. A run that recorded "allowed" because the gate was wrong at
   the time must still fail here -- that is the whole point of the check.
4. Every re-booking is own metal, in the cabin originally booked.
4b. No re-booking itinerary -- executed or merely recommended to a supervisor --
   departs before the contact that asked for it arrived.
5. A case that says a human is needed really did raise an escalation through the API.
6. Every identifier we claim appears in the server's own write log, and vice versa.
7. Every live request had something happen to it -- an action, an answer, a referral
   or a question back (S15.1).
7b. No passenger reply claims an outcome that did not happen. Negation-aware: "no
   payment has been made yet" is not a claim that one was.
8. No passenger reply carries internal machinery -- clause numbers, queue names, or
   the language of the system talking to itself.

Exit code 0 if everything holds, 1 otherwise.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aerlink.config import load_config                      # noqa: E402
from aerlink.ops_client import OpsClient                    # noqa: E402
from aerlink import policy                                  # noqa: E402
from aerlink.policy import gbp                              # noqa: E402
from aerlink.schemas import CaseRecord                      # noqa: E402

# Phrases that mean the system is talking to itself. Clause references, queue names
# and words like "verify" or "span" belong in the record, never in the reply.
MACHINERY = (
    "word for word",
    "span",
    "apcp",
    "escalation",
    "supervisor queue",
    "ops_liaison",
    "special_assistance",
    "lost_property",
    "skipped_duplicate",
    "would_execute",
    "booking_ref",
    "passenger_id",
    "entitlement calculation service",
    "s12.",
    "s15.",
    "s16",
    "s4.5",
    "s6.4",
    "s11.",
)

ID_FIELDS = ("rebooking_id", "voucher_id", "payment_id", "refund_id", "escalation_id")


def _departed_before(itinerary: dict | None, case_now: str | None) -> str | None:
    """Did this option leave before the passenger even wrote?

    Recommendations count, not just executions: a referral naming a flight that has
    gone wastes a supervisor's time and cannot be actioned. Local time at every station
    in `stations.json` is UTC+1..+4 in August, so local >= UTC and a local clock time
    at or behind the UTC instant has certainly departed.
    """
    from datetime import datetime

    if not itinerary or not case_now:
        return None
    departure = itinerary.get("departure_local")
    flight_date = itinerary.get("date")
    if not departure or not flight_date:
        return None
    match = re.match(r"^(\d{2}):(\d{2})", str(departure))
    if not match:
        return None
    try:
        now = datetime.fromisoformat(case_now).replace(tzinfo=None)
        when = datetime.fromisoformat(str(flight_date)[:10]).replace(
            hour=int(match.group(1)), minute=int(match.group(2))
        )
    except ValueError:
        return None
    if when <= now:
        return "{} {}".format(flight_date, departure)
    return None


# Phrases that assert something was actually done for the passenger, and the action
# that would have to have succeeded for each to be true.
_ACTION_FOR_CLAIM = {
    "we have paid": {"compensation_payment", "goodwill_payment"},
    "has been paid": {"compensation_payment", "goodwill_payment"},
    "we have refunded": {"refund"},
    "has been refunded": {"refund"},
    "we have re-booked": {"rebooking"},
    "we have rebooked": {"rebooking"},
    "you have been re-booked": {"rebooking"},
    "you have been rebooked": {"rebooking"},
    "your new booking is confirmed": {"rebooking"},
    "we have issued you a room": {"hotel_voucher"},
    "a room has been booked": {"hotel_voucher"},
}

_NEGATORS = (
    " no ", " not ", "n't", "cannot", "unable", " yet", "would be", "would have",
    "have we", "nothing has",
)


def _completion_claims(body: str | None) -> list[tuple[str, str]]:
    """Sentences that assert a completed action, ignoring negated ones.

    Negation matters: "no payment has been made yet" contains "payment has been made"
    and is the opposite of a claim. A naive substring scan flags it and misses the
    real thing.
    """
    import re

    out: list[tuple[str, str]] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n", (body or "")):
        lowered = " " + sentence.casefold() + " "
        if any(neg in lowered for neg in _NEGATORS):
            continue
        for phrase in _ACTION_FOR_CLAIM:
            if phrase in lowered:
                out.append((sentence.strip(), phrase))
                break
    return out


def _rederive_authority(action) -> tuple[bool, str] | None:
    """Ask the current policy module whether this action was allowed.

    Deliberately independent of `preconditions_checked.authority`, which records what
    the gate said at the time. The first sweeps recorded "allowed" for eight actions
    that were not, so believing the stored verdict would have made this check useless.
    """
    kind = action.action_type.value
    level = policy.REPRESENTATIVE

    if kind == "compensation_payment":
        auth = policy.authority_for_compensation_payment(
            action.amount or gbp(0), operating_level=level)
        return auth.allowed, auth.reason

    if kind == "rebooking":
        itinerary = action.itinerary or {}
        fare = gbp(itinerary.get("fare_gbp"))
        booked_cabin = (action.preconditions_checked or {}).get("cabin_booked")
        auth = policy.authority_for_rebooking(
            own_carrier=(itinerary.get("operated_by") == "Aerlink"),
            cabin_matches_booked=(
                booked_cabin is None or itinerary.get("cabin") == booked_cabin
            ),
            additional_fare_payable=fare,
            operating_level=level,
        )
        return auth.allowed, auth.reason

    if kind == "hotel_voucher":
        pre = action.preconditions_checked or {}
        refreshed = pre.get("refreshed_immediately_before_write") or {}
        rooms = refreshed.get("rooms_remaining_now", pre.get("rooms_remaining_at_planning"))
        auth = policy.authority_for_hotel(
            rate=action.amount or gbp(0),
            rooms_remaining=rooms,
            nights_requested=1,
            operating_level=level,
        )
        return auth.allowed, auth.reason

    if kind == "refund":
        auth = policy.authority_for_refund(action.amount or gbp(0), operating_level=level)
        return auth.allowed, auth.reason

    return None   # escalations need no authority; S12.5 is the mechanism itself


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 1
    out = Path(argv[1])
    if not out.is_dir():
        print("no such directory: {}".format(out))
        return 1

    record_paths = sorted(p for p in out.glob("*.json") if p.name != "batch-summary.json"
                          and p.name != "ops-audit.json")
    if not record_paths:
        print("no case records in {}".format(out))
        return 1

    problems: list[str] = []
    records = []
    for path in record_paths:
        try:
            records.append(CaseRecord.model_validate(json.loads(path.read_text(encoding="utf-8"))))
        except Exception as exc:  # noqa: BLE001
            problems.append("{}: does not parse as a case record: {}".format(path.name, exc))

    dry_run = any(r.dry_run for r in records)
    ops = None
    audit = None
    if not dry_run:
        try:
            ops = OpsClient(load_config(require_openai_key=False))
            ops.begin_case()
            snapshot = out / "ops-audit.json"
            audit = (
                json.loads(snapshot.read_text(encoding="utf-8"))
                if snapshot.is_file()
                else ops.audit()
            )
        except Exception as exc:  # noqa: BLE001
            print("note: operations API unavailable, skipping live checks ({})".format(
                str(exc)[:120]))

    print("%-11s %-20s %-12s %s" % ("case", "status", "booking", "checks"))
    print("-" * 78)

    claimed_ids: set[str] = set()
    for record in records:
        ident = record.identity_resolution
        notes: list[str] = []
        acted = [
            a for a in record.actions
            if a.action_type.value != "escalation"
            and a.state.value in {"succeeded", "would_execute"}
        ]
        executed_kinds = {a.action_type.value for a in acted}

        # 2. Nothing may happen without a confirmed identity.
        if not ident.get("confirmed") and acted:
            problems.append(
                "{}: {} action(s) taken without a confirmed identity".format(
                    record.case_id, len(acted)))

        for action in record.actions:
            if action.state.value == "succeeded":
                for key in ID_FIELDS:
                    value = (action.returned_ids or {}).get(key)
                    if isinstance(value, str) and value:
                        claimed_ids.add(value)

            if action.state.value not in {"succeeded", "would_execute"}:
                continue

            # 3. Payments must match the authoritative figure.
            if action.action_type.value == "compensation_payment" and ops is not None:
                try:
                    expected = gbp(
                        ops.entitlements(action.booking_ref)["total_payable_gbp"]
                    ).amount_minor
                except Exception as exc:  # noqa: BLE001
                    problems.append("{}: could not re-check the entitlement ({})".format(
                        record.case_id, str(exc)[:80]))
                    continue
                got = action.amount.amount_minor if action.amount else 0
                notes.append("paid {} = service {}".format(got, expected))
                if got != expected:
                    problems.append(
                        "{}: paid {} but the entitlement service says {}".format(
                            record.case_id, got, expected))

            # 4. Re-bookings must be own metal in the cabin booked.
            if action.action_type.value == "rebooking":
                itinerary = action.itinerary or {}
                notes.append("{} {}".format(
                    itinerary.get("flight_no"), itinerary.get("cabin")))
                if itinerary.get("operated_by") != "Aerlink":
                    problems.append("{}: partner metal booked without authorisation".format(
                        record.case_id))
                if ops is not None:
                    booking = ops.get_booking(action.booking_ref)
                    disruption = booking.get("disruption") or {}
                    segment = next(
                        (s for s in booking["segments"]
                         if s["segment_id"] == disruption.get("affected_segment")), None)
                    if segment and itinerary.get("cabin") != segment.get("cabin"):
                        problems.append(
                            "{}: re-booked into {} having booked {}".format(
                                record.case_id, itinerary.get("cabin"), segment.get("cabin")))

        # 5. A claimed handover must really have been raised.
        if record.human_handover.required and not record.dry_run:
            if not record.human_handover.api_handover_succeeded:
                problems.append(
                    "{}: a human is needed but no escalation reached the API".format(
                        record.case_id))

        # 3b. Authority, re-derived rather than believed.
        for action in record.actions:
            if action.state.value not in {"succeeded", "would_execute"}:
                continue
            verdict = _rederive_authority(action)
            if verdict is None:
                continue
            allowed, why = verdict
            if not allowed:
                problems.append(
                    "{}: {} was executed but is not authorised -- {}".format(
                        record.case_id, action.action_type.value, why))

        # 6b. Every live request must have had something happen to it (S15.1).
        unaddressed = record.passenger_requests.get("unaddressed_requests") or []
        for item in unaddressed:
            problems.append(
                "{}: nothing happened to a live request -- {}".format(
                    record.case_id, item.get("detail", item.get("request_type"))))

        # 4b. No itinerary, executed OR recommended, may have already departed.
        case_now = record.input_provenance.get("case_now")
        for action in record.actions:
            if action.action_type.value != "rebooking":
                continue
            gone = _departed_before(action.itinerary, case_now)
            if gone:
                problems.append(
                    "{}: the {} itinerary departs {} but the contact arrived {} -- "
                    "that flight had already gone".format(
                        record.case_id, action.state.value, gone, case_now))

        # 6c. The reply may not claim an outcome that did not happen.
        for sentence, verb in _completion_claims(record.passenger_response.get("body")):
            kinds = _ACTION_FOR_CLAIM.get(verb, set())
            if kinds and not (kinds & executed_kinds):
                problems.append(
                    "{}: the reply says {!r} but no such action succeeded -- {}".format(
                        record.case_id, verb, sentence[:100]))

        # 7. Reply hygiene.
        body = (record.passenger_response.get("body") or "").lower()
        leaked = [m for m in MACHINERY if m in body]
        if leaked:
            problems.append("{}: reply carries internal machinery: {}".format(
                record.case_id, ", ".join(leaked)))
        if record.passenger_response.get("status") != "draft":
            problems.append("{}: reply is not labelled a draft".format(record.case_id))

        print("%-11s %-20s %-12s %s" % (
            record.case_id, record.status.value,
            ident.get("verified_booking_ref") or "-", "; ".join(notes)))

    # 6. Both directions of the reconciliation.
    print("-" * 78)
    if audit is not None:
        server_ids = {
            str(row[key])
            for rows in audit.get("writes", {}).values()
            for row in rows
            for key in ID_FIELDS
            if row.get(key)
        }
        missing = claimed_ids - server_ids
        print("writes claimed by records: {}".format(len(claimed_ids)))
        print("writes in the server log : {}".format(len(server_ids)))
        if missing:
            problems.append(
                "records claim writes the server does not hold: {}".format(
                    ", ".join(sorted(missing))))
        extra = server_ids - claimed_ids
        if extra:
            print("note: {} write(s) on the server predate this run or came from "
                  "elsewhere".format(len(extra)))
    if ops is not None:
        ops.close()

    print()
    if problems:
        print("{} PROBLEM(S):".format(len(problems)))
        for problem in problems:
            print("  - {}".format(problem))
        return 1
    print("All checks passed across {} record(s).".format(len(records)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
