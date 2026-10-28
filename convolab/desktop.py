"""Request-free editor operations and the explicit synchronous UI coordinator.

This module knows no Streamlit APIs. Widget rendering belongs in app.py.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import csv
from importlib.resources import files
import io
from pathlib import Path
import random
import threading

from .config import (BIG_FIVE_FIELDS, ConfigError, ITEM_COLUMNS, PERSONA_COLUMNS,
                     RunSettings, SURVEY_INSTRUCTIONS, StudyEditor, dump_study_yaml,
                     freeze_execution, import_items, import_personas, load_study_yaml,
                     load_builtin_personas, resolve_credentials, study_to_dict, validate_study)
from .llm import Redactor, validate_capabilities, format_error
from .prompts import Participant, PromptBuilder, PublicEvent
from .simulation import resolve_names, run_study
from .state import mock_initial_state


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = PROJECT_ROOT / "outputs"
EXAMPLES = {"Custom hidden-profile study": "hidden_profile_study.yaml",
            "Opposing view (receptive)": "opposing_view_receptive.yaml",
            "Opposing view (control)": "opposing_view_control.yaml"}


def app_output_root(app_file):
    return Path(app_file).resolve().parent / "outputs"


def blank_editor():
    return StudyEditor({"study_name": "", "task_instructions": "",
                        "agents": [{"model": "mock"}, {"model": "mock"}], "items": [],
                        "run": RunSettings(speaker_order=(1, 2)).model_dump(mode="json"),
                        "survey_instructions": dict(SURVEY_INSTRUCTIONS), "prompt_templates": {}},
                       [{}, {}], [None, None], [], "builtin", "pool")


def table_csv(rows, columns):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: "TRUE" if value is True else "FALSE" if value is False else value
                         for key, value in row.items()})
    return buffer.getvalue().encode("utf-8")


def builtin_bytes():
    load_builtin_personas()  # Verify bytes before offering a download.
    return files("convolab").joinpath("data", "anes2016_personas.csv").read_bytes()


def builtin_choices(search=""):
    query = search.casefold().strip()
    return [p for p in load_builtin_personas()
            if not query or query in " ".join(str(v) for v in p.model_dump().values()).casefold()]


def safe_study_download(study):
    return Redactor([agent.api_key for agent in study.agents if agent.api_key]).text(dump_study_yaml(study))


def decode_upload(data):
    try:
        return data.decode("utf-8-sig")
    except UnicodeError:
        raise ConfigError("Study upload must be UTF-8 YAML. The editor has not changed.") from None


def questionnaire_input(state, state_key, revision, rows):
    """Keep a private, unchanged input for cumulative data-editor deltas.

    Feeding the edited result back as input resets Streamlit's dynamic table
    identity and can discard edits or apply row changes twice. Replacements
    and imports increment the editor revision and establish a fresh baseline.
    Use one session-state slot, cleared with other private state on auth loss.
    """
    baseline = state.get(state_key)
    if baseline is None or baseline[0] != revision:
        baseline = (revision, deepcopy(rows))
        state[state_key] = baseline
    return deepcopy(baseline[1])


def clean_grid(rows):
    """Grid blanks become None without coercing IDs or finite numeric values."""
    import math
    return [{key: None if isinstance(value, float) and math.isnan(value) else value
             for key, value in row.items()} for row in rows]


def estimate_calls(study):
    a, r = len(study.agents), study.run
    p, t, q = (int(any(getattr(item, phase) for item in study.items)) for phase in ("pre", "turn", "post"))
    per_run = r.max_turns + a * (p + r.max_turns // r.measure_every_n_turns * t + q)
    per_run += int(r.private_state) * a * (1 + r.max_turns)
    return {"per_run": per_run, "total": per_run * r.n_runs, "retry_ceiling": per_run * r.n_runs * 3}


def preview_prompts(study, seat=1):
    """Use real builders, with a private local RNG and explicitly described samples."""
    frozen = freeze_execution(study)
    if not 1 <= seat <= len(study.agents):
        raise ConfigError("Choose an actual seat for the preview.")
    personas = frozen.fixed_personas or frozen.persona_pool[:len(study.agents)]
    names = resolve_names(study.agents, personas, random.Random(0))
    participant = Participant(seat, names[seat - 1], personas[seat - 1], study.agents[seat - 1].role_instructions)
    builder = PromptBuilder(study.prompt_templates)
    sample = mock_initial_state(tuple(name for name in names if name != participant.display_name))
    events = (PublicEvent(1, names[0], "message", "Sample public message."),)
    sample_answers = {item.item_id: item.min if item.type == "likert" else "Sample pre answer."
                      for item in study.items if item.pre}
    memory = builder.pre_memory(study.items, sample_answers)
    blocks = []
    def add(label, call_type, user, *, sample_state=False, pre_memory=""):
        blocks.append({"label": label, "system": builder.system(
            participant, names, study.task_instructions, study.run, call_type,
            own_state=sample if sample_state and study.run.private_state else None, pre_memory=pre_memory), "user": user})
    if study.run.private_state:
        add("Exact initialization for this resolved example persona/name", "state_init",
            builder.state_user(participant.display_name, names, (), initializing=True, run=study.run))
        add("State update — sample prior state and public message; not a generated result", "state_update",
            builder.state_user(participant.display_name, names, events, initializing=False, run=study.run), sample_state=True)
    add("Main — sample state/history and own pre answers; not a generated result", "main",
        builder.main_user(participant.display_name, events, study.run), sample_state=True, pre_memory=memory)
    for phase in ("pre", "turn", "post"):
        selected = tuple(item for item in study.items if getattr(item, phase))
        if selected:
            add(f"{phase.capitalize()} survey — sample state/context; not a generated result", "survey",
                builder.survey_user(participant.display_name, phase, () if phase == "pre" else events,
                                    selected, study.run, study.survey_instructions),
                sample_state=True, pre_memory="" if phase == "pre" else memory)
    return {"seat": seat, "name": participant.display_name, "persona": participant.persona.model_dump(),
            "sample_state": sample.to_dict() if study.run.private_state else None, "blocks": blocks,
            "note": "Pool previews use the first available personas as examples, not an execution draw. "
                    "Automatic preview names use a separate local RNG. No requests are sent."}


@dataclass
class DesktopSession:
    editor: StudyEditor = field(default_factory=blank_editor)
    revision: int = 0
    active: bool = False
    frozen_study: object | None = None
    frozen_credentials: tuple = field(default=(), repr=False)
    frozen_redaction_secrets: tuple = field(default=(), repr=False)
    progress: dict = field(default_factory=dict)
    execution_identity: str | None = None
    result_paths: list[Path] = field(default_factory=list)
    last_error: str | None = None
    preview: dict | None = None
    live_transcript: list[dict] = field(default_factory=list)
    _pending: str | None = field(default=None, repr=False)
    _requested_start: str | None = field(default=None, repr=False)
    _requested_import: tuple | None = field(default=None, repr=False)
    _requested_preview: int | None = field(default=None, repr=False)
    _requested_owner: object | None = field(default=None, repr=False)
    _pending_owner: object | None = field(default=None, repr=False)

    def check_editable(self):
        if self.active:
            raise ConfigError("This execution is running. Wait for it to finish before changing the study.")

    def new_study(self):
        self.check_editable()
        self.editor = blank_editor()
        self.revision += 1
        self.preview = None

    def replace_yaml(self, text):
        self.check_editable()
        candidate = StudyEditor.from_study(load_study_yaml(text))
        self.editor = candidate  # Commit only after every shared check passed; old keys disappear.
        self.revision += 1
        self.preview = None

    def replace_upload(self, data):
        self.replace_yaml(decode_upload(data))

    def load_example(self, label):
        self.replace_yaml(files("convolab").joinpath("data", "examples", EXAMPLES[label]).read_text(encoding="utf-8"))

    def import_personas(self, data, filename):
        self.check_editable()
        if self.editor.persona_source != "custom":
            raise ConfigError("Choose Use my own personas before uploading personas.")
        rows = import_personas(data, filename, mode=self.editor.persona_mode,
                               n_agents=len(self.editor.settings["agents"]))
        candidate = deepcopy(self.editor)
        if candidate.persona_mode == "fixed":
            candidate.custom_fixed = [p.model_dump() for p in rows]
        else:
            candidate.custom_pool = [p.model_dump() for p in rows]
        candidate.validated()
        self.editor = candidate
        self.revision += 1
        self.preview = None

    def import_items(self, data, filename):
        self.check_editable()
        items = import_items(data, filename)
        candidate = deepcopy(self.editor)
        candidate.settings["items"] = [item.model_dump() for item in items]
        candidate.validated()
        self.editor = candidate
        self.revision += 1
        self.preview = None

    def set_source_mode(self, source=None, mode=None):
        self.check_editable()
        if source is not None:
            self.editor.persona_source = source
        if mode is not None:
            self.editor.persona_mode = mode
        self.revision += 1
        self.preview = None

    def resize(self, count):
        self.check_editable()
        self.editor.resize(count)
        self.revision += 1
        self.preview = None

    def validate(self, *, environment=None, demo=False):
        study = self.editor.validated()
        if demo:
            data = study_to_dict(study)
            for agent in data["agents"]:
                agent.update(model="mock", api_key_env=None, api_base=None)
            study = validate_study(data)
        credentials = resolve_credentials(study, environment)
        parameters = validate_capabilities(study)
        return study, credentials, tuple(warning for p in parameters for warning in p.warnings)

    def queue_start(self, kind="start", *, environment=None):
        self.check_editable()
        if kind not in {"start", "demo"}:
            raise ConfigError("Unknown execution action.")
        study, credentials, _ = self.validate(environment=environment, demo=kind == "demo")
        self.frozen_study = freeze_execution(study).study
        self.frozen_credentials = credentials
        self.frozen_redaction_secrets = tuple(agent.get("api_key") for agent in self.editor.settings["agents"] if agent.get("api_key"))
        self._pending, self.active = kind, True
        self._pending_owner = threading.current_thread()
        self.last_error, self.progress, self.preview = None, {}, None
        self.live_transcript = []

    def request_start(self, kind):
        self.check_editable()
        if kind not in {"start", "demo"} or self._requested_start is not None:
            raise ConfigError("An execution action is already requested or invalid.")
        self._requested_start = kind
        self._requested_owner = threading.current_thread()

    def prepare_requested_start(self):
        kind, self._requested_start = self._requested_start, None
        if kind is not None:
            if self._requested_owner is not threading.current_thread():
                self._requested_owner = None
                raise ConfigError("The original Start action was interrupted before execution. No requests were sent. Press Start again when ready.")
            self._requested_owner = None
            self.queue_start(kind)

    def request_import(self, kind, data, filename):
        self.check_editable()
        if kind not in {"personas", "items"}:
            raise ConfigError("Unknown import action.")
        self._requested_import = (kind, data, filename)

    def apply_requested_import(self):
        requested, self._requested_import = self._requested_import, None
        if requested is not None:
            kind, data, filename = requested
            (self.import_personas if kind == "personas" else self.import_items)(data, filename)

    def request_preview(self, seat):
        self.check_editable()
        self._requested_preview = seat

    def prepare_requested_preview(self):
        seat, self._requested_preview = self._requested_preview, None
        if seat is not None:
            self.preview = preview_prompts(self.editor.validated(), seat)

    def execute_pending(self, *, output_root=OUTPUT_ROOT, request=None, sleep=None, progress=None,
                        environment=None, store_factory=None):
        """Consume a user action once. Config/path presence can never dispatch work."""
        if not self.active or self._pending is None:
            return None
        if self._pending_owner is not threading.current_thread():
            self._pending = self._pending_owner = None
            self.active = False
            self.frozen_credentials = self.frozen_redaction_secrets = ()
            self.last_error = "The original Start action was interrupted before execution. No requests were sent. A reload cannot launch or resume it."
            return None
        self._pending = None  # Remove replay trigger before entering the coordinator.
        owner = threading.get_ident()
        study = self.frozen_study
        # Frozen resolved keys win over later environment changes, and never serialize.
        data = study_to_dict(study)
        for agent, key in zip(data["agents"], self.frozen_credentials, strict=True):
            agent["api_key"] = key
        def update(event):
            if threading.get_ident() != owner:
                raise RuntimeError("UI progress must run on the coordinator thread.")
            if event["event"] == "run_started":
                self.progress = {}
                self.execution_identity = str(event["folder"])
                self.live_transcript = []
            elif event["event"] == "turn":
                self.live_transcript.append(dict(event))
            self.progress.update(event)
            if progress:
                progress(dict(event))
        options = {"output_root": output_root, "request": request, "sleep": sleep, "progress": update,
                   "environment": environment, "additional_secrets": self.frozen_redaction_secrets}
        if store_factory is not None:
            options["store_factory"] = store_factory
        try:
            execution_study = validate_study(data)
            result = run_study(execution_study, **options)
            self.execution_identity = str(result.folder)
            self.result_paths.append(result.folder)
            self.last_error = result.error
            return result
        except Exception as error:
            # Application/provider exceptions are handled by the engine. Setup failures
            # still need a safe UI explanation and retained-folder reference.
            folder = getattr(error, "folder", None)
            if folder is not None:
                self.result_paths.append(Path(folder))
                self.execution_identity = str(folder)
            if isinstance(error, (ConfigError, OSError)):
                self.last_error = "Execution could not start or save its results. " + Redactor(
                    [k.get_secret_value() for k in self.frozen_credentials if k]).text(str(error))
            else:
                self.last_error = "An application error stopped execution. Retained files remain available; inspect them before starting a new execution."
            return None
        finally:
            self.active = False
            self._pending_owner = None
            self.frozen_credentials = ()
            self.frozen_redaction_secrets = ()


def progress_label(event, study, prior=None):
    prior = prior or {}
    number = event.get("number", prior.get("number", 1))
    turn = event.get("turn", prior.get("turn", 0))
    base = f"Run {number} of {study.run.n_runs}, turn {turn} of {study.run.max_turns}"
    stage = {"state_init": "Initializing private states", "state_update": f"Updating private states after turn {turn}",
             "survey": "Collecting surveys", "main": "Waiting for the next participant"}.get(event.get("call_type"), "")
    return base + (" — " + stage if stage else "")


def live_transcript_text(events, visibility):
    return "\n\n".join(f"{event['speaker']}: " +
        ("[Visible silence]" if visibility == "public" else "[Hidden pass — researcher only]")
        if event["passed"] else f"{event['speaker']}: {event['text']}" for event in events)
