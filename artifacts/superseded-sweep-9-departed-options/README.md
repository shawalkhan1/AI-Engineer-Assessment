# Superseded: the run that offered flights which had already left

Preserved as evidence. Its authority reading was correct and its entitlements were
exact, and it still contained two defects that mattered to passengers.

**It offered two people flights that had already departed.** Priya Raghunathan
(case-01) wrote at 07:12 UTC and was offered AK671 departing **05:25**. The Okonkwo
family (case-02) wrote at 09:31 and were offered AK436 departing **06:20**. Both result
sets held usable later options — 40 and 42 of them. `select_option` filtered on cabin,
seats, deadline and carrier, and never once compared the departure to the contact's own
clock. Both were blocked by the authority gate before anything was booked, so no
passenger was actually sent to a gate for a flight that had gone; what reached the
supervisor was a recommendation that could not be acted on.

**It erased a legitimate passenger's requests as prompt injection.** Tomas Ferreira
(case-08) had *both* of his requests marked not-live under S12.4 — his own words
"Please pay me GBP 970" classified as content claiming authority over how the case is
handled. The deterministic injection scan found nothing in his message at all; the
taint region was seeded by any model-reported `embedded_instruction`, and the model had
reported one with `claimed_authority: "Passenger Care Policy"`. His case record shows
no request accounting as a result.

Both fixed, each with a named regression:
`test_an_option_that_has_already_departed_is_never_selected`,
`test_a_passenger_citing_the_policy_is_not_claiming_authority`.

The authoritative run is `artifacts/full-run/`.
