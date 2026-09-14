# Aerlink Disruption Desk

A bounded OpenAI case worker for the supplied Aerlink Passenger Care Policy. It reads a message, establishes identity, checks airline records and entitlements, searches available remedies, executes authorised actions, and produces an auditable resolution or human referral. Passenger replies are drafts: the supplied API cannot send them.

## Setup and one command

Use Python 3.11 or newer (verified on Windows/Python 3.14). From the project directory, copy `.env.example` to `.env` and set `OPENAI_API_KEY` to your own key. Alternatively set it in your process environment. Never submit `.env`.

```sh
python run.py --cases cases --output artifacts/my-run
```

On systems where Python is named `python3`, use `python3 run.py ...`. Git Bash users may also use `bash run.sh ...`.

The launcher creates `.venv`, installs the exact dependency pins, starts the supplied operations server if needed, processes the cases and stops only the server it started. First setup needs internet access for packages; normal processing calls OpenAI and the local operations API only. `--output` must be empty/new: prior evidence is never overwritten. The supplied assessment brief is preserved in `ASSESSMENT-BRIEF.md`.

## Give it an unseen case

```sh
python run.py --inbound /absolute/path/inbound.txt --case-id new-001 --output artifacts/new-001
```

Optional `--meta /absolute/path/meta.json` supplies mail-system metadata:

```json
{"case_id":"new-001","from":"Passenger Name <email@example.com>","received_at":"2026-08-06T20:00:00Z"}
```

Metadata must come from your trusted intake system. Without it, the worker uses top-level message headers. A missing/invalid date causes a real referral instead of inventing a historical incident date. A reference/surname mismatch, ambiguous match, or unverified third party cannot authorise booking actions. Case IDs must be safe unique filenames.

## Check it

```sh
python run.py --test
python run.py --cases cases --dry-run --output artifacts/my-dry-run
python tools/inspect_run.py artifacts/audit-verification/full-run
python tools/check_transcript.py transcripts/
```

The offline suite contains **310 tests**, with no OpenAI calls. Under a restricted Windows environment, keep pytest's temporary files in the workspace:

```sh
python run.py --test --basetemp state/pytest-check --cache-clear
```

Use a fresh temporary path for each restricted run. Dry-run makes no operations writes, including referrals. `--dry-run --reset-ops` is rejected before either can execute. Model calls still cost money in dry-run. `--no-model` is available for plumbing checks; it hands work to humans and is not a substitute for the OpenAI agent.

`inspect_run.py` checks schemas, identity, authority, request coverage, future departures, handover success, reply claims and actual write details. It uses the saved `ops-audit.json` when present, so the delivered run can be inspected without a running server or API key. A missing audit without a reachable server fails verification; it never reports skipped verification as a pass.

For a fresh live integration check (uses your key):

```sh
python tools/validate_live.py --output artifacts/my-validation
```

This starts its own isolated operations server, runs all twelve cases, repeats them under another namespace, processes the unseen example and performs a dry-run. It asserts repeat/dry-run write counts did not change. Each run is capped at $0.25. The complete check normally costs much less than that; avoid unnecessary repetitions on the assessment key.

## Verified delivery

Current evidence: `artifacts/audit-verification/`. The full twelve-case run is `run-933736bb4f`: **11 handed over, 1 partially resolved; one GBP 165 hotel voucher and 20 real referrals**. All 21 writes reconcile. **24 OpenAI calls, 58,868 input tokens and 12,950 output tokens; $0.022925**, including cache writes, with no unresolved model charge.

This policy gives a representative limited authority. Compensation is calculated and referred because section 12.1 lists no authority for releasing it. All supplied rebooking options carry an additional fare, which also requires referral. The agent can execute eligible no-additional-fare rebookings and determinable refunds on other inputs; those paths are covered by offline tests, not claimed as live successes in this sweep.

`DECISIONS.md` covers tradeoffs and measured usage. `AUDIT-RESULTS.md` describes the adversarial passes. Historical mistakes are documented in `artifacts/ACTION-REVIEW.md` and the original transcripts; redundant development runs are omitted from this submission.

## Outputs and safety boundaries

Each case JSON contains identity evidence, passenger requests, operational facts, policy reasoning, consulted sources, every attempted/completed/blocked action, uncertainties, human next steps, a draft reply and model usage. A batch summary and captured API write log accompany live runs.

The SQLite journal lives at `state/journal.sqlite3`, outside the output directory. An OS-held lock serialises all namespaces using that journal. Requests are journalled before dispatch; model reservations are also persisted before dispatch. Budget admission includes every namespace in the journal. Crashes leave unresolved reservations rather than zero-cost calls.

Before any booking/money action, the worker requires a readable server audit. It checks for prior benefits, refreshes relevant inventory, and refuses changed fare/cabin/route/timing. Timed-out writes are never blindly retried. Unsettled or ambiguously attributed benefits go to a human. A separate journal or machine still cannot give distributed exactly-once execution; that needs server-side idempotency and atomic inventory reservations.

To test repeat behaviour manually, keep the same operations server alive between commands. A launcher-owned server is stopped at exit and loses its in-memory state. An explicit reset is available:

```sh
python run.py --cases cases --output artifacts/my-reset-run --reset-ops --journal-namespace deliberate-reset
```

It clears the mock's actions **and audit log**. It never runs implicitly. Old journal success without a recorded reset is a disagreement to investigate, not permission to repeat a payment.

Limits: 3 model calls/case, one schema repair, 30 operations attempts/case with 4 reserved for recovery, 3 tries/read, no write retry, 30-second request timeout. New ordinary calls stop at the 180-second case deadline; bounded recovery referrals may extend wall time. A $1.90 run ceiling and $5 journal-wide ceiling are defaults. Reported cost uses API token usage and published standard prices, not a provider invoice.

## Known limits

- Natural-language intent and thread interpretation remain model-dependent. Verified quotes prove presence, not semantic correctness. Replies need review before sending.
- Different traveller groups with shared/ambiguous dates or deadlines are referred rather than guessed. Assistance, YTP, receipts without a reimbursement endpoint, and partner bookings require humans.
- Inventory search is bounded and focuses on direct flights on the selected date; it does not optimise arbitrary connections or an unlimited calendar. Missing/truncated inventory is uncertainty, not proof no flight exists.
- Hotel automation covers the current station-local night. Longer/complex stays and changing locations need manual assessment.
- Airline state is a snapshot at the contact's received time for these historical cases. Real deployment needs a trusted processing clock and refreshed flight status.
- Historical development usage omitted cache-write counters. Its exact invoice cannot be reconstructed; current runs include them. See DECISIONS section 6.

## Transcripts

Actual credential-redacted Claude and Codex JSONL sessions are included in `transcripts/`, with an export manifest and cutoff. No conversation summary substitutes for them. Refresh the still-active session before submitting:

```sh
python tools/export_transcripts.py
python tools/check_transcript.py transcripts/
```

The exporter reads only sessions recorded in this workspace and never edits the originals. It redacts credentials, including matching fake credentials in tests, while keeping the conversation records.

Exit codes: `0` completed (a successful handover counts), `2` configuration/input/setup failure, `3` execution or reconciliation failure.
