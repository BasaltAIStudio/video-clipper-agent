"""agent/virality.py — scoring and de-duplication of candidate highlights.

Score-based dedup (from Anil-matcha/AI-Youtube-Shorts-Generator highlights.py)
suppresses any highlight that overlaps >50% of its own duration with a
higher-scoring highlight already kept. Heuristics: prefer earlier hooks, shorter
but complete clips, and balance uniqueness across the video timeline.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger("agent.virality")

__all__ = ["score_highlights", "dedupe_highlights"]


def _coerce_int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _coerce_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def dedupe_highlights(highlights: List[Dict], overlap_ratio: float = 0.5) -> List[Dict]:
    """Drop any highlight overlapping >overlap_ratio of its own duration with a
    higher-scoring one already kept. Returns a new list (input unchanged)."""
    if not highlights:
        return []
    ordered = sorted(highlights, key=lambda x: _coerce_int(x.get("score", 0), 0), reverse=True)
    kept: List[Dict] = []
    for h in ordered:
        h_start = _coerce_float(h.get("start_time"), 0.0)
        h_end = _coerce_float(h.get("end_time"), h_start)
        h_dur = h_end - h_start
        if h_dur <= 0:
            continue
        overlapping = False
        for k in kept:
            k_start = _coerce_float(k.get("start_time"), 0.0)
            k_end = _coerce_float(k.get("end_time"), k_start)
            overlap = min(h_end, k_end) - max(h_start, k_start)
            if overlap > 0 and overlap > overlap_ratio * h_dur:
                overlapping = True
                break
        if not overlapping:
            kept.append(dict(h))
    # Re-sort by score desc for stable output.
    return sorted(kept, key=lambda x: _coerce_int(x.get("score", 0), 0), reverse=True)


def score_highlights(
    highlights: List[Dict],
    *,
    clip_duration: float,
    video_duration: float,
) -> List[Dict]:
    """Re-rank / nudge highlight scores for consistency across candidates.

    `clip_duration` = length of a typical short (~90s). `video_duration` = source length.
    Applies light, deterministic nudges:
      - prefer highlights that start within the first 30s of the video (hook potential)
      - penalise near-duplicate timestamps
      - keep scores in [0, 100]
    """
    if not highlights:
        return highlights
    kept: List[Dict] = []
    for h in highlights:
        start = _coerce_float(h.get("start_time"), 0.0)
        # Hook-potential: earlier is better (scrolling context)
        hook_boost = max(0.0, 1.0 - start / 30.0) * 4.0
        score = _coerce_int(h.get("score", 0), 0) + hook_boost
        # Penalise being too close to an already-kept highlight
        for k in kept:
            k_start = _coerce_float(k.get("start_time"), 0.0)
            k_end = _coerce_float(k.get("end_time"), k_start)
            overlap = min(start + 90.0, k_end) - max(start, k_start)
            if overlap > 0:
                pen = (overlap / 30.0) * 3.0
                score -= pen
        score = max(0, min(100, int(score)))
        h["score"] = score
        kept.append(h)
    # Now de-duplicate overlapping candidates (keeps the best-scoring, non-overlapping).
    return dedupe_highlights(kept)
