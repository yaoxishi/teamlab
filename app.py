"""TeamLab's single-page Streamlit interface; research logic is in convolab."""
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
from streamlit.runtime.scriptrunner_utils.exceptions import ScriptControlException

from convolab.config import (BIG_FIVE_FIELDS, ConfigError, DEMOGRAPHIC_FIELDS, ITEM_COLUMNS,
                             PERSONA_COLUMNS, SURVEY_INSTRUCTIONS, dump_study_yaml)
from convolab.desktop import (DesktopSession, EXAMPLES, app_output_root, builtin_bytes, builtin_choices,
                             clean_grid, estimate_calls, live_transcript_text, preview_prompts, questionnaire_input,
                             progress_label, safe_study_download, table_csv)
from convolab.llm import Redactor, format_error
from convolab.prompts import ALLOWLISTS, DEFAULTS
from convolab.presentation import introduction, apply_style, study_setup_heading
from convolab.questionnaire_ui import questionnaire_editor
from convolab.results import (ResultsError, chart_data, measurement_view, open_results,
                             saved_folders, state_view, transcript_view)

OUTPUT_ROOT = app_output_root(__file__)


st.set_page_config(page_title="TeamLab", layout="wide")
apply_style()
st.title("TeamLab")
introduction()
if "desktop" not in st.session_state:
    st.session_state.desktop = DesktopSession()
session = st.session_state.desktop
editor = session.editor
prefix = f"editor_{session.revision}_"
busy = session.active


def action(method, *args, **kwargs):
    try:
        previous_revision=session.revision
        method(*args, **kwargs)
        if session.revision!=previous_revision and getattr(method,'__name__','') in {'load_example','replace_upload','replace_yaml'}:
            st.session_state.pop('study_editor_origin',None)
        st.session_state.notice = None
    except (ConfigError, ResultsError, OSError) as error:
        keys = [agent.get("api_key") for agent in session.editor.settings["agents"] if agent.get("api_key")]
        st.session_state.notice = Redactor(keys).text(str(error))


def text(label, value, key, help, *, area=False, secret=False):
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    widget = st.text_area if area else st.text_input
    options = dict(value="" if value is None else str(value), key=prefix + key, help=help, disabled=busy)
    if secret:
        options["type"] = "password"
    return widget(label, **options)


def create_new_study():
    previous_revision = session.revision
    action(session.new_study)
    if session.revision != previous_revision:
        st.session_state['study_editor_origin']='new'
        # Callbacks run before the new script: erase old editor widget values,
        # including password fields, without touching saved-result controls.
        for state_key in list(st.session_state):
            if isinstance(state_key, str) and state_key.startswith("editor_"):
                del st.session_state[state_key]
        st.session_state.pop("questionnaire_input", None)


study_setup_heading()
st.subheader("1. Create a new study")
st.write("Start with a blank setup, then add your task, agents, measures and models.")
st.button("Create a new study", key=prefix + "new_study", disabled=busy,
          help="Open a blank study with the default ANES persona pool. Replace your current setup and clear entered API keys. No API calls are made.",
          on_click=create_new_study)
st.subheader("2. Load an example study")
st.write("Open a prepared study to explore the settings. Choose Run with mock models later to try it with mock responses and no API key.")
example = st.selectbox("Example study", list(EXAMPLES), key=prefix + "example", disabled=busy,
                       help="Choose an example, then click Load example study to open it.")
st.button("Load example study", key=prefix + "load_example", disabled=busy, help="Replaces the editor and clears typed API keys.",
          on_click=action, args=(session.load_example, example))
st.subheader("3. Import a study file")
with st.expander("Import study"):
    st.write("Load study settings from a YAML file saved earlier. Choose a .yaml or .yml file, rather than the results ZIP. This loads the setup; it does not resume an earlier run. An invalid file leaves your study unchanged.")
    study_upload = st.file_uploader("Study YAML file", type=["yaml", "yml"], key=prefix + "study_upload",
                                    help="Replaces the whole study after validation and clears typed API keys.", disabled=busy)
    st.button("Apply uploaded study", key=prefix + "apply_study", help="Validate the entire upload before replacing your editor.",
              disabled=busy or study_upload is None,
              on_click=action, args=(session.replace_upload, study_upload.getvalue() if study_upload else b""))
if st.session_state.get('study_editor_origin')=='new':st.subheader('New study')
editor.settings["study_name"] = text("Study name", editor.settings["study_name"], "name", "A name for the saved execution folder.")
save_study_area = st.empty()
if st.session_state.get("notice"):
    st.error(st.session_state.notice)

st.header("Task")
editor.settings["task_instructions"] = text("Task instructions", editor.settings["task_instructions"], "task",
                                          "The task shared by every participant.", area=True)

st.header("Agents")
source_labels = {"builtin": "Use the built-in ANES 2016 personas", "custom": "Use my own personas"}
source = st.radio("Persona source", list(source_labels), index=list(source_labels).index(editor.persona_source),
                  format_func=source_labels.get, key=prefix + "source", disabled=busy,
                  help="The built-in file is verified before use; custom personas come from your typed fields or upload.")
mode_labels = {"fixed": "Same personas every run", "pool": "Draw from a persona pool"}
mode = st.radio("Persona selection", list(mode_labels), index=list(mode_labels).index(editor.persona_mode),
                format_func=mode_labels.get, key=prefix + "mode", disabled=busy,
                help="Fixed keeps the same personas. Pool draws uniformly without reuse across this execution.")
requested_source, requested_mode = source, mode
source, mode = editor.persona_source, editor.persona_mode
builtin = []
if source == "custom":
    persona_upload = st.file_uploader("Upload personas (Excel or CSV)", type=["xlsx", "csv"], key=prefix + "personas_upload",
                                      disabled=busy, help="Fixed uploads need exactly one row per seat. Pool uploads need unique text persona IDs.")
    st.button("Apply uploaded personas", key=prefix + "apply_personas", disabled=busy or persona_upload is None,
              help="Replaces only the active persona buffer after the entire candidate passes validation.",
              on_click=action, args=(session.request_import, "personas", persona_upload.getvalue() if persona_upload else b"",
                                    persona_upload.name if persona_upload else "personas.csv"))
    st.download_button("Download persona template", table_csv([], PERSONA_COLUMNS), "personas_template.csv", "text/csv",
                       key="persona_template", disabled=busy, on_click="ignore", help="CSV header with the required demographics and optional personality fields.")
    if mode == "pool":
        st.caption(f"Custom pool: {len(editor.custom_pool)} rows. Maximum runs: {len(editor.custom_pool) // len(editor.settings['agents'])}.")
        with st.expander("View uploaded personas", expanded=False):
            st.dataframe(pd.DataFrame(editor.custom_pool), hide_index=True)
else:
    try:
        builtin = builtin_choices()
        st.caption(f"{len(builtin):,} personas. Source: supplied ANES 2016 persona dataset and appendix. Uniform draws use no survey weights.")
        with st.expander("View built-in personas", expanded=False):
            search = st.text_input("Search built-in personas", key=prefix + "builtin_search", disabled=busy,
                                   help="Search persona IDs or demographic/personality values.")
            st.dataframe(pd.DataFrame([p.model_dump() for p in builtin_choices(search)]), hide_index=True)
        st.download_button("Download built-in dataset", builtin_bytes(), "anes2016_personas.csv", "text/csv",
                           key="builtin_download", disabled=busy, on_click="ignore", help="Verified original bytes, for filtering and re-uploading as custom personas.")
        if mode == "pool":
            st.info(f"This pool allows at most {len(builtin) // len(editor.settings['agents'])} runs with the selected seat count.")
    except ConfigError as error:
        st.error(str(error))
count = st.selectbox("Number of agents", [2, 3], index=len(editor.settings["agents"]) - 2, key=prefix + "count",
                     disabled=busy, help="Adds or removes the trailing seat and resets the speaker order; recheck the new seat.")
requested_count = count
for index, agent in enumerate(editor.settings["agents"]):
    with st.expander(f"Agent {index + 1}", expanded=True):
        for field, label, help in (
            ("display_name", "Display name (optional)", "Leave blank to pick a persona-appropriate name each run. Typed names must be unique, on one line, without a colon."),
            ("model", "Model", "Use mock, openai/<model>, anthropic/<model>, gemini/<model>, or ollama/<model>."),
            ("api_key", "API key", "An explicit key overrides its environment-variable reference and stays in this browser session's memory."),
            ("api_key_env", "Key environment variable (optional)", "Name of an existing environment variable; only required when used without a typed key."),
            ("api_base", "API endpoint (optional)", "Full http:// or https:// provider endpoint, without embedded credentials."),
            ("temperature", "Temperature", "Provider generation setting; incompatible models may omit it with a warning."),
            ("max_tokens", "Main response limit", "Positive whole-number output token limit for this seat's spoken replies."),
            ("role_instructions", "Private role instructions", "Visible only to this participant, unless it chooses to share them in speech.")):
            defaults = {"temperature": 1.0, "max_tokens": 400, "role_instructions": ""}
            agent[field] = text(label, agent.get(field, defaults.get(field)), f"seat{index}_{field}", help,
                                area=field == "role_instructions", secret=field == "api_key")
            if field == "api_key":
                st.caption("Keys stay in memory only and are never saved.")
        if mode == "fixed" and source == "custom":
            persona = editor.custom_fixed[index]
            for section, fields in (("Demographics", ("persona_id",) + DEMOGRAPHIC_FIELDS), ("Personality", BIG_FIVE_FIELDS)):
                with st.expander(section, expanded=False):
                    for field in fields:
                        persona[field] = text(field.replace("_", " ").capitalize(), persona.get(field), f"persona{index}_{field}",
                            "Optional text ID; leading zeros are preserved." if field == "persona_id" else
                            "Optional score from 1 to 5; blank omits it." if field in BIG_FIVE_FIELDS else
                            "Required whole-number age." if field == "age" else "Required persona demographic wording.")
        elif mode == "fixed" and builtin:
            choices = [None] + [p.persona_id for p in builtin]
            by_id = {p.persona_id: p for p in builtin}
            selected = editor.builtin_fixed_ids[index]
            selected = st.selectbox("Built-in persona ID", choices, index=choices.index(selected) if selected in choices else 0,
                format_func=lambda value: "Choose a persona" if value is None else value + " — " + ", ".join(str(getattr(by_id[value], f)) for f in PERSONA_COLUMNS[1:]),
                key=prefix + f"builtin_id_{index}", disabled=busy, help="Type to search by ID or the displayed demographic values. Each fixed seat needs a distinct ID.")
            editor.builtin_fixed_ids[index] = selected
            if selected:
                with st.expander("View selected persona", expanded=False):
                    st.dataframe(pd.DataFrame([by_id[selected].model_dump()]), hide_index=True)

st.header("Questionnaire")
editor.settings["items"], questionnaire_redraw = questionnaire_editor(editor.settings.get("items", []),
    revision=session.revision, prefix=prefix, baseline_key="questionnaire_input", disabled=busy)
items_upload = st.file_uploader("Upload questionnaire CSV", type=["csv"], key=prefix + "items_upload", disabled=busy,
                                help="Replaces the question table only after validation; seat keys and settings stay in memory.")
st.button("Apply uploaded questionnaire", key=prefix + "apply_items", disabled=busy or items_upload is None,
          help="Check and apply the complete question table.", on_click=action,
          args=(session.request_import, "items", items_upload.getvalue() if items_upload else b"", items_upload.name if items_upload else "items.csv"))
st.download_button("Download questionnaire CSV", table_csv(editor.settings["items"], ITEM_COLUMNS), "items.csv", "text/csv",
                   on_click="ignore", key="items_download", disabled=busy, help="Includes all question columns and TRUE/FALSE phase flags.")
instructions = editor.settings.setdefault("survey_instructions", dict(SURVEY_INSTRUCTIONS))
for phase in ("pre", "turn", "post"):
    instructions[phase] = text({"pre": "Before-conversation survey instructions", "turn": "During-conversation survey instructions", "post": "After-conversation survey instructions"}[phase], instructions.get(phase, SURVEY_INSTRUCTIONS[phase]),
                               "instructions_" + phase, "Instructions shown only during this questionnaire phase.", area=True)

st.header("Run settings")
st.caption("A run is one independent conversation. We recommend starting with 1 run. Each turn gives one agent a chance to speak; a pass also counts as a turn.")
run = editor.settings["run"]
for field, label, help in (("n_runs", "Number of runs", "We recommend starting with 1. Each run has fresh participant states and conversation history."),
                           ("max_turns", "Maximum conversation turns", "Maximum turns across all agents in one conversation. A turn is one agent’s chance to speak, including a pass; it is not a full round."),
                           ("measure_every_n_turns", "Survey every N conversation turns", "Ask during-conversation questions at turns N, 2N, 3N and so on after public events. A hidden pass at a scheduled turn does not trigger or postpone a survey.")):
    run[field] = st.number_input(label, min_value=1, value=int(run[field]), step=1, key=prefix + field, disabled=busy, help=help)
order = text("Speaker order", ", ".join(str(v) for v in run["speaker_order"]), "speaker_order", "Comma-separated seat numbers, each exactly once, such as 1, 2, 3.")
run["speaker_order"] = [part.strip() for part in order.split(",")]
for field, label, help in (
    ("shuffle_order_each_run", "Shuffle speaker order each run", "Shuffles once per run; the selected order remains round robin within that run."),
    ("allow_pass", "Allow pass", "Let an agent return exactly [PASS] instead of speaking. A pass still uses one conversation turn. Consecutive passes from all agents automatically end the conversation.")):
    run[field] = st.checkbox(label, value=run[field], key=prefix + field, disabled=busy, help=help)
run["silence_visibility"] = st.selectbox("Silence visibility", ["public", "hidden"], index=["public", "hidden"].index(run["silence_visibility"]),
    key=prefix + "visibility", disabled=busy or not run["allow_pass"],
    help="Public silence updates every agent's state and may trigger surveys. Hidden passes stay out of participant context. Disabling passes preserves this choice.")
for field, label, help in (
    ("allow_early_end", "Allow early ending", "Let agents end early when each ends its spoken reply with [END] in the same completed round. Earlier confirmations do not carry forward. These replies remain in the conversation; consecutive passes can also end it when enabled."),
    ("remember_pre_survey", "Remember own pre-conversation survey responses", "Let each agent see only its own valid pre-conversation survey responses in later speech and questionnaire prompts. Other agents cannot see them, and they are not included in state-update prompts."),
    ("randomize_item_order", "Randomize questionnaire order", "A separate seeded stream determines each administration's item order."),
    ("inner_reaction", "Collect private reactions", "Adds a private reaction to nonempty surveys; reactions never enter participant memory."),
    ("private_state", "Retain each agent’s private state during the conversation", "Keep and update each agent’s own simulated state after spoken messages and visible silence, before further questionnaires or speech. Agents see only their own state; hidden passes do not update it.")):
    run[field] = st.checkbox(label, value=run[field], key=prefix + field, disabled=busy, help=help)
run["seed"] = text("Seed (optional)", run.get("seed"), "seed", "Optional integer for reproducible draws/names/orders/mock answers. Blank generates and saves an effective seed.")
with st.expander("Advanced"):
    st.caption("Please inspect the prompts here to check what agents are told. Edit defaults only deliberately, keep required placeholders, and use Preview prompts to inspect an assembled example without API calls.")
    for field in ("state_max_tokens", "survey_max_tokens"):
        run[field] = st.number_input("Private-state response limit (tokens)" if field=="state_max_tokens" else "Questionnaire response limit (tokens)", min_value=1, value=int(run[field]), step=1,
                                     key=prefix + field, disabled=busy, help="Maximum output tokens for one state response or one agent’s complete questionnaire response, including any reaction. Retries keep the same limit. Reasoning may use part of it.")
    templates = editor.settings.setdefault("prompt_templates", {})
    for field, default in DEFAULTS.items():
        templates[field] = text(field, templates.get(field, default), "template_" + field,
                                "Allowed placeholders: " + (", ".join(sorted(ALLOWLISTS[field])) or "none") + ". Required placeholders must remain present.", area=True)

# Callbacks record intentions. Read all current widgets before imports, previews
# or freezing a start, including edits delivered in the same redraw as a click.
if requested_source != source or requested_mode != mode or requested_count != len(editor.settings["agents"]):
    session.set_source_mode(requested_source, requested_mode)
    if requested_count != len(editor.settings["agents"]):
        session.resize(requested_count)
    st.rerun()
if questionnaire_redraw:
    st.rerun()
if session._requested_import is not None:
    action(session.apply_requested_import)
    st.rerun()
if session._requested_start is not None:
    action(session.prepare_requested_start)
    st.rerun()
if session._requested_preview is not None:
    action(session.prepare_requested_preview)
    st.rerun()
study, demo_study, validation_error, ready = None, None, None, False
try:
    study = session.editor.validated()
    _, _, warnings = session.validate()
    ready = True
    for warning in dict.fromkeys(warnings):
        st.warning(warning)
except ConfigError as error:
    validation_error = str(error)
try:
    demo_study, _, _ = session.validate(demo=True)
except ConfigError:
    pass
save_study_area.download_button("Save study (YAML)", safe_study_download(study) if study else "", "study.yaml", "text/yaml", key="save_study",
                   disabled=busy or study is None, on_click="ignore", help="Downloads active settings with explicit API keys removed.")
preview_seat = st.selectbox("Preview seat", list(range(1, count + 1)), key=prefix + "preview_seat", disabled=busy,
                            help="Resolve an example persona/name without making an execution draw or a request.")
st.button("Preview prompts", disabled=busy or demo_study is None, key=prefix + "preview",
          help="Shows actual builder output with clearly labeled sample state/history. No requests or execution RNG use.",
          on_click=action, args=(session.request_preview, preview_seat))
if session.preview:
    st.info(session.preview["note"])
    st.write(f"Resolved example: seat {session.preview['seat']}, {session.preview['name']}")
    st.json(session.preview["persona"])
    if session.preview["sample_state"]:
        st.caption("Sample state — not a generated result")
        st.json(session.preview["sample_state"])
    for block in session.preview["blocks"]:
        with st.expander(block["label"]):
            st.code(block["system"], language=None)
            st.code(block["user"], language=None)

st.header("Run")
if validation_error:
    st.error(validation_error)
if demo_study:
    estimate = estimate_calls(demo_study)
    st.info(f"Maximum-length initial estimate: {estimate['per_run']} requests per run; {estimate['total']} across this execution. "
            f"At most {estimate['retry_ceiling']} with two retries per administration. Hidden passes, endings and failures can reduce this. Mock requests have no API cost.")
st.button("Start", key=prefix + "start", disabled=busy or not ready, on_click=action,
          args=(session.request_start, "start"), help="Captures the validated settings and resolved keys, then starts one synchronous execution. Real providers may incur charges.")
st.button("Run with mock models", key=prefix + "demo", disabled=busy or demo_study is None, on_click=action,
          args=(session.request_start, "demo"), help="Runs a copied study with mock models; the editor stays unchanged and no API is contacted.")
st.caption("Runs your current setup with mock responses. No provider API calls, API key or API charges. Your selected models stay unchanged.")
bar = st.progress(0, text="Ready" if not busy else "Starting execution")
live = st.empty()
live_errors = st.empty()
if session.last_error:
    st.error(session.last_error)
    if session.execution_identity:
        st.caption("Retained files: " + session.execution_identity)
if busy:
    running_progress = {}
    display_connected = [True]
    def update(event):
        if event["event"] == "run_started":
            running_progress.clear()
        running_progress.update(event)
        maximum = session.frozen_study.run.max_turns
        fraction = ((running_progress.get("number", 1) - 1) + min(running_progress.get("turn", 0), maximum) / maximum) / session.frozen_study.run.n_runs
        if not display_connected[0]:
            return
        try:
            bar.progress(min(1.0, fraction), text=progress_label(event, session.frozen_study, running_progress))
            live.text(live_transcript_text(session.live_transcript, session.frozen_study.run.silence_visibility))
            if event.get("error_details"):
                live_errors.warning(format_error(event["error_details"], folder=session.execution_identity))
            elif event.get("error"):
                live_errors.error(str(event["error"]))
        except ScriptControlException:
            # A reload/disconnect can remove this display context. Only this
            # original synchronous coordinator continues; reruns never resume it.
            display_connected[0] = False
    finished = session.execute_pending(output_root=OUTPUT_ROOT, progress=update)
    if finished:
        action(lambda: setattr(st.session_state, "saved_results", open_results(OUTPUT_ROOT, finished.folder)))
    st.rerun()

st.header("Results")
folders = saved_folders(OUTPUT_ROOT)
if folders:
    current = session.result_paths[-1] if session.result_paths else None
    selected = st.selectbox("Open existing results", folders, index=folders.index(current) if current in folders else 0,
                            format_func=lambda path: str(path.relative_to(OUTPUT_ROOT)), key="results_folder",
                            help="Inspect saved records only. No credentials, built-in dataset loading, requests or resume.")
    if st.button("Open selected results", key="open_results", help="Read this saved execution without launching or resuming requests."):
        action(lambda: setattr(st.session_state, "saved_results", open_results(OUTPUT_ROOT, selected)))
    if st.button("Refresh results", key="refresh_results", help="Re-read retained files and the operating-system owner lock; never restart work."):
        if st.session_state.get("saved_results"):
            action(lambda: setattr(st.session_state, "saved_results", open_results(OUTPUT_ROOT, st.session_state.saved_results.folder)))
    try:
        results = st.session_state.get("saved_results")
        if results is None:
            st.info("Choose a saved execution, then press Open selected results. This does not start requests.")
            st.stop()
        selected = results.folder
        st.caption("Viewing retained files: " + str(selected))
        status = results.metadata.get("status", "unknown")
        failed = sum(row.get("status") == "failed" for row in results.tables["runs"])
        st.write(f"Execution: {status}. Failed runs: {failed}. Frozen silence visibility: {results.visibility}. Persona source: {results.persona_source}.")
        st.caption("Execution completed means all requested runs were finalized, including failed runs. It does not mean every conversation or measurement succeeded.")
        st.write("Saved records verified complete: " + ("Yes" if results.files_complete else "No / unverified"))
        st.write("Measurements complete: " + ("Yes" if results.measurements_complete else "No / unverified"))
        for warning in results.warnings:
            st.warning(warning)
        for error in results.errors:
            st.error(error)
        run_ids = [r["run_id"] for r in results.tables["runs"] if r.get("run_id")]
        if run_ids:
            chosen_run = st.selectbox("Run to inspect", run_ids, key="result_run_" + str(selected), help="Runs have independent personas, states and measurements.")
            row = next(r for r in results.tables["runs"] if r["run_id"] == chosen_run)
            st.write(f"Conversation: {row.get('status', 'unknown')}; ending: {row.get('end_reason', 'unknown')}.")
            transcript = transcript_view(results, chosen_run)
            st.dataframe(pd.DataFrame(transcript), hide_index=True)
            measurements = measurement_view(results, chosen_run)
            st.subheader("Questionnaire measurements")
            st.dataframe(pd.DataFrame(measurements), hide_index=True)
            if results.tables["reactions"]:
                st.caption("Private reactions — researcher only")
                st.dataframe(pd.DataFrame([r for r in results.tables["reactions"] if r.get("run_id") == chosen_run]), hide_index=True)
            likert_ids = [item["item_id"] for item in results.study.get("items", []) if item.get("type") == "likert"]
            if likert_ids:
                chosen_item = st.selectbox("Likert question", likert_ids, key="result_item_" + str(selected),
                                           help="One line per agent. Pre, turn N and post remain distinct administrations; see after_turn in the table.")
                chart = chart_data(results, chosen_run, chosen_item)
                if not chart.empty:
                    plotted = chart.rename_axis("Administration").reset_index().melt(
                        id_vars=["Administration"], var_name="Agent", value_name="Response")
                    st.altair_chart(alt.Chart(plotted).mark_line().encode(
                        x=alt.X("Administration:N", sort=list(chart.index), title="Administration"),
                        y=alt.Y("Response:Q", title="Response"),
                        color=alt.Color("Agent:N", title="Agent")))
                    st.dataframe(chart)
            uncommitted = results.uncommitted.get(chosen_run, [])
            if uncommitted:
                st.error(f"Public events {uncommitted} have no complete state commitment. Earlier snapshots are historical; they are not current for those events.")
            if results.study.get("run", {}).get("private_state", True):
                st.subheader("Private states — researcher only")
                versions = results.versions.get(chosen_run, [])
                if versions:
                    seats = sorted({s["seat"] for s in results.states if s["run_id"] == chosen_run})
                    state_seat = st.selectbox("State seat", seats, key="state_seat_" + str(selected), help="Shows only this seat's own committed state.")
                    version = st.selectbox("Committed version", versions, index=len(versions) - 1, key="state_version_" + str(selected),
                                           help="Complete all-seat batches only. Earlier versions remain historical after an uncommitted final event.")
                    view = state_view(results, chosen_run, state_seat, version)
                    snapshot = view["snapshot"]
                    st.write(f"Seat {state_seat} ({snapshot['agent']}), version {version}; public event {snapshot['public_event_index']} ({snapshot['event_type']}); cumulative spoken messages {snapshot['public_message_index']}; after opportunity {snapshot['after_turn']}.")
                    st.json(snapshot["state"])
                    st.dataframe(pd.DataFrame(view["related_surveys"]), hide_index=True)
                    if view.get("related_reactions"):
                        st.dataframe(pd.DataFrame(view["related_reactions"]), hide_index=True)
                else:
                    st.info("No complete state version is available for this run.")
        st.download_button("Download results (zip)", results.archive or b"", selected.name + ".zip", "application/zip", key="results_zip",
                           disabled=results.archive is None, on_click="ignore", help="Saved research files and readable transcripts, excluding API keys and the ownership lock.")
    except (ResultsError, OSError) as error:
        st.error(str(error))
else:
    st.info("No saved execution folders yet. Load an example and run a mock demonstration to create results.")
