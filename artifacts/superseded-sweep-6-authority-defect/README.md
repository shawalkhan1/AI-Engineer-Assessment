# Superseded: sweep `run-9b598dc772`

**Preserved as evidence, not as a result.** This run paid **£855.00** and confirmed
**five re-bookings**. Eight of its nine mutations are now assessed as **unsupported**.

It was not a crash. It exited 0, reconciled to the penny against the server's own write
log (28 claimed, 28 present), passed 179 tests and passed its own audit tool. The
defect was a reading of the policy, and every automated check agreed with it.

## What was wrong

**Compensation (3 payments, £855.00).** S12.1 has no compensation row at any level and
says an unlisted action "is not authorised without referral"; S16 prohibits exceeding a
S12.1 limit without exception. This run paid anyway, reasoning from S5.1 "is payable",
S10.2 fixing the amount, and API.md §9/§15 working an example that pays exactly £415.
The policy preamble settles it the other way: the document is the authoritative
statement of what Aerlink owes *and of what a representative is permitted to do about
it*, and it prevails over any other document or system message. Entitlement is not
authority.

**Re-bookings (5, eight travellers).** The authority gate was handed
`passenger_charged=Money(0)` unconditionally — an assertion no supplied source makes.
The options carried additional fares of **£280.49, £108.67, £314.46, £311.58 and
£296.87**. API.md §3 defines `fare_gbp` as "the additional fare payable" and S12.1 puts
any fare difference at supervisor level.

Every figure it paid was arithmetically correct. Every passenger was correctly
identified. The consent evidence was sound. It simply did not have permission.

## Where to look

* `../ACTION-REVIEW.md` — all nine mutations, one at a time, with verdicts and the
  remediation a human needs to do.
* `../EXPECTATIONS.md` — what the sources say should happen, derived independently.
* `../full-run/` — the corrected run: £0 moved, one hotel voucher, everything else
  referred with a complete recommendation.

`tools/inspect_run.py` now re-derives authority from the recorded facts instead of
trusting the verdict a run stored. Pointed at this directory it reports all eight.
