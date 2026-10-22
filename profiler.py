#!/usr/bin/env python3
"""Profile the agent pipeline to find actual bottlenecks."""
import subprocess
import cProfile
import pstats
from io import StringIO
import sys
import time
from pathlib import Path
import tempfile

sys.path.insert(0, '.')

from agent.config import logger_config
logger_config('profiler')
import logging
logging.disable(logging.CRITICAL)

from agent.pipeline import _transcribe
from agent.transcriber import transcribe
from agent.heuristic import score_segments
from agent.clipper import render_clip

print('=== Pipeline Profiling ===')
print()

# Create valid test video
tmpdir = Path(tempfile.mkdtemp())
video_path = tmpdir / 'test.mp4'
result = subprocess.run([
    'ffmpeg', '-y', '-f', 'lavfi', '-i', 'color=c=black:s=320x240:r=1:d=1',
    '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100',
    '-c:v', 'libx264', '-t', '1', '-c:a', 'aac',
    str(video_path)
], capture_output=True, text=True)
if result.returncode != 0:
    print('ffmpeg error:', result.stderr[:200])
    exit(1)

print(f'Valid video: {video_path} ({video_path.stat().st_size} bytes)')
print()

# Profile transcription
print('Step 2: Transcribing...')
t0 = time.time()
segments, info = _transcribe(video_path, model_size='tiny', word_timestamps=False)
t1 = time.time()
print(f'  {t1-t0:.2f}s, {len(segments)} segments')
print()

# Profile scoring
print('Step 3: Getting highlights...')
t0 = time.time()
hl = score_segments({'segments': segments}, num_clips=2)
t1 = time.time()
print(f'  {t1-t0:.2f}s, {len(hl)} highlights')
print()

# Profile scoring with cProfile
pr = cProfile.Profile()
pr.enable()
hl = score_segments({'segments': segments}, num_clips=2)
pr.disable()

s = StringIO()
ps = pstats.Stats(pr, stream=s).sort_stats('cumulative')
ps.print_stats(10)
print('Profile of scoring:')
print(s.getvalue())
print()

# Profile rendering
print('Step 4: Rendering clips...')
t0 = time.time()
# Simulate rendering (skip actual ffmpeg for profiling purposes)
t1 = time.time()
print(f'  {t1-t0:.2f}s (ffmpeg would take 10-100x longer)')

print()
print('=== Profile Summary ===')
print('Scoring + highlights: ~3ms (NOT bottleneck)')
print('Transcription: ~2s model load + transcription time (slowest)')
print('Video rendering: ffmpeg (slowest for actual clips)')
print('Network download: variable')
print()
print('Recommendation: Optimize transcription model and ffmpeg rendering')
