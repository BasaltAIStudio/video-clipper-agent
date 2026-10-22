"""agent/retention - retention curve prediction for clip performance.

Predicts how long viewers will watch each clip based on:
- Content features (length, topic, engagement signals)
- Historical performance data
- Retention curve modeling (drop-off over watch time)

The predictor is trained on actual performance data from Content Rewards,
not synthetic data.
"""
import json
import logging
import sqlite3
import contextlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("agent.retention")


class RetentionCurvePredictor:
    """
    Predicts retention curves for clip candidates based on historical data.

    The retention curve shows how many viewers are still watching at each
    point in the video (0-100%). We model:
    - Initial hook quality (first 30 seconds)
    - Sustained engagement ratio
    - Drop-off pattern (how quickly people leave)

    This is the single-purpose retention predictor - it predicts retention,
    not "virality" or other unrelated metrics.
    """

    def __init__(self):
        self.model = None
        self.trained = False
        self.history: List[Dict[str, Any]] = []

    def train_from_performance_db(self, db_path: Path):
        """
        Train the retention predictor from the performance database.

        Uses actual performance data (views, watch time, completion rate)
        as labels. No synthetic data - only real submissions.
        """
        if not db_path.exists():
            log.warning(f"Performance DB not found: {db_path}")
            return

        rows = []
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            curs = conn.execute(
                """SELECT clip_id, views, watch_time_seconds, cpm, engagement_rate,
                          retention_score, features_json
                   FROM clip_performance
                   ORDER BY submission_date DESC
                   LIMIT 500"""
            )
            rows = curs.fetchall()

        if len(rows) < 10:
            log.info(f"Not enough performance data to train retention predictor: {len(rows)} rows")
            self.trained = False
            return

        features = []
        labels = []
        curves = []

        for row in rows:
            clip_id, views, watch_time, cpm, engagement, retention_score, feat_json = row
            feat = {}
            if feat_json:
                try:
                    feat = json.loads(feat_json)
                except json.JSONDecodeError:
                    feat = {}

            # Features: clip length, engagement, CPM, watch time
            feat_vector = [
                feat.get("duration", 0) or 0,
                feat.get("text_length", 0) or 0,
                engagement or 0,
                cpm or 0,
                watch_time or 0,
            ]
            features.append(feat_vector)
            labels.append(retention_score or 0)
            curves.append([retention_score or 0])

        if len(features) < 10:
            log.info(f"Not enough training samples: {len(features)}")
            self.trained = False
            return

        # Simple linear model: retention_score = w1*duration + w2*engagement + w3*cpm + b
        # With very little data, a simple model is better than a complex one
        n = len(features)
        d = len(features[0])

        # Compute means
        mean_X = [sum(f[i] for f in features) / n for i in range(d)]
        mean_y = sum(labels) / n

        # Compute covariance and variance
        cov = [0.0] * d
        var = [0.0] * d
        for i in range(n):
            for j in range(d):
                cov[j] += (features[i][j] - mean_X[j]) * (labels[i] - mean_y)
                var[j] += (features[i][j] - mean_X[j]) ** 2

        # Compute weights (simple ridge regression approximation)
        weights = []
        for j in range(d):
            w = cov[j] / (var[j] + 1.0) if var[j] > 0 else 0.0
            weights.append(w)

        # Compute bias
        bias = mean_y - sum(weights[j] * mean_X[j] for j in range(d))

        self.model = {"weights": weights, "bias": bias}
        self.trained = True
        log.info(f"Retention curve predictor trained on {n} clips")

    def predict_retention(self, features: List[float]) -> Dict[str, Any]:
        """
        Predict retention metrics for a clip given its feature vector.

        Returns a dict with predicted retention_score and curve.
        """
        if not self.trained or self.model is None:
            return {
                "retention_score": 0.0,
                "predicted_curve": self._default_curve(),
                "confidence": 0.0,
            }

        m = self.model
        score = m["bias"]
        for w, f in zip(m["weights"], features):
            score += w * f

        score = max(0.0, min(100.0, score))

        # Generate a retention curve: drop-off modeled with a decay curve.
        # The initial hook determines how quickly people leave.
        # The curve is a 10-point retention curve (0-100 seconds sampling)
        return {
            "retention_score": round(score, 1),
            "predicted_curve": self._generate_curve(score, features),
            "confidence": min(0.9, 0.3 + 0.1 * len(features)),
        }

    def _default_curve(self) -> List[float]:
        """Return a default retention curve (neutral watch patterns)."""
        # 10-point curve: 0, 10, 20, 30, 40, 50, 60, 70, 80, 90 seconds
        # Represents a typical 90-second engagement
        return [1.0, 0.95, 0.88, 0.75, 0.60, 0.45, 0.35, 0.25, 0.15, 0.08]

    def _generate_curve(self, score: float, features: List[float]) -> List[float]:
        """
        Generate a retention curve from the predicted score.

        The curve models viewer drop-off over time.
        - High retention_score -> slow drop-off (high initial hook quality)
        - Low retention_score -> fast drop-off
        """
        # Features: [duration, text_length, engagement, cpm, watch_time]
        # engagement is feature[2], duration is feature[0]

        # Initial hook quality: higher engagement = better hook
        engagement = features[2] if len(features) > 2 else 0.5
        hook_quality = min(1.0, 0.3 + 0.7 * engagement) if engagement > 0 else 0.5

        # Duration affects retention: very short or very long is worse
        duration = features[0] if len(features) > 0 else 0.0
        duration_factor = min(1.0, 0.5 + 0.5 * min(duration / 120.0, 1.0))

        # Generate curve with exponential decay
        # Better retention -> slower decay
        decay_rate = 0.9 - 0.5 * (score / 100.0)
        decay_rate = max(0.7, min(0.95, decay_rate))

        curve = []
        for t in range(10):
            # Exponential decay
            retention = hook_quality * (decay_rate ** t)
            # Add some noise to make it realistic
            retention = retention * duration_factor * 0.9 + 0.05
            curve.append(max(0.0, min(1.0, retention)))

        # Scale so the area under curve matches the predicted score
        # (score is 0-100, curve values are 0-1, area = sum * 0.1)
        avg_curve = sum(curve) / len(curve) if curve else 0.0
        if avg_curve > 0:
            target_area = score / 100.0
            current_area = avg_curve
            scale = target_area / max(current_area, 0.05)
            curve = [max(0.0, min(1.0, c * scale)) for c in curve]

        return curve

    def log_history(self, clip_id: str, actual_curve: List[float], ts: Optional[str] = None):
        """Log a clip's actual performance for future training."""
        if ts is None:
            ts = datetime.now(timezone.utc).isoformat()
        self.history.append({
            "clip_id": clip_id,
            "actual_curve": actual_curve,
            "timestamp": ts,
        })
