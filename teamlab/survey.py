"""Private questionnaires: strict scoring, frozen requests, coordinator exports."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import json
from types import MappingProxyType
from typing import Mapping

from .config import PHASES, Item
from .json_protocol import JsonProtocolError, decode_json_object
from .llm import (EXECUTION_ERRORS, CancelledWork, ValidationOutcome, complete,
                  error_details, utc_now)


class SurveyError(ValueError):
    pass


@dataclass(frozen=True)
class SurveyAnswers:
    # Missing/invalid answers are absent; valid supplied text stays unchanged.
    answers: Mapping[str, int | str]
    reaction: str | None = None
    error: str | None = None


def score_answer(value, item: Item) -> int | str | None:
    if item.type == "text":
        return value if isinstance(value, str) and value.strip() else None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    # Check bounds before int conversion, including unusually large exponents.
    if not number.is_finite() or number < item.min or number > item.max or number != number.to_integral_value():
        return None
    return int(number)


def parse_survey(text: str, items: tuple[Item, ...], inner_reaction: bool) -> SurveyAnswers:
    try:
        obj = decode_json_object(text)
    except (JsonProtocolError, ValueError) as error:
        raise SurveyError(str(error)) from None
    answers = {}
    for item in items:
        value = score_answer(obj.get(item.item_id), item)
        if value is not None:
            answers[item.item_id] = value
    reaction = obj.get("reaction") if inner_reaction else None
    if not isinstance(reaction, str) or not reaction.strip():
        reaction = None
    missing = len(answers) != len(items) or (inner_reaction and reaction is None)
    return SurveyAnswers(MappingProxyType(answers), reaction,
                         "A requested answer or required reaction is missing or invalid." if missing else None)


def final_answers(result, items, inner_reaction) -> SurveyAnswers:
    # Refusal/filter/tool/unsupported content never becomes scored data, even
    # when its retained audit text happens to contain a valid JSON object.
    if result.error_type in {None, "validation", "truncation"} and result.finish_reason in {"stop", "length"}:
        try:
            return parse_survey(result.text, items, inner_reaction)
        except SurveyError:
            pass
    return SurveyAnswers(MappingProxyType({}), error="No eligible final-attempt answers.")


@dataclass(frozen=True)
class MockSurveyInput:
    # Prepared once by the coordinator from independent per-item streams.
    answers: tuple[tuple[str, int | str], ...]
    inner_reaction: bool


def mock_survey_text(inputs: MockSurveyInput) -> str:
    obj = {"reaction": "Mock reaction."} if inputs.inner_reaction else {}
    obj.update(inputs.answers)
    return json.dumps(obj, ensure_ascii=False)


@dataclass(frozen=True)
class SurveyRequest:
    agent: object
    system: str
    user: str
    meta: Mapping
    items: tuple[Item, ...]
    inner_reaction: bool
    max_tokens: int


def _survey_call(request: SurveyRequest, abort, redactor):
    def validate(text):
        try:
            parsed = parse_survey(text, request.items, request.inner_reaction)
            return ValidationOutcome(error=parsed.error)
        except SurveyError as error:
            return ValidationOutcome(error=str(error))
    try:
        result = complete(request.agent, request.system, request.user, "survey",
                          request.max_tokens, request.meta, validate=validate)
        # Reparse ONLY the final returned attempt; validators store no earlier
        # answers and consume no randomness.
        return result, final_answers(result, request.items, request.inner_reaction)
    except CancelledWork:
        raise
    except Exception:
        abort.signal(redactor.value(error_details("application", request.agent,
                     meta=request.meta, call_type="survey")))
        raise


class SurveyCoordinator:
    """Seat maps stay here; a worker receives only one frozen administration."""
    def __init__(self, study, participants, agents, run_id, builder, store, abort, redactor, random_stream, *, progress=None):
        self.study, self.participants, self.agents = study, tuple(participants), tuple(agents)
        self.run_id, self.builder, self.store = run_id, builder, store
        self.abort, self.redactor, self.random_stream = abort, redactor, random_stream
        self.names = tuple(p.display_name for p in participants)
        self.pre_memory = MappingProxyType({})
        self.progress = progress

    def administer(self, phase, events, *, after_turn, public_message_index, snapshots=None):
        try:
            return self._administer(phase, events, after_turn=after_turn,
                                    public_message_index=public_message_index, snapshots=snapshots)
        except CancelledWork:
            raise
        except Exception:
            if not self.abort.event.is_set():
                self.abort.signal(self.redactor.value(error_details("application",
                    meta={"run_id": self.run_id, "phase": phase, "turn": after_turn}, call_type="survey")))
            raise

    def _administer(self, phase, events, *, after_turn, public_message_index, snapshots=None):
        if phase not in PHASES:
            raise ValueError("Unknown survey phase.")
        self.abort.check()
        selected = tuple(item for item in self.study.items if getattr(item, phase))
        if not selected:
            return  # Reactions alone never cause an administration.
        if self.progress:
            self.progress(self.redactor.value({"event": "stage", "run_id": self.run_id,
                "call_type": "survey", "phase": phase, "turn": after_turn}))
        events = tuple(events)
        event_index = events[-1].public_event_index if events else 0
        requests = {}
        for participant, agent in zip(self.participants, self.agents, strict=True):
            items = list(selected)
            if self.study.run.randomize_item_order:
                self.random_stream(self.study.run.seed, "item_order", self.run_id, agent.seat, phase, after_turn).shuffle(items)
            items = tuple(items)
            snapshot = snapshots[agent.seat] if snapshots is not None else None
            if self.study.run.private_state and (snapshot is None or snapshot.state_version != event_index):
                raise ValueError("A survey requires this event's committed own state.")
            system = self.builder.system(participant, self.names, self.study.task_instructions,
                        self.study.run, "survey", own_state=snapshot.state if snapshot else None,
                        pre_memory=self.pre_memory.get(agent.seat, "") if phase != "pre" else "")
            user = self.builder.survey_user(participant.display_name, phase, events, items,
                                           self.study.run, self.study.survey_instructions)
            meta = MappingProxyType({
                "run_id": self.run_id, "seat": agent.seat, "phase": phase, "turn": after_turn,
                "public_message_index": public_message_index, "public_event_index": event_index,
                "state_version": snapshot.state_version if snapshot else None, "items": items,
            })
            mock = None
            if agent.model == "mock":
                values = tuple((item.item_id, self.random_stream(self.study.run.seed, "mock_answer",
                    self.run_id, agent.seat, phase, after_turn, item.item_id).randint(item.min, item.max)
                    if item.type == "likert" else "Mock answer.") for item in items)
                mock = MockSurveyInput(values, self.study.run.inner_reaction)
            worker_agent = replace(agent, mock_survey_input=mock)
            requests[agent.seat] = SurveyRequest(worker_agent, system, user, meta, items,
                self.study.run.inner_reaction, self.study.run.survey_max_tokens)
        results = {}
        with ThreadPoolExecutor(max_workers=len(requests), thread_name_prefix="convolab-survey") as workers:
            futures = {workers.submit(_survey_call, request, self.abort, self.redactor): seat
                       for seat, request in requests.items()}
            for future in as_completed(futures):
                try:
                    results[futures[future]] = future.result()
                except Exception:
                    # Settle every submitted worker, including unsent cancellation.
                    if not self.abort.event.is_set():
                        raise
        aborted = self.abort.event.is_set()
        # Storage/application faults prevent further measurement writes.
        # A model abort permits only actual returned survey text to be retained.
        if aborted and (self.abort.cause or {}).get("error_type") not in EXECUTION_ERRORS:
            self.abort.check()
        memory = {}
        for seat in sorted(results):
            result, answers = results[seat]
            if aborted and not result.text:
                continue
            request = requests[seat]
            base = {"run_id": self.run_id, "seat": seat, "agent": request.agent.display_name,
                    "model": request.agent.model, "phase": phase, "after_turn": after_turn,
                    "public_event_index": event_index, "state_version": request.meta["state_version"],
                    "call_id": result.call_id, "timestamp": utc_now()}
            for position, item in enumerate(request.items, 1):
                self.store.append_csv("responses", {**base, "scale": item.scale, "item_id": item.item_id,
                    "item_position": position, "response": answers.answers.get(item.item_id, ""),
                    "valid": item.item_id in answers.answers})
            if request.inner_reaction:
                self.store.append_csv("reactions", {**base, "reaction": answers.reaction or "",
                                                   "valid": answers.reaction is not None})
            if self.progress and result.error_type and not aborted:
                details = error_details(result.error_type, request.agent, meta=request.meta,
                    call_type="survey", call_id=result.call_id, attempts=result.attempts)
                self.progress(self.redactor.value({"event": "measurement_warning", "call_type": "survey", **base,
                    "error_details": details,
                    "missing_items": [item.item_id for item in request.items if item.item_id not in answers.answers],
                    "reaction_missing": request.inner_reaction and answers.reaction is None}))
            if phase == "pre" and self.study.run.remember_pre_survey:
                memory[seat] = self.builder.pre_memory(self.study.items, answers.answers)
        self.abort.check()
        if set(results) != set(requests):
            raise CancelledWork("A survey batch was canceled.")
        if phase == "pre":
            self.pre_memory = MappingProxyType(memory)
