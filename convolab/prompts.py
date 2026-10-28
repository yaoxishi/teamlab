"""The complete editable wording registry and the only prompt assembly layer.

Builders accept one participant's private inputs and public events. They never
accept another seat's profile, survey results, state history, or credentials.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from types import MappingProxyType
from typing import Mapping, Sequence

from .config import BIG_FIVE_FIELDS, ConfigError, Item, Persona, RunSettings, TEMPLATE_KEYS
from .state import PrivateState

FEMALE_NAMES = ("Emily", "Sarah", "Jessica", "Laura", "Rachel", "Megan", "Hannah", "Olivia", "Grace", "Anna")
MALE_NAMES = ("Michael", "David", "James", "Daniel", "Matthew", "Ryan", "Andrew", "Kevin", "Brian", "Eric")
NEUTRAL_NAMES = ("Alex", "Jordan", "Taylor", "Casey", "Riley", "Morgan", "Jamie", "Avery", "Quinn", "Drew")

# Wording is copied verbatim from §§4.5 and 6. Protocol schemas are generated below.
DEFAULTS = MappingProxyType({
    "system": """You are taking part in a research study as one of {n_agents} participants in a group conversation. Stay in character as the person described below for the whole study.

YOUR PRIVATE INFORMATION (only you can see this)
Your name: {display_name}
You are a {ideology}, {age}-year-old, {race_ethnicity}, {gender}, American with {education}, who identifies as {party}.
{big_five_line}
Your role instructions: {role_instructions}
{pre_survey_block}
{private_state_block}
TASK INSTRUCTIONS (all participants received these same instructions)
{task_instructions}

PARTICIPANTS IN THIS CONVERSATION: {all_display_names}
{silence_guidance}""",
    "main_user": """CONVERSATION SO FAR:
{transcript_text}

It is your turn to speak, {display_name}. Write only your next message, as you would say it. Do not write messages for other participants. Do not start with your name.
{main_state_guidance}
{pass_line}
{early_end_line}""",
    "survey_user": """{phase_instructions}
{survey_guidance}
{survey_state_guidance}

{context}

QUESTIONS:
{questions_text}

{reaction_line}
Reply with only a JSON object that maps each item ID to your answer, in this form:
{survey_response_schema}""",
    "private_state_block": "YOUR CURRENT PRIVATE STATE (only you can see this)\n{state_json}",
    "state_guidance": "Describe your current private state as the participant in your profile. Use only your own supplied background, your prior state when available, and the public conversation. Distinguish personal impressions and tentative inferences from known facts. Do not invent past experiences, prior relationships, or access to another participant's private information. Changes are optional: retain what still applies, and allow uncertainty, mixed feelings, or no noticeable change. Do not turn the state into questionnaire ratings or a detailed reasoning trace. Return a complete replacement state using only the required JSON keys. Keep descriptions brief.",
    "state_init_user": """{state_guidance}

The conversation has not started. Form your initial position, experience, impressions, open questions, and intention from the task and your own profile. Where information is insufficient, remain undecided or say that an impression is not yet established. No questionnaire has been administered.

{state_response_instruction}""",
    "state_update_user": """{state_guidance}

Your current private state in the system message is the snapshot from before the latest public event. Read the event history from your own point of view and update your state after that event, whether you spoke, heard speech, remained silent, or observed another participant remain silent. A silence observation reveals no private reason or duration. Previously processed events are history; do not treat them as new events again. Preserve relevant prior concerns and impressions where appropriate; no change is required.

PUBLIC CONVERSATION SO FAR:
{state_transcript_text}

LATEST PUBLIC EVENT INDEX: {public_event_index}

{state_response_instruction}""",
    "state_response_instruction": "Reply with only one JSON object in this form:\n{state_response_schema}",
    "state_transcript_message": "[Event {public_event_index}] {speaker_label}: {text}",
    "state_transcript_silence": "[Event {public_event_index}] [Observation: {speaker_label} did not speak when given the opportunity.]",
    "main_state_guidance": "Speak as this participant, using your current position, experience, and intentions alongside the conversation. You may express some of your private feelings or keep them to yourself; do not output the private-state JSON or messages for other participants.",
    "survey_state_guidance": "Answer as the participant described in your profile, using your current private state and the available conversation. The state is your own contextual record, not a prescribed questionnaire answer. Interpret each question and its response options normally. You may give nuanced or uncertain answers where the question allows them. Do not copy previous questionnaire answers just to remain consistent.",
    "survey_guidance": "Answer with your honest first reaction, based on how you feel at this moment. You do not need to be consistent with any of your earlier answers, and you may use the full range of each scale. Your answers are private within the simulation: other participants never see them unless you later choose to express similar views in your public speech.",
    "pass_line": "If you decide not to say anything or have nothing to add right now, reply with exactly [PASS] to stay silent this turn.",
    "early_end_line": "If you believe the conversation is finished, end your message with the exact marker [END], with nothing after it. Everyone must confirm during the same round of speaking opportunities. Reconsider on each opportunity and repeat [END] if you still want to finish; an earlier-round confirmation does not carry forward. Passing does not confirm. Do not use [END] for other purposes.",
    "reaction_line": 'First, take a moment to notice your private thoughts and feelings right now, and write them in one or two sentences as "reaction". Then answer the questions.',
    "transcript_separator": "\n\n",
    "transcript_message": "{display_name}: {text}",
    "survey_transcript_message": "{speaker_label}: {text}",
    "transcript_silence": "[Observation: {display_name} did not speak when given the opportunity.]",
    "survey_transcript_silence": "[Observation: {speaker_label} did not speak when given the opportunity.]",
    "silence_guidance": "In this discussion, when a participant does not speak on their opportunity, everyone can observe that silence. Observation entries report only who did not speak. They do not reveal a reason, emotion, intention, or how long the silence lasted. You may form tentative interpretations, but do not treat an interpretation as a known fact or assume that silence means agreement, rejection, or distress. Your own silence is also visible to the others. A silence event need not change your feelings or views.",
    "main_empty_context": "(No public events yet.)",
    "empty_public_transcript": "(No public events.)",
    "pre_context": "(The conversation has not started yet.)",
    "turn_context": "CONVERSATION SO FAR:\n{transcript_text}\n\nTHE MOST RECENT EVENT:\n{latest_event_text}",
    "post_context": "FULL CONVERSATION:\n{transcript_text}",
    "likert_question": "{item_position}. [{item_id}] {text}\n   Answer with a whole number from {min} ({min_label}) to {max} ({max_label}).",
    "text_question": "{item_position}. [{item_id}] {text}\n   Answer in one to three sentences.",
    "question_separator": "\n",
    "big_five_line": "Your personality (Big Five, each scored from 1 = very low to 5 = very high): {traits_text}",
    "big_five_trait": "{trait_name} {score}",
    "big_five_separator": ", ",
    "pre_survey_block": "YOUR ANSWERS TO THE QUESTIONNAIRE BEFORE THE CONVERSATION (only you can see this)\n{answers_text}",
    "pre_memory_likert_answer": "- {text} ({min} = {min_label}, {max} = {max_label}): {answer}",
    "pre_memory_text_answer": "- {text}: {answer}",
    "pre_memory_separator": "\n",
})

ALLOWLISTS = MappingProxyType({
    "system": frozenset("n_agents display_name ideology age race_ethnicity gender education party big_five_line role_instructions pre_survey_block private_state_block task_instructions all_display_names silence_guidance".split()),
    "main_user": frozenset("transcript_text display_name main_state_guidance pass_line early_end_line".split()),
    "survey_user": frozenset("phase_instructions survey_guidance survey_state_guidance context questions_text reaction_line survey_response_schema".split()),
    "private_state_block": frozenset({"state_json"}),
    "state_init_user": frozenset({"state_guidance", "state_response_instruction"}),
    "state_update_user": frozenset({"state_guidance", "state_transcript_text", "public_event_index", "state_response_instruction"}),
    "state_response_instruction": frozenset({"state_response_schema"}),
    "state_transcript_message": frozenset({"public_event_index", "speaker_label", "text"}),
    "state_transcript_silence": frozenset({"public_event_index", "speaker_label"}),
    "transcript_message": frozenset({"display_name", "text"}),
    "survey_transcript_message": frozenset({"speaker_label", "text"}),
    "transcript_silence": frozenset({"display_name"}),
    "survey_transcript_silence": frozenset({"speaker_label"}),
    "turn_context": frozenset({"transcript_text", "latest_event_text"}),
    "post_context": frozenset({"transcript_text"}),
    "likert_question": frozenset("item_position item_id text min min_label max max_label".split()),
    "text_question": frozenset({"item_position", "item_id", "text"}),
    "big_five_line": frozenset({"traits_text"}),
    "big_five_trait": frozenset({"trait_name", "score"}),
    "pre_survey_block": frozenset({"answers_text"}),
    "pre_memory_likert_answer": frozenset("text min min_label max max_label answer".split()),
    "pre_memory_text_answer": frozenset({"text", "answer"}),
    **{name: frozenset() for name in (
        "state_guidance", "main_state_guidance", "survey_state_guidance", "survey_guidance",
        "pass_line", "early_end_line", "reaction_line", "transcript_separator",
        "silence_guidance", "main_empty_context", "empty_public_transcript", "pre_context",
        "question_separator", "big_five_separator", "pre_memory_separator",
    )},
})
OPTIONAL = frozenset("big_five_line role_instructions pre_survey_block private_state_block main_state_guidance survey_state_guidance pass_line early_end_line reaction_line silence_guidance".split())
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
assert set(DEFAULTS) == set(ALLOWLISTS) == set(TEMPLATE_KEYS)


def resolve_templates(overrides: Mapping[str, str] | None = None) -> Mapping[str, str]:
    overrides = overrides or {}
    if set(overrides) - TEMPLATE_KEYS:
        raise ConfigError("prompt_templates: unknown template key; use the registered names.")
    registry = {**DEFAULTS, **overrides}
    for key, source in registry.items():
        if not isinstance(source, str):
            raise ConfigError(f"prompt_templates.{key}: enter template text.")
        found = set(_PLACEHOLDER.findall(source))
        missing, unknown = ALLOWLISTS[key] - found, found - ALLOWLISTS[key]
        migration = ""
        if key.startswith("state_") and "public_message_index" in unknown:
            migration += " Replace {public_message_index} with {public_event_index}."
        if key == "turn_context" and unknown & {"latest_speaker", "latest_message_text"}:
            migration += " Replace {latest_speaker}/{latest_message_text} with {latest_event_text}."
        if key == "system" and "silence_guidance" in missing:
            migration += " Add {silence_guidance} on its own line."
        if missing or unknown:
            raise ConfigError(f"prompt_templates.{key}: missing placeholders {sorted(missing)}; unknown placeholders {sorted(unknown)}.{migration}")
        if key in {"system", "main_user", "survey_user"}:
            for line in source.splitlines():
                names = _PLACEHOLDER.findall(line)
                if any(name in OPTIONAL for name in names) and len(names) != 1:
                    raise ConfigError(f"prompt_templates.{key}: each optional placeholder must occupy its own line without another placeholder.")
    return MappingProxyType(registry)


@dataclass(frozen=True)
class Participant:
    seat: int
    display_name: str
    persona: Persona
    role_instructions: str = ""


@dataclass(frozen=True)
class PublicEvent:
    public_event_index: int
    display_name: str
    event_type: str
    text: str = ""

    def __post_init__(self):
        if self.event_type not in {"message", "silence"}:
            raise ValueError("Public events are speech or observable silence.")
        if self.public_event_index < 1 or (self.event_type == "silence" and self.text):
            raise ValueError("Silence has no invented utterance; public indices start at 1.")


class PromptBuilder:
    def __init__(self, overrides: Mapping[str, str] | None = None):
        self.templates = resolve_templates(overrides)

    def render(self, key: str, **values) -> str:
        if set(values) != ALLOWLISTS[key]:
            raise ValueError(f"Builder {key}: supply exactly the allowed inputs.")
        source = self.templates[key]
        if key in {"system", "main_user", "survey_user"}:
            source = "".join(line for line in source.splitlines(keepends=True)
                             if not any(name in OPTIONAL and values[name] == ""
                                        for name in _PLACEHOLDER.findall(line)))
        return _PLACEHOLDER.sub(lambda match: str(values[match[1]]), source)

    def transcript(self, events: Sequence[PublicEvent], *, own_name: str | None = None,
                   indexed: bool = False, empty: str = "empty_public_transcript") -> str:
        self.check_events(events)
        return self.templates["transcript_separator"].join(
            self.event(event, own_name=own_name, indexed=indexed) for event in events
        ) if events else self.templates[empty]

    @staticmethod
    def check_events(events: Sequence[PublicEvent]) -> None:
        if [event.public_event_index for event in events] != list(range(1, len(events) + 1)):
            raise ValueError("Participant context must contain consecutive public event indices.")

    def event(self, event: PublicEvent, *, own_name: str | None = None, indexed: bool = False) -> str:
        label = f"You ({own_name})" if event.display_name == own_name else event.display_name
        if indexed:
            values = {"public_event_index": event.public_event_index, "speaker_label": label}
            if event.event_type == "message":
                values["text"] = event.text
            return self.render("state_transcript_" + event.event_type, **values)
        if own_name is not None:
            values = {"speaker_label": label}
            key = "survey_transcript_" + event.event_type
        else:
            values = {"display_name": event.display_name}
            key = "transcript_" + event.event_type
        if event.event_type == "message":
            values["text"] = event.text
        return self.render(key, **values)

    def pre_memory(self, items: Sequence[Item], own_valid_answers: Mapping[str, int | str]) -> str:
        # The caller provides individually validated answers from this seat only.
        lines = []
        for item in items:  # configured order, not presentation order
            if item.pre and item.item_id in own_valid_answers:
                values = {"text": item.text, "answer": own_valid_answers[item.item_id]}
                if item.type == "likert":
                    values.update({key: getattr(item, key) for key in ("min", "max", "min_label", "max_label")})
                lines.append(self.render("pre_memory_" + item.type + "_answer", **values))
        return self.render("pre_survey_block", answers_text=self.templates["pre_memory_separator"].join(lines)) if lines else ""

    def system(self, participant: Participant, names: Sequence[str], task: str, run: RunSettings,
               call_type: str, *, own_state: PrivateState | None = None, pre_memory: str = "") -> str:
        if participant.display_name not in names or len(names) not in (2, 3) or len(set(names)) != len(names):
            raise ValueError("Use the resolved unique participant names.")
        if call_type not in {"main", "survey", "state_init", "state_update"}:
            raise ValueError("Unknown call type.")
        state_block = ""
        if run.private_state and call_type != "state_init":
            if own_state is None:
                raise ValueError("A committed own state is required.")
            if set(own_state.views_of_others) != set(names) - {participant.display_name}:
                raise ValueError("The committed state must belong to this participant.")
            state_block = self.render("private_state_block", state_json=json.dumps(
                own_state.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        traits = [self.render("big_five_trait", trait_name=key, score=str(getattr(participant.persona, key)))
                  for key in BIG_FIVE_FIELDS if getattr(participant.persona, key) is not None]
        return self.render("system", n_agents=len(names), display_name=participant.display_name,
                           **{key: getattr(participant.persona, key) for key in
                              ("ideology", "age", "race_ethnicity", "gender", "education", "party")},
                           big_five_line=self.render("big_five_line", traits_text=self.templates["big_five_separator"].join(traits)) if traits else "",
                           role_instructions=participant.role_instructions,
                           pre_survey_block=pre_memory if run.remember_pre_survey and call_type in {"main", "survey"} else "",
                           private_state_block=state_block, task_instructions=task,
                           all_display_names=", ".join(names),
                           silence_guidance=self.templates["silence_guidance"] if run.allow_pass and run.silence_visibility == "public" else "")

    def main_user(self, name: str, events: Sequence[PublicEvent], run: RunSettings) -> str:
        self.check_visibility(events, run)
        return self.render("main_user", display_name=name,
                           transcript_text=self.transcript(events, empty="main_empty_context"),
                           main_state_guidance=self.templates["main_state_guidance"] if run.private_state else "",
                           pass_line=self.templates["pass_line"] if run.allow_pass else "",
                           early_end_line=self.templates["early_end_line"] if run.allow_early_end else "")

    @staticmethod
    def check_visibility(events: Sequence[PublicEvent], run: RunSettings):
        if (not run.allow_pass or run.silence_visibility == "hidden") and any(event.event_type == "silence" for event in events):
            raise ValueError("A hidden pass must never enter participant context.")

    def survey_user(self, name: str, phase: str, events: Sequence[PublicEvent], items: Sequence[Item],
                    run: RunSettings, instructions: Mapping[str, str]) -> str:
        self.check_visibility(events, run)
        self.check_events(events)
        if not items or phase not in {"pre", "turn", "post"}:
            raise ValueError("Use a non-empty frozen administration and a survey phase.")
        transcript = self.transcript(events, own_name=name)
        if phase == "pre":
            if events:
                raise ValueError("Pre survey precedes all public events.")
            context = self.templates["pre_context"]
        elif phase == "turn":
            if not events:
                raise ValueError("A turn survey requires a public event.")
            context = self.render("turn_context", transcript_text=transcript,
                                  latest_event_text=self.event(events[-1], own_name=name))
        else:
            context = self.render("post_context", transcript_text=transcript)
        questions = []
        for position, item in enumerate(items, 1):
            values = {"item_position": position, "item_id": item.item_id, "text": item.text}
            if item.type == "likert":
                values.update({key: getattr(item, key) for key in ("min", "max", "min_label", "max_label")})
            questions.append(self.render(item.type + "_question", **values))
        entries = [('reaction', '"<text>"')] if run.inner_reaction else []
        entries += [(item.item_id, "<number>" if item.type == "likert" else '"<text>"') for item in items]
        schema = "{" + ", ".join(json.dumps(key, ensure_ascii=False) + ": " + value for key, value in entries) + "}"
        return self.render("survey_user", phase_instructions=instructions[phase],
                           survey_guidance=self.templates["survey_guidance"],
                           survey_state_guidance=self.templates["survey_state_guidance"] if run.private_state else "",
                           context=context, questions_text=self.templates["question_separator"].join(questions),
                           reaction_line=self.templates["reaction_line"] if run.inner_reaction else "",
                           survey_response_schema=schema)

    def state_user(self, own_name: str, names: Sequence[str], events: Sequence[PublicEvent], *,
                   initializing: bool, run: RunSettings) -> str:
        self.check_visibility(events, run)
        self.check_events(events)
        others = [name for name in names if name != own_name]
        if len(others) not in (1, 2) or own_name not in names or len(set(names)) != len(names):
            raise ValueError("State schema requires resolved other participant names.")
        schema = json.dumps({
            "current_position": "<text>", "current_experience": "<text>",
            "views_of_others": {name: "<text>" for name in others},
            "open_questions": ["<text, or use an empty array>"], "next_intention": "<text>",
        }, ensure_ascii=False, indent=2)
        values = {"state_guidance": self.templates["state_guidance"],
                  "state_response_instruction": self.render("state_response_instruction", state_response_schema=schema)}
        if initializing:
            if events:
                raise ValueError("Initialization precedes all public events.")
            return self.render("state_init_user", **values)
        if not events:
            raise ValueError("A state update requires a public event.")
        return self.render("state_update_user", **values,
                           state_transcript_text=self.transcript(events, own_name=own_name, indexed=True),
                           public_event_index=events[-1].public_event_index)


def clean_main(text: str, own_name: str, other_names: Sequence[str]) -> str:
    """§6.2 ordered cleaning; retries are exclusively owned by llm.complete."""
    cleaned = text.strip()
    if cleaned.startswith(own_name + ":"):
        cleaned = cleaned[len(own_name) + 1:].lstrip()
    lines = cleaned.splitlines(keepends=True)
    retained = []
    for line in lines:
        if any(line.startswith(name + ":") for name in other_names):
            break
        retained.append(line)
    return "".join(retained).strip()
