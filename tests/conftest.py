"""Test doubles.

The fake operations API is our own fixture, not a copy of the assessment data: it
serves invented bookings whose expected entitlements were worked out by hand from
env/data/policy.md, so a test that passes says the *policy* was applied correctly
rather than that a supplied answer was reproduced.

Nothing here touches the network, the real server, or an OpenAI key.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from aerlink.config import Config, ModelPrice
from aerlink.journal import Journal
from aerlink.ops_client import OpsClient

# ---------------------------------------------------------------------------
# Fixture world
# ---------------------------------------------------------------------------

# Band B (LIS-LHR 1585 km), TECHNICAL, arrival delay 220 min, re-routed.
#   S5.3 Band B  = GBP 350
#   S5.4 re-routed and 220 < 300  -> halved -> GBP 175
#   S9.1 Band B downgrade = 50% of the AFFECTED SEGMENT fare 480 -> GBP 240
#   S9.3 both are paid                            -> total GBP 415
BOOKING_DOWNGRADE = {
    "booking_ref": "TST-000001",
    "customer_id": "CUS-90001",
    "contact_email": "ada.lovelace@example.test",
    "contact_phone": "+447700900001",
    "tier": "SILVER",
    "total_paid_gbp": 1240.0,
    "special_requests": "",
    "fare_breakdown": {
        "base_fares_gbp": 910.0,
        "taxes_and_charges_gbp": 220.0,
        "ancillaries_gbp": 110.0,
    },
    "passengers": [
        {
            "passenger_id": "P1",
            "given_name": "Ada",
            "surname": "Lovelace",
            "passenger_type": "ADULT",
            "age": 45,
            "assistance": None,
            "cabin_booked": "BUSINESS",
            "cabin_flown": "ECONOMY",
        }
    ],
    "segments": [
        {
            "segment_id": "S1",
            "flight_no": "ZZ100",
            "date": "2026-08-01",
            "origin": "LIS",
            "destination": "LHR",
            "segment_fare_gbp": 480.0,
            "cabin": "BUSINESS",
            "is_affected": True,
        }
    ],
    "final_destination": "LHR",
    "disruption": {
        "affected_segment": "S1",
        "rerouted_onto": "ZZ104:2026-08-01",
        "arrival_delay_minutes": 220,
        "informed_days_before": 0,
    },
}

# Cancelled, not yet re-routed: arrival delay unknown, so not assessable.
BOOKING_STRANDED = {
    "booking_ref": "TST-000002",
    "customer_id": "CUS-90002",
    "contact_email": "grace.hopper@example.test",
    "contact_phone": "+447700900002",
    "tier": "GOLD",
    "total_paid_gbp": 420.0,
    "special_requests": "",
    "passengers": [
        {
            "passenger_id": "P1",
            "given_name": "Grace",
            "surname": "Hopper",
            "passenger_type": "ADULT",
            "age": 38,
            "assistance": None,
            "cabin_booked": "ECONOMY",
            "cabin_flown": None,
        }
    ],
    "segments": [
        {
            "segment_id": "S1",
            "flight_no": "ZZ200",
            "date": "2026-08-06",
            "origin": "LGW",
            "destination": "FCO",
            "segment_fare_gbp": 320.0,
            "cabin": "ECONOMY",
            "is_affected": True,
        }
    ],
    "final_destination": "FCO",
    "disruption": {
        "affected_segment": "S1",
        "rerouted_onto": None,
        "arrival_delay_minutes": None,
        "informed_days_before": 0,
    },
}

# Two passengers, one of whom has a confirmed assistance requirement (S14.4).
BOOKING_GROUP = {
    "booking_ref": "TST-000003",
    "customer_id": "CUS-90003",
    "contact_email": "alan.turing@example.test",
    "contact_phone": "+447700900003",
    "tier": "NONE",
    "total_paid_gbp": 600.0,
    "special_requests": "WCHR requested for Joan Clarke, both directions. Confirmed.",
    "passengers": [
        {
            "passenger_id": "P1",
            "given_name": "Alan",
            "surname": "Turing",
            "passenger_type": "ADULT",
            "age": 41,
            "assistance": None,
            "cabin_booked": "ECONOMY",
            "cabin_flown": None,
        },
        {
            "passenger_id": "P2",
            "given_name": "Joan",
            "surname": "Clarke",
            "passenger_type": "ADULT",
            "age": 71,
            "assistance": "WCHR",
            "cabin_booked": "ECONOMY",
            "cabin_flown": None,
        },
    ],
    "segments": [
        {
            "segment_id": "S1",
            "flight_no": "ZZ200",
            "date": "2026-08-06",
            "origin": "LGW",
            "destination": "FCO",
            "segment_fare_gbp": 500.0,
            "cabin": "ECONOMY",
            "is_affected": True,
        }
    ],
    "final_destination": "FCO",
    "disruption": {
        "affected_segment": "S1",
        "rerouted_onto": None,
        "arrival_delay_minutes": None,
        "informed_days_before": 0,
    },
}

# Young Traveller Programme: S13.2 blocks everything.
BOOKING_YTP = {
    "booking_ref": "TST-000004",
    "customer_id": "CUS-90004",
    "contact_email": "parent@example.test",
    "contact_phone": "+447700900004",
    "tier": "NONE",
    "total_paid_gbp": 500.0,
    "special_requests": "YTP escort booked both ends.",
    "passengers": [
        {
            "passenger_id": "P1",
            "given_name": "Kit",
            "surname": "Brunel",
            "passenger_type": "YTP",
            "age": 11,
            "assistance": None,
            "cabin_booked": "ECONOMY",
            "cabin_flown": None,
        }
    ],
    "segments": [
        {
            "segment_id": "S1",
            "flight_no": "ZZ200",
            "date": "2026-08-06",
            "origin": "LGW",
            "destination": "FCO",
            "segment_fare_gbp": 500.0,
            "cabin": "ECONOMY",
            "is_affected": True,
        }
    ],
    "final_destination": "FCO",
    "disruption": {
        "affected_segment": "S1",
        "rerouted_onto": None,
        "arrival_delay_minutes": None,
        "informed_days_before": 0,
    },
}

# Two bookings carrying the same passenger name: identity must NOT resolve.
BOOKING_TWIN_A = {
    "booking_ref": "TST-000005",
    "customer_id": "CUS-90005",
    "contact_email": "j.doe.one@example.test",
    "contact_phone": "+447700900005",
    "tier": "NONE",
    "total_paid_gbp": 100.0,
    "special_requests": "",
    "passengers": [
        {
            "passenger_id": "P1",
            "given_name": "Jo",
            "surname": "Doe",
            "passenger_type": "ADULT",
            "age": 42,
            "assistance": None,
            "cabin_booked": "ECONOMY",
            "cabin_flown": None,
        }
    ],
    "segments": [
        {
            "segment_id": "S1",
            "flight_no": "ZZ300",
            "date": "2026-08-04",
            "origin": "LHR",
            "destination": "DUB",
            "segment_fare_gbp": 90.0,
            "cabin": "ECONOMY",
            "is_affected": True,
        }
    ],
    "final_destination": "DUB",
    "disruption": None,
}
BOOKING_TWIN_B = dict(
    BOOKING_TWIN_A,
    booking_ref="TST-000006",
    customer_id="CUS-90006",
    contact_email="j.doe.two@example.test",
    contact_phone="+447700900006",
)

BOOKINGS = {
    b["booking_ref"]: b
    for b in (
        BOOKING_DOWNGRADE,
        BOOKING_STRANDED,
        BOOKING_GROUP,
        BOOKING_YTP,
        BOOKING_TWIN_A,
        BOOKING_TWIN_B,
    )
}

FLIGHTS = {
    "ZZ100:2026-08-01": {
        "flight_no": "ZZ100",
        "date": "2026-08-01",
        "origin": "LIS",
        "destination": "LHR",
        "distance_km": 1585,
        "scheduled_departure": "2026-08-01T09:00:00Z",
        "scheduled_arrival": "2026-08-01T11:40:00Z",
        "status": "CANCELLED",
        "cause_code": "TECHNICAL",
        "cause_note": "Cabin pressurisation fault.",
        "departure_delay_minutes": None,
        "operated_by": "Aerlink",
    },
    "ZZ200:2026-08-06": {
        "flight_no": "ZZ200",
        "date": "2026-08-06",
        "origin": "LGW",
        "destination": "FCO",
        "distance_km": 1435,
        "scheduled_departure": "2026-08-06T19:00:00Z",
        "scheduled_arrival": "2026-08-06T22:30:00Z",
        "status": "CANCELLED",
        "cause_code": "TECHNICAL",
        "cause_note": "Nosewheel steering fault.",
        "departure_delay_minutes": None,
        "operated_by": "Aerlink",
    },
    "ZZ300:2026-08-04": {
        "flight_no": "ZZ300",
        "date": "2026-08-04",
        "origin": "LHR",
        "destination": "DUB",
        "distance_km": 449,
        "scheduled_departure": "2026-08-04T07:00:00Z",
        "scheduled_arrival": "2026-08-04T08:20:00Z",
        "status": "DELAYED",
        "cause_code": "WEATHER",
        "cause_note": "Fog.",
        "departure_delay_minutes": 200,
        "operated_by": "Aerlink",
    },
}

ENTITLEMENTS = {
    # Worked by hand from policy.md; see the comment above BOOKING_DOWNGRADE.
    "TST-000001": {
        "booking_ref": "TST-000001",
        "status": "ASSESSED",
        "authoritative": True,
        "policy_version": "APCP-2026-04 v11.3",
        "journey": {
            "origin": "LIS",
            "final_destination": "LHR",
            "great_circle_distance_km": 1585,
            "band": "B",
            "affected_flight": "ZZ100:2026-08-01",
            "cause_code": "TECHNICAL",
            "cause_is_extraordinary": False,
            "flight_status": "CANCELLED",
            "departure_delay_minutes": None,
            "arrival_delay_minutes_at_final_destination": 220,
            "rerouted_onto": "ZZ104:2026-08-01",
        },
        "compensation": {
            "amount_gbp": 175.0,
            "status": "PAYABLE",
            "reasoning": ["S5.3: Band B is GBP 350.", "S5.4: halved to GBP 175."],
        },
        "duty_of_care": {"triggered": True, "basis": "S4.1: cancelled.", "entitlements": []},
        "passengers": [
            {
                "passenger_id": "P1",
                "name": "Ada Lovelace",
                "passenger_type": "ADULT",
                "compensation_gbp": 175.0,
                "compensation_status": "PAYABLE",
                "downgrade_reimbursement_gbp": 240.0,
                "downgrade_reasoning": ["S9.4: 50% of the GBP 480.00 segment fare."],
                "flags": [],
                "total_payable_gbp": 415.0,
            }
        ],
        "total_payable_gbp": 415.0,
    },
}

for _ref in ("TST-000002", "TST-000003", "TST-000004"):
    ENTITLEMENTS[_ref] = {
        "booking_ref": _ref,
        "status": "ASSESSED",
        "authoritative": True,
        "policy_version": "APCP-2026-04 v11.3",
        "journey": {
            "origin": "LGW",
            "final_destination": "FCO",
            "great_circle_distance_km": 1435,
            "band": "A",
            "affected_flight": "ZZ200:2026-08-06",
            "cause_code": "TECHNICAL",
            "cause_is_extraordinary": False,
            "flight_status": "CANCELLED",
            "departure_delay_minutes": None,
            "arrival_delay_minutes_at_final_destination": None,
            "rerouted_onto": None,
        },
        "compensation": {
            "amount_gbp": 0.0,
            "status": "INSUFFICIENT_DATA",
            "reasoning": ["S5.2: arrival delay not yet known."],
        },
        "duty_of_care": {
            "triggered": True,
            "basis": "S4.1: flight cancelled, care owed immediately.",
            "entitlements": [
                {"type": "hotel", "cap_gbp": 180.0, "per": "room per night", "max_nights": 3}
            ],
        },
        "passengers": [
            {
                "passenger_id": p["passenger_id"],
                "name": "{} {}".format(p["given_name"], p["surname"]),
                "passenger_type": p["passenger_type"],
                "compensation_gbp": 0.0,
                "compensation_status": "INSUFFICIENT_DATA",
                "downgrade_reimbursement_gbp": 0.0,
                "downgrade_reasoning": [],
                "flags": [],
                "total_payable_gbp": 0.0,
            }
            for p in BOOKINGS[_ref]["passengers"]
        ],
        "total_payable_gbp": 0.0,
    }

CUSTOMERS = {
    "CUS-90001": {"customer_id": "CUS-90001", "tier": "SILVER", "history": []},
    "CUS-90002": {"customer_id": "CUS-90002", "tier": "GOLD", "history": []},
    "CUS-90003": {"customer_id": "CUS-90003", "tier": "NONE", "history": []},
    "CUS-90004": {"customer_id": "CUS-90004", "tier": "NONE", "history": []},
    "CUS-90005": {"customer_id": "CUS-90005", "tier": "NONE", "history": []},
    "CUS-90006": {"customer_id": "CUS-90006", "tier": "NONE", "history": []},
}

HOTELS = {
    "LGW:2026-08-06": {
        "station": "LGW",
        "night": "2026-08-06",
        "provider": "Test Lodging",
        "rooms_total": 10,
        "rooms_remaining": 1,
        "rate_gbp": 165.0,
    },
    "LGW:2026-08-07": {
        "station": "LGW",
        "night": "2026-08-07",
        "provider": "Test Lodging",
        "rooms_total": 10,
        "rooms_remaining": 0,
        "rate_gbp": 165.0,
    },
}


def availability_rows(origin: str, destination: str, date_iso: str) -> list[dict[str, Any]]:
    """A small, fully predictable inventory."""
    return [
        {
            "option_id": "OPT-EARLY",
            "flight_no": "ZZ900",
            "operated_by": "Aerlink",
            "origin": origin,
            "destination": destination,
            "date": date_iso,
            "departure_local": "06:00",
            "arrival_local": "09:00",
            "cabin": "ECONOMY",
            "seats_available": 4,
            "fare_gbp": 12.50,
        },
        {
            "option_id": "OPT-FULL",
            "flight_no": "ZZ901",
            "operated_by": "Aerlink",
            "origin": origin,
            "destination": destination,
            "date": date_iso,
            "departure_local": "05:00",
            "arrival_local": "08:00",
            "cabin": "ECONOMY",
            "seats_available": 0,
            "fare_gbp": 5.00,
        },
        {
            "option_id": "OPT-BUSINESS",
            "flight_no": "ZZ902",
            "operated_by": "Aerlink",
            "origin": origin,
            "destination": destination,
            "date": date_iso,
            "departure_local": "07:00",
            "arrival_local": "10:00",
            "cabin": "BUSINESS",
            "seats_available": 9,
            "fare_gbp": 0.0,
        },
        {
            "option_id": "OPT-LATE",
            "flight_no": "ZZ903",
            "operated_by": "Aerlink",
            "origin": origin,
            "destination": destination,
            "date": date_iso,
            "departure_local": "20:00",
            "arrival_local": "23:30",
            "cabin": "ECONOMY",
            "seats_available": 6,
            "fare_gbp": 8.00,
        },
        {
            "option_id": "OPT-PARTNER",
            "flight_no": "IB123",
            "operated_by": "Iberia",
            "origin": origin,
            "destination": destination,
            "date": date_iso,
            "departure_local": "06:30",
            "arrival_local": "09:30",
            "cabin": "ECONOMY",
            "seats_available": 5,
            "fare_gbp": 410.0,
        },
    ]


class FakeOpsServer:
    """An in-memory stand-in for env/ops_server.py, with the behaviours that matter.

    Notably it reproduces the real server's first-attempt 503 on availability, and it
    does *not* validate `option_id` or seat counts on POST /rebooking -- because the
    real one does not either, and our guard has to be what stops a false confirmation.
    """

    def __init__(self) -> None:
        self.writes: dict[str, list[dict[str, Any]]] = {
            "rebookings": [],
            "refunds": [],
            "payments": [],
            "hotel_vouchers": [],
            "escalations": [],
        }
        self.hotel_remaining = {k: v["rooms_remaining"] for k, v in HOTELS.items()}
        self.availability_attempts: dict[str, int] = {}
        self.seq = 0
        self.request_log: list[tuple[str, str]] = []
        # Test switches
        self.availability_flaky_first_attempt = True
        self.availability_override: dict[str, list[dict[str, Any]]] = {}
        self.fail_next_write_with: Exception | None = None
        self.silently_commit_then_fail = False

    def next_id(self, prefix: str) -> str:
        self.seq += 1
        return "{}-{:05d}".format(prefix, self.seq)

    @property
    def write_count(self) -> int:
        return sum(len(v) for v in self.writes.values())

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rstrip("/") or "/"
        params = dict(request.url.params)
        self.request_log.append((request.method, path))

        if path == "/health":
            return _json(200, {"status": "ok", "service": "aerlink-ops"})
        if request.headers.get("X-Ops-Key") != "test-key":
            return _json(401, {"error": "unauthorized", "message": "bad key"})

        if request.method == "GET":
            return self._get(path, params)
        return self._post(path, json.loads(request.content or b"{}"))

    # -- reads ---------------------------------------------------------

    def _get(self, path: str, params: dict[str, str]) -> httpx.Response:
        if path == "/bookings/search":
            return _json(200, _search(params.get("q", "")))
        if path.startswith("/bookings/"):
            ref = path.split("/")[-1].upper()
            booking = BOOKINGS.get(ref)
            if not booking:
                return _json(404, {"error": "not_found", "message": "no booking"})
            return _json(200, booking)
        if path in ("/flights/availability", "/flights/availability/partners"):
            key = "{}:{}:{}:{}".format(
                params.get("from"), params.get("to"), params.get("date"), path
            )
            attempts = self.availability_attempts.get(key, 0) + 1
            self.availability_attempts[key] = attempts
            if self.availability_flaky_first_attempt and attempts == 1 and path.endswith(
                "availability"
            ):
                return _json(
                    503, {"error": "service_unavailable", "message": "retry"}
                )
            rows = self.availability_override.get(
                key,
                availability_rows(
                    params.get("from", ""), params.get("to", ""), params.get("date", "")
                ),
            )
            if path.endswith("partners"):
                rows = [r for r in rows if r["operated_by"] != "Aerlink"]
            else:
                rows = [r for r in rows if r["operated_by"] == "Aerlink"]
            return _json(
                200,
                {
                    "query": params,
                    "page": 1,
                    "page_size": 100,
                    "total_results": len(rows),
                    "total_pages": 1 if rows else 0,
                    "results": rows,
                },
            )
        if path.startswith("/flights/"):
            key = "{}:{}".format(path.split("/")[-1].upper(), params.get("date"))
            flight = FLIGHTS.get(key)
            if not flight:
                return _json(404, {"error": "not_found", "message": "no flight"})
            return _json(200, flight)
        if path == "/entitlements/calculate":
            result = ENTITLEMENTS.get((params.get("booking_ref") or "").upper())
            if not result:
                return _json(
                    200,
                    {
                        "booking_ref": params.get("booking_ref"),
                        "status": "NO_DISRUPTION_RECORDED",
                        "note": "nothing recorded",
                        "passengers": [],
                    },
                )
            return _json(200, result)
        if path.startswith("/customers/"):
            cid = path.split("/")[2].upper()
            customer = CUSTOMERS.get(cid)
            if not customer:
                return _json(404, {"error": "not_found", "message": "no customer"})
            return _json(200, customer)
        if path.startswith("/stations/"):
            key = "{}:{}".format(path.split("/")[2].upper(), params.get("night"))
            base = HOTELS.get(key)
            if not base:
                return _json(404, {"error": "not_found", "message": "no allocation"})
            out = dict(base)
            out["rooms_remaining"] = self.hotel_remaining.get(key, 0)
            return _json(200, out)
        if path == "/policy/search":
            return _json(200, {"query": params.get("q"), "match_count": 0, "results": []})
        if path == "/disruption/feed":
            return _json(200, {"network_advisories": [], "flight_events": []})
        if path == "/_audit":
            money = sum(
                Decimal(str(w["amount_gbp"]))
                for key in ("payments", "refunds")
                for w in self.writes[key]
            )
            return _json(
                200,
                {
                    "total_requests": len(self.request_log),
                    "writes": self.writes,
                    "totals": {
                        "money_paid_gbp": float(money),
                        "rebookings_confirmed": len(self.writes["rebookings"]),
                        "hotel_vouchers_issued": len(self.writes["hotel_vouchers"]),
                        "escalations_raised": len(self.writes["escalations"]),
                    },
                    "requests": [],
                },
            )
        return _json(404, {"error": "not_found", "message": "no route"})

    # -- writes --------------------------------------------------------

    def _post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        if self.fail_next_write_with is not None:
            error = self.fail_next_write_with
            self.fail_next_write_with = None
            if self.silently_commit_then_fail:
                # The server commits, then the response is lost: the exact case that
                # must end as `unknown` and be reconciled, never retried.
                self._commit(path, body)
            raise error

        if path == "/_reset":
            for value in self.writes.values():
                value.clear()
            self.hotel_remaining = {k: v["rooms_remaining"] for k, v in HOTELS.items()}
            return _json(200, {"status": "reset"})

        if path == "/vouchers/hotel":
            key = "{}:{}".format(body["station"].upper(), body["night"])
            if key not in self.hotel_remaining:
                return _json(404, {"error": "not_found", "message": "no allocation"})
            if self.hotel_remaining[key] <= 0:
                return _json(
                    409, {"error": "allocation_exhausted", "message": "no rooms"}
                )

        record = self._commit(path, body)
        if record is None:
            return _json(404, {"error": "not_found", "message": "no route"})
        return _json(201, record)

    def _commit(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        if path == "/rebooking":
            # Deliberately no validation of option_id, seats, cabin or fare -- the
            # real server does not validate them either.
            record = {
                "rebooking_id": self.next_id("RBK"),
                "confirmed_at": "2026-08-07T09:00:00Z",
                "status": "CONFIRMED",
                **body,
            }
            self.writes["rebookings"].append(record)
            return record
        if path == "/vouchers/hotel":
            key = "{}:{}".format(body["station"].upper(), body["night"])
            self.hotel_remaining[key] -= 1
            record = {
                "voucher_id": self.next_id("HTL"),
                "issued_at": "2026-08-07T09:00:00Z",
                "status": "ISSUED",
                "rate_gbp": HOTELS[key]["rate_gbp"],
                "rooms_remaining_after": self.hotel_remaining[key],
                "station": body["station"].upper(),
                "night": body["night"],
                "booking_ref": body["booking_ref"].upper(),
                "passenger_ids": body["passenger_ids"],
            }
            self.writes["hotel_vouchers"].append(record)
            return record
        if path in ("/payments/compensation", "/payments/goodwill"):
            kind = "COMPENSATION" if path.endswith("compensation") else "GOODWILL"
            record = {
                "payment_id": self.next_id("CMP" if kind == "COMPENSATION" else "GWP"),
                "type": kind,
                "paid_at": "2026-08-07T09:00:00Z",
                "status": "PAID",
                "amount_gbp": round(float(body["amount_gbp"]), 2),
                "booking_ref": body["booking_ref"].upper(),
                "passenger_ids": body.get("passenger_ids"),
            }
            self.writes["payments"].append(record)
            return record
        if path == "/refunds":
            record = {
                "refund_id": self.next_id("RFD"),
                "issued_at": "2026-08-07T09:00:00Z",
                "status": "ISSUED",
                "amount_gbp": round(float(body["amount_gbp"]), 2),
                "booking_ref": body["booking_ref"].upper(),
                "passenger_ids": body["passenger_ids"],
            }
            self.writes["refunds"].append(record)
            return record
        if path == "/escalations":
            record = {
                "escalation_id": self.next_id("ESC"),
                "raised_at": "2026-08-07T09:00:00Z",
                "status": "OPEN",
                "queue": body.get("queue", "GENERAL"),
                **body,
            }
            self.writes["escalations"].append(record)
            return record
        return None


def _search(query: str) -> dict[str, Any]:
    query_lower = query.strip().lower()
    results = []
    for ref, booking in BOOKINGS.items():
        matched: list[str] = []
        if query_lower == ref.lower():
            matched.append("booking_reference_exact")
        if query_lower == booking["contact_email"].lower():
            matched.append("contact_email_exact")
        digits = "".join(c for c in query if c.isdigit())
        if digits and len(digits) >= 9 and digits in booking["contact_phone"].replace("+", ""):
            matched.append("contact_phone_match")
        for pax in booking["passengers"]:
            full = "{} {}".format(pax["given_name"], pax["surname"]).lower()
            if query_lower == full:
                matched.append("passenger_name_exact:{}".format(pax["passenger_id"]))
            elif query_lower and (query_lower in full or full in query_lower):
                matched.append("passenger_name_partial:{}".format(pax["passenger_id"]))
            elif pax["surname"].lower() == query_lower:
                matched.append("passenger_surname_exact:{}".format(pax["passenger_id"]))
        if matched:
            results.append(
                {
                    "booking_ref": ref,
                    "customer_id": booking["customer_id"],
                    "contact_email": booking["contact_email"],
                    "passengers": [
                        "{} {}".format(p["given_name"], p["surname"])
                        for p in booking["passengers"]
                    ],
                    "segments": [
                        "{} {} {}-{}".format(s["flight_no"], s["date"], s["origin"], s["destination"])
                        for s in booking["segments"]
                    ],
                    "matched_on": sorted(set(matched)),
                }
            )
    results.sort(key=lambda r: r["booking_ref"])
    return {"query": query, "match_count": len(results), "results": results}


def _json(status: int, payload: Any) -> httpx.Response:
    return httpx.Response(status, json=payload)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_PRICE = ModelPrice(
    model="test-model",
    input_usd_per_mtok=Decimal("0.20"),
    cached_input_usd_per_mtok=Decimal("0.02"),
    output_usd_per_mtok=Decimal("1.20"),
    source_url="https://example.test/pricing",
    verified_on="2026-09-13",
    is_reasoning_model=False,
    reasoning_effort=None,
)


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        ops_base_url="http://127.0.0.1:8642",
        ops_api_key="test-key",
        openai_api_key="test-openai-key",
        openai_model="test-model",
        price=TEST_PRICE,
        journal_path=tmp_path / "journal.sqlite3",
        journal_namespace="test",
        allow_non_loopback_ops=False,
        authority_level="representative",
    )


@pytest.fixture
def server() -> FakeOpsServer:
    return FakeOpsServer()


@pytest.fixture
def ops(config, server) -> OpsClient:
    client = OpsClient(
        config,
        transport=httpx.MockTransport(server.handler),
        sleep=lambda _s: None,
    )
    client.begin_case()
    yield client
    client.close()


@pytest.fixture
def journal(config) -> Journal:
    j = Journal(config.journal_path, namespace="test")
    yield j
    j.close()
