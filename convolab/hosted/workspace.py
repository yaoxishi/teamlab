"""Per-browser editor/credential lifecycle; no filesystem or global result list."""
from copy import deepcopy
from dataclasses import dataclass, field
from functools import wraps
import threading

from pydantic import SecretStr

from convolab.config import (ConfigError, StudyEditor, dump_study_yaml, load_study_yaml,
                            import_personas, import_items, study_to_dict, validate_study)
from convolab.desktop import blank_editor, decode_upload, EXAMPLES
from importlib.resources import files
from convolab.llm import Redactor
from .auth import VerifiedIdentity
from .policy import freeze_hosted_inputs, validate_hosted_policy
from .settings import HostedError


WIDGET_PREFIX = "hosted_private_"
WORKSPACE_KEY = WIDGET_PREFIX + "workspace"


def _synchronized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


@dataclass(repr=False)
class HostedWorkspace:
    owner_id: str
    editor: StudyEditor = field(default_factory=blank_editor, repr=False)
    _keys: dict[int, SecretStr] = field(default_factory=dict, repr=False)
    revision: int = 0
    _lock: object = field(default_factory=threading.RLock, repr=False, compare=False)

    def __repr__(self):
        return "HostedWorkspace(private)"

    def require_owner(self, identity):
        if not isinstance(identity, VerifiedIdentity) or identity.owner_id != self.owner_id:
            raise HostedError("The requested private workspace is unavailable.")

    @_synchronized
    def new_study(self, identity):
        self.require_owner(identity)
        self.clear_keys()
        self.editor = blank_editor()
        self.revision += 1

    @_synchronized
    def clear_keys(self):
        self._keys.clear()
        for agent in self.editor.settings["agents"]:
            agent.pop("api_key", None)

    @_synchronized
    def set_key(self, identity, seat, value, *, expected_revision=None):
        self.require_owner(identity)
        if expected_revision is not None and expected_revision != self.revision:
            return  # A stale UI render cannot restore a terminal-cleared key.
        if type(seat) is not int or not 1 <= seat <= len(self.editor.settings["agents"]):
            raise ConfigError("Choose an existing agent seat.")
        if not isinstance(value, str):
            raise ConfigError("Enter a text API key in the password field.")
        if value.strip():
            self._keys[seat] = SecretStr(value)
        else:
            self._keys.pop(seat, None)

    @_synchronized
    def replace_yaml(self, identity, text):
        self.require_owner(identity)
        candidate = load_study_yaml(text)  # Original import clears explicit keys.
        validate_hosted_policy(candidate)
        new_editor = StudyEditor.from_study(candidate)
        self.clear_keys()
        self.editor = new_editor
        self.revision += 1

    @_synchronized
    def study(self, identity, *, demo=False):
        self.require_owner(identity)
        study = self.editor.validated()
        validate_hosted_policy(study)
        if demo:
            data = study_to_dict(study)
            for agent in data['agents']:
                agent.update(model='mock', api_base=None, api_key_env=None)
            study = validate_study(data)
        return study

    @_synchronized
    def yaml(self, identity):
        return Redactor(tuple(self._keys.values())).text(dump_study_yaml(self.study(identity)))

    @_synchronized
    def freeze(self, identity, *, demo=False):
        self.require_owner(identity)
        return freeze_hosted_inputs(identity, self.study(identity, demo=demo), deepcopy(self._keys))

    @_synchronized
    def editor_copy(self, identity):
        self.require_owner(identity)
        return deepcopy(self.editor), self.revision, deepcopy(self._keys)

    @_synchronized
    def commit_editor(self, identity, editor, expected_revision):
        self.require_owner(identity)
        if expected_revision != self.revision:
            return False
        if any(a.get('api_key') is not None for a in editor.settings['agents']):
            raise ConfigError('Enter model keys only in the password fields.')
        self.editor = deepcopy(editor)
        return True

    @_synchronized
    def select_personas(self, identity, source, mode, count):
        self.require_owner(identity)
        if source not in {'custom', 'builtin'} or mode not in {'fixed', 'pool'} or count not in {2, 3}:
            raise ConfigError('Choose a supported persona source, selection and seat count.')
        candidate = deepcopy(self.editor)
        candidate.persona_source, candidate.persona_mode = source, mode
        if count != len(candidate.settings['agents']):
            candidate.resize(count)
        self.editor = candidate
        self._keys = {s:k for s,k in self._keys.items() if s <= count}
        self.revision += 1

    @_synchronized
    def load_example(self, identity, label):
        if label not in EXAMPLES:
            raise ConfigError('Choose a supplied example study.')
        self.replace_yaml(identity, files('convolab').joinpath('data', 'examples', EXAMPLES[label]).read_text(encoding='utf-8'))

    @_synchronized
    def replace_upload(self, identity, data):
        from .limits import check_upload
        self.replace_yaml(identity, decode_upload(check_upload(data)))

    @_synchronized
    def import_table(self, identity, kind, data, filename):
        from .limits import check_upload
        self.require_owner(identity)
        candidate = deepcopy(self.editor)
        data = check_upload(data)
        if kind == 'personas':
            if candidate.persona_source != 'custom':
                raise ConfigError('Choose Use my own personas before uploading personas.')
            rows = import_personas(data, filename, mode=candidate.persona_mode,
                                   n_agents=len(candidate.settings['agents']))
            if candidate.persona_mode == 'fixed':
                candidate.custom_fixed = [p.model_dump() for p in rows]
            else:
                candidate.custom_pool = [p.model_dump() for p in rows]
        elif kind == 'items':
            candidate.settings['items'] = [i.model_dump() for i in import_items(data, filename)]
        else:
            raise ConfigError('Choose a supported import.')
        validate_hosted_policy(candidate.validated())
        self.editor = candidate
        self.revision += 1

    @_synchronized
    def execution_finished(self, identity):
        # The Stage 3 coordinator must call this on every terminal/failure path.
        self.require_owner(identity)
        self.clear_keys()
        self.revision += 1


def clear_private_state(state):
    workspace = state.get(WORKSPACE_KEY)
    if isinstance(workspace, HostedWorkspace):
        workspace.clear_keys()
    for key in list(state):
        if isinstance(key, str) and key.startswith(WIDGET_PREFIX):
            del state[key]


def bind_workspace(state, identity):
    if not isinstance(identity, VerifiedIdentity):
        clear_private_state(state)
        raise HostedError("Connect your private workspace before viewing private data.")
    workspace = state.get(WORKSPACE_KEY)
    if not isinstance(workspace, HostedWorkspace) or workspace.owner_id != identity.owner_id:
        clear_private_state(state)
        workspace = HostedWorkspace(identity.owner_id)
        state[WORKSPACE_KEY] = workspace
    return workspace


def clear_key_widgets(state):
    for key in list(state):
        if isinstance(key, str) and key.startswith(WIDGET_PREFIX + "key_"):
            del state[key]
