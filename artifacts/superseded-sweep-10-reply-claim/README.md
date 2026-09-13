# Superseded: the run whose reply contradicted itself

Preserved as evidence. Every decision in it is the same as the final run's; one
sentence of one reply was wrong, and it was wrong in the direction that matters.

Tomas Ferreira's reply (case-08) correctly said "We have worked out what you are owed
and we agree you are owed it... No money has moved yet" — and then, four paragraphs
later, "**We have paid what the policy entitles you to.**" That sentence came from a
referral note written while compensation was still being executed. After compensation
became a referral it was simply false, and it sat in the same letter as the truth.

Found by a consistency check comparing completion claims in the reply against actions
that actually succeeded. The first version of that check was useless: it matched
substrings, so it flagged case-07's "**no** payment has been made yet" and would have
missed a real claim phrased with a negation elsewhere in the sentence. It is now
negation-aware and lives in `tools/inspect_run.py` as check 7b, with
`test_the_completion_claim_detector_understands_negation` pinning it.

The authoritative run is `artifacts/full-run/`.
