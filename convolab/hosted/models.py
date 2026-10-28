"""Website model choices. IDs are explicit; imports are never silently remapped.

Catalog reviewed 2026-10-27 against official model/lifecycle documentation.
Offline adapter and fake-transport checks are not a live availability guarantee.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelChoice:
    id: str
    name: str
    note: str = ''


MODELS = (
    ModelChoice('mock', 'Mock demonstration — no API key or model charge'),
    ModelChoice('openai/gpt-4o-mini-2024-07-18', 'OpenAI · GPT-4o mini · 2024-07-18'),
    ModelChoice('openai/gpt-4o-2024-11-20', 'OpenAI · GPT-4o · 2024-11-20'),
    ModelChoice('openai/gpt-4.1-2025-04-14', 'OpenAI · GPT-4.1 · 2025-04-14'),
    ModelChoice('openai/gpt-4.1-mini-2025-04-14', 'OpenAI · GPT-4.1 mini · 2025-04-14'),
    ModelChoice('openai/gpt-4.1-nano-2025-04-14', 'OpenAI · GPT-4.1 nano · 2025-04-14'),
    ModelChoice('openai/gpt-5.4', 'OpenAI · GPT-5.4'),
    ModelChoice('openai/gpt-5.2', 'OpenAI · GPT-5.2'),
    ModelChoice('openai/gpt-5.1', 'OpenAI · GPT-5.1'),
    ModelChoice('openai/gpt-5', 'OpenAI · GPT-5'),
    ModelChoice('openai/gpt-5-mini', 'OpenAI · GPT-5 mini'),
    ModelChoice('openai/gpt-5-nano', 'OpenAI · GPT-5 nano'),
    ModelChoice('anthropic/claude-sonnet-4-6', 'Anthropic · Claude Sonnet 4.6'),
    ModelChoice('anthropic/claude-opus-4-6', 'Anthropic · Claude Opus 4.6'),
    ModelChoice('anthropic/claude-haiku-4-5-20251001', 'Anthropic · Claude Haiku 4.5 · 2025-10-01'),
    ModelChoice('gemini/gemini-2.5-flash', 'Google · Gemini 2.5 Flash · existing API users only'),
    ModelChoice('gemini/gemini-2.5-flash-lite', 'Google · Gemini 2.5 Flash-Lite · existing API users only'),
    ModelChoice('gemini/gemini-2.5-pro', 'Google · Gemini 2.5 Pro · existing API users only'),
)
BY_ID = {model.id: model for model in MODELS}


def choices(current):
    """Keep an imported identifier verbatim, including unsupported routes for validation."""
    known = tuple(BY_ID)
    return known if current in BY_ID or not current else known + (current,)


def label(model):
    return BY_ID[model].name if model in BY_ID else f'Imported model · {model} · not in checked list'


def guidance(model):
    if model == 'mock':
        return 'Mock responses demonstrate the workflow. They are not model-generated research results.'
    lines = [f'API model ID: {model}.']
    choice = BY_ID.get(model)
    if choice and choice.note:
        lines.append(choice.note)
    else:
        lines.append('Please use a key from this provider with access to this model and sufficient API quota.')
    if model.startswith('openai/gpt-5') or model.startswith('gemini/') and not any(
            name in model for name in ('flash-lite',)):
        lines.append('Reasoning may use part of the output limit. Small speech, survey or state limits can leave no usable answer; review all three limits before starting.')
    if model.startswith('gemini/gemini-2.5'):
        lines.append('Google currently restricts this model family to existing API users. New Gemini projects need a newer adapter, which this prototype does not yet include.')
    if not choice:
        lines.append('Kept exactly as imported. Choose a listed model if this identifier is unavailable; TeamLab never substitutes a model automatically.')
    return ' '.join(lines)
