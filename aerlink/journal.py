"""Durable local execution journal (SQLite, standard library only).

Two jobs:

* **Write-ahead intent.** Every mutation is recorded and committed here *before* the
  HTTP request leaves, so a crash mid-write still leaves a trace to reconcile from.
* **Duplicate prevention.** Actions are keyed by a fingerprint derived from the
  verified booking, the disruption event and the canonical action parameters -- not
  from a filename or a run id. Re-running the same case under a different name
  therefore cannot pay twice.

What this cannot do, and the README and DECISIONS.md say so: it cannot give
exactly-once execution across independent machines, and it cannot settle a write the
server may or may not have committed. `GET /_audit` is the authority on what actually
happened; the journal is how we know what to go and check.

The journal lives outside `--output` on purpose. Changing the output directory must
not re-enable an action that has already been taken.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS action_intents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace       TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    ops_base_url    TEXT NOT NULL,
    case_id         TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    action_type     TEXT NOT NULL,
    booking_ref     TEXT,
    path            TEXT NOT NULL,
    request_body    TEXT NOT NULL,
    state           TEXT NOT NULL,
    response_status INTEGER,
    returned_id     TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intents_fp
    ON action_intents (namespace, fingerprint);

CREATE TABLE IF NOT EXISTS model_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace         TEXT NOT NULL,
    run_id            TEXT NOT NULL,
    case_id           TEXT,
    purpose           TEXT NOT NULL,
    model             TEXT NOT NULL,
    request_id        TEXT,
    input_tokens      INTEGER,
    cached_tokens     INTEGER,
    output_tokens     INTEGER,
    reasoning_tokens  INTEGER,
    cost_usd          TEXT,
    reserved_usd      TEXT NOT NULL,
    resolved          INTEGER NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_ns ON model_usage (namespace);

-- Benefits already granted, keyed by WHAT was granted rather than by the exact
-- request. S12.2: "The limits are assessed per booking and per disruption event. They
-- may not be circumvented by splitting an amount into several smaller payments, by
-- paying separate passengers on one booking separately, or by issuing a payment and a
-- voucher that together exceed the limit." An exact request fingerprint cannot see any
-- of that -- change the amount and it is a different fingerprint but the same benefit.
CREATE TABLE IF NOT EXISTS benefit_grants (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace    TEXT NOT NULL,
    benefit_key  TEXT NOT NULL,
    ops_base_url TEXT NOT NULL,
    booking_ref  TEXT,
    action_type  TEXT NOT NULL,
    journal_key  INTEGER NOT NULL,
    state        TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_benefit_key
    ON benefit_grants (namespace, benefit_key);

-- Written only by an explicit --reset-ops. A journal row created before a recorded
-- reset is known-stale; one created after it is not, and a server that does not show
-- it is a disagreement to reconcile rather than a licence to repeat the action.
CREATE TABLE IF NOT EXISTS reset_markers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace    TEXT NOT NULL,
    ops_base_url TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""

OPEN_STATES = ("succeeded", "unknown", "attempted")


def utcnow_iso() -> str:
    """Microsecond precision: two journal rows written in the same second still order."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def canonical_fingerprint(
    *,
    ops_base_url: str,
    booking_ref: str,
    action_type: str,
    disruption_scope: str,
    params: dict[str, Any],
) -> str:
    """Stable identity for "this action, for this passenger, for this disruption".

    `disruption_scope` is the flight the case is about (``AK640:2026-08-01``), so the
    same passenger can legitimately be paid twice for two different events, but not
    twice for one.
    """
    payload = {
        "ops": ops_base_url.rstrip("/"),
        "booking": booking_ref.upper(),
        "action": action_type,
        "scope": disruption_scope,
        "params": _canonicalise(params),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# What a benefit *is*, for S12.2 purposes. Deliberately coarser than the request
# fingerprint: the amount is not part of the key, because "split it into two smaller
# payments" is exactly the circumvention S12.2 names.
def benefit_keys_for(
    *,
    ops_base_url: str,
    action_type: str,
    booking_ref: str,
    disruption_scope: str,
    passenger_ids: list[str] | None,
    station: str | None = None,
    night: str | None = None,
) -> list[str]:
    """The benefit-scope keys an action would consume.

    * compensation and goodwill are per booking per event (S12.2, S11.2);
    * refund and re-booking are per passenger, since S7.3 lets passengers on one
      booking take different remedies;
    * a hotel voucher is per passenger per station per night.
    """
    base = "{}|{}|{}".format(ops_base_url.rstrip("/"), booking_ref.upper(), disruption_scope)
    people = sorted(passenger_ids or []) or ["*"]

    if action_type in {"compensation_payment", "goodwill_payment"}:
        kind = "money" if action_type == "compensation_payment" else "goodwill"
        return ["{}|{}".format(base, kind)]
    if action_type == "refund":
        return ["{}|refund|{}".format(base, p) for p in people]
    if action_type == "rebooking":
        return ["{}|rebooking|{}".format(base, p) for p in people]
    if action_type == "hotel_voucher":
        return [
            "{}|hotel|{}|{}|{}".format(base, (station or "").upper(), night or "", p)
            for p in people
        ]
    return []


def _canonicalise(value: Any) -> Any:
    """Normalise so that trivially different spellings fingerprint identically."""
    if isinstance(value, dict):
        return {str(k): _canonicalise(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple, set)):
        return sorted((_canonicalise(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        # Money never reaches here as a float, but be deterministic regardless.
        return "{:.2f}".format(value)
    if isinstance(value, str):
        return value.strip().upper()
    return value


@dataclass
class PriorAction:
    journal_key: int
    state: str
    case_id: str
    run_id: str
    returned_id: str | None
    created_at: str
    request_body: dict[str, Any]
    path: str

    def predates(self, moment: str | None) -> bool:
        """True if this row was written strictly before `moment`.

        Parsed rather than compared as strings: the journal has carried both second
        and microsecond precision, and '.' sorts before 'Z', so a lexicographic
        comparison gets the order backwards across that boundary.
        """
        if not moment:
            return False
        mine, theirs = _parse_ts(self.created_at), _parse_ts(moment)
        if mine is None or theirs is None:
            return False
        return mine < theirs


class JournalLocked(Exception):
    """Another process holds the journal for this namespace."""


class Journal:
    """Single-writer. Two CLI runs sharing a journal could otherwise both pass the
    duplicate check before either recorded its intent, and both pay.

    The lock is a lock file beside the journal, held for the life of the process. It
    is a practical control for a local single-machine application and nothing more:
    it does not coordinate across machines and it is not an exactly-once guarantee.
    """

    def __init__(self, path: Path, namespace: str = "default") -> None:
        self.path = Path(path)
        self.namespace = namespace
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_suffix(".{}.lock".format(namespace))
        self._lock_handle = self._acquire_lock()
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA)

    def _acquire_lock(self):
        """Exclusive-create a lock file. Stale locks from dead processes are cleared."""
        import os

        for attempt in range(2):
            try:
                handle = os.open(
                    str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                os.write(handle, str(os.getpid()).encode("ascii"))
                return handle
            except FileExistsError:
                if attempt == 0 and self._lock_is_stale():
                    continue
                raise JournalLocked(
                    "Another run is using the journal namespace {!r} ({}). Wait for it "
                    "to finish, or use a different --journal-namespace. If you are "
                    "certain no run is active, delete {}.".format(
                        self.namespace, self._lock_path, self._lock_path
                    )
                )
        raise JournalLocked("could not acquire {}".format(self._lock_path))

    def _lock_is_stale(self) -> bool:
        """True if the recorded pid is not running, so the lock can be reclaimed."""
        import os

        try:
            pid = int(self._lock_path.read_text(encoding="ascii").strip() or "0")
        except (OSError, ValueError):
            pid = 0
        if pid == os.getpid():
            # Held by us. A second Journal in the same process is a real conflict,
            # not a stale file -- and on Windows the open handle cannot be unlinked.
            return False
        if pid > 0:
            try:
                os.kill(pid, 0)
                return False                      # that process is alive
            except ProcessLookupError:
                pass                              # gone for certain; reclaim below
            except (PermissionError, OSError):
                # Windows does not report a missing pid as ProcessLookupError, so the
                # signal test is inconclusive. The unlink below settles it instead: a
                # live holder still has the file open and Windows will refuse.
                pass
        try:
            self._lock_path.unlink()
        except OSError:
            return False                          # still held open by someone
        return True

    def close(self) -> None:
        import os

        self._conn.close()
        if getattr(self, "_lock_handle", None) is not None:
            try:
                os.close(self._lock_handle)
            finally:
                self._lock_handle = None
                self._lock_path.unlink(missing_ok=True)

    # -- mutation intents --------------------------------------------------

    def find_prior_actions(self, fingerprint: str) -> list[PriorAction]:
        rows = self._conn.execute(
            "SELECT id, state, case_id, run_id, returned_id, created_at, request_body, path "
            "FROM action_intents WHERE namespace = ? AND fingerprint = ? "
            "AND state IN (?, ?, ?) ORDER BY id",
            (self.namespace, fingerprint, *OPEN_STATES),
        ).fetchall()
        return [
            PriorAction(
                journal_key=r["id"],
                state=r["state"],
                case_id=r["case_id"],
                run_id=r["run_id"],
                returned_id=r["returned_id"],
                created_at=r["created_at"],
                request_body=json.loads(r["request_body"]),
                path=r["path"],
            )
            for r in rows
        ]

    def record_intent(
        self,
        *,
        fingerprint: str,
        ops_base_url: str,
        case_id: str,
        run_id: str,
        action_type: str,
        booking_ref: str | None,
        path: str,
        request_body: dict[str, Any],
    ) -> int:
        """Commit the intent to disk before the request is sent."""
        now = utcnow_iso()
        cur = self._conn.execute(
            "INSERT INTO action_intents (namespace, fingerprint, ops_base_url, case_id, "
            "run_id, action_type, booking_ref, path, request_body, state, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.namespace,
                fingerprint,
                ops_base_url,
                case_id,
                run_id,
                action_type,
                booking_ref,
                path,
                json.dumps(request_body, sort_keys=True),
                "attempted",
                now,
                now,
            ),
        )
        return int(cur.lastrowid)

    def update_outcome(
        self,
        journal_key: int,
        *,
        state: str,
        response_status: int | None = None,
        returned_id: str | None = None,
        error: str | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE action_intents SET state = ?, response_status = ?, returned_id = ?, "
            "error = ?, updated_at = ? WHERE id = ?",
            (state, response_status, returned_id, error, utcnow_iso(), journal_key),
        )

    def open_unknowns(self) -> list[PriorAction]:
        rows = self._conn.execute(
            "SELECT id, state, case_id, run_id, returned_id, created_at, request_body, path "
            "FROM action_intents WHERE namespace = ? AND state IN ('unknown','attempted') "
            "ORDER BY id",
            (self.namespace,),
        ).fetchall()
        return [
            PriorAction(
                journal_key=r["id"],
                state=r["state"],
                case_id=r["case_id"],
                run_id=r["run_id"],
                returned_id=r["returned_id"],
                created_at=r["created_at"],
                request_body=json.loads(r["request_body"]),
                path=r["path"],
            )
            for r in rows
        ]

    def record_reset(self, *, ops_base_url: str, run_id: str) -> str:
        """Note an explicit operations-API reset. Only `--reset-ops` calls this."""
        now = utcnow_iso()
        self._conn.execute(
            "INSERT INTO reset_markers (namespace, ops_base_url, run_id, created_at) "
            "VALUES (?,?,?,?)",
            (self.namespace, ops_base_url.rstrip("/"), run_id, now),
        )
        return now

    def last_reset_at(self, ops_base_url: str) -> str | None:
        row = self._conn.execute(
            "SELECT MAX(created_at) AS at FROM reset_markers "
            "WHERE namespace = ? AND ops_base_url = ?",
            (self.namespace, ops_base_url.rstrip("/")),
        ).fetchone()
        return row["at"] if row and row["at"] else None

    # -- benefit grants (S12.2) --------------------------------------------

    def find_benefit_grants(self, benefit_keys: list[str]) -> list[dict[str, Any]]:
        """Benefits of this kind already granted, whatever the exact parameters were."""
        if not benefit_keys:
            return []
        placeholders = ",".join("?" for _ in benefit_keys)
        rows = self._conn.execute(
            "SELECT benefit_key, booking_ref, action_type, journal_key, state, "
            "created_at FROM benefit_grants WHERE namespace = ? AND state IN "
            "('succeeded','unknown','attempted') AND benefit_key IN ({})".format(
                placeholders
            ),
            (self.namespace, *benefit_keys),
        ).fetchall()
        return [dict(r) for r in rows]

    def record_benefit_grants(
        self,
        *,
        benefit_keys: list[str],
        ops_base_url: str,
        booking_ref: str | None,
        action_type: str,
        journal_key: int,
        state: str,
    ) -> None:
        now = utcnow_iso()
        for key in benefit_keys:
            self._conn.execute(
                "INSERT INTO benefit_grants (namespace, benefit_key, ops_base_url, "
                "booking_ref, action_type, journal_key, state, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    self.namespace,
                    key,
                    ops_base_url.rstrip("/"),
                    booking_ref,
                    action_type,
                    journal_key,
                    state,
                    now,
                ),
            )

    def update_benefit_state(self, journal_key: int, state: str) -> None:
        self._conn.execute(
            "UPDATE benefit_grants SET state = ? WHERE namespace = ? AND journal_key = ?",
            (state, self.namespace, journal_key),
        )

    # -- model usage ledger ------------------------------------------------

    def record_usage(
        self,
        *,
        run_id: str,
        case_id: str | None,
        purpose: str,
        model: str,
        request_id: str | None,
        input_tokens: int | None,
        cached_tokens: int | None,
        output_tokens: int | None,
        reasoning_tokens: int | None,
        cost_usd: Decimal | None,
        reserved_usd: Decimal,
        resolved: bool,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO model_usage (namespace, run_id, case_id, purpose, model, "
            "request_id, input_tokens, cached_tokens, output_tokens, reasoning_tokens, "
            "cost_usd, reserved_usd, resolved, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.namespace,
                run_id,
                case_id,
                purpose,
                model,
                request_id,
                input_tokens,
                cached_tokens,
                output_tokens,
                reasoning_tokens,
                str(cost_usd) if cost_usd is not None else None,
                str(reserved_usd),
                1 if resolved else 0,
                utcnow_iso(),
            ),
        )
        return int(cur.lastrowid)

    def usage_totals(self, run_id: str | None = None) -> dict[str, Any]:
        """Calculated spend, plus any reservations we could never resolve.

        Unresolved reservations are reported separately as an upper bound. They are
        never silently counted as zero.
        """
        where = "WHERE namespace = ?"
        args: list[Any] = [self.namespace]
        if run_id:
            where += " AND run_id = ?"
            args.append(run_id)
        rows = self._conn.execute(
            "SELECT input_tokens, cached_tokens, output_tokens, reasoning_tokens, "
            "cost_usd, reserved_usd, resolved FROM model_usage " + where,
            tuple(args),
        ).fetchall()

        total_cost = Decimal("0")
        unresolved = Decimal("0")
        tokens = {"input": 0, "cached_input": 0, "output": 0, "reasoning": 0}
        unresolved_calls = 0
        for r in rows:
            if r["resolved"]:
                total_cost += Decimal(r["cost_usd"] or "0")
                tokens["input"] += r["input_tokens"] or 0
                tokens["cached_input"] += r["cached_tokens"] or 0
                tokens["output"] += r["output_tokens"] or 0
                tokens["reasoning"] += r["reasoning_tokens"] or 0
            else:
                unresolved += Decimal(r["reserved_usd"])
                unresolved_calls += 1
        return {
            "calls": len(rows),
            "tokens": tokens,
            "calculated_cost_usd": str(total_cost.quantize(Decimal("0.000001"))),
            "unresolved_reservation_upper_bound_usd": str(
                unresolved.quantize(Decimal("0.000001"))
            ),
            "unresolved_calls": unresolved_calls,
            "worst_case_total_usd": str(
                (total_cost + unresolved).quantize(Decimal("0.000001"))
            ),
        }

    def committed_spend(self) -> Decimal:
        """Calculated spend plus unresolved reservations, for budget admission."""
        totals = self.usage_totals()
        return Decimal(totals["calculated_cost_usd"]) + Decimal(
            totals["unresolved_reservation_upper_bound_usd"]
        )
