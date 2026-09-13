# Superseded sweep 1 (run-c07db2b5fe, 2026-09-13)

Kept as evidence, not as a result. This was the first clean full sweep. It completed,
reconciled to the penny against `GET /_audit` (GBP 855.00 both sides), and looked
fine on the console -- and it contained a real defect that the exit code did not
show.

**What it got wrong.** `case-04` (Peter Lindqvist) never reached a human. Escalation
equivalence was keyed on booking reference, queue and blocking clause. Two contacts
from *different people* that both fail identity verification share all three -- no
booking reference, `SUPERVISOR`, `S2.2, S12.1` -- so case-04's referral was
classified `skipped_duplicate` against case-03's `ESC-00007` and was never raised.
`human_handover.api_handover_succeeded` is `false` in `case-04.json`, which is how it
was caught.

It also mislabelled `case-05`: Marta's GBP 112 hotel receipt claim was run through the
discretionary-payment test and reported to a supervisor as a demand for more than her
entitlement. S4.4 reimbursement is an entitlement, and that framing is close to the
mischaracterisation S16 prohibits.

**Both are fixed**, each with a regression test
(`test_two_different_unidentified_contacts_each_reach_a_human`,
`test_a_receipts_claim_is_not_reported_as_a_demand_for_more_than_the_entitlement`).
The definitive results are in `artifacts/full-run`, from a second sweep against a
freshly reset server under a separate journal namespace. The two runs are not mixed.
