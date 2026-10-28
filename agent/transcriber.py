"""agent: transcription via faster-whisper (local, offline).

Borrowed the local-mode pattern from Anil-matcha/AI-Youtube-Shorts-Generator
(pipeline.py _run_local): yt-dlp -> faster-whisper -> LLM -> ffmpeg. This module
is the whisper step; the pipeline orchestrates the rest.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import shutil  # Added for parallel transcribe
import wave  # For reading WAV files extracted by ffmpeg
import numpy as np  # For audio array processing

import yt_dlp

from .clipper import ClipError
from .config import (
    LOCAL_WHISPER_COMPUTE_TYPE,
    LOCAL_WHISPER_DEVICE,
    LOCAL_WHISPER_MODEL,
    LOCAL_WHISPER_WORD_TIMESTAMPS,
    MAX_VIDEO_SECONDS,
    logger_config,
)

def _download_only(url: str, tmpdir: Path) -> Path:
    """Download video to tmpdir (for parallel execution)."""
    tmpdir = Path(tmpdir)
    ydl_opts = {
        'outtmpl': str(tmpdir / 'source_video.%(ext)s'),
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'merge_output_format': 'mp4',
        'quiet': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    video_files = list(tmpdir.glob('source_video.*'))
    if not video_files:
        raise ClipError('yt-dlp produced no video file')
    return video_files[0]



log = logger_config("agent.transcriber")

__all__ = ["transcribe", "transcribe_chunked", "transcribe_parallel", "get_model"]

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

TRANSCRIPT_CACHE_DIR = Path(os.getenv("AGENT_TRANSCRIPT_CACHE", "/app/cache/transcripts"))
try:
    TRANSCRIPT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    TRANSCRIPT_CACHE_DIR = Path(tempfile.gettempdir()) / "transcripts"
    TRANSCRIPT_CACHE_DIR.mkdir(parents=True, exist_ok=True)

CACHE_SUFFIX = ".transcript.json"


def _transcript_cache_path(video_path: Path) -> Path:
    """Generate a cache file path for a video."""
    video_path = Path(video_path)
    video_hash = hashlib.md5(str(video_path).encode()).hexdigest()[:8]
    return TRANSCRIPT_CACHE_DIR / f"{video_hash}{CACHE_SUFFIX}"


def _load_cached_transcript(video_path: Path) -> Optional[dict]:
    """Load a cached transcription if it exists."""
    cache_path = _transcript_cache_path(video_path)
    if cache_path.exists():
        try:
            with open(cache_path, "r") as f:
                data = json.load(f)
            log.info(f"Loaded cached transcription from {cache_path}")
            return data
        except Exception as e:
            log.warning(f"Failed to load cache {cache_path}: {e}")
            return None
    return None


def _save_transcript_cache(video_path: Path, result: dict):
    """Save a transcription to disk cache."""
    cache_path = _transcript_cache_path(video_path)
    try:
        with open(cache_path, "w") as f:
            json.dump(result, f, indent=2)
        log.info(f"Cached transcription to {cache_path}")
    except Exception as e:
        log.warning(f"Failed to cache transcription: {e}")


def get_model(model_size: str = None):
    """Lazily load faster-whisper with the specified model size ('tiny', 'base', 'small')."""
    try:
        from faster_whisper import WhisperModel

        if model_size is None:
            model_size = LOCAL_WHISPER_MODEL

        log.info(f"Loading whisper model: {model_size} "
                 f"(device={LOCAL_WHISPER_DEVICE}, compute={LOCAL_WHISPER_COMPUTE_TYPE})")
        return WhisperModel(model_size, device=LOCAL_WHISPER_DEVICE, compute_type=LOCAL_WHISPER_COMPUTE_TYPE)
    except Exception as e:
        raise RuntimeError(f"faster-whisper not available or failed to load '{model_size}': {e}")


# ---------------------------------------------------------------------------
# Duration helper
# ---------------------------------------------------------------------------

def _get_video_duration(video_path: Path) -> float:
    """Get video duration using ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1", str(video_path)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except Exception as e:
        log.warning(f"Failed to get video duration: {e}")
    return 0.0


# ---------------------------------------------------------------------------
# Main transcription entry point
# ---------------------------------------------------------------------------

def transcribe(
    video_path: Path,
    language: Optional[str] = None,
    word_timestamps: bool = False,
    model_size: str = None,
    use_cache: bool = True,
    chunked: bool = False,
    chunk_duration: float = 900.0,
) -> dict:
    """
    Transcribe a video file and return the segment structure.

    Optimizations:
      1. Cache: loads from disk if available
      2. Model: select 'tiny' (fastest), 'base', 'small' (best quality)
      3. Chunked: splits long videos into segments > chunk_duration

    Returns:
        {"duration": float, "segments": [{"start","end","text","words"}, ...]}
    """
    video_path = Path(video_path)

    # Step 0: Check cache (only for non-chunked)
    if use_cache and not chunked:
        cached = _load_cached_transcript(video_path)
        if cached is not None:
            if cached.get("chunked", False):
                cached["_chunked"] = True
            return cached

    # Step 1: Get video duration
    duration = _get_video_duration(video_path)
    if duration == 0:
        log.warning("Could not determine video duration, transcribing anyway")
        return _transcribe_full(video_path, language, word_timestamps, model_size, use_cache)

    # Step 2: Handle chunking for long videos
    if chunked and duration > chunk_duration:
        log.info(f"Video is {duration:.0f}s — chunking into {chunk_duration:.0f}s segments")
        return transcribe_chunked(
            video_path, language, word_timestamps, model_size, chunk_duration, use_cache
        )

    # Step 3: Standard transcription
    return _transcribe_full(video_path, language, word_timestamps, model_size, use_cache)


def _extract_audio_ffmpeg(video_path: Path) -> np.ndarray:
    """Extract audio from video using ffmpeg command-line, return as numpy float32 array.

    Bypasses PyAV entirely, which can fail on minimal container images due to
    missing shared libraries or codec registration issues.
    """
    output_wav = str(video_path) + ".tmp.wav"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            output_wav, "-y",
        ],
        capture_output=True,
        timeout=120,
        check=True,
    )
    with wave.open(output_wav, "rb") as wf:
        n_frames = wf.getnframes()
        raw_data = wf.readframes(n_frames)
    audio = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32) / 32768.0
    Path(output_wav).unlink(missing_ok=True)
    return audio


def _transcribe_full(
    video_path: Path,
    language: Optional[str] = None,
    word_timestamps: bool = False,
    model_size: str = None,
    use_cache: bool = True,
) -> dict:
    """Transcribe the full video (cache, model selection)."""
    model = get_model(model_size)

    log.info(f"transcribing {video_path} (model={model_size}, device={LOCAL_WHISPER_DEVICE}, compute={LOCAL_WHISPER_COMPUTE_TYPE})")

    segments: List[dict] = []

    # Extract audio via ffmpeg (avoids PyAV which can fail on minimal containers)
    audio = _extract_audio_ffmpeg(video_path)

    generator, _info = model.transcribe(
        audio,
        language=language,
        word_timestamps=word_timestamps,
        vad_filter=True,
        beam_size=1,  # greedy decoding for speed (production default)
    )

    for i, segment in enumerate(generator):
        segments.append({
            "start": float(segment.start),
            "end": float(segment.end),
            "text": segment.text.strip(),
            "words": [{"start": float(w.start), "end": float(w.end), "word": w.word} for w in (segment.words or [])],
        })

    duration = float(segments[-1]["end"]) if segments else 0.0
    log.info(f"transcribed {len(segments)} segments, {duration:.0f}s of audio")

    result = {"duration": duration, "segments": segments}

    if use_cache:
        _save_transcript_cache(video_path, result)

    return result


def _split_video_into_chunks(video_path: Path, chunk_duration: float) -> List[dict]:
    """Split a video into chunks using ffmpeg subclip. Returns a list of chunk specs."""
    chunks = []
    chunk_start = 0.0
    duration = _get_video_duration(video_path)

    while chunk_start < duration:
        chunk_end = min(chunk_start + chunk_duration, duration)
        temp_file = video_path.parent / f"chunk_{chunk_start:.0f}_{chunk_end:.0f}.mp4"

        if not temp_file.exists():
            cmd = [
                "ffmpeg", "-y", "-ss", str(chunk_start), "-t", str(chunk_end - chunk_start),
                "-i", str(video_path), "-c", "copy", str(temp_file)
            ]
            try:
                subprocess.run(cmd, capture_output=True, timeout=60)
            except Exception as e:
                log.warning(f"Failed to create chunk {temp_file}: {e}")
                temp_file = None

        if temp_file and temp_file.exists():
            chunks.append({
                "start": chunk_start,
                "end": chunk_end,
                "temp_file": str(temp_file),
                "original": False,
            })
        else:
            chunks.append({
                "start": chunk_start,
                "end": chunk_end,
                "temp_file": str(video_path),
                "original": True,
            })

        chunk_start = chunk_end

    return chunks


def transcribe_chunked(
    video_path: Path,
    language: Optional[str] = None,
    word_timestamps: bool = False,
    model_size: str = None,
    chunk_duration: float = 900.0,
    use_cache: bool = True,
) -> dict:
    """
    Split a long video into chunks and transcribe each chunk separately.

    This avoids the case where a 2-hour video causes Whisper to hang or
    produce very long processing times. Each chunk is processed independently
    and results are merged into a single segment list.

    Args:
        video_path: Path to the video file
        language: Language code
        word_timestamps: Return per-word timing
        model_size: Whisper model size ('tiny', 'base', 'small')
        chunk_duration: Duration of each chunk in seconds (default 15 min)
        use_cache: Load from cache if available

    Returns:
        {"duration": float, "segments": [...]}
    """
    video_path = Path(video_path)
    duration = _get_video_duration(video_path)
    if duration == 0:
        log.warning("Could not determine video duration, transcribing as single clip")
        return _transcribe_full(video_path, language, word_timestamps, model_size, use_cache)

    if duration <= chunk_duration:
        log.info(f"Video is {duration:.0f}s — standard transcription (under {chunk_duration:.0f}s chunk limit)")
        return _transcribe_full(video_path, language, word_timestamps, model_size, use_cache)

    log.info(f"Video is {duration:.0f}s — chunking into {chunk_duration:.0f}s segments")

    chunks = []
    t = 0.0
    while t < duration:
        chunks.append({"index": len(chunks), "start": t, "end": min(t + chunk_duration, duration)})
        t += chunk_duration

    # Phase 1: Download audio splits for each chunk in parallel (FIX #3)
    # Each chunk is an audio segment extracted from the original video.
    # Downloading all splits concurrently overlaps I/O with processing.
    with ThreadPoolExecutor(max_workers=min(len(chunks), os.cpu_count() or 4)) as executor:
        chunk_futures = {
            i: executor.submit(_extract_chunk_audio, chunk, video_path)  # noqa
            for i, chunk in enumerate(chunks)
        }

        chunk_results = []
        for i, chunk in enumerate(chunks):
            chunk_data = chunk_futures[i].result()
            chunk_results.append(chunk_data)

    # Phase 2: Transcribe each chunk (model stays loaded in the main thread)
    # The Whisper model is already initialized; each chunk is a small audio file
    # so transcription is fast. Sequential is fine since the model isn't
    # re-initialized per chunk.
    all_segments = []
    for i, chunk_data in enumerate(chunk_results):
        if not chunk_data.get("temp_file"):
            continue
        log.info(f"Chunk {i}: transcribing {chunk_data['duration']:.0f}s audio (temp file: {chunk_data['temp_file']})")
        chunk_result = _transcribe_full(
            Path(chunk_data["temp_file"]), language, word_timestamps, model_size, use_cache
        )

        # Adjust segment times to be relative to the original video
        offset = chunk_data["start"]
        for seg in chunk_result["segments"]:
            seg["start"] += offset
            seg["end"] += offset

        all_segments.extend(chunk_result["segments"])

        # Clean up temp file
        if Path(chunk_data["temp_file"]).exists():
            try:
                os.remove(chunk_data["temp_file"])
                log.info(f"Cleaned up temp chunk file: {chunk_data['temp_file']}")
            except Exception as e:
                log.warning(f"Failed to delete temp chunk {chunk_data['temp_file']}: {e}")

    # Sort segments by start time
    all_segments.sort(key=lambda s: s["start"])

    # Merge overlapping segments
    merged = []
    for seg in all_segments:
        if merged and seg["start"] <= merged[-1]["end"]:
            # Overlap found, extend the previous segment
            merged[-1]["end"] = max(merged[-1]["end"], seg["end"])
            if seg["text"]:
                merged[-1]["text"] += " " + seg["text"]
        else:
            merged.append(seg)

    duration_out = max((s["end"] for s in merged), default=0.0)

    log.info(f"Chunked transcription complete: {len(merged)} segments, {duration_out:.0f}s")

    result = {"duration": duration_out, "segments": merged}

    if use_cache:
        _save_transcript_cache(video_path, result)

    return result


def _extract_chunk_audio(chunk: Dict[str, Any], video_path: Path) -> Dict[str, Any]:
    """
    Extract a single chunk's audio from the video file.

    This is a standalone function that can run in a thread.
    Uses ffmpeg to extract audio for each chunk independently.
    """
    import subprocess
    import os

    temp_dir = tempfile.mkdtemp()
    temp_file = os.path.join(temp_dir, f"chunk_{chunk['index']}.mp3")

    # Build ffmpeg command to extract audio segment
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-ss", str(chunk["start"]),
        "-to", str(chunk["end"]),
        "-ac", "1",  # Mono
        "-ar", "16000",  # 16kHz for Whisper
        "-f", "mp3",
        temp_file,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            log.warning(f"ffmpeg failed for chunk {chunk['index']}: {result.stderr[:200]}")
            return {
                "temp_file": None,
                "duration": chunk["end"] - chunk["start"],
                "start": chunk["start"],
            }
    except Exception as e:
        log.warning(f"ffmpeg exception for chunk {chunk['index']}: {e}")
        return {
            "temp_file": None,
            "duration": chunk["end"] - chunk["start"],
            "start": chunk["start"],
        }

    return {
        "temp_file": temp_file,
        "duration": chunk["end"] - chunk["start"],
        "start": chunk["start"],
    }


def transcribe_parallel(
    video_url: str,
    work_dir: Path,
    model_size: str = "tiny",
    use_cache: bool = True,
    chunked: bool = False,
) -> tuple:
    """
    Download a video and transcribe it concurrently.

    The download runs in a background thread while the Whisper model
    loads, so model loading overlaps with I/O. The model is pre-loaded
    in a separate thread before transcription starts, so both the
    download and model loading happen in parallel, and transcription
    starts as soon as both complete.

    Args:
        video_url: YouTube URL to download
        work_dir: Working directory for the download
        model_size: Whisper model size ('tiny', 'base', 'small')
        use_cache: Load from cache if available
        chunked: Handle long videos by chunking

    Returns:
        (segments, info) tuple
    """
    from .transcriber import get_model, _get_video_duration

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    tmpdir = tempfile.mkdtemp(dir=str(work_dir))

    try:
        # Phase 1: Download video AND pre-load model in parallel (FIX #3)
        with ThreadPoolExecutor(max_workers=2) as executor:
            download_future = executor.submit(_download_only, video_url, tmpdir)
            model_future = executor.submit(get_model, model_size)

            video_path = download_future.result()
            model = model_future.result()  # may already be cached

        log.info(f"Downloaded video: {video_path}")

        # Phase 2: Transcribe (model already loaded, starts immediately)
        duration = _get_video_duration(video_path)
        log.info(f"Video duration: {duration:.0f}s")

        result = transcribe(
            video_path,
            language="en",
            word_timestamps=True,
            model_size=model_size,
            use_cache=use_cache,
            chunked=chunked,
        )

        log.info(f"Transcription complete: {result.get('duration', 0):.1f}s, "
                 f"{len(result.get('segments', []))} segments")
        return result["segments"], result

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
