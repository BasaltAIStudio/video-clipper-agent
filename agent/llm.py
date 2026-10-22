"""agent: LLM fallback chain (OpenRouter -> NVIDIA NIM).

Borrowed the pluggable-LLM design from Anil-matcha/AI-Youtube-Shorts-Generator,
wired to the user's two-key chain. All providers speak the OpenAI
``chat/completions`` shape, so a single POST helper covers OpenRouter and NVIDIA NIM.

Usage:
    >>> from agent.llm import prompt_highlights
    >>> highlights = prompt_highlights(transcript_text, content_type, density,
    ...                                num_clips, duration, use_llm=True)
    >>> # For zero-LLM mode, pass use_llm=False (or better: use
    >>> # agent.heuristic.score_segments(transcript, num_clips))
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests

from .config import (
    NVIDIA_NIM_API_KEY,
    NVIDIA_NIM_BASE_URL,
    NVIDIA_NIM_MODEL,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL,
    PROVIDER_ORDER,
    logger_config,
)
from .config import PROVIDER_CONFIG  # noqa: F401  (documented config surface)

log = logger_config("agent.llm")

__all__ = [
    "LLMError",
    "LLMRateLimitError",
    "LLMTimeoutError",
    "completions",
    "prompt_highlights",
]


class LLMError(Exception):
    """All LLM providers failed for a non-recoverable reason."""


class LLMRateLimitError(LLMError):
    """Rate-limited; the caller should back off."""


class LLMTimeoutError(LLMError):
    """Request timed out."""


def _post_chat_completions(base_url: str, api_key: str, model: str, prompt: str, timeout: int) -> str:
    """POST a chat completions request in the OpenAI shape."""
    if not api_key:
        raise LLMError("no API key configured for this provider")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }
    resp = requests.post(
        f"{base_url}/chat/completions",
        json=payload,
        headers=headers,
        timeout=timeout,
    )
    if resp.status_code == 429:
        raise LLMRateLimitError(f"rate limited (HTTP {resp.status_code})")
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def completions(prompt: str, *, timeout: int = 120, max_retries: int = 1) -> dict[str, Any]:
    """Try providers in priority order; return parsed JSON.

    On total failure, raises LLMError carrying a list of the per-provider
    errors so the caller can log them and report a useful message.
    """
    errors: list[str] = []
    for cfg in PROVIDER_ORDER:
        api_key = cfg["api_key"]
        if not api_key:
            errors.append(f"{cfg['model']} provider: no API key configured (set {cfg['model'].upper()}_API_KEY)")
            continue
        try:
            raw = _post_chat_completions(cfg["base_url"], api_key, cfg["model"], prompt, timeout=timeout)
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                # Prompt asked for JSON but the model emitted prose/fences.
                raise LLMError(f"{cfg['model']} returned non-JSON: {e}") from e
        except (LLMRateLimitError, LLMTimeoutError):
            errors.append(f"{cfg['model']}: {type(_) if isinstance(_, LLMRateLimitError) else getattr(_, '__name__', type(_).__name__)}")
            continue
        except Exception as e:
            errors.append(f"{cfg['model']}: {type(e).__name__}: {e}")
            continue

    raise LLMError("all LLM providers failed: " + "; ".join(errors))


def prompt_highlights(
    transcript,
    content_type: str,
    density: str,
    num_clips: int,
    duration: float,
    use_llm: bool = True,
) -> dict:
    """Run the highlight-generation prompt against the fallback chain.

    If use_llm is False, the transcript is a dict (with "segments") and the
    heuristic scorer (Chopify-style) is used instead. Returns {"highlights": [...]}.
    """
    if not use_llm:
        from .heuristic import score_segments

        return {"highlights": score_segments(transcript, num_clips)}

    transcript_text = transcript  # type: ignore  (str if LLM path; heuristic path returns early)

    # Ask for ~2x the target so dedupe has headroom (see AI-Youtube-Shorts-Generator
    # call_highlight_api).
    target = max(num_clips * 2, 5)
    natural_max = max(2, int(duration / 90))
    min_clips = min(target, natural_max, 8)

    system = (
        "You are an elite short-form video editor who has studied thousands of "
        "viral clips on TikTok, Instagram Reels, and YouTube Shorts. "
        "You know exactly what makes viewers stop scrolling, watch to the end, "
        "and share.\n\n"
        "Virality signals to prioritize (ranked by impact):\n"
        "1. HOOK MOMENTS - statements that create immediate curiosity\n"
        "2. EMOTIONAL PEAKS - genuine surprise, laughter, anger, vulnerability, excitement\n"
        "3. OPINION BOMBS - strong, polarizing or counter-intuitive statements\n"
        "4. REVELATION MOMENTS - surprising facts, stats, or confessions\n"
        "5. CONFLICT/TENSION - disagreement, pushback, problem confronted head-on\n"
        "6. QUOTABLE ONE-LINERS - a sentence that works as a standalone quote card\n"
        "7. STORY PEAKS - the climax or twist of an anecdote; the payoff moment\n"
        "8. PRACTICAL VALUE - a concrete tip, hack, or insight the viewer can apply\n\n"
        f"Content type: {content_type} | Density: {density}\n"
        "Every highlight must open with a strong HOOK (grab attention within the "
        "first 3 seconds) and must NOT cut mid-sentence or mid-thought - each clip "
        "must be complete and self-contained.\n"
        f"Generate at least {min_clips} highlights, each 20-180 seconds.\n"
        "Respond ONLY with valid JSON (no markdown, no explanation):\n"
        '{"highlights":[{"title":"string","start_time":float,"end_time":float,'
        '"score":int,"hook_sentence":"string","virality_reason":"string"}]}\n'
    )

    prompt = (
        system
        + "\n\nTranscript (absolute-start speaker:segment lines):\n"
        + transcript_text
    )

    return completions(prompt, timeout=180)
