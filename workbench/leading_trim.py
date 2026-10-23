"""Deterministic leading-cover removal for downloaded production videos.

The source file is never changed.  A task-scoped, verified derivative is
created after subtitle processing and becomes the only Step 1/2 handoff.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from common_io import atomic_write_json as _atomic_json


LEADING_TRIM_SECONDS = 2.0
MIN_REMAINING_SECONDS = 1.0


class LeadingTrimError(RuntimeError):
    pass


def _probe(ffprobe: Path, media: Path) -> dict:
    completed = subprocess.run(
        [
            str(ffprobe), "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate",
            "-of", "json", str(media),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode:
        raise LeadingTrimError(f"ffprobe failed: {completed.stderr.strip()[:300]}")
    try:
        payload = json.loads(completed.stdout)
        duration = float((payload.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LeadingTrimError("cannot read media duration") from exc
    streams = list(payload.get("streams") or [])
    if duration <= 0 or not any(item.get("codec_type") == "video" for item in streams):
        raise LeadingTrimError("media has no valid video stream")
    return {"duration": duration, "streams": streams}


def _sidecar_path(output: Path) -> Path:
    return output.with_suffix(output.suffix + ".leading-trim.json")


def _source_identity(source: Path) -> dict:
    stat = source.stat()
    return {"path": str(source.resolve()), "size": stat.st_size, "mtimeNs": stat.st_mtime_ns}


def _expected_output(source: Path, seconds: float) -> Path:
    label = f"{seconds:g}".replace(".", "_")
    return source.with_name(f"{source.stem}_leadtrim{label}s{source.suffix or '.mp4'}")


def _reusable(output: Path, source: Path, seconds: float, ffprobe: Path) -> dict | None:
    sidecar = _sidecar_path(output)
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        if payload.get("source") != _source_identity(source):
            return None
        if float(payload.get("leadingTrimSeconds") or 0) != float(seconds):
            return None
        metadata = _probe(ffprobe, output)
        if metadata["duration"] < MIN_REMAINING_SECONDS:
            return None
        return {**payload, "reused": True, "outputPath": str(output.resolve())}
    except (OSError, ValueError, json.JSONDecodeError, LeadingTrimError):
        return None


def trim_leading_cover(source_path: str | Path, ffmpeg_path: str | Path, ffprobe_path: str | Path,
                       seconds: float = LEADING_TRIM_SECONDS) -> dict:
    """Remove an exact leading duration without overwriting the source media."""
    source = Path(source_path).resolve()
    ffmpeg = Path(ffmpeg_path)
    ffprobe = Path(ffprobe_path)
    if not source.is_file():
        raise LeadingTrimError("subtitle output does not exist")
    if not ffmpeg.is_file() or not ffprobe.is_file():
        raise LeadingTrimError("ffmpeg or ffprobe is unavailable for leading-cover trim")
    if seconds <= 0:
        raise LeadingTrimError("leading trim must be positive")

    source_metadata = _probe(ffprobe, source)
    source_duration = source_metadata["duration"]
    if source_duration <= seconds + MIN_REMAINING_SECONDS:
        raise LeadingTrimError(
            f"source is too short for a {seconds:g}s leading trim ({source_duration:.3f}s)"
        )

    output = _expected_output(source, seconds)
    existing = _reusable(output, source, seconds, ffprobe)
    if existing:
        return existing

    temporary = output.with_name(f"{output.stem}.partial{output.suffix}")
    temporary.unlink(missing_ok=True)
    command = [
        str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-ss", f"{seconds:.3f}", "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "18", "-c:a", "aac",
        "-movflags", "+faststart", "-avoid_negative_ts", "make_zero", str(temporary),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if completed.returncode or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        raise LeadingTrimError(f"ffmpeg leading trim failed: {completed.stderr.strip()[:500]}")

    output_metadata = _probe(ffprobe, temporary)
    expected_duration = source_duration - seconds
    if output_metadata["duration"] < MIN_REMAINING_SECONDS or abs(output_metadata["duration"] - expected_duration) > 0.40:
        temporary.unlink(missing_ok=True)
        raise LeadingTrimError(
            f"leading trim duration verification failed: expected {expected_duration:.3f}s, "
            f"got {output_metadata['duration']:.3f}s"
        )
    os.replace(temporary, output)
    payload = {
        "source": _source_identity(source),
        "outputPath": str(output.resolve()),
        "leadingTrimSeconds": seconds,
        "sourceDuration": round(source_duration, 3),
        "outputDuration": round(output_metadata["duration"], 3),
        "verified": True,
    }
    _atomic_json(_sidecar_path(output), payload)
    return payload
