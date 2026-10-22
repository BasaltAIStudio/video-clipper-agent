#!/usr/bin/env python3
"""Test imports."""
import os
os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'
import sys
sys.path.insert(0, '.')

print("Testing imports...")

from agent.transcriber import (transcribe, transcribe_chunked, get_model,
                                _load_cached_transcript, _save_transcript_cache,
                                _transcript_cache_path, _get_video_duration)
print('transcriber module: OK')

from agent.pipeline import _transcribe, transcribe_parallel
print('pipeline module: OK')

from agent.learning_loop import LearningLoop, get_learning_stats
print('learning_loop module: OK')

from agent.highlight_selection import UnifiedHighlightSelector, get_selector, select_highlights
print('highlight_selection module: OK')

from agent.llm_retry import create_fallback_chain, LLMClient, RetryConfig
print('llm_retry module: OK')

from agent.profiler import PipelineProfiler
print('profiler module: OK')

print()
print('All modules import successfully!')
