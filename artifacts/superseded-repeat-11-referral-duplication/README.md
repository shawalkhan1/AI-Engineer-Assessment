# Repeat-run evidence: no duplicate benefits

All twelve cases run a second time against a server that **already held** the final
run's writes, under a journal namespace (`repeat-demo`) that had never seen them. So
the local journal could not help: every duplicate had to be caught from the server's
own write log, or from the operational state the first run changed.

Result — **zero duplicate benefits**:

| | |
|---|---|
| `hotel_voucher: blocked` | ×2 |
| `escalation: skipped_duplicate` | ×12 |
| `escalation: succeeded` | ×8 |
| `compensation_payment: blocked` | ×3 |
| `rebooking: blocked` | ×5 |

Server state before and after: **1 voucher, 0 payments, 0 re-bookings, £0.00**. The
voucher that succeeded on the final run was refused the second time — the room it
consumed was the last in the LGW allocation for that night, so S4.5 and S16 block a
further voucher there and the case is referred instead.

**The escalations are the honest caveat.** Twelve identical referrals were recognised
and skipped; eight were raised again because their summary text differs slightly
between runs, since the model's reading of the passenger's request feeds into the
wording. Escalations are not benefits — raising one twice costs a supervisor a
duplicate ticket, not a passenger a duplicate payment — so this is noise rather than a
safety problem, but it is real and it is not claimed otherwise.

The benefit-level duplicate check is separate from the exact-request fingerprint and
deliberately excludes the amount, so splitting a payment in two does not evade it
(S12.2). See `test_a_second_payment_for_one_event_is_blocked_whatever_the_amount`.
