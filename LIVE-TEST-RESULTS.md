# Live AI verification - 2026-09-15

Local app: http://127.0.0.1:8080 (AI enabled, running at verification).

- Offline suite: 314 passed in the preceding repair verification.
- Full live batch: 12 cases passed all output audits; 21 mock operations writes reconciled.
- Repeat batch: 12 cases passed; audit snapshots confirm zero additional writes.
- Unseen case: passed; two referrals reconciled.
- Preview rerun: 12 cases passed; validator confirmed zero operations writes and zero unresolved model calls in this rerun.
- App submission: desk-579ba8175c26 completed with two successful model calls, a model-generated draft, and three audited mock writes (hotel voucher plus referrals).
- Final health check: AI enabled and operations server running.

## Transient failure retained

The first preview run had one OpenAI extraction timeout in case-04. The app used its fallback and its output audit passed, but strict live validation failed due to an unresolved billing reservation. A separate complete preview rerun succeeded. The original evidence and reservation were preserved; the timeout is not claimed to have been reconciled.

Calculated cost for all live attempts and the app submission: $0.091261.
Unresolved upper bound from the timed-out call: $0.003705.
Combined upper bound: $0.094966. These are application calculations, not a provider invoice.

## Evidence

- artifacts/resubmission-verification/full-run
- artifacts/resubmission-verification/repeat-run
- artifacts/resubmission-verification/unseen
- artifacts/resubmission-verification/preview-timeout (original timeout)
- artifacts/resubmission-verification/preview-validation.json
- artifacts/resubmission-verification/app-submission

The app uses real OpenAI inference against supplied mock airline data. Replies remain drafts. It is available on this computer, not publicly deployed. Browser visual verification was unavailable; HTTP submissions and JavaScript syntax were checked.

Restart from the project directory with:

```powershell
.\.venv\Scripts\python.exe serve.py --enable-ai
```

The server was left running at verification. If port 8080 is already in use by the desk, open the existing instance instead. A new checkout requires starting the server.
