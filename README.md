# TeamLab

TeamLab simulates independent participants in two- or three-person interaction
studies. You can set their task, personas, private roles and models, collect
questionnaire responses, and download conversation transcripts and other study
records. Use it to explore team dynamics and develop hypotheses. Simulated
responses do not replace evidence from human studies.

## Use the website

Open **[TeamLab](https://teamlab-prototype.streamlit.app/)**. No installation is needed.

1. Click **Connect private workspace** and wait for the connected message.
2. Create a study, import a saved YAML setup, or load an example:
   **Custom hidden-profile study**, **Opposing view (receptive)** or
   **Opposing view (control)**.
3. Review the settings and preview the prompts. Click **Run with mock models**
   to try the workflow without an API key or model charges. For real models,
   choose a model for each agent, enter your provider keys and click **Start**.
4. Open **My results** and download your research ZIP. It includes the study
   setup, assigned personas, transcripts, questionnaire answers and run records,
   plus private states and reactions when enabled.

Keep using the same browser profile to find your results. Finished outputs are
available for **24 hours**; download them promptly. Another browser or device
has a separate workspace. Clearing site data loses access to that workspace.

The shared website allows **one active execution**, up to 300 estimated model
requests and **30 minutes**, including paused time. Pause and Continue work only
while the same server is running. Free hosting can sleep or be unavailable.

Real models may charge your provider account. Keys pass through the hosting
server and remain temporary; they are excluded from saved setups, outputs and
logs. Please use only the API-key fields. Mock workflows have been tested; one
small OpenAI test passed. Other real-provider combinations remain unverified.

## Run on your own computer

Download or clone this repository. Install Python **3.12** and open a terminal
in the project folder. On Windows:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -c constraints.txt -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation .
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address=127.0.0.1 --browser.gatherUsageStats=false
```

On Linux, create the environment with `python3.12 -m venv .venv`, use
`.venv/bin/python` in the remaining commands and install from
`requirements-linux.lock` instead. Open the local address printed by Streamlit.
The local app keeps results in `outputs/` until you remove them.
Keep the bundled CSV and dependency locks
unchanged. Hosting your own shared website also requires the
[service setup](deploy/README.md).

## License and data

Code uses the [MIT license](LICENSE). Built-in ANES-derived personas have
separate [data conditions](DATA_TERMS.md) and a
[methods and citation summary](docs/DATA_METHODS.md). Follow those conditions
and cite ANES when using the data. No credentials or user results are included
in this repository.
