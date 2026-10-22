#!/usr/bin/env python3
"""Fix circular import: move _download_only from pipeline.py to transcriber.py"""
import re

# 1. Read pipeline.py and extract _download_only function
with open('agent/pipeline.py', 'r') as f:
    p_content = f.read()

# Find and remove _download_only function
download_only_match = re.search(r'def _download_only\(url: str, tmpdir: Path\) -> Path:.*?\n    return video_files\[0\]\n\n', p_content, re.DOTALL)
if download_only_match:
    p_content = p_content[:download_only_match.start()] + p_content[download_only_match.end():]
    with open('agent/pipeline.py', 'w') as f:
        f.write(p_content)
    print('Removed _download_only from pipeline.py')

# 2. Read transcriber.py and add _download_only function
with open('agent/transcriber.py', 'r') as f:
    t_content = f.read()

# Add ClipError import if needed
if 'from .clipper import ClipError, render_clip' in t_content:
    pass  # already imported

# Add _download_only function after the imports
download_only_func = '''
def _download_only(url: str, tmpdir: Path) -> Path:
    """Download video to tmpdir (for parallel execution)."""
    ydl_opts = {
        'outtmpl': str(tmpdir / 'source_video.%(ext)s'),
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'merge_output_format': 'mp4',
        'quiet': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    video_files = list(tmpdir.glob("source_video.*"))
    if not video_files:
        raise ClipError("yt-dlp produced no video file")
    return video_files[0]


'''

# Insert after the clipper import
t_content = t_content.replace(
    'from .clipper import ClipError, render_clip',
    'from .clipper import ClipError, render_clip\n'
    + download_only_func.strip(),
    1
)

with open('agent/transcriber.py', 'w') as f:
    f.write(t_content)

print('Added _download_only to transcriber.py')
