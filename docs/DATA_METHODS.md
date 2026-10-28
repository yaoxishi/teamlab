# ANES-derived persona methods and citation

This summary describes the project owner's supplied methods appendix. The owner
confirmed public-use provenance and sharing authority. The raw ANES microdata
and recoding script were not supplied, so TeamLab has not independently
reproduced the original transformation. The bundled CSV and supplied summary
statistics were checked for consistency; no rewriting of data is performed.

Source: **ANES 2016 Time Series Study, version 20190904**, with 1,180 face-to-face
and 3,090 internet respondents (4,270 total). Demographics/politics are from
pre-election interviews and TIPI personality items from post-election
self-administered questions.
[Official source and release](https://electionstudies.org/data-center/2016-time-series-study/).

| App field | Supplied derivation |
| --- | --- |
| `persona_id` | Original `respondent_id` as a string; bundled header renamed only |
| `ideology` | V161126, seven-point liberal–conservative self-placement |
| `age` | V161267; 18–90, top-coded at 90 |
| `race_ethnicity` | V161310x; Hispanic regardless of race; residual non-Hispanic multiracial/other distinguished |
| `gender` | V161342, male/female in this complete-case derivative |
| `education` | V161270; 16 levels collapsed; associate types merged; code 90 treated as high-school diploma |
| `party` | V161158x, seven-point party identification including leaners and independent |
| Five traits | TIPI V162333–V162342; paired reverse scoring and rescaling |

For each TIPI trait, the paired mean uses the positively keyed item and reversed
item `8 - x` on the original 1–7 scale. Openness pairs open/complex with
conventional/uncreative; conscientiousness dependable/self-disciplined with
disorganized/careless; extraversion extraverted/enthusiastic with reserved/quiet;
agreeableness sympathetic/warm with critical/quarrelsome; neuroticism
anxious/easily-upset with calm/emotionally-stable. Mean `s` becomes
`1 + (s - 1) * 4/6`, rounded to one decimal. These are TIPI scores, not BFI-2.

Sequential complete-case exclusions were gender refused/other (52), missing age
(107), missing race/ethnicity (14), unclassifiable/refused education (26), no
ideology placement (922), missing party identification (5), incomplete TIPI
(436). The 1,562 exclusions leave 2,708 personas. No imputation or survey weights
were applied. The derivative under-represents some respondents, including those
without ideology placement. Uniform draws reflect this file's composition,
not a weighted population estimate. Custom personas may use other values.

The packaged CSV keeps 2,709 Windows CRLF lines and SHA-256
`ab0064577dc0afd8c1ffa9946ca541318f1fd735d3c0358d40364350909cb7f7`.
The app checks its original bytes, rows, required fields and finite ranges before
using it. Missing or modified resources fail closed. Git treats this CSV as
binary to prevent newline conversion. Filtering should produce a separate
custom persona file; do not modify the bundled resource.

Code licensing does not cover these data. See [DATA_TERMS.md](../DATA_TERMS.md)
for conditions, data citation and the TIPI methods reference. Cite ANES and the
derivation when using this resource; TeamLab is not endorsed by ANES.
