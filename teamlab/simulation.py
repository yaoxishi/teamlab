"""Sequential Runs with separate private-state and questionnaire barriers."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import random
import secrets
from typing import Callable

from .config import freeze_execution, resolve_credentials, study_to_dict, validate_study
from .llm import (AbortSignal, CancelledWork, ModelClient, Redactor, RuntimeAgent, ValidationOutcome,
                  complete, error_details, format_error, utc_now, validate_capabilities)
from .outputs import OutputError, OutputStore
from .prompts import (FEMALE_NAMES, MALE_NAMES, NEUTRAL_NAMES, Participant, PromptBuilder,
                      PublicEvent, clean_main)
from .state import StateCoordinator
from .survey import SurveyCoordinator


def stream_seed(effective_seed: int, purpose: str, *identifiers) -> int:
    encoded = json.dumps([effective_seed, purpose, *identifiers],
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big")


def rng(effective_seed: int, purpose: str, *identifiers) -> random.Random:
    return random.Random(stream_seed(effective_seed, purpose, *identifiers))


def resolve_names(agents, personas, name_rng) -> tuple[str, ...]:
    used = {agent.display_name for agent in agents if agent.display_name}
    names = []
    for agent, persona in zip(agents, personas, strict=True):
        if agent.display_name:
            name = agent.display_name
        else:
            gender = persona.gender.lower()
            candidates = FEMALE_NAMES if gender in {"woman", "female", "f"} else MALE_NAMES if gender in {"man", "male", "m"} else NEUTRAL_NAMES
            name = name_rng.choice([name for name in candidates if name not in used])
        used.add(name)
        names.append(name)
    return tuple(names)


@dataclass(frozen=True)
class ExecutionResult:
    folder: Path
    status: str
    n_runs_finalized: int
    n_failed_runs: int
    error: str | None
    error_details: dict | None


def run_conversation(study, participants, agents, order, run_id, builder, store,
                     abort, redactor, *, progress=None) -> tuple[str, str | None]:
    """One resolved run; no memory, history, or confirmations carry to another."""
    count = len(participants)
    names = tuple(participant.display_name for participant in participants)
    events = []
    message_count = 0
    consecutive_passes = 0
    confirmations = set()
    end_reason = "max_turns"
    failure = None
    agent = None
    last_turn = 0
    meta = {"run_id": run_id, "phase": "turn", "turn": 0}
    try:
        states = StateCoordinator(study, participants, agents, run_id, builder, store, abort, redactor) if study.run.private_state else None
        if states:
            if progress:
                progress(redactor.value({"event": "stage", "run_id": run_id, "call_type": "state_init", "turn": 0}))
            failure = states.advance((), after_turn=0, public_message_index=0)
            if failure:
                return "failed", failure
        surveys = SurveyCoordinator(study, participants, agents, run_id, builder, store, abort, redactor, rng, progress=progress)
        surveys.administer("pre", (), after_turn=0, public_message_index=0,
                           snapshots=states.snapshots if states else None)
        for turn in range(1, study.run.max_turns + 1):
            abort.check()
            if (turn - 1) % count == 0:
                confirmations.clear()
            seat = order[(turn - 1) % count]
            agent = agents[seat - 1]
            participant = participants[seat - 1]
            meta = {
                "run_id": run_id, "seat": seat, "phase": "turn", "turn": turn,
                "public_message_index": message_count, "public_event_index": len(events),
                "state_version": states.snapshots[seat].state_version if states else None,
            }
            system = builder.system(participant, names, study.task_instructions, study.run, "main",
                                    own_state=states.snapshots[seat].state if states else None,
                                    pre_memory=surveys.pre_memory.get(seat, ""))
            user = builder.main_user(participant.display_name, tuple(events), study.run)
            def validate(text):
                cleaned = clean_main(text, participant.display_name,
                                     [name for name in names if name != participant.display_name])
                return ValidationOutcome(cleaned_text=cleaned, error=None if cleaned else "The answer became empty after removing participant labels.")
            if progress:
                progress(redactor.value({"event": "stage", **meta, "call_type": "main",
                    "agent": agent.display_name, "model": agent.model}))
            result = complete(agent, system, user, "main", agent.config.max_tokens, meta, validate=validate)
            abort.check()
            if result.error:
                failure = result.error
                end_reason = "failed"
                break
            text = result.cleaned_text
            passed = study.run.allow_pass and text.casefold() == "[pass]"
            event_index = None
            spoken_index = None
            if passed:
                consecutive_passes += 1
                if study.run.silence_visibility == "public":
                    event_index = len(events) + 1
                    events.append(PublicEvent(event_index, participant.display_name, "silence"))
            else:
                consecutive_passes = 0
                message_count += 1
                spoken_index = message_count
                event_index = len(events) + 1
                events.append(PublicEvent(event_index, participant.display_name, "message", text))
                if study.run.allow_early_end and text.rstrip().casefold().endswith("[end]"):
                    confirmations.add(seat)
            store.record_turn({
                "run_id": run_id, "turn": turn, "public_message_index": spoken_index,
                "public_event_index": event_index, "event_type": "silence" if passed else "message",
                "seat": seat, "speaker": participant.display_name, "model": agent.model,
                "passed": passed, "text": "" if passed else text, "timestamp": utc_now(),
            })
            last_turn = turn
            if progress:
                progress(redactor.value({"event": "turn", "run_id": run_id, "turn": turn, "speaker": participant.display_name,
                                         "passed": passed, "public_event_index": event_index, "text": "" if passed else text}))
            if states and event_index is not None:
                if progress:
                    progress(redactor.value({"event": "stage", "run_id": run_id, "call_type": "state_update", "turn": turn}))
                failure = states.advance(tuple(events), after_turn=turn, public_message_index=message_count)
                if failure:
                    end_reason = "failed"
                    break
            if event_index is not None and turn % study.run.measure_every_n_turns == 0:
                surveys.administer("turn", tuple(events), after_turn=turn, public_message_index=message_count,
                                   snapshots=states.snapshots if states else None)
            if consecutive_passes >= count:
                end_reason = "all_passed"
                break
            if turn % count == 0 and len(confirmations) == count:
                end_reason = "all_end"
                break
        if end_reason != "failed":
            surveys.administer("post", tuple(events), after_turn=last_turn, public_message_index=message_count,
                               snapshots=states.snapshots if states else None)
    except Exception:
        if not abort.event.is_set():
            abort.signal(redactor.value(error_details("application", agent, meta=meta)))
        raise
    return end_reason, failure


def run_study(study, output_root: str | Path = "outputs", *, environment=None,
              request: Callable | None = None, sleep: Callable | None = None,
              progress: Callable[[dict], None] | None = None,
              store_factory=OutputStore, abort_signal: AbortSignal | None = None,
              additional_secrets=()) -> ExecutionResult:
    original = validate_study(study)
    credentials = resolve_credentials(original, environment)
    # Derive an immutable, detached execution with a persisted effective seed.
    data = study_to_dict(original)
    effective_seed = original.run.seed if original.run.seed is not None else secrets.randbits(64)
    data["run"]["seed"] = effective_seed
    frozen = freeze_execution(validate_study(data))
    parameters = validate_capabilities(frozen.study)
    builder = PromptBuilder(frozen.study.prompt_templates)
    redactor = Redactor((*credentials, *(agent.api_key for agent in original.agents), *additional_secrets))
    abort = abort_signal or AbortSignal()
    abort.check()
    store = store_factory(Path(output_root), frozen, parameters, abort, redactor)
    client_kwargs = {"request": request}
    if sleep is not None:
        client_kwargs["sleep"] = sleep
    client = ModelClient(store.audit, abort, redactor, **client_kwargs)
    pool = list(frozen.persona_pool)
    if pool:
        rng(effective_seed, "persona_pool").shuffle(pool)
    offset = 0
    count = len(frozen.study.agents)
    active_run = None
    current_agent = None
    current_meta = {}
    try:
        for number in range(1, frozen.study.run.n_runs + 1):
            abort.check()
            run_id = f"run_{number:03d}"
            # Reserve only the next run's allocation when that run starts.
            personas = frozen.fixed_personas or tuple(pool[offset:offset + count])
            if pool:
                offset += count
            names = resolve_names(frozen.study.agents, personas, rng(effective_seed, "names", run_id))
            order = list(frozen.study.run.speaker_order)
            if frozen.study.run.shuffle_order_each_run:
                rng(effective_seed, "speaker_order", run_id).shuffle(order)
            participants = [Participant(seat, names[seat - 1], persona, config.role_instructions)
                            for seat, (config, persona) in enumerate(zip(frozen.study.agents, personas, strict=True), 1)]
            agents = [RuntimeAgent(seat, names[seat - 1], config, credentials[seat - 1],
                                   tuple(p for p in parameters if p.seat == seat), client=client)
                      for seat, config in enumerate(frozen.study.agents, 1)]
            agent_records = [{"run_id": run_id, "seat": p.seat, "agent": p.display_name,
                              "model": agent.model, **p.persona.model_dump(),
                              "role_instructions": p.role_instructions}
                             for p, agent in zip(participants, agents, strict=True)]
            active_run = run_id
            current_meta = {"run_id": run_id, "phase": "turn", "turn": 0}
            store.start_run(run_id, agent_records, order)
            if progress:
                progress(redactor.value({"event": "run_started", "run_id": run_id,
                    "number": number, "n_runs": frozen.study.run.n_runs, "folder": str(store.folder)}))
            end_reason, failure = run_conversation(frozen.study, participants, agents, order, run_id,
                                                   builder, store, abort, redactor, progress=progress)
            status = "failed" if failure else "completed"
            store.finish_run(run_id, status, end_reason, failure or "")
            active_run = None
            if progress:
                progress(redactor.value({"event": "run_finished", "run_id": run_id, "status": status, "end_reason": end_reason, "error": failure}))
            if abort.event.is_set():
                break
    except (OutputError, CancelledWork) as error:
        # The primary cause has already been signaled at the actual fault.
        if not abort.event.is_set():
            abort.signal(redactor.value(error_details("storage" if isinstance(error, OutputError) else "application",
                                                     current_agent, meta=current_meta)))
    except Exception:
        abort.signal(redactor.value(error_details("application", current_agent, meta=current_meta)))
    except BaseException:
        # Interpreter/framework interruptions must still finalize with a known
        # cause and release ownership. Propagate only after the finally block.
        abort.signal(redactor.value(error_details("application", current_agent, meta=current_meta)))
        raise
    finally:
        if active_run is not None:
            try:
                if store._runs.get(active_run, {}).get("status") == "running":
                    store.finish_run(active_run, "failed", "failed", format_error(abort.cause, folder=store.folder))
            except OutputError:
                pass
        try:
            store.finalize()
        except OutputError:
            pass  # safe structured failure returned; folder remains incomplete
    return ExecutionResult(store.folder, store.metadata["status"], store.metadata["n_runs_finalized"],
                           sum(row["status"] == "failed" for row in store._runs.values()),
                           store.metadata["error"], store.metadata["error_details"])
