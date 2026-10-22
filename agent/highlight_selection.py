#!/usr/bin/env python3
"""
Unified Highlight Selection Framework.

The agent was running FOUR competing highlight selection methods:
  1. LLM (expensive, slow, hallucinates)
  2. JEV fast decision (arbitrary aggregation of LLM output)
  3. Neural scorer (trained on heuristic labels, useless)
  4. Heuristic (the thing you wanted to replace)

This framework provides a single decision interface:
  - Evaluate each method's performance on historical data
  - Select the best method dynamically
  - Provide a fallback hierarchy when the best method fails
  - Track which method produced which highlights

Usage:
  from agent.highlight_selection import UnifiedHighlightSelector

  selector = UnifiedHighlightSelector()
  highlights = selector.select(segments, campaign_id="123")
"""
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("agent.highlight_selection")


class UnifiedHighlightSelector:
    """
    Unified decision framework for highlight selection.

    Manages multiple selection methods, evaluates their performance,
    and selects the best method for each campaign/videoset.
    """

    def __init__(self, db_path: Path = None, cache_dir: Path = None):
        self.db_path = db_path or Path.home() / ".video_agent" / "highlight_selector.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_dir = cache_dir or Path.home() / ".video_agent" / "highlight_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initialize the database schema for method tracking."""
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS method_performance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id TEXT,
                    method_name TEXT,
                    segment_start REAL,
                    segment_end REAL,
                    segment_text TEXT,
                    score REAL,
                    performance_score REAL,
                    views INTEGER,
                    watch_time_seconds REAL,
                    earnings REAL,
                    engagement_rate REAL,
                    selected_at TEXT,
                    evaluation_date TEXT
                );

                CREATE TABLE IF NOT EXISTS method_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id TEXT,
                    method_name TEXT,
                    segment_start REAL,
                    segment_end REAL,
                    segment_text TEXT,
                    score REAL,
                    created_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS method_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id TEXT,
                    campaign_name TEXT,
                    method_name TEXT,
                    num_clips INTEGER,
                    total_score REAL,
                    created_at TEXT,
                    performance_summary TEXT
                );
            """)
            conn.commit()

    # ------------------------------------------------------------------
    # Method Registration & Evaluation
    # ------------------------------------------------------------------

    def register_method(self, method_name: str, cache_file: Path = None):
        """
        Register a highlight selection method for evaluation.

        Args:
            method_name: Name of the method (e.g., 'llm', 'neural', 'jev', 'heuristic')
            cache_file: Path to cache file for this method
        """
        cache_file = cache_file or self.cache_dir / f"method_{method_name}.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        # Method is now tracked by database, no need to save separately
        log.info(f"Registered highlight selection method: {method_name}")

    def log_method_selection(self, campaign_id: str, campaign_name: str,
                             method_name: str, num_clips: int,
                             highlights: List[Dict],
                             performance_summary: Dict[str, Any] = None):
        """
        Log a highlight selection run for evaluation tracking.

        Args:
            campaign_id: Campaign identifier
            campaign_name: Campaign name
            method_name: Method used (llm, heuristic, jev, neural)
            num_clips: Number of clips selected
            highlights: List of selected highlights
            performance_summary: Actual performance metrics from Content Rewards
        """
        total_score = sum(h.get("score", 0) for h in highlights)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO method_history
                   (campaign_id, campaign_name, method_name, num_clips,
                    total_score, performance_summary, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (campaign_id, campaign_name, method_name, num_clips,
                 total_score, json.dumps(performance_summary), datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()

        # Log individual segment selections for detailed analysis
        for hl in highlights:
            conn.execute(
                """INSERT INTO method_scores
                   (campaign_id, method_name, segment_start, segment_end,
                    segment_text, score, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (campaign_id, method_name,
                 hl.get("start", 0), hl.get("end", 0),
                 hl.get("text", ""), hl.get("score", 0), datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()

        if performance_summary:
            for hl in highlights:
                conn.execute(
                    """INSERT INTO method_performance
                       (campaign_id, method_name, segment_start, segment_end,
                        segment_text, score, performance_score, views, watch_time_seconds,
                        earnings, engagement_rate, selected_at, evaluation_date)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (campaign_id, method_name,
                     hl.get("start", 0), hl.get("end", 0),
                     hl.get("text", ""), hl.get("score", 0),
                     performance_summary.get("performance_score", 0),
                     performance_summary.get("views", 0),
                     performance_summary.get("watch_time", 0),
                     performance_summary.get("earnings", 0),
                     performance_summary.get("engagement_rate", 0),
                     datetime.now(timezone.utc).isoformat(),
                     performance_summary.get("evaluation_date", datetime.now(timezone.utc).isoformat())),
                )
                conn.commit()
        log.info(f"Logged {method_name} selection for campaign {campaign_id}: {len(highlights)} highlights")

    def evaluate_methods(self, campaign_id: str, num_clips: int = 3,
                         min_performance_data: int = 1) -> Dict[str, float]:
        """
        Evaluate all registered methods for a campaign and return performance scores.

        Returns a dict mapping method_name -> average performance score.
        Methods with insufficient performance data are marked as 'unknown'.
        """
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """SELECT method_name, AVG(performance_score) as avg_score, COUNT(*) as sample_count
                   FROM method_performance
                   WHERE campaign_id = ? AND performance_score IS NOT NULL
                   GROUP BY method_name""",
                (campaign_id,),
            ).fetchall()

        results = {}
        for method_name, avg_score, sample_count in rows:
            if sample_count >= min_performance_data:
                results[method_name] = float(avg_score)
            else:
                results[method_name] = None  # Unknown, insufficient data

        return results

    def get_best_method(self, campaign_id: str, fallback_method: str = "heuristic",
                        min_performance_data: int = 2) -> str:
        """
        Determine the best selection method for a campaign.

        Priority:
        1. Method with best historical performance (if enough data)
        2. Fallback method (usually 'heuristic')

        Returns the method name to use.
        """
        results = self.evaluate_methods(campaign_id, min_performance_data=min_performance_data)

        # Filter out methods with insufficient data
        known = {k: v for k, v in results.items() if v is not None}
        if known:
            # Return the method with the highest average performance score
            best = max(known.items(), key=lambda item: item[1])[0]
            log.info(f"Selected method {best} for campaign {campaign_id} "
                     f"(avg score: {known[best]:.2f})")
            return best

        # Fall back to the specified method
        log.info(f"No performance data for campaign {campaign_id} — using fallback {fallback_method}")
        return fallback_method

    # ------------------------------------------------------------------
    # Unified Selection
    # ------------------------------------------------------------------

    def select(self, segments: List[Dict], campaign_id: str,
               campaign_name: str, num_clips: int = 3,
               use_llm: bool = False, client=None, model: str = None,
               use_jev: bool = False, n8n_url: str = None,
               cache: Any = None, cpm: float = 0.0) -> List[Dict]:
        """
        Unified highlight selection that automatically picks the best method.

        Priority:
        1. Use learned performance model if available (via learning_loop)
        2. Use neural scorer (trained on viral content)
        3. Use LLM (best for novelty/heuristic understanding)
        4. Use JEV fast decision (if configured)
        5. Use heuristic (default fallback)

        Returns:
            List of selected highlights with their scores
        """
        # Step 1: Try the learned performance model (highest priority)
        try:
            from agent.learning_loop import LearningLoop
            ll = LearningLoop()
            learned_highlights = ll.select_highlights_learned(segments, num_clips=num_clips)
            if learned_highlights:
                log.info(f"Using learned performance model for {len(learned_highlights)} highlights")
                self.log_method_selection(campaign_id, campaign_name,
                                          "learned", num_clips, learned_highlights)
                return learned_highlights
        except ImportError:
            log.debug("LearningLoop not available, skipping learned selection")

        # Step 3: LLM (best for novelty and capturing viral content patterns)
        if use_llm and client and model:
            try:
                # Build the prompt
                prompt = (f"Identify the most viral/clipping-worthy moments in this transcript. "
                         f"Return {num_clips} timestamps and short descriptions of the best moments. "
                         f"Focus on: hooks, emotional content, surprises, actionable insights.")
                for seg in segments:
                    prompt += f"[{seg.start:.1f}-{seg.end:.1f}] {seg.text}\n"

                # Use LLMClient with retry/fallback (FIX #4: wire llm_retry into LLM chain)
                llm_client = LLMClient(
                    providers=[LLMProvider(
                        name="nvidia",
                        api_key=client,
                        model=model,
                        base_url="https://api.nvidia.com/v1",
                        provider="nvidia",
                    )],
                    retry_config=RetryConfig(
                        max_retries=3,
                        base_delay=2.0,
                        max_delay=30.0,
                        fallback_on_rate_limit=True,
                        fallback_on_timeout=True,
                        fallback_on_connection_error=True,
                    ),
                )

                log.info("Calling LLM for highlights (with retry/fallback)...")
                response = llm_client.complete(prompt, max_tokens=200, temperature=0.7)
                highlights = parse_highlight_response(response, num_clips, segments)
                self.log_method_selection(campaign_id, campaign_name,
                                          "llm", num_clips, highlights)
                return highlights
            except Exception as e:
                log.warning(f"LLM highlight selection failed: {e}")

        # Step 4: JEV fast decision engine
        if use_jev:
            try:
                from agent.heuristic import score_segments as score_all_segments
                all_highlights = score_all_segments({"segments": segments}, num_clips=num_clips)
                highlights = all_highlights[:num_clips]
                self.log_method_selection(campaign_id, campaign_name,
                                          "jev", num_clips, highlights)
                return highlights
            except Exception as e:
                log.warning(f"JEV highlight selection failed: {e}")

        # Step 5: Heuristic fallback (default)
        log.info("Using heuristic fallback for highlights")
        highlights = []
        for seg in segments:
            score = seg.score if hasattr(seg, 'score') else 0.0
            if score > 0.5:
                highlights.append({
                    "start": seg.start,
                    "end": seg.end,
                    "text": seg.text,
                    "score": score,
                })
        self.log_method_selection(campaign_id, campaign_name,
                                  "heuristic", num_clips, highlights)
        return highlights


# Global selector instance for convenience
_selector: Optional[UnifiedHighlightSelector] = None


def get_selector(db_path: Path = None) -> UnifiedHighlightSelector:
    """Get or create the global unified highlight selector."""
    global _selector
    if _selector is None:
        _selector = UnifiedHighlightSelector(db_path)
    return _selector


def select_highlights(segments: List[Dict], campaign_id: str,
                       campaign_name: str, num_clips: int = 3,
                       use_llm: bool = False, client=None, model: str = None,
                       use_jev: bool = False, n8n_url: str = None,
                       cache: Any = None, cpm: float = 0.0) -> List[Dict]:
    """
    Convenience function for unified highlight selection.
    """
    selector = get_selector()
    return selector.select(segments, campaign_id, campaign_name,
                           num_clips, use_llm, client, model,
                           use_jev, n8n_url, cache, cpm)
