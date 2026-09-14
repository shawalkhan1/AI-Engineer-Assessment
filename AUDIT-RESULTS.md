# Adversarial audit results

The agent works end to end on the twelve supplied cases and an unseen example. Material defects were fixed; arbitrary-message perfection is not established.

## Passes

1. Identity/hostile input: conflicting references, forged forwarded headers, unsupported extracted phones and ambiguous passenger mapping now fail closed.
2. Policy/planning: corrected advance-notice handling, rebooking eligibility, refund/travel overlap, hotel passenger coverage, local-time deadlines and alternative-origin selection.
3. Execution/recovery: require readable audit even with a fresh journal; attribute benefits across namespaces; stop unknown overlapping benefits; recheck fare, route, cabin, timing and hotel rates; retain action evidence and create real handovers after failures.
4. Budget/runtime: persist pre-call reservations, include cache-write charges, share journal admission/OS locks, bound deadlines and attempts, reject dry-run/reset conflicts.
5. Deliverables: portable launcher, safe unique outputs, actual-write reconciliation, original redacted transcripts and measured-cost documentation.

## Evidence

| Check | Result |
|---|---|
| Offline tests | 310 passed |
| Full twelve | 11 handovers, 1 partial resolution; GBP 165 voucher and 20 referrals |
| Reconciliation | All 21 writes match audit |
| OpenAI full run | 24 calls; 58,868 input / 12,950 output; $0.022925 |
| Repeat in another namespace | Zero added writes |
| Unseen example | Processed; 2 real referrals |
| Dry-run twelve | Zero added writes |
| Clean source bootstrap | Created virtualenv, installed pins, ran own server/no-model dry-run; exit 0 |

Evidence: `artifacts/audit-verification/`, especially `validation.json`. New audit calls together cost $0.069423. Historical cache-write accounting is incomplete and disclosed in DECISIONS.md. A final referral wording correction removes an unsupported claim that assistance inventory was searched; recorded live outputs are preserved.

## Requirement coverage

| Requirement | Evidence |
|---|---|
| End-to-end resolution/handover | Case JSONs and real API writes |
| OpenAI | Live usage records |
| Unseen input | README --inbound command and unseen output |
| Reasons, sources, actions, uncertainty, human tasks | Structured case records |
| Twelve under $2 | $0.022925 measured token cost |
| Clean-clone command | README / run.py |
| Behaviour checks | tests, inspect_run.py, validate_live.py |
| Decisions template | DECISIONS.md sections 1-10 |
| Actual assistant sessions | transcripts JSONL and manifest |

## Limits

Many outcomes require humans because policy authority, fare differences or assistance prevent automatic completion. Direct-date search, current-night hotels, language interpretation and policy-version handling need further development. Independent journals cannot prevent distributed races without server idempotency. Replies are unsent drafts. Twelve cases plus one unseen input are not exhaustive evaluation. Redundant historical run directories were removed; their lessons remain in the action review and transcripts. Transcripts capture available records through the manifest cutoff; refresh the active session before later submission.
