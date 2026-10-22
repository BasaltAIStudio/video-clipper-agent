# =============================================================================
# jev_decision.py — Fast CPU decision engine wrapper (from JEV's choice.hpp)
#
# Purpose: Bring the JEV fast decision algorithm into the agent's highlight
# selection pipeline. JEV's `choose()` aggregates probabilities across multiple
# phrasings of a choice question, normalizes, picks the best option, and reports
# a confidence score. The agent uses this to make faster, more confident
# decisions about which segments are best for clipping, without the LLM being
# the bottleneck.
#
# API:
#   from jev_decision import JVEDecisionEngine
#   engine = JVEDecisionEngine(num_options=3)
#   engine.add_phrasing_probs(phrasing_index, {option_idx: probability})
#   result = engine.decide()   # ChoiceAnswer-like dict
# =============================================================================

from __future__ import annotations
import math
from typing import Dict, List, Optional, Tuple


class ChoiceAnswer:
    """Mirrored from JEV's choice.hpp ChoiceAnswer struct."""

    def __init__(self, best: int, probabilities: List[float], confidence: float):
        self.best = best
        self.probabilities = probabilities
        self.confidence = confidence

    def __repr__(self) -> str:
        return f"ChoiceAnswer(best={self.best}, confidence={self.confidence:.3f}, probs={self.probabilities})"


class JVEDecisionEngine:
    """
    Fast CPU decision engine. Mirrors JEV's inline `choose()` algorithm
    (src/choice.hpp).

    The engine does NOT generate probabilities; it consumes them. The agent's
    LLM pipeline must supply a probability per option per phrasing. This module
    aggregates across phrasings and produces a normalized choice.
    """

    def __init__(self, num_options: int, num_default_phrasings: int = 3):
        self.num_options = num_options
        self.num_phrasings = num_default_phrasings
        # probs[f][i] = P(option i is correct under phrasing f)
        self._probs: List[List[float]] = [[] for _ in range(num_default_phrasings)]
        self._num_phrasings_set = 0

    def add_phrasing_probs(self, phrasing_idx: int,
                           option_probs: Dict[int, float]) -> None:
        """Register the probability that each option is correct under one phrasing."""
        if phrasing_idx >= self.num_phrasings:
            self._probs.append([0.0] * self.num_options)
        self._probs[phrasing_idx] = [option_probs.get(i, 0.0) for i in range(self.num_options)]
        self._num_phrasings_set = max(self._num_phrasings_set, phrasing_idx + 1)

    def set_num_options(self, num: int) -> None:
        self.num_options = num
        for p in self._probs:
            p.extend([0.0] * (num - len(p)))
        while len(self._probs) < self.num_phrasings:
            self._probs.append([0.0] * self.num_options)

    def decide(self) -> Optional[ChoiceAnswer]:
        """
        Run the JEV choose() algorithm.
        Returns a ChoiceAnswer or None if no probabilities were registered.
        """
        if self._num_phrasings_set == 0:
            return None

        phrasings = self._num_phrasings_set
        n = self.num_options
        p = [0.0] * n

        # Average across phrasings
        for f in range(phrasings):
            for i in range(n):
                p[i] += self._probs[f][i] / float(phrasings)

        # Normalize
        total = sum(p)
        if total > 0.0:
            p = [x / total for x in p]
        else:
            p = [1.0 / n] * n

        # Pick best
        best = 0
        for i in range(1, n):
            if p[i] > p[best]:
                best = i

        # Confidence
        uniform = 1.0 / n
        confidence = (p[best] - uniform) / (1.0 - uniform) if n > 1 else 0.0
        confidence = max(0.0, min(1.0, confidence))

        return ChoiceAnswer(best, list(p), confidence)


def jev_choose(ps: List[List[float]], n: int) -> Optional[ChoiceAnswer]:
    """
    Standalone version of JEV's inline `choose()` for direct use.

    ps: phrasing-major, phrase-major array of shape [phrasings][n]
    """
    phrasings = len(ps)
    if phrasings == 0 or n == 0:
        return None
    p = [0.0] * n
    for f in range(phrasings):
        for i in range(n):
            p[i] += ps[f][i] / float(phrasings)
    total = sum(p)
    if total > 0.0:
        p = [x / total for x in p]
    else:
        p = [1.0 / n] * n
    best = 0
    for i in range(1, n):
        if p[i] > p[best]:
            best = i
    uniform = 1.0 / n
    confidence = (p[best] - uniform) / (1.0 - uniform) if n > 1 else 0.0
    confidence = max(0.0, min(1.0, confidence))
    return ChoiceAnswer(best, list(p), confidence)
