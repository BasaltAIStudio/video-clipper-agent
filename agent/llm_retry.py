#!/usr/bin/env python3
"""
LLM Retry & Fallback System.

The agent makes LLM calls for highlight selection, but the LLM API is
unreliable — it can rate-limit, timeout, or fail. This module implements
robust retry logic with exponential backoff and automatic fallback to
cheaper models or alternative providers.

Usage:
    from agent.llm_retry import LLMClient, LLMProvider

    # Create a client with multiple fallback strategies
    client = LLMClient(
        providers=[
            LLMProvider(name="nous", api_key=KEY1, model="nous-protocol-7b"),
            LLMProvider(name="nous", api_key=KEY2, model="llama-3-8b"),
            LLMProvider(name="nous", api_key=KEY3, model="llama-3-8b"),
        ],
        retry_config=RetryConfig(
            max_retries=3,
            base_delay=2.0,
            max_delay=60.0,
            fallback_on_rate_limit=True,
            fallback_on_timeout=True,
        )
    )

    # Use it
    response = client.complete(prompt, max_tokens=100)
    # If the first provider fails, automatically tries the next
"""
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("agent.llm_retry")


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""
    max_retries: int = 3
    base_delay: float = 2.0
    max_delay: float = 60.0
    jitter: float = 0.5
    # Retry on these HTTP status codes
    retry_on_status: List[int] = None
    fallback_on_rate_limit: bool = True
    fallback_on_timeout: bool = True
    fallback_on_connection_error: bool = True

    def __post_init__(self):
        if self.retry_on_status is None:
            self.retry_on_status = [429, 500, 502, 503, 504]


@dataclass
class LLMProvider:
    """A single LLM provider endpoint."""
    name: str
    api_key: str
    model: str
    base_url: str = "https://api.nvidia.com/v1"
    provider: str = "nvidia"  # 'nvidia', 'openrouter', 'nous' etc.

    def __post_init__(self):
        if self.api_key is None or self.api_key == "":
            raise ValueError(f"API key required for provider {self.name}")


class LLMClient:
    """
    Robust LLM client with automatic retry and fallback.

    Automatically tries each provider in order until one succeeds.
    Retries with exponential backoff on rate limits and timeouts.
    """

    def __init__(self, providers: List[LLMProvider],
                 retry_config: RetryConfig = None):
        self.providers = providers
        self.retry_config = retry_config or RetryConfig()
        self._provider_index = 0

    def _get_next_provider(self) -> Optional[LLMProvider]:
        """Get the next provider in the fallback chain."""
        if self._provider_index >= len(self.providers):
            return None
        provider = self.providers[self._provider_index]
        self._provider_index += 1
        return provider

    def _should_retry(self, status_code: int, error_type: str) -> bool:
        """Determine if we should retry this error."""
        if error_type == "rate_limit" or status_code == 429:
            return self.retry_config.fallback_on_rate_limit
        if error_type == "timeout":
            return self.retry_config.fallback_on_timeout
        if error_type == "connection_error":
            return self.retry_config.fallback_on_connection_error
        if status_code in self.retry_config.retry_on_status:
            return True
        return False

    def _delay(self, attempt: int) -> float:
        """Calculate delay with exponential backoff and jitter."""
        delay = self.retry_config.base_delay * (2 ** attempt)
        delay = min(delay, self.retry_config.max_delay)
        # Add jitter
        delay = delay * (1 + self.retry_config.jitter * (random.random() - 0.5))
        return delay

    def complete(self, prompt: str, max_tokens: int = 100,
                 temperature: float = 0.7, system_prompt: str = None) -> str:
        """
        Call an LLM, with automatic fallback to next provider on failure.

        Returns the response text from the first successful provider.
        Raises the last exception if all providers fail.
        """
        last_error = None

        for attempt in range(self.retry_config.max_retries):
            # Find next available provider
            provider = self._get_next_provider()
            if provider is None:
                break

            log.info(f"LLM attempt {attempt + 1}: using provider {provider.name} "
                     f"({provider.model})")

            try:
                response = self._call_provider(provider, prompt, max_tokens, temperature, system_prompt)
                return response
            except Exception as e:
                last_error = e
                error_type = self._classify_error(e)

                if self._should_retry(None, error_type):
                    delay = self._delay(attempt)
                    log.warning(f"LLM call failed: {e}. Retrying in {delay:.1f}s "
                               f"(attempt {attempt + 1}/{self.retry_config.max_retries})")
                    time.sleep(delay)
                else:
                    log.error(f"LLM call failed: {e}. Not retrying.")
                    raise

        # All attempts failed
        raise last_error

    def _classify_error(self, error: Exception) -> str:
        """Classify the error type for retry decisions."""
        error_str = str(error).lower()
        if "timeout" in error_str or "timed out" in error_str:
            return "timeout"
        if "connection" in error_str or "network" in error_str:
            return "connection_error"
        if "429" in str(error) or "rate limit" in error_str:
            return "rate_limit"
        if "503" in str(error) or "502" in str(error) or "500" in str(error):
            return "server_error"
        return "unknown"

    def _call_provider(self, provider: LLMProvider, prompt: str,
                       max_tokens: int, temperature: float,
                       system_prompt: str = None) -> str:
        """Make a direct call to a provider."""
        # Determine the endpoint
        if provider.provider == "nvidia":
            url = f"{provider.base_url}/chat/completions"
        elif provider.provider == "openrouter":
            url = "https://openrouter.ai/api/v1/chat/completions"
        elif provider.provider == "nous":
            url = f"https://nousresearch.com/api/v1/chat/completions"
        else:
            url = f"{provider.base_url}/chat/completions"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {provider.api_key}",
        }

        payload = {
            "model": provider.model,
            "messages": [],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if system_prompt:
            payload["messages"].append({"role": "system", "content": system_prompt})

        payload["messages"].append({"role": "user", "content": prompt})

        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()

        result = response.json()
        if "choices" in result and len(result["choices"]) > 0:
            return result["choices"][0]["message"]["content"]
        else:
            raise ValueError(f"Unexpected response format: {result}")

    def complete_with_history(self, messages: List[Dict[str, str]],
                              max_tokens: int = 100,
                              temperature: float = 0.7) -> str:
        """
        Call LLM with a full message history (for multi-turn conversations).

        Args:
            messages: List of {"role": "user"/"assistant"/"system", "content": "..."}
        """
        # Get a fresh provider each time (for multi-turn, we want fresh attempts)
        provider = self._get_next_provider()
        if provider is None:
            raise RuntimeError("No providers available in the fallback chain")

        log.info(f"LLM call with history using provider {provider.name}")

        try:
            return self._call_provider_with_history(provider, messages, max_tokens, temperature)
        except Exception as e:
            error_type = self._classify_error(e)
            if self._should_retry(None, error_type):
                log.warning(f"LLM history call failed: {e}. Falling back to single prompt.")
                # Fall back to single prompt using the last user message
                last_user = next((m for m in reversed(messages) if m["role"] == "user"), None)
                if last_user:
                    return self.complete(last_user["content"], max_tokens, temperature)
            raise

    def _call_provider_with_history(self, provider: LLMProvider,
                                    messages: List[Dict[str, str]],
                                    max_tokens: int, temperature: float) -> str:
        """Call a provider with full message history."""
        if provider.provider == "nvidia":
            url = f"{provider.base_url}/chat/completions"
        elif provider.provider == "openrouter":
            url = "https://openrouter.ai/api/v1/chat/completions"
        elif provider.provider == "nous":
            url = "https://nousresearch.com/api/v1/chat/completions"
        else:
            url = f"{provider.base_url}/chat/completions"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {provider.api_key}",
        }

        payload = {
            "model": provider.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()

        result = response.json()
        if "choices" in result and len(result["choices"]) > 0:
            return result["choices"][0]["message"]["content"]
        else:
            raise ValueError(f"Unexpected response format: {result}")


# ---------------------------------------------------------------------------
# Convenience functions for one-shot highlight selection with retry
# ---------------------------------------------------------------------------

def select_highlights_with_retry(segments: List[Dict], prompt: str,
                                   providers: List[LLMProvider],
                                   max_tokens: int = 100,
                                   temperature: float = 0.7,
                                   n: int = 3,
                                   cache_dir: Path = None) -> List[Dict]:
    """
    Call the LLM with fallback, parse the response, and return highlights.

    This is the highest-level function — handles the full retry/fallback logic.
    """
    client = LLMClient(providers, RetryConfig(max_retries=3))

    # Build the full prompt
    prompt = f"{prompt}\n\n"
    for seg in segments:
        prompt += f"[{seg['start']:.1f}-{seg['end']:.1f}] {seg['text']}\n"

    # Call LLM
    response = client.complete(prompt, max_tokens=max_tokens, temperature=temperature)

    # Parse the highlights from the response
    highlights = parse_highlight_response(response, n, segments)
    return highlights


def parse_highlight_response(response: str, n: int, segments: List[Dict]) -> List[Dict]:
    """Parse LLM response into highlight dicts."""
    import re

    lines = response.strip().split("\n")
    highlights = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Try to match format: [time] description
        match = re.match(r'\[(\d+\.?\d*)\s*[–-]\s*(\d+\.?\d*)\]\s*(.*)', line)
        if match:
            start = float(match.group(1))
            end = float(match.group(2))
            text = match.group(3).strip()
            highlights.append({
                "start": start,
                "end": end,
                "text": text,
                "score": 0.0,  # Will be replaced by scorer
            })

        # Also handle: "time1-time2: description"
        match = re.match(r'(\d+\.?\d*)[-–](\d+\.?\d*):\s*(.*)', line)
        if match and not highlights:
            start = float(match.group(1))
            end = float(match.group(2))
            text = match.group(3).strip()
            highlights.append({
                "start": start,
                "end": end,
                "text": text,
                "score": 0.0,
            })

    # If no highlights parsed, return first N segments
    if not highlights:
        highlights = [{"start": s["start"], "end": s["end"], "text": s["text"]}
                      for s in segments[:n]]

    return highlights[:n]


# ---------------------------------------------------------------------------
# LLMProvider factory
# ---------------------------------------------------------------------------

def create_fallback_chain(api_keys: List[str], models: List[str] = None) -> List[LLMProvider]:
    """
    Create a fallback chain of LLM providers from API keys.

    Args:
        api_keys: List of API keys (or full API key strings)
        models: List of model names to use for each key

    Returns:
        List of LLMProvider objects in fallback order
    """
    if models is None:
        models = ["llama-3-8b", "llama-3-70b", "llama-3-8b"]

    # Parse keys in format "provider:key" or just "key"
    parsed = []
    for i, key in enumerate(api_keys):
        if ":" in key:
            provider, key = key.split(":", 1)
        else:
            provider = f"key{i + 1}"

        model = models[i] if i < len(models) else models[0]
        parsed.append(LLMProvider(
            name=provider,
            api_key=key,
            model=model,
            provider="nvidia",
        ))

    return parsed
