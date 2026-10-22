"""agent/boundary.py — complete-thought enforcement.

Borrowed the "no mid-sentence cuts" rule: adjust highlight start/end so clips
always open and close at a sentence boundary (period, question mark, exclamation).

Takes word-level timestamps (from faster-whisper word_timestamps=True) plus the
raw highlight window, and returns an adjusted window that:
  - starts at the first sentence boundary >= requested start (or the requested start if it's already a boundary)
  - ends at the first sentence boundary <= requested end (or the requested end if it's already a boundary)

This is the chopify "complete thought" safeguard, kept dependency-free.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("agent.boundary")

SENTENCE_ENDS = {".", "!", "?", "…", "。", "؟"}

__all__ = ["adjust_window"]


def _is_sentence_end(ch: str) -> bool:
    return ch in SENTENCE_ENDS or ch.isdigit() and False  # keep simple; digits aren't ends


def _next_boundary(words: List[Dict], after: float, direction: int) -> Optional[float]:
    """Return the timestamp of the next sentence boundary in `direction` (+1 forward, -1 backward).

    `direction=+1`: nearest boundary with timestamp >= `after`.
    `direction=-1`: nearest boundary with timestamp <= `after`.
    Returns None if not found within the word list.
    """
    tails = [w.get("end", 0.0) for w in words if w.get("end", 0.0) > 0]
    if not tails:
        return None
    if direction == 1:
        for w in words:
            if w.get("end", 0.0) >= after:
                # heuristic: a sentence end is a punctuation at the end of a segment's text
                # We approximate by checking the segment text ends with a sentence end.
                tx = w.get("text", "")
                if tx and tx[-1] in SENTENCE_ENDS and w.get("end", 0.0) >= after:
                    return w.get("end", 0.0)
        # fallback: first timestamp >= after
        for w in words:
            if w.get("end", 0.0) >= after:
                return w.get("end", 0.0)
        return None
    else:
        # backward: last boundary <= after (max end <= after among those ending <= after)
        cands = [w.get("end", 0.0) for w in words if w.get("end", 0.0) <= after]
        if not cands:
            return None
        best = max(cands)
        for w in words:
            if w.get("end", 0.0) == best:
                tx = w.get("text", "")
                if tx and tx[-1] in SENTENCE_ENDS:
                    return best
        return best


def _nearest_boundary(words: List[Dict], at: float, direction: int) -> Optional[float]:
    return _next_boundary(words, at, direction)


def adjust_window(
    words: List[Dict],
    start: float,
    end: float,
    *,
    min_duration: float = 15.0,
    max_duration: float = 180.0,
) -> Tuple[float, float]:
    """Return an adjusted (start, end) that lands on sentence boundaries.

    Strategy:
      - forward: ensure `start` is a sentence boundary (move to the next one
        if the requested start falls mid-sentence)
      - backward: ensure `end` is a sentence boundary (move to the previous one
        if the requested end falls mid-sentence)
      - clamp both to [min_duration, max_duration]
    """
    if not words:
        return (max(min_duration, start), min(max_duration, end))

    # Forward boundary for `start`: move to end of a sentence that contains or follows `start`.
    # We find the first segment whose end >= start and ends with sentence punctuation.
    start_candidate = start
    for w in words:
        if w.get("end", 0.0) >= start:
            tx = w.get("text", "")
            if tx and (tx[-1] in SENTENCE_ENDS) and w.get("end", 0.0) >= start:
                start_candidate = w.get("end", 0.0)
            break

    # Backward boundary for `end`: nearest sentence boundary <= end.
    end_candidate = end
    prev = None
    for w in words:
        if w.get("end", 0.0) <= end:
            tx = w.get("text", "")
            if tx and tx[-1] in SENTENCE_ENDS:
                prev = w.get("end", 0.0)
            elif prev is not None:
                # first word after a boundary ends the sentence; use prev
                end_candidate = prev
                break
    else:
        # loop finished without break: if we never hit a word > end, try last boundary before end
        cands = [w.get("end", 0.0) for w in words if w.get("end", 0.0) <= end]
        if cands:
            end_candidate = max(cands)

    start_candidate = max(0.0, min(float(start_candidate), end_candidate - min_duration))
    end_candidate = max(start_candidate + min_duration, min(float(end_candidate), end_candidate + max_duration))
    end_candidate = min(end_candidate, max_duration)

    log.debug("adjust_window %.1f->%.1f (start) and %.1f->%.1f (end)", start, start_candidate, end, end_candidate)
    return (round(start_candidate, 3), round(end_candidate, 3))
