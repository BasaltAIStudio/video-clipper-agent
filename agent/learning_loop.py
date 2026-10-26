#!/usr/bin/env python3
"""
Learning Loop — tracks clip performance and uses it to improve future video selection.

The core feedback loop the agent was built for:
  1. Submit clips to Content Rewards → get views/earnings/engagement data
  2. Log every clip: features (text, duration, position, engagement rate, etc.)
  3. Build a model that maps video features → predicted performance
  4. Use the model to select highlights for the next batch of videos
  5. Retrain and iterate

This module handles:
  - Logging clip submissions with performance data
  - Computing engagement metrics from raw performance
  - Training a performance predictor (neural network)
  - Selecting highlights using the predicted performance
"""
import json
import logging
import sqlite3
import contextlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# torch is heavy and only needed once 50+ clips have performance data, so it is
# imported lazily. The service must start and clip without it installed.
try:  # pragma: no cover
    import torch
    import torch.nn as nn
except ImportError:  # pragma: no cover
    torch = None
    nn = None

# Neural scorer was deleted - use highlight_selection's heuristic scoring instead
_SCORER_AVAILABLE = False

from .config import DATA_DIR

log = logging.getLogger("agent.learning_loop")


def datetime_now() -> str:
    """Return current UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()

# ---------------------------------------------------------------------------
# Database schema for learning loop
# ---------------------------------------------------------------------------

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id TEXT UNIQUE,
    campaign_id TEXT,
    campaign_name TEXT,
    video_url TEXT,
    transcript_text TEXT,
    clip_start REAL,
    clip_end REAL,
    clip_duration REAL,
    clip_features_json TEXT,  -- serialized feature vector for ML
    cpm REAL,                -- CPM at submission time
    views INTEGER,           -- post-submission views (filled by analytics)
    watch_time_seconds REAL, -- total watch time (filled by analytics)
    earnings REAL,           -- actual earnings (filled by analytics)
    engagement_rate REAL,    -- completed views / total views (computed)
    performance_score REAL,  -- combined score (views * CPM / 1000) (computed)
    feature_vector TEXT,     -- JSON array of feature values
    target_label REAL,       -- what we want to predict (engagement_rate)
    submitted_at TEXT,
    completed_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS model_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT UNIQUE,
    trained_at TEXT,
    train_samples INTEGER,
    train_loss REAL,
    model_path TEXT
);

CREATE TABLE IF NOT EXISTS highlight_selections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT,
    campaign_name TEXT,
    selection_method TEXT,  -- 'llm', 'heuristic', 'jev', 'neural', 'learned'
    segment_start REAL,
    segment_end REAL,
    segment_text TEXT,
    score REAL,
    selected_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


class LearningLoop:
    """Track clip performance and use it to improve future highlight selection."""

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or DATA_DIR / "learning.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(DB_SCHEMA)
            conn.commit()

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def log_clip(self, clip: Dict[str, Any], cpm: float = None,
                  features: List[float] = None, selected_method: str = None):
        """Log a clip submission with its features and performance metrics."""
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """INSERT INTO clip_log
                   (clip_id, campaign_id, campaign_name, video_url,
                    transcript_text, clip_start, clip_end, clip_duration,
                    clip_features_json, cpm, performance_score,
                    feature_vector, target_label, submitted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    clip.get("clip_id"),
                    clip.get("campaign_id"),
                    clip.get("campaign_name"),
                    clip.get("video_url"),
                    clip.get("text", ""),
                    clip.get("start", 0),
                    clip.get("end", 0),
                    clip.get("duration", 0),
                    json.dumps(features) if features else None,
                    cpm or 0.0,
                    None,  # performance_score filled later
                    json.dumps(features) if features else None,
                    None,  # target_label filled later
                    datetime_now(),
                ),
            )
            conn.commit()
            clip_id = conn.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]
        log.info(f"Logged clip {clip_id}: {clip.get('text', '')[:40]}...")

    def record_performance(self, clip_id: str, views: int, watch_time: float,
                           earnings: float, cpm: float = None):
        """Record actual performance data from Content Rewards."""
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            # Compute engagement rate
            # engagement_rate = completed_views / total_views (proxy)
            # We derive it from watch time vs views as a proxy
            eng_rate = self._compute_engagement(watch_time, views) if views > 0 else 0.0

            # Combined performance score: views * CPM / 1000 (proxy earnings)
            perf_score = (views * (cpm or 0.0)) / 1000.0 if views > 0 and cpm else 0.0

            conn.execute(
                """UPDATE clip_log
                   SET views = ?, watch_time_seconds = ?, earnings = ?, engagement_rate = ?,
                       cpm = ?, performance_score = ?, completed_at = ?, target_label = ?
                   WHERE clip_id = ?""",
                (views, watch_time, earnings, eng_rate, cpm or 0.0, perf_score,
                 datetime_now(), eng_rate, clip_id),
            )
            conn.commit()
            log.info(f"Recorded performance for {clip_id}: views={views}, "
                     f"earnings={earnings:.4f}, eng_rate={eng_rate:.3f}")

    def _compute_engagement(self, watch_time: float, views: int) -> float:
        """Compute engagement rate proxy from watch time and views."""
        if views <= 0:
            return 0.0
        avg_watch = watch_time / views
        # Cap at 300 seconds (5 min); anything above is a long watch
        avg_watch = min(avg_watch, 300.0)
        # Normalize to 0-1 scale: 0s = 0, 300s = 1
        return avg_watch / 300.0

    # ------------------------------------------------------------------
    # Model Training
    # ------------------------------------------------------------------

    def get_training_data(self, min_samples: int = 50, min_views: int = 10) -> Tuple[np.ndarray, np.ndarray]:
        """Fetch training data from the clip log."""
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                """SELECT feature_vector, target_label
                   FROM clip_log
                   WHERE views >= ? AND performance_score > 0 AND target_label IS NOT NULL
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (min_views, min_samples),
            ).fetchall()

        if not rows:
            log.warning(f"Insufficient training data: {len(rows)} samples")
            return None, None

        features = []
        targets = []
        for feat_json, target in rows:
            try:
                vec = json.loads(feat_json)
                if isinstance(vec, list) and len(vec) > 0:
                    features.append(vec)
                    targets.append(target)
            except json.JSONDecodeError:
                continue

        if len(features) < min_samples:
            log.warning(f"Only {len(features)} valid training samples")
            return None, None

        X = np.array(features, dtype=np.float32)
        y = np.array(targets, dtype=np.float32)

        # Normalize features
        mean = X.mean(axis=0)
        std = X.std(axis=0) + 1e-8
        X = (X - mean) / std

        return X, y

    def train_performance_model(self, epochs: int = 100, lr: float = 5e-4, dropout: float = 0.3) -> Optional[str]:
        """Train a neural network to predict clip engagement.

        Returns the path to the saved model. Uses actual performance data
        as labels - never synthetic data.

        Parameters:
            epochs: Number of training epochs (higher for small datasets)
            lr: Learning rate (lower avoids overshooting on small data)
            dropout: Dropout rate for regularization (reduces overfitting)
        """
        if torch is None:
            log.warning("torch not installed - skipping model training")
            return None
        X, y = self.get_training_data(min_samples=50)
        if X is None or len(X) < 50:
            log.warning("Not enough training data - skipping model training")
            return None

        log.info(f"Training performance model on {len(X)} samples...")

        # Build improved model with better regularization
        input_dim = X.shape[1]
        model = nn.Sequential(
            nn.Linear(input_dim, 128),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(64, 32),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(32, 1),
        )

        # Use Adam with weight decay for L2 regularization
        optimizer = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-4)
        criterion = nn.MSELoss()

        # Learning rate scheduler: reduce on plateau
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            optimizer, mode='min', factor=0.5, patience=10
        )

        dataset = torch.utils.data.TensorDataset(
            torch.tensor(X), torch.tensor(y)
        )
        # Use smaller batch size for better gradient estimates on small data
        dataloader = torch.utils.data.DataLoader(
            dataset, batch_size=min(8, len(dataset)), shuffle=True, drop_last=False
        )

        model.train()
        best_loss = float('inf')
        patience_counter = 0
        patience = 30  # Early stopping patience

        for epoch in range(epochs):
            total_loss = 0.0
            num_batches = 0
            for xb, yb in dataloader:
                optimizer.zero_grad()
                pred = model(xb).squeeze(-1)
                loss = criterion(pred, yb)
                loss.backward()
                # Gradient clipping for stability
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
                optimizer.step()
                total_loss += loss.item()
                num_batches += 1

            avg_loss = total_loss / max(num_batches, 1)
            scheduler.step(avg_loss)

            if (epoch + 1) % 10 == 0:
                log.info(f"  Epoch {epoch+1}/{epochs}, Loss: {avg_loss:.4f}")

            # Early stopping
            if avg_loss < best_loss:
                best_loss = avg_loss
                patience_counter = 0
            else:
                patience_counter += 1
                if patience_counter >= patience:
                    log.info(f"Early stopping at epoch {epoch+1} (no improvement for {patience} epochs)")
                    break

        # Save model with normalization stats
        model_path = Path(__file__).parent / "data" / "performance_predictor.pkl"
        model_path.parent.mkdir(parents=True, exist_ok=True)

        # Convert model state dict to plain tensors for saving
        state_dict = {}
        for k, v in model.state_dict().items():
            state_dict[k] = v.cpu().numpy().tolist()

        saved_state = {
            "state_dict": state_dict,
            "input_dim": input_dim,
            "mean": X.mean(axis=0).tolist(),
            "std": X.std(axis=0).tolist(),
            "trained_at": datetime_now(),
            "train_samples": len(X),
            "best_loss": best_loss,
            "epochs_trained": epoch + 1,
        }

        # Save as pickle for faster loading
        try:
            import pickle
            with open(model_path, "wb") as f:
                pickle.dump(saved_state, f)
        except Exception:
            # Fallback to JSON
            with open(model_path, "w") as f:
                json.dump(saved_state, f, indent=2)

        # Record model version
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """INSERT INTO model_versions
                   (version, trained_at, train_samples, model_path, train_loss)
                   VALUES (?, ?, ?, ?, ?)""",
                (f"v{len(self._get_model_versions()) + 1}",
                 saved_state["trained_at"], len(X), str(model_path),
                 best_loss),
            )
            conn.commit()

        log.info(f"Performance model saved to {model_path}")
        return str(model_path)

    def _get_model_versions(self) -> List[Dict]:
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute("SELECT * FROM model_versions ORDER BY trained_at DESC").fetchall()
        return rows

    # ------------------------------------------------------------------
    # Highlight Selection with Learned Model
    # ------------------------------------------------------------------

    def select_highlights_learned(self, segments: List[Dict],
                                   model_path: str = None,
                                   num_clips: int = 3) -> List[Dict]:
        """
        Select highlights using the learned performance model.

        The model predicts engagement/retention for each segment based on its features.
        We then pick the top-N non-overlapping segments with the highest predicted engagement.

        No neural scorer is used — this is purely features-based scoring from
        actual performance data.
        """
        # Load the performance model if available (needs torch + a trained model)
        model = None
        if torch is not None:
            try:
                model = self._load_performance_model(model_path)
            except Exception as e:  # noqa: BLE001
                log.warning("could not load performance model: %s", e)
        if model is None:
            return []

        # Score each segment using learned features or simple heuristic fallback
        scored_segments = []
        for seg in segments:
            features = self._extract_features(seg)
            if model is not None:
                try:
                    import torch
                    with torch.no_grad():
                        x = torch.tensor([features], dtype=torch.float32)
                        pred = model(x).item()
                except Exception:
                    pred = self._heuristic_score(seg)
            else:
                pred = self._heuristic_score(seg)
            seg["learned_score"] = float(pred)
            scored_segments.append(seg)

        log.info(f"Scored {len(scored_segments)} segments with learned model")
        return self._select_with_model(scored_segments, model, num_clips)

    def _extract_features(self, seg: Dict) -> List[float]:
        """Extract simple numeric features from a segment for model scoring."""
        text = seg.get("text", "")
        duration = seg.get("end", 0) - seg.get("start", 0)
        return [
            len(text),
            duration,
            text.count("!"),
            text.count("?"),
            sum(1 for w in ["but", "however", "actually", "the truth", "secret"] if w in text.lower()),
        ]

    def _heuristic_score(self, seg: Dict) -> float:
        """Simple heuristic score for segments when no model is available."""
        text = seg.get("text", "")
        score = 0.5
        if len(text) > 20:
            score += 0.1
        if any(w in text.lower() for w in ["but", "however", "actually"]):
            score += 0.05
        return min(1.0, score)

    def _load_performance_model(self, model_path: str = None):
        """Load a trained performance model."""
        if model_path is None:
            model_path = Path(__file__).parent / "data" / "performance_predictor.pkl"
        model_path = Path(model_path)
        if not model_path.exists():
            return None

        # Try pickle first, fallback to JSON
        try:
            import pickle
            with open(model_path, "rb") as f:
                saved = pickle.load(f)
        except Exception:
            with open(model_path, "r") as f:
                saved = json.load(f)

        input_dim = saved["input_dim"]
        model = nn.Sequential(
            nn.Linear(input_dim, 64),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(64, 32),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(32, 1),
        )
        # Load state dict
        state_dict = {}
        for k, v in saved["state_dict"].items():
            state_dict[k] = torch.tensor(v)
        model.load_state_dict(state_dict)
        model.eval()

        # Store normalization stats
        self.normalization = {
            "mean": np.array(saved["mean"]),
            "std": np.array(saved["std"]),
        }

        return model

    def _select_with_model(self, segments: List[Dict], model: nn.Module,
                           num_clips: int = 3) -> List[Dict]:
        """Select highlights using the learned model's predictions."""
        # Compute feature vectors for each segment
        features = []
        for seg in segments:
            # Build feature vector from segment data
            vec = [
                seg["neural_score"],           # 0-100 neural score
                seg["start"],                   # start position
                seg["end"] - seg["start"],      # duration
                len(seg["text"]),               # text length
                seg["text"].count("!"),         # exclamation count (emotional signal)
                seg["text"].count("?"),         # question mark count
            ]
            features.append(vec)

        X = np.array(features, dtype=np.float32)
        if hasattr(self, "normalization"):
            X = (X - self.normalization["mean"]) / self.normalization["std"]

        # Predict performance
        with torch.no_grad():
            pred = model(torch.tensor(X, dtype=torch.float32)).squeeze(-1).numpy()

        # Scale predictions to 0-100 range
        pred = np.clip(pred, 0, 1) * 100

        # Attach predicted scores to segments
        for i, seg in enumerate(segments):
            seg["learned_score"] = float(pred[i])

        # Sort by learned score
        sorted_segments = sorted(segments, key=lambda s: s.get("learned_score", s.get("neural_score", 0)), reverse=True)

        # Greedily pick non-overlapping highlights
        selected = []
        for seg in sorted_segments:
            if len(selected) >= num_clips:
                break
            h = {
                "start": seg["start"],
                "end": seg["end"],
                "text": seg["text"],
                "score": seg.get("learned_score", seg.get("neural_score", 0)),
                "learned_score": seg.get("learned_score", 0),
                "embedding": seg.get("embedding", []),
            }
            h_start, h_end = h["start"], h["end"]
            h_dur = h_end - h_start
            if h_dur <= 0:
                continue
            overlap = False
            for k in selected:
                k_start, k_end = k["start"], k["end"]
                ov = min(h_end, k_end) - max(h_start, k_start)
                if ov > min(h_dur, k_dur := (k_end - k_start)) * 0.5:
                    overlap = True
                    break
            if not overlap:
                selected.append(h)

        return selected

    def _select_neural(self, segments: List[Dict], num_clips: int) -> List[Dict]:
        """Removed — neural scorer deleted. Use _select_with_model instead."""
        return []


        # ---------------------------------------------------------------------------
        # Learning loop maintenance and monitoring
        # ---------------------------------------------------------------------------

def get_learning_stats(db_path: Path) -> Dict[str, Any]:
    """Get current learning loop statistics."""
    loop = LearningLoop(db_path)
    with sqlite3.connect(loop.db_path) as conn:
        stats = {
            "total_clips": conn.execute("SELECT COUNT(*) FROM clip_log").fetchone()[0],
            "total_viewable": conn.execute("SELECT COUNT(*) FROM clip_log WHERE views > 0").fetchone()[0],
            "total_earnings": conn.execute("SELECT COALESCE(SUM(earnings), 0) FROM clip_log").fetchone()[0],
            "avg_engagement": conn.execute(
                "SELECT AVG(engagement_rate) FROM clip_log WHERE views > 0"
            ).fetchone()[0] or 0.0,
            "model_versions": conn.execute(
                "SELECT COUNT(*) FROM model_versions"
            ).fetchone()[0],
            "recent_selections": conn.execute(
                "SELECT COUNT(*) FROM highlight_selections"
            ).fetchone()[0],
        }
    return stats
