# Example studies

These are the same three setups offered by the app's **Example study** list.
Every agent uses `model: mock`, so the supplied files run without API keys or
model charges. Mock responses demonstrate the workflow; they do not follow
the study's substantive instructions as a real language model would.

| File | Example in the app | What it sets up |
| --- | --- | --- |
| [hidden_profile_study.yaml](hidden_profile_study.yaml) | Custom hidden-profile study | Three hiring-committee agents with different private notes; three runs, private states and questionnaires. |
| [opposing_view_receptive.yaml](opposing_view_receptive.yaml) | Opposing view (receptive) | Two agents with opposing positions, drawn from the ANES persona pool; one agent receives receptive-response instructions. |
| [opposing_view_control.yaml](opposing_view_control.yaml) | Opposing view (control) | The corresponding two-agent setup without the receptive-response instructions. |

The opposing-view examples discuss campus sexual-assault policy. Review the
task and private roles before using or adapting them.

## Make your own study

1. Copy a YAML file to a new filename outside this folder. Keep the original
   examples available as references.
2. Import your copy through **Setup a study → Import a study file**. You can
   edit the setup in the app and save a new YAML file without writing code.
3. Review the task, each agent's persona and private role, questionnaire items,
   model choices, run settings and prompt preview. Start with one run and mock
   models to check the workflow. For real models, enter keys in the app's
   API-key fields; never put keys in example files or Git.

When editing YAML directly, keep the field names, indentation and value types
shown in the original. `items` contains individual questions; `scale` groups
them in the editor. `pre`, `turn` and `post` choose measurement timing. The
`run` section controls repetitions, conversation turns and collection settings.
The app validates an imported setup before replacing the current study.

To offer an additional example in your own copy of the app, add a mock-only
YAML file here and add its display label and filename to `EXAMPLES` in
[desktop.py](../desktop.py). Both interfaces use that list. Reinstall after
editing an installed copy, and check that the new file is included in the
built package.
