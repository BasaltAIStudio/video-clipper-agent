---
name: ai-agent-content-clipping
description: |
  Decision engine for content clipping on Content Rewards — closed analytics feedback loop.
  Use when building or debugging an agent that submits clips, measures retention, trains a
  performance model, and improves highlight selection across submit → measure → train → improve → resend cycles.
metadata:
  category: agent-development
  permissions:
    - file:read
    - file:write
    - terminal:execute
---

# AI Agent Decision Engine — Content Clipping

## Context

This agent is a decision engine, **not** a linear pipeline, and **not** the "Scrap & Sabotage" Roblox game. Its entire purpose is content clipping for Content Rewards. The neural scorer is deleted; selection is single-purpose: predict retention (watch time, completion, earnings per view), not "virality."

## Core Workflow

```
Submit clip → n8n → Content Rewards scraper → measure views/completion/earnings
   → record in SQLite (clip_log) → train performance model on real data
   → predict retention curve for candidate highlights → A/B test selection methods
   → render clips → post to n8n → (loop back with next campaign)
```

Each cycle retires the model's `train_loss` (not a fixed epoch count) as the stopping criterion.

## Training Model

- **Labels:** actual views × CPM / 1000 (proxy earnings) as `performance_score`.
- **Retention score:** `views × cpm / 1000 × 0.7 + completed_views × 0.3`.
- Build a neural net: `Linear(128) → ReLU → Dropout(0.3) → Linear(64) → ReLU → Dropout(0.3) → Linear(32) → ReLU → Dropout(0.3) → Linear(1)`.
- Optimizer: Adam with `weight_decay=1e-4`.
- LR scheduler: `ReduceLROnPlateau(mode='min', factor=0.5, patience=10)`.
- Batch size: `min(8, len(dataset))`; `drop_last=False`.
- **Stopping criterion:** train loss (not epoch count) with early stopping patience of 30 epochs.
- Training must start with ≥50 samples (log the sample count so the loss curve is meaningful).

## Retention Curve Prediction

- Model the curve as exponential decay over 10 time points: `hook_quality × decay_rate^t`.
- `hook_quality = min(1.0, 0.3 + 0.7 × engagement)` where engagement is the 3rd feature value.
- Decay rate caps at `[0.7, 0.95]`; scales by duration and target retention area.

## DecisionLoop

- `DecisionLoop` takes `engine`, `campaign_queue`, `poll_interval`, `max_cycles`.
- Runs cycles until `max_cycles` or queue empty; each cycle pops a campaign and runs `engine.run()`.
- Verify two consecutive cycles complete without errors before declaring the loop healthy.

## Pitfalls

- The task description in the Slack post may lie. If the bot cannot predict what viewers will engage with, something is wrong — and it's probably the model training, not the loop.
- SQLite file locks: `with sqlite3.connect() as conn:` commits but does not close. Use `contextlib.closing(sqlite3.connect())` everywhere.
- `ReduceLROnPlateau` dropped `verbose=True` in newer PyTorch — check the error on import and fix.
- `clip_log` vs `clip_performance` schema mismatch causes `sqlite3.OperationalError: 6 values for 5 columns`. Keep the UPDATE statement's column count matching on both sides.

## Procedure

1. Write the real training code in `train_performance_model()` (neural net, Adam, ReduceLROnPlateau, early stop).
2. Add a real retention curve model (exponential decay, not flat).
3. Verify `transcribe_parallel` uses `ThreadPoolExecutor` so download + model load overlap.
4. Wire `llm_retry` into the LLM call chain (`LLMClient` + `RetryConfig`, 3 retries, fallback on rate limit/timeout/connection error).
5. Use real feature vectors (not hardcoded 4-feature debug vectors) in `log_clip`.
6. Move model data off the agent root dir — use a subdir like `agent/data`.
7. Exercise `DecisionLoop` with a real queue (mocked URLs are fine for the loop test; the engine itself can skip downloads when URLs are fake).
8. Run an end-to-end test with mocked n8n and Content Rewards; load `N8N_WEBHOOK_URL` from env or use the default mock.

## Reference Files

- `references/training-code.md` — the real training code pattern
- `references/retention-curve.md` — retention curve model details
- `references/decision-loop.md` — DecisionLoop walkthrough
- `references/e2e-test.md` — end-to-end test pattern
