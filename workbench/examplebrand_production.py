from __future__ import annotations

import hashlib
import csv
import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from common_io import atomic_write_json as _atomic_json


PROFILE_VERSION = "examplebrand-production-v1"
SELECTION_VERSION = "examplebrand-window-v1"
MAX_SOURCE_SECONDS = 120.0
AI_SECONDS = 4.0
ENDING_SECONDS = 3.0
MIN_ORDINARY_SECONDS = 2.0
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".mts", ".ts"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
PRODUCT_AI_DIRS = {
    "清淤泵": r"G:\潜污泵AI素材",
    "潜污泵": r"G:\潜污泵AI素材",
    "离心泵": r"G:\离心泵AI素材",
    "轴流泵": r"G:\轴流泵AI素材",
    "粉碎格栅": r"G:\粉碎型格栅AI素材",
    "一体化预制泵站": r"G:\一体化泵站AI素材",
    "大型泵站": r"G:\一体化泵站AI素材",
    "MaterialPump": r"G:\一体化泵车AI素材(1)",
}
COPY_LIBRARY_VERSION = "ExampleBrand_Pump_Reels_Copy_FINAL_100_EN_ES_v1"
DEFAULT_ENDING = r"C:\Users\ExampleUser\Desktop\潜污泵 口播 2026.8.10 魏文旭 (14).MOV"


def _video_files(folder: Path) -> list[Path]:
    return sorted(
        (item for item in folder.rglob("*") if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS),
        key=lambda item: str(item).casefold(),
    ) if folder.is_dir() else []


class ExamplebrandProductionError(RuntimeError):
    def __init__(self, code: str, message: str, **diagnostic):
        super().__init__(message)
        self.error_code = code
        self.diagnostic = diagnostic


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_video: bool = False
    has_audio: bool = False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fraction(value: str) -> float:
    try:
        numerator, denominator = str(value or "0/1").split("/", 1)
        return float(numerator) / float(denominator or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


class ExamplebrandMediaTools:
    def __init__(self, ffmpeg: str | None = None, ffprobe: str | None = None):
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg") or "ffmpeg"
        self.ffprobe = ffprobe or shutil.which("ffprobe") or "ffprobe"

    def run(self, args: list[str], *, capture=False) -> subprocess.CompletedProcess:
        startup = None
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        result = subprocess.run(
            args, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE, startupinfo=startup, check=False,
        )
        if result.returncode:
            tail = result.stderr.decode("utf-8", errors="replace").splitlines()[-12:]
            raise ExamplebrandProductionError("ExampleBrand_FFMPEG_FAILED", "\n".join(tail) or "FFmpeg执行失败")
        return result

    def probe(self, path: Path) -> MediaInfo:
        if not path.is_file() or path.stat().st_size < 1024:
            raise ExamplebrandProductionError("ExampleBrand_SOURCE_MEDIA_INVALID", f"媒体不存在或为空：{path}")
        result = self.run([
            self.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
        ], capture=True)
        try:
            payload = json.loads(result.stdout.decode("utf-8", errors="replace"))
            streams = payload.get("streams") or []
            video = next((item for item in streams if item.get("codec_type") == "video"), None)
            audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
            duration = float((payload.get("format") or {}).get("duration") or 0)
            if duration <= 0 and video:
                duration = float(video.get("duration") or 0)
            return MediaInfo(
                duration=duration,
                width=int((video or {}).get("width") or 0), height=int((video or {}).get("height") or 0),
                fps=_fraction((video or {}).get("avg_frame_rate")), has_video=bool(video), has_audio=bool(audio),
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ExamplebrandProductionError("ExampleBrand_SOURCE_MEDIA_INVALID", f"无法读取媒体信息：{path}") from exc

    def verify_decodable_video(self, path: Path) -> MediaInfo:
        """Check metadata and decode a short real frame sample."""
        info = self.probe(path)
        if not info.has_video or info.duration <= 0:
            raise ExamplebrandProductionError("ExampleBrand_SOURCE_MEDIA_INVALID", f"AI素材没有有效视频流：{path}")
        self.run([
            self.ffmpeg, "-v", "error", "-t", "0.25", "-i", str(path),
            "-an", "-f", "null", "-",
        ], capture=True)
        return info

    @staticmethod
    def fit_filter() -> str:
        return (
            "scale=1080:1920:force_original_aspect_ratio=decrease,"
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30,format=yuv420p"
        )

    def prepare_video(self, source: Path, output: Path, duration: float, start: float = 0.0, loop=False) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.stem + ".partial" + output.suffix)
        if temporary.exists():
            temporary.unlink()
        args = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        if loop:
            args += ["-stream_loop", "-1"]
        if start > 0:
            args += ["-ss", f"{start:.6f}"]
        args += [
            "-i", str(source), "-t", f"{duration:.6f}", "-vf", self.fit_filter(), "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-r", "30", "-vsync", "cfr", "-movflags", "+faststart", str(temporary),
        ]
        self.run(args)
        info = self.probe(temporary)
        if not info.has_video or abs(info.duration - duration) > max(0.1, 2 / 30):
            raise ExamplebrandProductionError(
                "ExampleBrand_TIMELINE_DURATION_MISMATCH",
                f"标准化片段时长不一致：目标{duration:.3f}s，实际{info.duration:.3f}s",
            )
        os.replace(temporary, output)

    def concat(self, sources: list[Path], output: Path, duration: float) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.stem + ".partial" + output.suffix)
        args = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        for source in sources:
            args += ["-i", str(source)]
        labels = "".join(f"[{index}:v]" for index in range(len(sources)))
        args += [
            "-filter_complex", f"{labels}concat=n={len(sources)}:v=1:a=0[v]", "-map", "[v]",
            "-an", "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary),
        ]
        self.run(args)
        info = self.probe(temporary)
        if abs(info.duration - duration) > max(0.1, 2 / 30):
            raise ExamplebrandProductionError(
                "ExampleBrand_TIMELINE_DURATION_MISMATCH",
                f"主体时长不一致：目标{duration:.3f}s，实际{info.duration:.3f}s",
            )
        os.replace(temporary, output)

    def sparse_scores(self, source: Path, duration: float) -> list[float]:
        """Decode at 1fps/96px and score motion, detail, exposure and freezes."""
        width, height = 96, 170
        result = self.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source),
            "-vf", f"fps=1,scale={width}:{height},format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "-"
        ], capture=True)
        raw, size = result.stdout, width * height
        frames = [raw[index:index + size] for index in range(0, len(raw) - size + 1, size)]
        if not frames:
            raise ExamplebrandProductionError("ExampleBrand_CLIP_ANALYSIS_FAILED", "无法提取普通素材分析帧")
        scores, previous = [], None
        for frame in frames:
            count = len(frame)
            mean = sum(frame) / count
            variance = sum((value - mean) ** 2 for value in frame[::4]) / max(1, count // 4)
            exposure = 1.0 - min(1.0, abs(mean - 128.0) / 128.0)
            detail = min(1.0, math.sqrt(variance) / 64.0)
            if previous is None:
                motion = 0.25
            else:
                motion = min(1.0, sum(abs(a - b) for a, b in zip(frame[::4], previous[::4])) / max(1, count // 4) / 28.0)
            black_penalty = 1.0 if mean < 12 else 0.0
            freeze_penalty = 1.0 if previous is not None and motion < 0.025 else 0.0
            scores.append(0.46 * motion + 0.30 * detail + 0.24 * exposure - 0.8 * black_penalty - 0.35 * freeze_penalty)
            previous = frame
        return scores


def deterministic_ai(ai_files: list[Path], identity: str) -> Path:
    if not ai_files:
        raise ExamplebrandProductionError("ExampleBrand_ASSET_IDENTITY_MISMATCH", "ExampleBrand产品专属AI素材为空")
    index = int(hashlib.sha256((identity + "|ai").encode("utf-8")).hexdigest()[:12], 16) % len(ai_files)
    return sorted(ai_files, key=lambda item: item.name.casefold())[index]


def validate_static_resources(task: dict, suite_root: Path | None = None, *, tools: ExamplebrandMediaTools | None = None) -> dict:
    """Validate only resources that must exist before Step 1 is claimed."""
    if str(task.get("batchKind") or "") != "examplebrand-natural-flow":
        raise ExamplebrandProductionError("ExampleBrand_ASSET_IDENTITY_MISMATCH", "任务不是ExampleBrand独立合同")
    identity = tuple(str(task.get(key) or "").strip() for key in ("runGroupId", "batchId", "canonicalTaskId"))
    if not all(identity):
        raise ExamplebrandProductionError("ExampleBrand_ASSET_IDENTITY_MISMATCH", "ExampleBrand任务身份不完整")
    outputs = task.get("outputPlan") if isinstance(task.get("outputPlan"), list) else []
    if len(outputs) != 21:
        raise ExamplebrandProductionError("ExampleBrand_ASSET_IDENTITY_MISMATCH", f"ExampleBrand输出计划应为21条，实际{len(outputs)}条")
    products = {str(item.get("product") or "") for item in outputs}
    media_tools = tools or ExamplebrandMediaTools()
    verify_video = getattr(media_tools, "verify_decodable_video", media_tools.probe)
    asset_report = {}
    for product in products:
        folder = Path(PRODUCT_AI_DIRS.get(product, ""))
        media = _video_files(folder)
        valid_media = []
        invalid_media = []
        for item in media:
            try:
                verify_video(item)
                valid_media.append(item)
            except (OSError, ExamplebrandProductionError) as exc:
                invalid_media.append({"path": str(item), "reason": str(exc)})
        if not valid_media:
            raise ExamplebrandProductionError("ExampleBrand_ASSET_IDENTITY_MISMATCH", f"{product}专属AI素材不可用：{folder}")
        asset_report[product] = {
            "folder": str(folder), "valid": len(valid_media), "invalid": invalid_media,
            "validPaths": [str(item) for item in valid_media],
        }
    ending = Path(DEFAULT_ENDING)
    ending_info = verify_video(ending)
    if not ending_info.has_video or ending_info.duration < ENDING_SECONDS:
        raise ExamplebrandProductionError("ExampleBrand_ENDING_PREPARE_FAILED", "ExampleBrand结尾素材不足3秒")
    copy_library = Path(suite_root or Path(__file__).resolve().parents[1]) / "voice-client" / "data" / "copy_library.csv"
    if not copy_library.is_file():
        raise ExamplebrandProductionError("ExampleBrand_ASSET_IDENTITY_MISMATCH", f"ExampleBrand文案库不存在：{copy_library}")
    try:
        with copy_library.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ExamplebrandProductionError("ExampleBrand_ASSET_IDENTITY_MISMATCH", f"ExampleBrand文案库无法读取：{copy_library}") from exc
    for product in products:
        for language in ("英语", "西班牙语"):
            pool = [row for row in rows if row.get("material") == product and row.get("language") == language]
            ids = {str(row.get("copyId") or "").strip() for row in pool}
            if not pool or len(ids) != len(pool) or any(not str(row.get("text") or "").strip() for row in pool):
                raise ExamplebrandProductionError("ExampleBrand_ASSET_IDENTITY_MISMATCH", f"ExampleBrand文案池不完整：{product}/{language}")
    return {
        "products": sorted(products), "ending": str(ending),
        "copyLibrary": str(copy_library), "copyLibraryVersion": COPY_LIBRARY_VERSION,
        "aiAssets": asset_report,
    }


def choose_window(scores: list[float], source_duration: float, target: float, identity: str) -> tuple[float, float, str]:
    if source_duration <= target + 0.1:
        return 0.0, 1.0, "full_or_loop"
    span = max(1, int(math.ceil(target)))
    candidates = []
    for start in range(0, max(1, len(scores) - span + 1)):
        window = scores[start:start + span]
        if window:
            candidates.append((sum(window) / len(window), start))
    if not candidates:
        available = max(0.0, source_duration - target)
        seed = int(hashlib.sha256((identity + "|fallback").encode("utf-8")).hexdigest()[:12], 16)
        return round(available * (0.4 + (seed % 2001) / 10000.0), 6), 0.0, "deterministic_fallback"
    best_score, best_start = max(candidates, key=lambda pair: (round(pair[0], 9), -pair[1]))
    return min(float(best_start), max(0.0, source_duration - target)), float(best_score), "smart_continuous_window"


def validate_source_duration(duration: float, source_name: str = "") -> None:
    """Apply the ExampleBrand-only source-duration contract before any transcode."""
    if duration > MAX_SOURCE_SECONDS + 1e-6:
        raise ExamplebrandProductionError(
            "ExampleBrand_SOURCE_DURATION_EXCEEDED",
            f"普通素材{duration:.2f}秒，超过120秒：{source_name}",
        )
    if duration < MIN_ORDINARY_SECONDS:
        raise ExamplebrandProductionError(
            "ExampleBrand_SOURCE_TOO_SHORT", f"普通素材少于2秒：{source_name}"
        )


class ExamplebrandProductionComposer:
    def __init__(self, task: dict, sources: list[dict], audio_dir: Path, work_root: Path,
                 *, ffmpeg: str | None = None, ffprobe: str | None = None,
                 ending_path: Path | None = None):
        self.task = dict(task)
        self.sources = list(sources)
        self.audio_dir = Path(audio_dir)
        self.work_root = Path(work_root)
        self.tools = ExamplebrandMediaTools(ffmpeg, ffprobe)
        self.ending_path = Path(ending_path or DEFAULT_ENDING)
        self.owner = str(task.get("canonicalTaskId") or task.get("taskId") or "").strip()
        self.run_group = str(task.get("runGroupId") or "").strip()
        self.batch_id = str(task.get("batchId") or "").strip()
        if str(task.get("batchKind") or "") != "examplebrand-natural-flow":
            raise ExamplebrandProductionError("ExampleBrand_ASSET_IDENTITY_MISMATCH", "非ExampleBrand合同不得进入ExampleBrand生产策略")
        if not all((self.owner, self.run_group, self.batch_id)):
            raise ExamplebrandProductionError("ExampleBrand_ASSET_IDENTITY_MISMATCH", "ExampleBrand任务身份不完整")
        self.root = self.work_root / self.owner
        self.ledger_path = self.root / "execution-manifest.json"

    def _load_ledger(self) -> dict:
        try:
            value = json.loads(self.ledger_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _source_map(self) -> dict[str, Path]:
        result = {}
        for item in self.sources:
            url, value = str(item.get("url") or "").strip(), str(item.get("path") or "").strip()
            if url and value:
                result[url] = Path(value)
        return result

    def _audio_map(self) -> dict[str, Path]:
        if not self.audio_dir.is_dir():
            raise ExamplebrandProductionError("ExampleBrand_ASSET_IDENTITY_MISMATCH", f"ExampleBrand音频目录不存在：{self.audio_dir}")
        files = [item for item in self.audio_dir.iterdir() if item.is_file() and item.suffix.lower() in AUDIO_EXTENSIONS]
        result = {}
        aliases = {"en": ("英语", "_en_"), "es": ("西班牙语", "_es_")}
        for code, terms in aliases.items():
            matches = [item for item in files if any(term.casefold() in item.name.casefold() for term in terms)]
            if len(matches) != 1:
                raise ExamplebrandProductionError(
                    "ExampleBrand_ASSET_IDENTITY_MISMATCH",
                    f"ExampleBrand {code} 音频必须唯一，实际{len(matches)}条：{self.audio_dir}",
                )
            result[code] = matches[0]
        return result

    def validate_resources(self) -> dict:
        outputs = self.task.get("outputPlan") if isinstance(self.task.get("outputPlan"), list) else []
        if len(outputs) != 21 or len({str(item.get("canonicalTaskId") or "") for item in outputs}) != 21:
            raise ExamplebrandProductionError("ExampleBrand_ASSET_IDENTITY_MISMATCH", "ExampleBrand outputPlan必须包含21个唯一输出身份")
        source_map = self._source_map()
        missing = [item.get("sourceUrl") for item in outputs if str(item.get("sourceUrl") or "") not in source_map]
        if missing:
            raise ExamplebrandProductionError("ExampleBrand_ASSET_IDENTITY_MISMATCH", f"ExampleBrand素材身份缺失{len(missing)}条")
        products = {str(item.get("product") or "") for item in outputs}
        ai_dirs = {product: Path(PRODUCT_AI_DIRS.get(product, "")) for product in products}
        ai_files = {}
        for product, folder in ai_dirs.items():
            files = _video_files(folder)
            valid = []
            invalid = []
            for item in files:
                try:
                    info = self.tools.probe(item)
                    if not info.has_video or info.duration <= 0:
                        raise ExamplebrandProductionError("ExampleBrand_SOURCE_MEDIA_INVALID", f"无有效视频流：{item}")
                    valid.append(item)
                except (OSError, ExamplebrandProductionError) as exc:
                    invalid.append({"path": str(item), "reason": str(exc)})
            if not valid:
                raise ExamplebrandProductionError("ExampleBrand_ASSET_IDENTITY_MISMATCH", f"{product}专属AI素材不可用：{folder}")
            ai_files[product] = valid
        ending = self.tools.probe(self.ending_path)
        if not ending.has_video or ending.duration < ENDING_SECONDS:
            raise ExamplebrandProductionError("ExampleBrand_ENDING_PREPARE_FAILED", f"ExampleBrand结尾素材不足{ENDING_SECONDS}秒")
        return {"outputs": outputs, "sourceMap": source_map, "audioMap": self._audio_map(), "aiDirs": ai_dirs, "aiFiles": ai_files}

    def execute(self, progress=None) -> tuple[list[dict], Path]:
        resources = self.validate_resources()
        output_rule = str(((self.task.get("pipelineProfile") or {}).get("4_stitch") or {}).get("outputDirectoryRule") or "").strip()
        if not output_rule:
            raise ExamplebrandProductionError("ExampleBrand_ASSET_IDENTITY_MISMATCH", "ExampleBrand第4步输出目录未配置")
        output_root = Path(output_rule)
        output_root.mkdir(parents=True, exist_ok=True)
        ledger = self._load_ledger()
        if ledger and (ledger.get("runGroupId"), ledger.get("batchId"), ledger.get("canonicalTaskId")) != (
            self.run_group, self.batch_id, self.owner
        ):
            raise ExamplebrandProductionError("ExampleBrand_ASSET_IDENTITY_MISMATCH", "ExampleBrand执行清单属于其他任务")
        rows = ledger.get("outputs") if isinstance(ledger.get("outputs"), dict) else {}
        records = []
        for ordinal, plan in enumerate(resources["outputs"], 1):
            output_id = str(plan.get("canonicalTaskId") or "").strip()
            if progress:
                progress(ordinal - 1, 21, output_id)
            source = resources["sourceMap"][str(plan.get("sourceUrl"))]
            audio = resources["audioMap"][str(plan.get("language"))]
            product = str(plan.get("product") or "")
            identity = "|".join((self.run_group, self.batch_id, output_id, _sha256(source), SELECTION_VERSION))
            source_info, audio_info = self.tools.probe(source), self.tools.probe(audio)
            if not source_info.has_video or source_info.duration <= 0:
                raise ExamplebrandProductionError("ExampleBrand_SOURCE_MEDIA_INVALID", f"普通素材不可解码：{source}")
            validate_source_duration(source_info.duration, source.name)
            voice_duration = audio_info.duration
            ordinary_target = voice_duration - AI_SECONDS - ENDING_SECONDS
            body_target = voice_duration - ENDING_SECONDS
            if ordinary_target < MIN_ORDINARY_SECONDS:
                raise ExamplebrandProductionError("ExampleBrand_DURATION_ALLOCATION_INVALID", f"配音{voice_duration:.2f}秒，无法容纳4+2+3秒结构")
            row = rows.get(output_id) if isinstance(rows.get(output_id), dict) else {}
            source_hash = _sha256(source)
            if row.get("sourceFileHash") == source_hash and row.get("selectionVersion") == SELECTION_VERSION:
                start, score, method = float(row.get("clipStartSeconds") or 0), float(row.get("selectionScore") or 0), str(row.get("selectionMethod") or "persisted")
            else:
                try:
                    scores = self.tools.sparse_scores(source, source_info.duration)
                    start, score, method = choose_window(scores, source_info.duration, ordinary_target, identity)
                except ExamplebrandProductionError:
                    available = max(0.0, source_info.duration - ordinary_target)
                    start, score, method = available / 2.0, 0.0, "deterministic_fallback"
            item_root = self.root / "outputs" / f"{ordinal:02d}-{hashlib.sha256(output_id.encode()).hexdigest()[:10]}"
            ordinary = item_root / "ordinary.mp4"
            ai = item_root / "ai.mp4"
            body = output_root / str(plan.get("outputName") or f"ExampleBrand_{ordinal:02d}.mp4")
            ai_source = deterministic_ai(resources["aiFiles"][product], identity)
            if not ordinary.is_file():
                self.tools.prepare_video(source, ordinary, ordinary_target, start=start, loop=source_info.duration < ordinary_target)
            if not ai.is_file():
                self.tools.prepare_video(ai_source, ai, AI_SECONDS, loop=self.tools.probe(ai_source).duration < AI_SECONDS)
            if not body.is_file():
                self.tools.concat([ai, ordinary], body, body_target)
            body_info = self.tools.probe(body)
            if abs((body_info.duration + ENDING_SECONDS) - voice_duration) > max(0.1, 2 / 30):
                raise ExamplebrandProductionError("ExampleBrand_TIMELINE_DURATION_MISMATCH", f"主体+结尾与配音不一致：{body_info.duration:.3f}+3 != {voice_duration:.3f}")
            row = {
                "canonicalTaskId": output_id, "sourceUrl": str(plan.get("sourceUrl") or ""),
                "sourcePath": str(source.resolve()), "sourceDuration": source_info.duration,
                "sourceFileHash": source_hash, "voiceDuration": voice_duration,
                "aiTargetDuration": AI_SECONDS, "ordinaryTargetDuration": ordinary_target,
                "endingTargetDuration": ENDING_SECONDS, "clipStartSeconds": start,
                "clipEndSeconds": min(source_info.duration, start + ordinary_target),
                "selectionMethod": method, "selectionScore": score, "selectionVersion": SELECTION_VERSION,
                "preparedOrdinaryArtifact": str(ordinary.resolve()), "aiSource": str(ai_source.resolve()),
                "bodyArtifact": str(body.resolve()), "timelineDuration": voice_duration,
                "preparedEndingArtifact": "capcut_task_scoped", "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            rows[output_id] = row
            ledger = {
                "schemaVersion": 1, "executionProfileVersion": PROFILE_VERSION,
                "runGroupId": self.run_group, "batchId": self.batch_id, "canonicalTaskId": self.owner,
                "maximumSourceDurationSeconds": MAX_SOURCE_SECONDS, "durationPolicy": "voice_aligned",
                "ordinaryClipPolicy": "best_continuous_window", "endingPolicy": "capcut_append_once",
                "outputs": rows, "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            _atomic_json(self.ledger_path, ledger)
            records.append({
                "created_at": row["updatedAt"], "project": "ExampleBrand", "board": product,
                "language": str(plan.get("languageName") or plan.get("language") or ""),
                "language_code": str(plan.get("language") or ""), "audio_path": str(audio.resolve()),
                "selection_mode": method, "output": str(body.resolve()), "status": "success", "error": "",
                "items": [{"type": "ai", "file": str(ai_source.resolve()), "duration": AI_SECONDS},
                          {"type": "ordinary", "file": str(source.resolve()), "duration": ordinary_target, "start": start}],
                "cover": {"material_label": product, "language_code": str(plan.get("language") or ""), "logo": "none"},
                "timing": row, "subject_source_paths": [str(source.resolve())],
                "runGroupId": self.run_group, "batchId": self.batch_id,
                "canonicalTaskId": output_id, "parentCanonicalTaskId": self.owner,
                "remotePipelineTaskId": self.owner, "remotePipelineTaskIdSource": "examplebrand_output_plan",
                "formalTaskIdentity": True, "expectedTaskCount": 21,
                "productionDate": str(self.task.get("productionDate") or self.task.get("date") or ""),
                "channelAccount": str(plan.get("channelAccount") or ""), "outputIndex": int(plan.get("outputIndex") or ordinal),
                "outputName": str(plan.get("outputName") or body.name), "originalSamplePath": str(self.ending_path.resolve()),
                "sampleTargetSeconds": ENDING_SECONDS, "executionProfileVersion": PROFILE_VERSION,
            })
        record_path = self.root / "generation-record.json"
        _atomic_json(record_path, records)
        if progress:
            progress(21, 21, "completed")
        return records, record_path


def derived_execution_profile(task: dict) -> dict:
    return {
        "schemaVersion": 1,
        "taskId": str(task.get("taskId") or ""),
        "canonicalTaskId": str(task.get("canonicalTaskId") or task.get("taskId") or ""),
        "runGroupId": str(task.get("runGroupId") or ""),
        "batchId": str(task.get("batchId") or ""),
        "batchKind": "examplebrand-natural-flow",
        "executionProfileVersion": PROFILE_VERSION,
        "durationPolicy": "voice_aligned",
        "maximumSourceDurationSeconds": MAX_SOURCE_SECONDS,
        "aiDurationSeconds": AI_SECONDS,
        "endingReserveSeconds": ENDING_SECONDS,
        "ordinaryClipPolicy": "best_continuous_window",
        "endingPolicy": "capcut_append_once",
        "startPolicy": "auto_when_idle",
        "createdAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
