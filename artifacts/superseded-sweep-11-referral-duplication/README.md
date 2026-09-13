# Superseded: the sweep whose referrals could not be recognised on a repeat

This is a clean twelve-case sweep that was correct in every decision it made. It is
kept because of what happened when it was **run again**, which is recorded in
`../superseded-repeat-11-referral-duplication/`.

## The defect

Escalation equivalence included the `summary` field. That summary is assembled from
model-authored text — the passenger's request as the model read it, identity candidates,
the reasoning — so the same case re-read produces the same referral in slightly
different words. On the repeat, **8 of 20 referrals were raised a second time** and 12
were correctly skipped.

`summary` was in the matcher for a real reason. Removing it once (sweep 1) collapsed
case-03 and case-04 — two different unidentified people who share an absent booking
reference, a `SUPERVISOR` queue and an `S2.2, S12.1` clause — into one referral, and
Peter Lindqvist never reached a human. Fixing that had re-broken this.

A duplicate referral is not a duplicate payment. It costs a colleague a second ticket,
not a passenger a second charge. But "the desk generates work every time you re-run it"
is a real defect, and it is the kind that looks like noise until someone is triaging
the queue.

## What replaced it

Two designs were tried. The first — keying on the booking plus a multiset of blocking
clauses — drifted too, for the same underlying reason: how finely the model chooses to
slice one complaint is not a fact about the case. See `DECISIONS.md` §8, sweep 12.

Each referral now carries a `[referral-key: xxxxxxxx]` marker derived from the inbound
message's own sha256, the queue and the disruption event, and from nothing the model
writes. The same message re-processed yields the same key however it is worded; a
genuinely new issue arrives as a different message and is never suppressed.

The current evidence is in `../full-run/` and `../repeat-run-no-duplicates/`, where the
repeat adds **zero** writes of any kind.
