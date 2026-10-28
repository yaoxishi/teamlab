"""Private-state validation and coordinator-owned all-agent commit barriers."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
import json
from types import MappingProxyType
from typing import Mapping

from pydantic import BaseModel, ConfigDict, ValidationError, ValidationInfo, field_serializer, model_validator

from .config import ConfigError, _validation_message
from .json_protocol import JsonProtocolError, decode_json_object

STATE_KEYS = ("current_position", "current_experience", "views_of_others", "open_questions", "next_intention")


class StateError(ConfigError):
    pass


class PrivateState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    current_position: str
    current_experience: str
    views_of_others: Mapping[str, str]
    open_questions: tuple[str, ...]
    next_intention: str

    @field_serializer("views_of_others")
    def serialize_views(self, value):
        return dict(value)

    @model_validator(mode="before")
    @classmethod
    def validate_object(cls, value):
        if not isinstance(value, Mapping):
            raise StateError("State must be one object with exactly the five required keys.")
        if set(value) != set(STATE_KEYS):
            raise StateError("State must have exactly current_position, current_experience, views_of_others, open_questions, and next_intention.")
        data = dict(value)
        for key in ("current_position", "current_experience", "next_intention"):
            if not isinstance(data[key], str) or not data[key].strip():
                raise StateError(f"{key}: require a non-empty string.")
        views = data["views_of_others"]
        if not isinstance(views, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) or not v.strip() for k, v in views.items()):
            raise StateError("views_of_others: require an object of other participant names and non-empty strings.")
        questions = data["open_questions"]
        if not isinstance(questions, list) or any(not isinstance(q, str) or not q.strip() for q in questions):
            raise StateError("open_questions: require an array of non-empty strings; an empty array is allowed.")
        data["views_of_others"] = dict(views)
        data["open_questions"] = tuple(questions)
        return data

    @model_validator(mode="after")
    def validate_names(self, info: ValidationInfo):
        if not info.context or "other_names" not in info.context:
            raise StateError("State validation requires the actual other participant names.")
        if set(self.views_of_others) != set(info.context["other_names"]):
            raise StateError("views_of_others: include exactly the other participant names, without your own name.")
        object.__setattr__(self, "views_of_others", MappingProxyType(dict(self.views_of_others)))
        return self

    def to_dict(self) -> dict:
        return {
            "current_position": self.current_position,
            "current_experience": self.current_experience,
            "views_of_others": dict(self.views_of_others),
            "open_questions": list(self.open_questions),
            "next_intention": self.next_intention,
        }


def validate_state_object(value: object, other_names: tuple[str, ...] | list[str]) -> PrivateState:
    if not isinstance(other_names, (tuple, list)) or len(other_names) not in (1, 2) or any(not isinstance(n, str) or not n.strip() for n in other_names) or len(set(other_names)) != len(other_names):
        raise StateError("State validation needs one or two distinct other participant names.")
    try:
        return PrivateState.model_validate(value, context={"other_names": tuple(other_names)})
    except ValidationError as error:
        raise StateError(_validation_message(error)) from None


def mock_initial_state(other_names: tuple[str, ...] | list[str]) -> PrivateState:
    """The exact §7 initial fixture, without a request or any orchestration."""
    return validate_state_object({
        "current_position": "Undecided.",
        "current_experience": "The conversation has not started.",
        "views_of_others": {name: "No impression established." for name in other_names},
        "open_questions": [],
        "next_intention": "Listen and take part.",
    }, other_names)


def parse_state(text: str, other_names) -> PrivateState:
    """No repairs/partial acceptance; the caller owns the sole request budget."""
    try:
        value = decode_json_object(text)
    except (JsonProtocolError, json.JSONDecodeError) as error:
        raise StateError(str(error)) from None
    return validate_state_object(value, other_names)


@dataclass(frozen=True)
class MockStateInput:
    """One administration's own allowed inputs, never a whole state store."""
    other_names: tuple[str, ...]
    previous: PrivateState | None
    public_event_index: int


def mock_state_text(inputs: MockStateInput, *, initializing: bool) -> str:
    if initializing:
        if inputs.previous is not None or inputs.public_event_index != 0:
            raise ValueError("Mock initialization has no prior state or event.")
        state = mock_initial_state(inputs.other_names)
    else:
        if inputs.previous is None or inputs.public_event_index < 1:
            raise ValueError("Mock updates require their own prior state and public event.")
        data = inputs.previous.to_dict()  # detached own copy; no RNG or survey input
        data["current_experience"] = f"I have observed public event {inputs.public_event_index}."
        state = validate_state_object(data, inputs.other_names)
    return json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class StateSnapshot:
    run_id: str
    seat: int
    agent: str
    model: str
    state_version: int
    after_turn: int
    public_message_index: int
    public_event_index: int
    event_type: str
    state: PrivateState
    call_id: str
    timestamp: str

    def to_record(self):
        return {key: getattr(self, key) for key in (
            "run_id", "seat", "agent", "model", "state_version", "after_turn",
            "public_message_index", "public_event_index", "event_type", "call_id", "timestamp"
        )} | {"state": self.state.to_dict()}


def _state_call(agent, system, user, meta, other_names, max_tokens, abort, redactor):
    """Private worker: own frozen strings/snapshot only; writes audit via LLM."""
    from .llm import CancelledWork, ValidationOutcome, complete, error_details
    candidate = None

    def validate(text):
        nonlocal candidate
        candidate = None  # never combine valid fields or retain an older attempt
        try:
            candidate = parse_state(text, other_names)
        except StateError as error:
            return ValidationOutcome(error=str(error))
        return ValidationOutcome()

    call_type = "state_init" if meta["phase"] == "init" else "state_update"
    try:
        result = complete(agent, system, user, call_type, max_tokens, meta, validate=validate)
        return result, candidate if not result.error else None
    except CancelledWork:
        raise
    except Exception:
        abort.signal(redactor.value(error_details("application", agent, meta=meta, call_type=call_type)))
        raise


class StateCoordinator:
    """Only the coordinator sees the seat map; workers get one seat's inputs."""
    def __init__(self, study, participants, agents, run_id, builder, store, abort, redactor):
        self.study, self.participants, self.agents = study, tuple(participants), tuple(agents)
        self.run_id, self.builder, self.store = run_id, builder, store
        self.abort, self.redactor = abort, redactor
        self.names = tuple(p.display_name for p in participants)
        self.snapshots = MappingProxyType({})

    def advance(self, events, *, after_turn: int, public_message_index: int) -> str | None:
        """Return a call-local failure; execution failures raise after settling."""
        from .llm import CancelledWork, utc_now
        self.abort.check()
        initializing = not self.snapshots
        events = tuple(events)
        version = 0 if initializing else events[-1].public_event_index
        if initializing and (events or after_turn or public_message_index):
            raise ValueError("State initialization precedes all conversation.")
        if not initializing and version != next(iter(self.snapshots.values())).state_version + 1:
            raise ValueError("A state batch must advance exactly one public event.")
        call_type = "state_init" if initializing else "state_update"
        requests = []
        for participant, agent in zip(self.participants, self.agents, strict=True):
            previous = None if initializing else self.snapshots[agent.seat]
            own_state = previous.state if previous else None
            others = tuple(name for name in self.names if name != participant.display_name)
            system = self.builder.system(participant, self.names, self.study.task_instructions,
                                         self.study.run, call_type, own_state=own_state)
            user = self.builder.state_user(participant.display_name, self.names, events,
                                           initializing=initializing, run=self.study.run)
            meta = MappingProxyType({
                "run_id": self.run_id, "seat": agent.seat, "phase": "init" if initializing else "turn",
                "turn": after_turn, "public_message_index": public_message_index,
                "public_event_index": version, "input_state_version": previous.state_version if previous else None,
                "target_state_version": version,
            })
            # No global/in-place runtime-agent mutation. A mock receives only
            # this administration's immutable own input.
            worker_agent = replace(agent, mock_state_input=MockStateInput(others, own_state, version)
                                   if agent.model == "mock" else None)
            requests.append((worker_agent, system, user, meta, others, self.study.run.state_max_tokens,
                             self.abort, self.redactor))
        results = {}
        with ThreadPoolExecutor(max_workers=len(requests), thread_name_prefix="convolab-state") as workers:
            futures = {workers.submit(_state_call, *request): request[0].seat for request in requests}
            for future in as_completed(futures):
                try:
                    results[futures[future]] = future.result()
                except Exception:
                    # Worker signals application/fatal cause at its source.
                    # Let every submitted worker settle before propagating.
                    if not self.abort.event.is_set():
                        raise
        self.abort.check()
        for seat in sorted(results):
            result, candidate = results[seat]
            if result.error:
                return result.error
            if candidate is None:
                raise ValueError("Successful state call has no validated candidate.")
        if set(results) != {agent.seat for agent in self.agents}:
            raise CancelledWork("A state batch was canceled; no candidate was committed.")
        snapshots = {}
        for agent in self.agents:
            result, candidate = results[agent.seat]
            snapshots[agent.seat] = StateSnapshot(
                self.run_id, agent.seat, agent.display_name, agent.model, version, after_turn,
                public_message_index, version, "init" if initializing else events[-1].event_type,
                candidate, result.call_id, utc_now())
        # All candidates validated, all workers settled, no abort. Persist one
        # full coordinator batch before publishing any new in-memory snapshot.
        self.store.append_jsonl("states", [snapshots[seat].to_record() for seat in sorted(snapshots)])
        self.snapshots = MappingProxyType(snapshots)
        return None
