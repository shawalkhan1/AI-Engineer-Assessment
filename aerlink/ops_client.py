"""Typed client for the Aerlink operations API.

Everything the runtime learns about the airline comes through here. Three things
this module is responsible for:

* **Budget.** A per-case attempt budget, with a reserve kept back so a case can
  always still raise a handover. Reads retry a bounded number of times; writes are
  never retried automatically.
* **Honesty about writes.** A write whose outcome we could not establish is reported
  as `unknown`, not as a failure. `reconcile_write` re-reads `GET /_audit` to settle
  it where it can.
* **Evidence.** Each read appends a bounded `SourceRef` -- enough to audit the
  decision, never a full payload dump.

The base URL is pinned at construction. No URL from a passenger message or an API
payload is ever fetched.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import httpx

from .config import (
    MAX_OPS_ATTEMPTS_PER_CASE,
    MAX_READ_ATTEMPTS,
    OPS_ATTEMPTS_RESERVED_FOR_HANDOVER,
    REQUEST_TIMEOUT_S,
    THROTTLE_MAX_REQUESTS,
    THROTTLE_WINDOW_S,
    Config,
    redact,
)
from .schemas import SourceRef


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class OpsError(Exception):
    """A structured error response from the operations API."""

    def __init__(self, status: int, error_code: str, message: str, path: str) -> None:
        super().__init__("{} {} on {}: {}".format(status, error_code, path, message))
        self.status = status
        self.error_code = error_code
        self.message = message
        self.path = path


class OpsTransportError(Exception):
    """The request did not produce a usable HTTP response."""


class AttemptBudgetExhausted(Exception):
    """The per-case HTTP attempt budget is spent."""


@dataclass
class WriteOutcome:
    """The result of a mutation. `state` is one of succeeded / failed / unknown."""

    state: str
    status: int | None
    body: dict[str, Any] | None
    error: str | None
    attempted_at: str
    path: str
    request_body: dict[str, Any]


@dataclass
class _Throttle:
    """Keeps us under the documented 30 requests / 10 seconds."""

    max_requests: int = THROTTLE_MAX_REQUESTS
    window_s: float = THROTTLE_WINDOW_S
    _times: list[float] = field(default_factory=list)

    def wait(self) -> None:
        now = time.monotonic()
        self._times = [t for t in self._times if now - t < self.window_s]
        if len(self._times) >= self.max_requests:
            sleep_for = self.window_s - (now - self._times[0]) + 0.05
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            self._times = [t for t in self._times if now - t < self.window_s]
        self._times.append(time.monotonic())


class OpsClient:
    def __init__(
        self,
        config: Config,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], str] = utcnow_iso,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._clock = clock
        self._sleep = sleep
        self._throttle = _Throttle()
        self._client = httpx.Client(
            base_url=config.ops_base_url,
            headers={"X-Ops-Key": config.ops_api_key, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT_S,
            transport=transport,
        )
        self.attempts_used = 0
        self.attempt_limit = MAX_OPS_ATTEMPTS_PER_CASE
        self.sources: list[SourceRef] = []
        self.request_log: list[dict[str, Any]] = []
        self._source_seq = 0

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def begin_case(self) -> None:
        """Reset the per-case attempt budget and evidence list."""
        self.attempts_used = 0
        self.sources = []
        self.request_log = []
        self._source_seq = 0

    def attempts_remaining(self, *, reserved: bool = True) -> int:
        limit = self.attempt_limit
        if reserved:
            limit -= OPS_ATTEMPTS_RESERVED_FOR_HANDOVER
        return max(0, limit - self.attempts_used)

    # -- plumbing ----------------------------------------------------------

    def _next_source_id(self, prefix: str) -> str:
        self._source_seq += 1
        return "{}-{:02d}".format(prefix, self._source_seq)

    def _spend_attempt(self, *, allow_reserve: bool) -> None:
        limit = self.attempt_limit if allow_reserve else (
            self.attempt_limit - OPS_ATTEMPTS_RESERVED_FOR_HANDOVER
        )
        if self.attempts_used >= limit:
            raise AttemptBudgetExhausted(
                "Per-case operations API attempt budget spent "
                "({} of {} used).".format(self.attempts_used, self.attempt_limit)
            )
        self.attempts_used += 1

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        allow_reserve: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        self._spend_attempt(allow_reserve=allow_reserve)
        self._throttle.wait()
        started = self._clock()
        try:
            response = self._client.request(
                method, path, params=params, json=json_body
            )
        except httpx.HTTPError as exc:
            self.request_log.append(
                {
                    "at": started,
                    "method": method,
                    "path": path,
                    "status": None,
                    "error": redact(str(exc), self.config.ops_api_key),
                }
            )
            raise OpsTransportError(
                redact(str(exc), self.config.ops_api_key)
            ) from exc

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            payload = {"error": "unparseable", "message": response.text[:400]}

        self.request_log.append(
            {
                "at": started,
                "method": method,
                "path": path,
                "params": _scrub_params(params),
                "status": response.status_code,
            }
        )

        if response.status_code >= 400:
            raise OpsError(
                response.status_code,
                str(payload.get("error", "unknown")),
                redact(str(payload.get("message", "")), self.config.ops_api_key),
                path,
            )
        return response.status_code, payload

    def _read(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        source_prefix: str,
        evidence: Callable[[dict[str, Any]], Any],
        retry_on: Iterable[int] = (429, 503),
        max_attempts: int = MAX_READ_ATTEMPTS,
    ) -> dict[str, Any]:
        """GET with bounded retry, recording bounded evidence on success.

        The availability endpoint returns 503 on the first call for any distinct
        query by design (`_should_fail_flaky` in env/ops_server.py), so retrying
        reads is mandatory here rather than defensive.
        """
        retry_codes = set(retry_on)
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                _status, payload = self._request("GET", path, params=params)
            except OpsError as exc:
                last_exc = exc
                if exc.status in retry_codes and attempt < max_attempts:
                    self._sleep(min(0.4 * attempt, 1.2))
                    continue
                raise
            except OpsTransportError as exc:
                last_exc = exc
                if attempt < max_attempts:
                    self._sleep(min(0.4 * attempt, 1.2))
                    continue
                raise

            self.sources.append(
                SourceRef(
                    source_id=self._next_source_id(source_prefix),
                    kind="ops_api",
                    endpoint=_display_path(path, params),
                    retrieved_at=self._clock(),
                    evidence=evidence(payload),
                )
            )
            return payload
        raise last_exc if last_exc else OpsTransportError("read failed")

    def _write(
        self, path: str, body: dict[str, Any], *, allow_reserve: bool = False
    ) -> WriteOutcome:
        """POST exactly once. Never retried; an ambiguous outcome stays `unknown`."""
        attempted_at = self._clock()
        try:
            status, payload = self._request(
                "POST", path, json_body=body, allow_reserve=allow_reserve
            )
        except OpsError as exc:
            # A structured 4xx means the server rejected it and did not act. A 5xx
            # or 429 could be either, so it is not treated as a clean failure.
            state = "failed" if 400 <= exc.status < 500 and exc.status != 429 else "unknown"
            return WriteOutcome(
                state=state,
                status=exc.status,
                body=None,
                error="{}: {}".format(exc.error_code, exc.message),
                attempted_at=attempted_at,
                path=path,
                request_body=body,
            )
        except OpsTransportError as exc:
            # Timeout or lost response: the server may or may not have committed.
            return WriteOutcome(
                state="unknown",
                status=None,
                body=None,
                error=str(exc),
                attempted_at=attempted_at,
                path=path,
                request_body=body,
            )
        return WriteOutcome(
            state="succeeded",
            status=status,
            body=payload,
            error=None,
            attempted_at=attempted_at,
            path=path,
            request_body=body,
        )

    # -- reads -------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        _status, payload = self._request("GET", "/health")
        return payload

    def search_bookings(self, query: str) -> dict[str, Any]:
        return self._read(
            "/bookings/search",
            params={"q": query},
            source_prefix="SRC-booking-search",
            evidence=lambda p: {
                "query": p.get("query"),
                "match_count": p.get("match_count"),
                "results": [
                    {
                        "booking_ref": r.get("booking_ref"),
                        "matched_on": r.get("matched_on"),
                        "passengers": r.get("passengers"),
                        "segments": r.get("segments"),
                    }
                    for r in p.get("results", [])
                ],
            },
        )

    def get_booking(self, booking_ref: str) -> dict[str, Any]:
        return self._read(
            "/bookings/{}".format(_safe_segment(booking_ref)),
            source_prefix="SRC-booking",
            evidence=lambda p: {
                "booking_ref": p.get("booking_ref"),
                "customer_id": p.get("customer_id"),
                "tier": p.get("tier"),
                "final_destination": p.get("final_destination"),
                "total_paid_gbp": p.get("total_paid_gbp"),
                "fare_breakdown": p.get("fare_breakdown"),
                "special_requests": p.get("special_requests"),
                "passengers": [
                    {
                        k: pax.get(k)
                        for k in (
                            "passenger_id",
                            "given_name",
                            "surname",
                            "passenger_type",
                            "age",
                            "assistance",
                            "cabin_booked",
                            "cabin_flown",
                        )
                    }
                    for pax in p.get("passengers", [])
                ],
                "segments": p.get("segments"),
                "disruption": p.get("disruption"),
            },
        )

    def get_flight(self, flight_no: str, date_iso: str) -> dict[str, Any]:
        return self._read(
            "/flights/{}".format(_safe_segment(flight_no)),
            params={"date": date_iso},
            source_prefix="SRC-flight",
            evidence=lambda p: {
                k: p.get(k)
                for k in (
                    "flight_no",
                    "date",
                    "origin",
                    "destination",
                    "distance_km",
                    "scheduled_departure",
                    "scheduled_arrival",
                    "status",
                    "cause_code",
                    "cause_note",
                    "departure_delay_minutes",
                )
            },
        )

    def availability(
        self,
        origin: str,
        destination: str,
        date_iso: str,
        *,
        booking_ref: str | None = None,
        after: str | None = None,
        partners: bool = False,
        page_size: int = 100,
        max_pages: int = 3,
    ) -> dict[str, Any]:
        """Own-carrier or partner inventory, paged.

        `page_size=100` is the documented maximum and covers the 58-74 rows the
        service generates in one page. If more pages exist than `max_pages`, the
        result says so rather than silently truncating the candidate set.
        """
        path = "/flights/availability/partners" if partners else "/flights/availability"
        params: dict[str, Any] = {
            "from": origin,
            "to": destination,
            "date": date_iso,
            "page_size": page_size,
            "page": 1,
        }
        if booking_ref:
            params["booking_ref"] = booking_ref
        if after:
            params["after"] = after

        results: list[dict[str, Any]] = []
        first = self._read(
            path,
            params=dict(params),
            source_prefix="SRC-availability",
            evidence=lambda p: {
                "query": p.get("query"),
                "total_results": p.get("total_results"),
                "total_pages": p.get("total_pages"),
                "sample": p.get("results", [])[:3],
            },
        )
        results.extend(first.get("results", []))
        total_pages = int(first.get("total_pages") or 1)
        truncated = False
        for page in range(2, total_pages + 1):
            if page > max_pages or self.attempts_remaining() < 2:
                truncated = True
                break
            params["page"] = page
            more = self._read(
                path,
                params=dict(params),
                source_prefix="SRC-availability",
                evidence=lambda p: {
                    "page": p.get("page"),
                    "returned": len(p.get("results", [])),
                },
            )
            results.extend(more.get("results", []))

        return {
            "query": first.get("query"),
            "total_results": first.get("total_results"),
            "total_pages": total_pages,
            "pages_fetched": min(total_pages, max_pages),
            "candidate_set_truncated": truncated,
            "carrier_scope": "partner" if partners else "own",
            "results": results,
        }

    def policy_search(self, query: str, limit: int = 3) -> dict[str, Any]:
        return self._read(
            "/policy/search",
            params={"q": query, "limit": limit},
            source_prefix="SRC-policy",
            evidence=lambda p: {
                "query": p.get("query"),
                "sections": [
                    {"section": r.get("section"), "heading": r.get("heading")}
                    for r in p.get("results", [])
                ],
            },
        )

    def policy_document(self) -> dict[str, Any]:
        return self._read(
            "/policy/document",
            source_prefix="SRC-policy-doc",
            evidence=lambda p: {
                "document_ref": p.get("document_ref"),
                "version": p.get("version"),
                "characters": p.get("characters"),
            },
        )

    def customer_history(self, customer_id: str) -> dict[str, Any]:
        return self._read(
            "/customers/{}/history".format(_safe_segment(customer_id)),
            source_prefix="SRC-customer",
            evidence=lambda p: {
                "customer_id": p.get("customer_id"),
                "tier": p.get("tier"),
                "flags": p.get("flags", []),
                "history": [
                    {
                        "case_id": h.get("case_id"),
                        "opened": h.get("opened"),
                        "status": h.get("status"),
                        "booking_ref": h.get("booking_ref"),
                        "actions": h.get("actions", []),
                        "summary": h.get("summary"),
                    }
                    for h in p.get("history", [])
                ],
            },
        )

    def entitlements(
        self, booking_ref: str, passenger_id: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"booking_ref": booking_ref}
        if passenger_id:
            params["passenger_id"] = passenger_id
        return self._read(
            "/entitlements/calculate",
            params=params,
            source_prefix="SRC-entitlement",
            evidence=lambda p: {
                "booking_ref": p.get("booking_ref"),
                "status": p.get("status"),
                "authoritative": p.get("authoritative"),
                "policy_version": p.get("policy_version"),
                "note": p.get("note"),
                "journey": p.get("journey"),
                "compensation": p.get("compensation"),
                "duty_of_care": p.get("duty_of_care"),
                "passengers": p.get("passengers"),
                "total_payable_gbp": p.get("total_payable_gbp"),
            },
        )

    def hotel_allocation(self, iata: str, night_iso: str) -> dict[str, Any]:
        return self._read(
            "/stations/{}/hotel-allocation".format(_safe_segment(iata)),
            params={"night": night_iso},
            source_prefix="SRC-hotel",
            evidence=lambda p: p,
        )

    def disruption_feed(self) -> dict[str, Any]:
        return self._read(
            "/disruption/feed",
            source_prefix="SRC-feed",
            evidence=lambda p: {
                "generated_at": p.get("generated_at"),
                "advisory_count": len(p.get("network_advisories", [])),
                "event_count": len(p.get("flight_events", [])),
            },
        )

    def audit(self) -> dict[str, Any]:
        """GET /_audit. Not rate limited and not itself audited (API.md S19)."""
        _status, payload = self._request("GET", "/_audit", allow_reserve=True)
        return payload

    # -- writes ------------------------------------------------------------

    def post_rebooking(self, body: dict[str, Any]) -> WriteOutcome:
        return self._write("/rebooking", body)

    def post_hotel_voucher(self, body: dict[str, Any]) -> WriteOutcome:
        return self._write("/vouchers/hotel", body)

    def post_compensation(self, body: dict[str, Any]) -> WriteOutcome:
        return self._write("/payments/compensation", body)

    def post_goodwill(self, body: dict[str, Any]) -> WriteOutcome:
        return self._write("/payments/goodwill", body)

    def post_refund(self, body: dict[str, Any]) -> WriteOutcome:
        return self._write("/refunds", body)

    def post_escalation(self, body: dict[str, Any]) -> WriteOutcome:
        # Handover writes draw on the reserved attempts so a case can always be
        # handed to a human even when the ordinary budget is spent.
        return self._write("/escalations", body, allow_reserve=True)

    def reset(self) -> dict[str, Any]:
        """POST /_reset. Only ever called from an explicit, documented flag."""
        _status, payload = self._request("POST", "/_reset", json_body={}, allow_reserve=True)
        return payload

    # -- reconciliation ----------------------------------------------------

    def find_matching_writes(
        self, audit: dict[str, Any], collection: str, predicate: Callable[[dict], bool]
    ) -> list[dict[str, Any]]:
        """Search the server's own write log. This is the authority on what happened."""
        return [w for w in audit.get("writes", {}).get(collection, []) if predicate(w)]


_AUDIT_COLLECTION_FOR_PATH = {
    "/rebooking": "rebookings",
    "/vouchers/hotel": "hotel_vouchers",
    "/payments/compensation": "payments",
    "/payments/goodwill": "payments",
    "/refunds": "refunds",
    "/escalations": "escalations",
}


def audit_collection_for(path: str) -> str | None:
    return _AUDIT_COLLECTION_FOR_PATH.get(path)


def _safe_segment(value: str) -> str:
    """Path segments come from verified records, but never trust them blindly."""
    cleaned = re.sub(r"[^A-Za-z0-9_\-]", "", str(value))
    if not cleaned:
        raise ValueError("Refusing to build a request path from {!r}.".format(value))
    return cleaned


def _scrub_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    if not params:
        return None
    return {k: v for k, v in params.items() if k not in {"key", "api_key"}}


def _display_path(path: str, params: dict[str, Any] | None) -> str:
    if not params:
        return path
    rendered = "&".join("{}={}".format(k, v) for k, v in sorted(params.items()))
    return "{}?{}".format(path, rendered)
