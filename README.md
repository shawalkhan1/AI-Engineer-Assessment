# Aerlink Disruption Desk

Works an inbound passenger disruption case end to end: establishes who is writing and
which booking they mean, establishes what actually happened from Aerlink's own
records, works out what the passenger is owed as distinct from what they asked for,
finds out what options exist right now, and then either acts or hands the case to a
human with enough to decide on.

> **The supplied brief has moved to `ASSESSMENT-BRIEF.md`, byte for byte unaltered**
> (sha256 `a3c90eb809170351b07262c70373951e3ad91180e38e06e45e783d659d583fce`). Only its filename changed, so that
> this file — how to run what I built — is what you land on first. The reasoning is in
> `DECISIONS.md`.

---

## The one command

```bash
bash run.sh --cases cases --output artifacts/full-run
```

That processes all twelve supplied cases and takes **real** actions against the
operations API — hotel vouchers, refunds, re-bookings and escalations, within the
authority APCP-2026-04 §12.1 grants.

**On the supplied twelve that comes to one hotel voucher and twenty referrals**
(final run `run-2fa5fb03ba`, in `artifacts/full-run/`). The policy grants a representative very
little: statutory compensation appears nowhere in the §12.1 table at any level, and
every re-routing option the supplied inventory generates carries an additional fare,
which §12.1 puts at supervisor level. Everything is still assessed to the penny and
referred with a recommendation a supervisor can act on in one pass.

That is a finding about the authority table, not a description of a system that cannot
do much. What it does automatically, end to end and without a human: resolve identity
under §2, verify the disruption against the operational record, compute the exact
entitlement and independently cross-check it, choose a re-routing option against the
passenger's stated constraints, issue duty-of-care accommodation within the §4.2 caps,
refuse what it may not do, and raise a complete §12.5 referral through the real API for
everything else. `DECISIONS.md` §2 sets out the clauses; `artifacts/ACTION-REVIEW.md`
sets out what an earlier version did before that reading was corrected.

### Before the first run

1. **Python 3.11 or newer.** Developed and verified on **3.14.0**.
2. **`.env`**: `cp .env.example .env`, then put the supplied key in it:
   ```
   OPENAI_API_KEY=...
   ```
   `.env` is already in `.gitignore`. The key is never logged, never printed and never
   written into a case record. If `.env` is missing, `run.sh` creates it from
   `.env.example` and stops so you can add the key.
   `OPS_BASE_URL` and `OPS_API_KEY` fall back to the `.env.example` values if absent,
   and the run says so.
3. **Network, once.** `run.sh` builds `.venv` and installs the pins in
   `requirements.txt` the first time. After that the only outbound traffic is your own
   OpenAI calls.

`run.sh` starts the supplied operations server only if nothing valid is already
listening, waits for `/health`, and **stops only a server it started itself**. If
something else holds the port it stops with an actionable error rather than killing it.

## Two processes, and which is which

These are easy to confuse, and confusing them is how people end up with a port they
cannot free or a case worker that appears to hang.

| | **Operations server** | **CLI case worker** |
|---|---|---|
| What | The supplied airline API, `env/ops_server.py` | `aerlink.cli`, via `run.sh` |
| Lifetime | Long-running; holds all state **in memory** | Starts, works the cases, exits |
| Listens | `127.0.0.1:8642` | Nothing |
| Started by | You, or `run.sh` if the port is free | You |

`run.sh` starts a server **only if nothing valid is already listening**, and stops
**only a server it started itself**. So the one-command path needs no server management
at all. Run it twice in a row, though, and each run gets a fresh server with an empty
write log — which is exactly what you do **not** want when checking that a repeat run
creates no duplicates. For that, start the server yourself and leave it up.

Everything below was run, in this order, on Windows 11 with Git Bash.

**1. Start the operations server** (leave this running):

```bash
.venv/Scripts/python.exe env/ops_server.py > state/ops-server.log 2>&1 &   # Windows
python3 env/ops_server.py > state/ops-server.log 2>&1 &                    # macOS / Linux
```

**2. Check it is healthy:**

```bash
curl -s http://127.0.0.1:8642/health
# {"status": "ok", "service": "aerlink-ops", "time": "2026-09-13T21:43:36Z"}
```

**3. Dry run — no writes of any kind, including handovers:**

```bash
bash run.sh --cases cases --dry-run --output artifacts/dry-run
```

**4. Process a message the system has never seen:**

```bash
bash run.sh --inbound "$(pwd)/examples/unseen-case/inbound.txt" \
            --meta "$(pwd)/examples/unseen-case/meta.json" \
            --case-id unseen-001 --output artifacts/unseen-001
```

**5. Inspect what came out** — the record, and the API's own write log:

```bash
python tools/inspect_run.py artifacts/unseen-001       # exits 1 if anything is wrong

# What the operations API itself says it committed -- the ground truth, not our claim.
curl -s -H "X-Ops-Key: $(grep OPS_API_KEY .env.example | cut -d= -f2)" \
     http://127.0.0.1:8642/_audit \
  | python -c 'import json,sys; a=json.load(sys.stdin); print({k: len(v) for k, v in a["writes"].items()})'
# {'rebookings': 0, 'refunds': 0, 'payments': 0, 'hotel_vouchers': 1, 'escalations': 22}
```

`writes[]` holds committed outcomes only; `requests[]` holds every attempt, including
the ones that were refused. A refused write appears in `requests[]` and never in
`writes[]` — verified against this server with a deliberate `409`, not assumed.

`inspect_run.py` re-derives authority from the recorded facts and cross-checks every
claimed write against the live server, so **run it against the same server session that
produced the run.** Restart the server and its write log is empty, which the inspector
will correctly report as claimed writes it cannot find.

**6. Stop the server.** It spawns a child process, so stop both:

```bash
# Windows (PowerShell)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*ops_server.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# macOS / Linux
pkill -f ops_server.py
```

## The other commands

```bash
# An unseen inbound message, from anywhere on disk.
bash run.sh --inbound /absolute/path/inbound.txt --case-id new-001 --output artifacts/new-001

# The same, with optional metadata.
bash run.sh --inbound /absolute/path/inbound.txt --meta /absolute/path/meta.json \
            --case-id new-001 --output artifacts/new-001

# Read-only rehearsal: zero operations API writes, including handovers.
# Model calls still happen and still cost money.
bash run.sh --cases cases --dry-run --output artifacts/dry-run

# Tests. No server, no API key, no mutations.
bash run.sh --test
```

`--inbound` is the documented way to hand the system a case it has never seen. It does
not need to live under `cases/`, and `--meta` is optional — without it, identity is
attempted from the message alone and the case falls back to a clarification or a
handover if that is not enough.

**A worked example is in `examples/unseen-case/`** — a message that appears nowhere in
`cases/`, about a booking none of the twelve touch:

```bash
bash run.sh --inbound "$(pwd)/examples/unseen-case/inbound.txt" \
            --meta "$(pwd)/examples/unseen-case/meta.json" \
            --case-id unseen-001 --output artifacts/unseen-001
```

The output of that run is in `artifacts/unseen-001/` (run `run-2112529a1d`). It
confirmed identity from the reference and surname, verified against the operational
record that AK588 was cancelled for a crew reason, and searched the live inventory:
63 options inspected, 28 rejected for arriving after the deadline he stated, 13 for the
wrong cabin, 3 for insufficient seats. The best remaining option carries an additional
fare of £272.80, which §12.1 puts at supervisor level, so it was **not** booked — it was
referred with the option identified and the fare stated, so a supervisor can approve it
in one pass. Compensation was referred separately because the arrival delay is not
knowable until he has flown. The drafted reply says plainly that nothing has been
booked.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | The batch completed and every case produced a record. |
| `2` | Configuration failure — missing key, unpriced model, unrecognised service on the port. |
| `3` | Execution failure — a case could not produce a record. |

**A case correctly handed to a human exits `0`.** A handover is a good outcome, not a
crash.

---

## What comes out

`--output` gets one JSON record per case plus `batch-summary.json`.

Each record carries the decision and what the passenger gets, the reasoning and what
it rests on, every source consulted with bounded evidence, every action attempted or
taken with its policy basis and returned identifiers, what remains uncertain, what a
human still needs to do, the drafted reply, and honest token and cost figures. Records
are written atomically, and one is written even when a case throws.

The batch summary reconciles what we claim against `GET /_audit` — the operations
API's own write log. A run that exits cleanly is not evidence that the right things
happened; the reconciliation is.

**The passenger reply is always a draft.** The operations API has no send operation,
so nothing this system writes has reached a passenger.

---

## Repeat runs, and the journal

Actions are journalled in SQLite at `state/journal.sqlite3` — deliberately **outside**
`--output`, so changing the output directory cannot re-enable an action that has
already been taken.

Each action has a fingerprint built from the operations base URL, the verified booking
reference, the action type, the disruption event (`AK640:2026-08-01`) and the canonical
parameters. It is not built from the filename or the run id, so **re-running the same
case under a different name does not pay twice.**

Before any write, the system checks the journal *and* `GET /_audit`, and **the server
wins**:

* server has an equivalent write → skip it, whatever the journal says;
* journal says done but the server does not have it → the server was reset, so the
  action has not happened, and it is performed.

`artifacts/repeat-run-no-duplicates/` (run `run-199b71c85e`) is a run of all twelve
cases against a server that already held the writes, under a journal namespace that
knew nothing about them — so the duplicate check had to work from the server's own log
alone. **All twenty referrals came back `skipped_duplicate`, the hotel voucher was
recognised as already in place, and the server's write count did not move: £0.00
additional, zero new rows.**

### Referrals need an identity the model cannot move

A remedy is easy to deduplicate: the booking, the amount and the event are facts.
A *referral* is not, because its text is assembled from model-authored summaries, and
the same case re-read produces slightly different words. Two designs failed here before
the current one, and both are written up in `DECISIONS.md` §8 rather than quietly
replaced:

1. **Matching on the summary text.** Re-raised 8 of 20 referrals on an unchanged
   repeat, because the wording drifted.
2. **Matching on the booking plus the multiset of blocking clauses.** Also drifted. On
   a repeat of case-02 the model split one assistance complaint into three items where
   it had previously found two; the clause multiset grew, and a duplicate
   `SPECIAL_ASSISTANCE` referral went out. The multiset was meant to detect a genuinely
   new issue. What it actually tracked was how finely the model chose to slice the same
   complaint.

Each referral now carries a `[referral-key: xxxxxxxx]` marker derived from the inbound
message's own sha256, the queue and the disruption event — and from nothing the model
writes. The same message re-processed yields the same key however it is worded, so the
referral already open is reused. A genuinely new issue arrives as a *different message*,
hashes differently, and reaches a human on its own; it is never suppressed. The key
travels inside `summary`, a field `API.md` §18 defines, so no undocumented field is
invented and whoever picks the referral up can see it.

**The known limit:** two separate messages describing the same problem on the same
booking raise two referrals. That is the safer direction — a colleague reads a case
twice rather than a passenger going unanswered — and under §15 every inbound contact
arguably warrants acknowledgement anyway.

### A benefit already given is not a benefit that failed

Related, and found by the same repeat run. Our own voucher in run 1 took the last LGW
room for that night. On the repeat, case-09 found the allocation exhausted and referred
the case to Accommodation Services saying no room could be sourced — for a passenger
already holding voucher `HTL-00012` for that exact station and night. True about the
allocation, false about the passenger, and it made a colleague read a case that needed
nothing.

The planner now asks whether the booking already holds that benefit *before* it treats
unavailability as a blocking problem. Unavailability only matters when it stands
between a passenger and something they do not already have.

**What this does not give you.** It is not exactly-once. Local journalling cannot
coordinate two machines, and it cannot settle a write the server may or may not have
committed. It narrows the window; it does not close it. See *Failure and recovery*.

### Deliberate reset

```bash
bash run.sh --cases cases --output artifacts/clean --reset-ops --journal-namespace fresh-1
```

`--reset-ops` never fires implicitly and **requires** a distinct `--journal-namespace`,
so stale local rows cannot suppress actions in a run that is meant to be clean.

Note that `POST /_reset` clears the mock's `/_audit` log as well as its writes. The
brief says actions are "logged permanently"; in this environment they are not, and the
batch summary says so rather than implying otherwise.

---

## Failure and recovery

| Situation | What happens |
|---|---|
| Availability returns `503` | Retried, up to 3 attempts. The supplied server fails the *first* call for every distinct route/date by design, so this is required, not defensive. |
| Rate limited | Client-side throttle at 25 requests / 10s keeps us under the documented 30. |
| A write times out or the response is lost | Marked **`unknown`**, never retried. Reconciled against `/_audit` by matching the request body. If still unsettled: later money on that booking is blocked, and the case is handed to a human with the exact request that was sent. An unknown write is never treated as a safe failure. |
| Seats or rooms disappear between planning and the write | Preconditions are re-read immediately before every write. `POST /rebooking` does not validate `option_id` or seat counts, so this check is the only thing preventing a confirmation the passenger cannot use. |
| The model is unavailable or the budget is spent | The case still produces a full record and still raises a real escalation. The reply falls back to a deterministic template. |
| Identity is not confirmed | Zero actions on any booking (S16), a referral, and a reply asking for what would confirm it. |
| An action is above the desk's authority | Assessed in full, executed not at all, and referred with the exact figure or option under S12.5. `AERLINK_AUTHORITY_LEVEL` (`representative` by default, or `supervisor` / `manager`) says which row of the §12.1 table this desk sits on. Compensation is unlisted at every level, so it is referred whatever you set. |
| Our record and the server's write log disagree | Blocked and handed over, never repeated. A local "succeeded" that the server cannot confirm is only treated as stale when an explicit `--reset-ops` was recorded *after* it. |
| A case throws | A record is still written, with the sanitised error and status `failed`. |

Nothing is ever rolled back automatically. `POST /rebooking/{id}/cancel` costs £65 and
does not restore inventory, so it is never called on the system's own initiative.
Partial success is preserved and explained.

## Final results and what they cost

Four runs, all on `gpt-5.6-luna`, all against one operations server session, all
reproduced by the commands above. `tools/inspect_run.py` exits `0` on each.

| Run | Output | Result | Cost |
|---|---|---|---|
| Twelve supplied cases | `artifacts/full-run/` (`run-2fa5fb03ba`) | 11 handed over, 1 partially resolved. 1 hotel voucher (£165), 20 referrals, 9 remedies refused for want of authority | **$0.019573** |
| Unchanged repeat | `artifacts/repeat-run-no-duplicates/` (`run-199b71c85e`) | 20 referrals reused, voucher recognised as already in place, **zero new writes, £0.00** | $0.020677 |
| Unseen message | `artifacts/unseen-001/` (`run-2112529a1d`) | Identity confirmed, re-routing referred (£272.80 fare, supervisor level), compensation deferred | $0.001412 |
| Dry run | `artifacts/dry-run/` (`run-8f5202b432`) | 12 records, **0 writes** — server log unchanged at 23 before and after | $0.021085 |

A twelve-case sweep costs **about $0.021** against the brief's $2.00 ceiling, roughly
1% of it. 24 model calls, ~59k input tokens (~39k of them cached), ~13k output.

**Whole project to date: $0.714 across 821 recorded calls**, against the $15.00 limit —
every call, including every superseded run, is in the `model_usage` ledger in
`state/journal.sqlite3` and can be re-totalled from it. Spend is *reserved before it is
spent* and the run refuses to start a call that would breach the ceiling, so the
ceiling holds even if a response never arrives.

Tests: **246 passing**, no server, no API key, no mutations.

## Honest limitations

* **The compensation-authority question is unresolved, and deliberately so.** §12.1
  lists no compensation-payment row at representative, supervisor *or* manager level,
  while §5.1 and §10.2 establish that the debt exists and fix its amount. The system
  therefore assesses compensation exactly and refers it, rather than asserting that
  anyone may pay it. Three cases in the sweep are blocked on this. An earlier version
  paid £855 across those cases on the strength of an `API.md` example; that was wrong,
  and `artifacts/ACTION-REVIEW.md` records what it did. See `DECISIONS.md` §2.
* **Two messages about the same problem raise two referrals** (see above). Reuse is
  anchored on the inbound message, not on a judgement about whether two messages mean
  the same thing.
* **The repeat guarantee depends on `GET /_audit`.** There is no documented read
  endpoint for vouchers or referrals by booking, so the debug audit log is the only
  live source. When it cannot be read the system refers rather than guesses — safe in
  that direction, but it means a server that hides its audit log degrades to referrals.
* **Not exactly-once.** Local journalling narrows the window between "written" and
  "recorded as written"; it does not close it, and it cannot coordinate two machines.
* **`POST /_reset` clears the audit log too**, so "logged permanently" is not true of
  this environment. The batch summary says so rather than implying otherwise.
* **Every reply is a draft.** The operations API has no send operation, so nothing has
  reached a passenger.
* **One model, one provider.** No fallback if `gpt-5.6-luna` is unavailable; the run
  fails closed rather than silently switching to an unpriced model.

## Limits

Per case: 3 model requests (one repair at most), 30 operations API attempts with 4 held
back so a handover is always possible, 3 attempts per read, **0 automatic retries on
any write**, 30s per request, 180s per case. The OpenAI SDK runs with `max_retries=0`
so nothing silently multiplies them.

## Ordering, and why it matters

Cases run sequentially in filename order. **Inventory is shared, so order changes
outcomes.** `LGW` holds one room for the night of 2026-08-06, and both case-09 and
case-11 are stranded there. Case-09 runs first and gets it; case-11 gets
`409 allocation_exhausted` and a §4.5 referral. That is correct behaviour, and it is
reported rather than smoothed over.

---

## Layout

```
aerlink/
  cli.py         argparse, sequential batch, exit codes
  config.py      env loading, validation, the verified model price table
  schemas.py     pydantic schemas: model I/O and the case record
  ops_client.py  typed operations API client, throttle, retries, attempt budget
  llm.py         OpenAI access with the spend reserved before it is spent
  extract.py     model call 1 + span verification
  untrusted.py   fencing, span checks, injection detection, taint regions
  identity.py    §2 identity rules, deterministic
  policy.py      rule→clause table, §12.1 authority gate, money in integer pence
  planner.py     verified facts → typed proposals, deterministic
  executor.py    the only thing that writes
  journal.py     durable intents, fingerprints, usage ledger
  narrate.py     model call 2 — prose only, after the fact
  report.py      case records and the batch summary
tests/           234 tests, no network, no key, no mutations
tools/
  inspect_run.py     audits a completed run against the live API
  check_transcript.py scans an exported transcript for anything key-shaped
```

## Auditing a run

```bash
python tools/inspect_run.py artifacts/full-run     # audit a completed run
python tools/check_transcript.py transcripts/      # scan an export for secrets
```

Reads every record the way a reviewer would and checks the things that would be
embarrassing to get wrong: every payment re-checked against the entitlement service,
**every executed action's authority re-derived from the recorded facts** rather than
read back from the verdict the run stored, no action on an unconfirmed identity, every
re-booking own metal in the cabin booked, every claimed handover actually raised
through the API, every live request accounted for, identifiers reconciled both ways,
and no internal machinery in a passenger reply. Exits non-zero if anything fails.

The authority check is the important one and it is deliberately independent. Point it
at the preserved defective run and it reports eight unsupported actions:

```bash
python tools/inspect_run.py artifacts/superseded-sweep-6-authority-defect
```

A check that cannot fail on known-bad input is not a check.

Full reasoning, the assumptions, and what I would do next are in **`DECISIONS.md`**.
