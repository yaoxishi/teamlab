# Changing TeamLab

Use the [README](../README.md) to install TeamLab and run the local app.
Make changes in your own clone or fork. Use mock models while developing;
never commit model keys, service credentials or participant outputs.

## Find the right files

| Change | Files |
| --- | --- |
| An example study's task, roles or measures | [`teamlab/example_studies/`](../teamlab/example_studies/) |
| Add an example to both app menus | `EXAMPLES` in `teamlab/desktop.py` |
| Local interface | `app.py` |
| Shared-website interface | `hosted_app.py`, `teamlab/hosted/interface.py` |
| Shared wording and styles | `teamlab/presentation.py` |
| Questionnaire and scale editors | `teamlab/questionnaire_ui.py`, `teamlab/scale_editor_ui.py` |
| Study fields, defaults and validation | `teamlab/config.py` |
| Participant prompts and permitted placeholders | `teamlab/prompts.py` |
| Conversation sequencing and ending rules | `teamlab/simulation.py` |
| Private states and questionnaire collection | `teamlab/state.py`, `teamlab/survey.py` |
| Provider requests and model choices | `teamlab/llm.py`, `teamlab/hosted/models.py`, `teamlab/hosted/policy.py` |
| Saved records and result inspection | `teamlab/outputs.py`, `teamlab/results.py` |
| Workspace privacy, execution admission and saved bundles | `teamlab/hosted/` |
| Database, download gateway and scheduled cleanup | [`deploy/`](../deploy/) |

Python code now lives in one package. For example, use
`from teamlab.config import load_study`. Version 0.2.0 replaces the old
`convolab.*` Python import paths; update those imports in your own scripts.
The existing `python -m teamlab` command is unchanged.

To run an example from the command line after installation:

```powershell
.\.venv\Scripts\python.exe -m teamlab run teamlab/example_studies/opposing_view_control.yaml
```

On Linux, use `.venv/bin/python`. Results go into `outputs/` in the current
working directory. The app also supports importing a saved study setup through
the browser; importing settings does not resume an earlier execution.

## Keep source and installations consistent

The examples, built-in CSV and browser assets are package resources declared
in `pyproject.toml`. Keep each resource in its documented folder so a source
checkout and an installed package both find it. The two Streamlit entry-point
scripts are run from the downloaded repository. After changing Python code or
resources, repeat the README's local install command to refresh the installed
package before checking it from another directory.

Preserve the bundled CSV bytes and `.gitattributes`: startup checks the file's
hash, including its line endings. Create a separate custom persona file when
you need different data. Browser asset manifests also contain integrity hashes;
changing an asset requires reviewing and updating its matching manifest.
Keep dependency versions and hashes aligned when changing dependencies.

## Existing hosting identifiers

`CONVOLAB_*` setting names, `convolab_*` database functions and tables,
`convolab-private`, browser storage/component identifiers and
`convolab-checkpoint.json` are retained compatibility identifiers. They must
agree with the deployed services and existing saved records. They do not refer
to a missing Python package. Do not rename them as a cosmetic code edit.
See [hosting setup](../deploy/README.md) for the settings template and service
configuration. Never reapply database setup to an existing deployment casually.

## Check a change before sharing it

Load each affected example in the local app, inspect its saved setup and prompt
preview, then run it with mock models. Check transcripts, measurement timing,
completion status and downloaded outputs. For hosting changes, also verify
separate browser workspaces, owned downloads, expiry and failure handling.
Build a wheel and check its installed resources from outside the repository
when changing names or packaging. A mock check does not validate a real provider;
test that integration separately before making claims about it.

The public repository contains the runtime and supporting guides. Private
development tests, research outputs and service credentials are not included.
