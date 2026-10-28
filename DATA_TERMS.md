# Built-in ANES-derived data

The MIT license applies to TeamLab's code and project documentation. It does
not license `convolab/data/anes2016_personas.csv` or the underlying ANES data.
Third-party libraries and browser assets retain their own terms.

The combined package declares `MIT AND LicenseRef-ANES-Conditions` so packaging
metadata does not suggest that the included CSV has the code's MIT permissions.
`LicenseRef-ANES-Conditions` refers to this data notice and ANES's linked
conditions; it does not create a new data license.

The project owner confirmed that the supplied persona CSV and methods appendix
were derived from public-use ANES data, that their team can authorize sharing
these derivatives, and approved public distribution of the CSV and a methods
and citation summary on October 9, 2026. The original appendix and build
specification are excluded from the public source package.

Follow [ANES's conditions](https://electionstudies.org/data-center/#terms-of-use):

- Use the data for research or statistical purposes; do not investigate
  individual respondents.
- Do not use a respondent's identity if discovered; notify ANES of the discovery.
- Cite the ANES data and documentation. Send citations of resulting publications
  to ANES for its bibliography.
- ANES, the original collectors and funders are not responsible for users'
  interpretations or inferences.

These conditions are not a new unrestricted data license or an ANES endorsement
of TeamLab. Include this notice and the source/methods citation when sharing
the supplied derivative. Citation:

American National Election Studies (2019). *ANES 2016 Time Series Study:
Data set and documentation*, version 20190904.
[Official study and release](https://electionstudies.org/data-center/2016-time-series-study/).

Personality methods: Gosling, S. D., Rentfrow, P. J., & Swann, W. B., Jr. (2003).
*A very brief measure of the Big-Five personality domains*. Journal of Research
in Personality, 37(6), 504–528.
[DOI](https://doi.org/10.1016/S0092-6566(03)00046-1).

The derivative contains 2,708 complete-case personas, without survey weights
or imputation. Uniform draws describe this file, not the US population.
The app checks the unchanged CSV bytes against SHA-256
`ab0064577dc0afd8c1ffa9946ca541318f1fd735d3c0358d40364350909cb7f7`.
The derivation is documented in the public methods summary; the original raw
recoding has not been independently reproduced.
