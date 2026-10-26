"""agent/heuristic.py — Chopify-style heuristic highlight scorer (no LLM).

Pure-keyword + density-based scoring. Extracts the feature weights from
mehbul/chopify (hook/shock/humour/controversy/insight/emotion/energy/arc)
and scores each transcript segment without any LLM call.

Usage:
    >>> from agent.heuristic import score_segments
    >>> highlights = score_segments(transcript, num_clips=3)

With this you can run the agent overnight on a free tier with no LLM key at all:
LLM_PROVIDER=none in .env, or ``python run.py --no-llm <url> --num-clips 3``.
"""

from __future__ import annotations
import re
from typing import Dict, List, Tuple

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Cue groups (adapted from Chopify: keyword detection for virality signals)
# Each group is matched case-insensitively against each segment's text.
# ---------------------------------------------------------------------------
HOOK_CUES = {
    "the secret", "nobody talks", "no one says", "everyone's wrong",
    "the truth", "here's the thing", "what they don't tell you",
    "stop scrolling", "without paying", "you won't believe",
    "here's the catch", "here's the problem", "here's the solution",
    "here's how to", "what you need to know", "here's the breakdown",
    "what actually", "what's really", "here's the why", "here's the info",
    "here's the method", "here's the complete", "here's the guide",
    "here's the plan", "here's the system", "here's the process",
    "here's the formula", "here's the secret", "here's the way",
}

SHOCK_CUES = {
    "but here's the thing", "actually", "here's the twist",
    "nobody expected", "unbelievable", "shocking", "scary",
    "dangerous", "life-threatening", "no one warns you",
    "everyone's wrong", "the secret", "nobody knows",
    "hidden", "secret", "unexpectedly",
}

HUMOUR_CUES = {
    "lol", "funny", "humour", "comedy", "joke", "meme",
    "parody", "satire", "prank", "gotcha", "fail",
    "laugh", "joking", "satirical", "cringey",
}

CONTROVERSY_CUES = {
    "controversial", "banned", "cancelled", "wrong",
    "toxic", "dangerous", "harmful", "problematic",
    "against the law", "illegal", "criticized",
    "hate", "bias", "discrimination", "unfair",
    "controlling", "oppressive", "oppression",
}

INSIGHT_CUES = {
    "the insight", "here's the key", "the trick", "how to",
    "here's what", "here's the thing", "here's the secret",
    "actually", "the real", "never knew", "you didn't know",
    "the truth", "the deal", "what matters", "here's the catch",
    "here's the problem", "here's the solution", "here's the info",
    "what you need to know", "here's the breakdown",
    "what actually", "what's really", "here's the why",
    "what the experts", "here's the method", "here's the complete",
    "here's the guide", "here's the plan", "here's the system",
    "here's the process", "here's the formula", "here's the secret",
    "here's the truth", "here's the way", "here's the trick",
    "here's the hack", "here's the shortcut", "here's the step",
    "here's the key", "here's the secret", "here's the deal",
}

EMOTION_CUES = {
    "excited", "angry", "scared", "happy", "sad", "shocked",
    "surprised", "furious", "devastated", "euphoric",
    "terrified", "relieved", "hopeless", "hopeful",
    "crying", "laughing", "giggly", "heated",
    "freaking", "absolutely", "incredible", "amazing",
    "awesome", "terrible", "horrible", "wild", "insane",
    "crazy", "unbelievable", "mind-blowing", "mind-bending",
    "devastating", "thrilled", "depressed", "nervous",
    "fulfilled", "overwhelmed", "paranoid",
    "delighted", "jealous", "envious", "suspicious",
    "terrifying", "heartbreaking", "powerful", "amazing",
}

# Weight per cue group; overall = sum(weight * score) * 10
WEIGHTS = {
    "hook": 0.20,
    "shock": 0.10,
    "humour": 0.05,
    "controversy": 0.10,
    "insight": 0.15,
    "emotion": 0.10,
    "energy": 0.15,
    "arc": 0.15,
}

# Sentence-end punctuation
SENTENCE_ENDS = {".", "!", "?", "…", "。", "؟", "✍", "❕", "❔", "❗", "〰"}

# Speech density target (words per second)
ENERGY_TARGET_WPS = 3.0

# Minimum words for a segment to be considered "dense" enough to score
MIN_WORDS_FOR_DENSITY = 3


def _cues(low: str, group: set) -> float:
    """Fraction of cue-group matches, capped at 1.0."""
    return min(1.0, sum(c in low for c in group) / 2.0)


def _norm_token(w: str) -> str:
    """Lowercase and strip punctuation from a word."""
    return re.sub(r"[^\w']", "", w.lower())


def _is_arc_complete(text: str) -> bool:
    """Heuristic: does the sentence look like a complete thought?
    A complete thought ends with sentence punctuation and has enough clause
    structure (at least one comma or sentence-final mark).
    """
    t = text.strip()
    if not t:
        return False
    if t[-1] not in SENTENCE_ENDS:
        return False
    clause_marks = t.count(",") + t.count("!") + t.count("?") + t.count(".")
    return clause_marks >= 1


def _calculate_salience(text: str, words: list, position: int, total_segments: int,
                        avg_segment_length: float) -> float:
    """Calculate a rough signal-activity salience score (0-1).

    Proxies for "interestingness" using cheap heuristics:
    - Position: earlier segments are more likely to be hooks (viewers scroll fast)
    - Length: longer segments carry more content per unit
    - Punctuation density: exclamation/question marks signal excitement
    - Word variety: unique words / total words (a proxy for vocabulary richness)
    """
    if total_segments <= 1:
        position_salience = 1.0
    else:
        position_ratio = position / (total_segments - 1)
        position_salience = 1.0 - position_ratio  # earlier = more salient

    # Length salience (relative to average segment length)
    if avg_segment_length > 0:
        length_salience = min(1.0, len(text) / avg_segment_length)
    else:
        length_salience = 1.0

    # Punctuation density
    punctuation = sum(1 for c in text if c in "!?")
    punctuation_density = punctuation / max(1, len(text))

    # Word variety
    word_count = len(words)
    unique_words = len(set(w.get("word", "").lower() for w in words if w.get("word")))
    word_variety = unique_words / max(1, word_count)

    # Weighted combination
    salience = (
        0.35 * position_salience +
        0.25 * length_salience +
        0.20 * min(1.0, punctuation_density * 6) +
        0.20 * word_variety
    )
    return min(1.0, salience)


def score_sentence(segment: Dict) -> Tuple[Dict[str, float], float]:
    """Score one transcript segment using Chopify-style cues.

    Returns (scores dict, overall float in [0, 100]).
    """
    text = segment.get("text", "") or ""
    words = segment.get("words", []) or []
    low = " " + text.lower() + " "

    # Speech density (words per second)
    start = float(segment.get("start", 0.0) or 0.0)
    end = float(segment.get("end", 0.0) or 0.0)
    duration = max(0.1, end - start)
    word_count = len(words)
    wps = word_count / duration if duration > 0 else 0.0

    # Arc completeness (sentence-structure heuristic)
    arc = 1.0 if _is_arc_complete(text) else 0.55

    # Cue matches
    scores = {
        "hook": _cues(low, HOOK_CUES),
        "shock": _cues(low, SHOCK_CUES),
        "humour": _cues(low, HUMOUR_CUES),
        "controversy": _cues(low, CONTROVERSY_CUES),
        "insight": _cues(low, INSIGHT_CUES),
        "emotion": _cues(low, EMOTION_CUES),
        "energy": min(1.0, wps / ENERGY_TARGET_WPS),
        "arc": arc,
    }

    # Weighted sum, scaled to 0-100
    overall = sum(scores[k] * WEIGHTS[k] for k in WEIGHTS) * 10.0
    return scores, round(overall, 2)


def score_segments(transcript: Dict, num_clips: int = 3) -> List[Dict]:
    """Score all segments and return the top `num_clips` as highlight candidates.

    Each segment is scored by its content cues + density. The highest-scoring
    (non-overlapping, complete-thought) segments are returned as highlights.

    Returns a list of dicts with keys: text, start, end, scores, overall, words.
    """
    if not transcript.get("segments"):
        return []

    segments = transcript["segments"]
    total = len(segments)
    avg_segment_length = 0.0
    if segments:
        avg_segment_length = sum(len(s.get("text", "") or "") for s in segments) / total
        avg_segment_length = max(1.0, avg_segment_length)

    scored: List[Dict] = []
    for idx, seg in enumerate(segments):
        if not (seg.get("text") or "").strip():
            continue
        text = seg["text"].strip()
        words = seg.get("words", []) or []
        start = float(seg.get("start", 0.0) or 0.0)
        end = float(seg.get("end", 0.0) or 0.0)

        # 1. Pure cue + density scoring (no context)
        scores, overall = score_sentence(seg)

        # 2. Salience detection (position, length, punctuation, word variety)
        salience = _calculate_salience(text, words, idx, total, avg_segment_length)

        # 3. Combine: base score + salience bonus
        salience_bonus = round(salience * 3.0, 2)  # max +3 points
        overall = round(overall + salience_bonus, 2)

        scored.append({
            "text": text,
            "start": start,
            "end": end,
            "scores": scores,
            "overall": overall,
            "salience": round(salience, 2),
            "words": words,
        })

    if not scored:
        return []

    # Rank by combined cue + salience score. (JEV aggregation was removed: it
    # only looked at the first num_clips+2 segments and raised IndexError on
    # longer transcripts.)
    dimension_scores = scored
    dimension_scores.sort(key=lambda x: x["overall"], reverse=True)

    # Greedily pick non-overlapping highlights (max 50% overlap dedupe)
    kept: List[Dict] = []
    for h in dimension_scores:
        h_start, h_end, h_dur = h["start"], h["end"], h["end"] - h["start"]
        if h_dur <= 0:
            continue
        overlap = False
        for k in kept:
            k_start, k_end = k["start"], k["end"]
            ov = min(h_end, k_end) - max(h_start, k_start)
            if ov > 0 and ov > 0.5 * h_dur:
                overlap = True
                break
        if not overlap:
            kept.append(h)

    # Return top num_clips (sorted by score)
    return kept[:num_clips]
