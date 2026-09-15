# How the Aerlink agent works and what is now verified

## Summary

The application is a local passenger-care review desk backed by real OpenAI inference and the supplied mock airline operations API. The repaired version passed 314 offline tests, the 12-case live batch, repeat-run duplicate checks, an unseen case, and a complete preview rerun. A submission through the web app also produced a model-generated draft and independently audited mock actions.

These checks demonstrate the tested workflows work. They do not establish that every future natural-language input will be interpreted correctly or that an external API can never fail. The initial preview timeout is included in the evidence.

## Processing a case

1. **Read the input.** The launcher loads a UTF-8 passenger message and optional trusted transport metadata. The received timestamp anchors historical travel dates. Missing or invalid dates require a human review rather than an invented date.
2. **Interpret the request with OpenAI.** The first model call extracts structured requests, booking references, preferences, and supporting quotes. The application checks quoted spans against the message. Passenger-provided instructions cannot grant authority to change airline records.
3. **Confirm identity.** The application matches the sender, booking reference, and surname against airline records. Unconfirmed identities cannot authorize booking or money actions.
4. **Gather facts.** It reads bookings, flight status, customer history, entitlements, inventory, and the operations audit. These records, rather than passenger assertions, determine operational facts.
5. **Plan deterministically.** Python policy and planning code evaluates eligibility, authority, timing, consent, and available options. The model does not choose a payment amount or authorize a booking change. Requests outside the representative's authority go to a human.
6. **Execute and reconcile.** The executor checks existing server actions and the journal, refreshes mutable inventory, records intent before dispatch, and verifies completed writes. Uncertain writes are not blindly retried. Journal/server disagreements remain blocked until reconciled.
7. **Draft a response.** A second model call receives confirmed outcomes and drafts the passenger reply. A deterministic fallback is available if the model fails. Replies are drafts: the supplied API has no email-send operation.
8. **Audit before success.** The command writes structured case records and an operations snapshot, then runs the independent output inspector. Failed cases or failed quality audits produce an execution-failure exit code. The web app displays the outcome, draft, actions, and audit details.

## What was fixed

| Observed issue | Change | Verification |
| --- | --- | --- |
| Fallback replies exposed internal policy references and missing values | Added passenger-facing text cleanup | Regression test and fresh 12-case fallback audit |
| Missing server referrals were labelled successful handovers | Require a confirmed referral receipt; blocked handovers now fail | Status tests and an integration regression that removes a server referral |
| Recovery replies did not explain an incomplete handover | Recovery drafts now distinguish completed and incomplete handovers | Regression coverage and full offline suite |
| Process success could hide audit failures | CLI performs an independent quality audit before returning success | Fresh fallback and live runs include saved quality-audit results |
| No interactive test interface | Added a loopback-only review screen with example cases, input fields, preview mode, and results | HTTP submissions, actual OpenAI processing, audit checks, and JavaScript syntax check |
| Old journals and restarted mock servers could disagree | The desk owns an isolated mock server per session; safety blocks remain intact | Successful app submission; missing-referral regression retains duplicate protection |
| Connection errors were presented as definite model-access failures | Startup error now identifies connectivity, credentials, and model access as possible causes | Live model verification succeeded after authorized network access |

## Latest verification

- **314 offline tests passed.** These cover policy, identity, budget, executor, pipeline, adversarial inputs, and the added runtime regressions.
- **Full live batch:** 12 cases, 21 reconciled mock writes; 11 human handovers and one partial resolution with a GBP 165 hotel voucher.
- **Repeat batch:** all 12 cases audited; no additional writes.
- **Unseen case:** passed, with two verified referrals.
- **Preview rerun:** all 12 cases audited; zero operations writes and no unresolved model calls in that rerun.
- **Web-app submission:** two successful model calls, a model-generated reply, and three reconciled writes, including the hotel voucher.
- **Cost:** calculated $0.091261 across the live attempts and app submission, plus an unresolved upper bound of $0.003705 from one timeout; combined upper bound $0.094966.

The original preview attempt timed out during extraction for case-04. Fallback handling and its output audit passed, but strict live validation rejected the unresolved billing reservation. A complete preview-only rerun passed. The original failed attempt remains included; its charge is not claimed to be resolved.

See [LIVE-TEST-RESULTS.md](LIVE-TEST-RESULTS.md) and the JSON evidence in [artifacts/resubmission-verification](artifacts/resubmission-verification). The manifest records source paths, credential replacement counts, and hashes for the copied run JSON files.

## Reviewer quick start

Use Python 3.11 or newer. From the repository directory, copy `.env.example` to `.env` and set your own `OPENAI_API_KEY`. Keep `.env` private.

```powershell
# Bootstrap dependencies and run the offline suite.
python run.py --test --basetemp state/pytest-reviewer

# Start the web desk with real OpenAI calls.
.\.venv\Scripts\python.exe serve.py --enable-ai
```

Open http://127.0.0.1:8080, select an example, and click **Review case**. Preview is enabled by default and still uses paid model calls. Uncheck preview to allow changes to the isolated mock airline server. Use the original received timestamp for historical cases. For model-free fallback testing, omit `--enable-ai`.

On macOS/Linux, the virtual-environment interpreter is `.venv/bin/python`. The server is local to the reviewer's machine; the submission does not depend on the author's running process.

Verify the saved successful runs without making model calls:

```powershell
.\.venv\Scripts\python.exe tools/inspect_run.py artifacts/resubmission-verification/full-run
.\.venv\Scripts\python.exe tools/inspect_run.py artifacts/resubmission-verification/repeat-run
.\.venv\Scripts\python.exe tools/inspect_run.py artifacts/resubmission-verification/unseen
.\.venv\Scripts\python.exe tools/inspect_run.py artifacts/resubmission-verification/preview-passed
.\.venv\Scripts\python.exe tools/inspect_run.py artifacts/resubmission-verification/app-submission
```

To repeat paid integration validation, choose a new output directory:

```powershell
.\.venv\Scripts\python.exe tools/validate_live.py --output artifacts/reviewer-live
.\.venv\Scripts\python.exe tools/validate_live.py --preview-only --output artifacts/reviewer-preview
```

The full validator caps each of its four runs at $0.25. Each web submission is capped at $0.25, with a $5 cumulative journal ceiling. Actual provider billing can differ from calculated usage.

## Operating limits

- Real OpenAI inference is used, but airline bookings, vouchers, and referrals are mock operations. This is an assessment demo, not a deployed airline integration.
- Many cases correctly require human decisions under the supplied policy. A handover is not an automatic resolution of the passenger's request.
- Model interpretation and external availability remain fallible. The observed timeout demonstrates the fallback path, not perfect reliability.
- The app binds to the local computer and is not publicly deployed. Browser visual verification was unavailable; HTTP behavior and JavaScript syntax were checked.
- Mock server state lasts for that desk session. Saved case records remain under `artifacts/desk`; journal accounting remains under `state/desk-journal.sqlite3`. Restarting creates a new isolated mock session.
- Credentials, virtual environments, runtime databases, and server logs are excluded from Git. Submitted run evidence contains credential-redacted JSON, and assistant transcripts are exported with credential redaction and an explicit cutoff.
