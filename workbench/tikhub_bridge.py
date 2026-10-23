"""Headless TikHub bridge with durable status relay and persistent OCR reuse."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import sys
import threading
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from common_io import atomic_write_json as atomic_json
from leading_trim import LEADING_TRIM_SECONDS, trim_leading_cover
from persistent_subtitle_client import PersistentSubtitleClient


PORTABLE_ROOT = Path(r"E:\TikHub_AI_Subtitle_2.0.0-ai_Portable")
SOURCE_ROOT = Path(__file__).resolve().parent.parent / "subtitle-tool"
APP_DIR = Path(__file__).resolve().parent
FFMPEG_ROOT = PORTABLE_ROOT / "AI_Subtitle_Backend" / "backend" / "ffmpeg" / "win_x64"
FFMPEG_PATH = FFMPEG_ROOT / "ffmpeg.exe"
FFPROBE_PATH = FFMPEG_ROOT / "ffprobe.exe"
EMIT_LOCK = threading.Lock()
RELAY = None


class StatusRelay:
    """Durable at-least-once delivery; receiver de-duplicates eventId."""
    def __init__(self, request: dict):
        self.request = request
        self.task_id = str(request.get("taskId") or f"local-{datetime.now():%Y%m%d%H%M%S}")
        self.endpoint = "http://127.0.0.1:8766/api/pipeline/events"
        self.path = APP_DIR / "handoff" / "status" / "outbox" / f"{self.task_id}.json"
        self.lock = threading.RLock()
        try:
            self.outbox = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.outbox = {"taskId": self.task_id, "nextSequence": 1, "pending": []}

    def publish(self, event: dict) -> None:
        with self.lock:
            sequence = int(self.outbox.get("nextSequence") or 1)
            payload = {
                "taskId": self.task_id,
                "batchId": str(self.request.get("batchId") or ""),
                "batchSequence": int(self.request.get("batchSequence") or 1),
                "batchSize": int(self.request.get("batchSize") or 1),
                "date": str(self.request.get("productionDate") or self.request.get("date") or ""),
                "productionDate": str(self.request.get("productionDate") or self.request.get("date") or ""),
                "project": str(self.request.get("project") or ""),
                "material": str(self.request.get("material") or ""),
                "expectedCount": int(self.request.get("expectedCount") or len(self.request.get("urls") or [])),
                **event,
                "sentAt": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            }
            identity = f"{self.task_id}|{sequence}|{payload.get('url','')}|{payload.get('stage','')}"
            payload["eventId"] = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            self.outbox["nextSequence"] = sequence + 1
            self.outbox.setdefault("pending", []).append(payload)
            atomic_json(self.path, self.outbox)
            self.flush()

    def flush(self) -> None:
        pending = list(self.outbox.get("pending") or [])
        delivered = 0
        for payload in pending:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                request = urllib.request.Request(self.endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(request, timeout=2) as response:
                    if response.status != 200:
                        break
                delivered += 1
            except (OSError, urllib.error.URLError):
                break
        if delivered:
            self.outbox["pending"] = pending[delivered:]
            atomic_json(self.path, self.outbox)


class Checkpoint:
    def __init__(self, task_id: str):
        safe_id = task_id or "local"
        self.path = APP_DIR / "handoff" / "download" / "checkpoints" / f"{safe_id}.json"
        self.lock = threading.RLock()
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"taskId": task_id, "items": {}}

    def completed_output(self, url: str) -> str:
        item = (self.data.get("items") or {}).get(url) or {}
        path = Path(str(item.get("outputPath") or ""))
        try:
            if item.get("status") != "completed" or not path.is_file() or path.stat().st_size < 1024:
                return ""
            outcome = str(item.get("subtitleOutcome") or "").strip()
            legacy_processed = path.stem.lower().endswith(("_ai_sttn", "_ai_lama", "_blur", "_covered"))
            if outcome in {"processed", "verified_no_subtitle", "minor_residual", "sttn_clean", "blur_fallback"} or legacy_processed:
                return str(path)
            # A raw file previously looked like a completed item merely because
            # it existed.  It must be rechecked instead of silently bypassing
            # the AI subtitle outcome contract on a later retry.
            return ""
        except OSError:
            return ""

    def update(self, url: str, status: str, **values) -> None:
        with self.lock:
            item = self.data.setdefault("items", {}).setdefault(url, {"url": url})
            item.update({"status": status, "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"), **values})
            atomic_json(self.path, self.data)


def emit(kind, **data):
    if kind == "task" and str(data.get("stage") or "") in {"subtitle", "blur"} and str(data.get("detail") or "") in {"ocr", "sttn", "encode"}:
        data = {**data, "stage": str(data.get("detail"))}
    with EMIT_LOCK:
        # stdout is a machine-readable transport and may be attached to a
        # Windows GBK console.  Escape non-ASCII characters so an emoji or a
        # Chinese filename can never make the bridge crash while emitting an
        # otherwise valid event.  Files and HTTP relay payloads remain UTF-8.
        print(json.dumps({"type": kind, **data}, ensure_ascii=True), flush=True)
    if kind == "task" and RELAY is not None:
        stage = str(data.get("stage") or "waiting")
        detail = str(data.get("detail") or "")
        if stage in {"subtitle", "blur"} and detail in {"ocr", "sttn", "encode"}:
            stage = detail
        files = data.get("files") or []
        output_path = next((str(value) for value in reversed(files) if isinstance(value, str) and Path(value).suffix.lower() in {".mp4", ".mov", ".mkv"}), "")
        RELAY.publish({
            "url": str(data.get("url") or ""), "stage": stage,
            "status": str(data.get("status") or ("failed" if stage == "failed" else "running")),
            "progress": data.get("progress"), "message": str(data.get("message") or ""),
            "error": str(data.get("message") or "") if stage == "failed" else "", "outputPath": output_path,
        })


DEFAULT_API_BASE_URL = "https://api.tikhub.io"
RESOLVE_RETRY_DELAYS = (3, 8)  # seconds before attempts 2 and 3


def choose_network_route(base_url, probe=None):
    """Bypass the system proxy for this process when TikHub answers directly.

    The machine-wide HTTPS_PROXY points at a local v2rayN port.  Every httpx
    client honours it, so Douyin CDN downloads took a detour abroad (about 5x
    slower, measured 2026-10-20) and nothing started while v2rayN was closed.
    The choice is made once, before any client exists, because changing the
    environment while download threads build clients would race.  When the
    direct probe fails, the system proxy stays in effect exactly as before.
    """
    if probe is None:
        def probe(url):
            import httpx
            with httpx.Client(trust_env=False, timeout=6.0) as client:
                return client.get(url).status_code
    url = (str(base_url or "").strip() or DEFAULT_API_BASE_URL).rstrip("/") + "/"
    try:
        reachable = int(probe(url)) < 500
    except Exception:
        reachable = False
    if reachable:
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"
        return "direct"
    return "proxy"


def describe_resolve_failure(raw):
    """Return why a raw TikHub answer has no usable data, or None when usable."""
    if not isinstance(raw, dict):
        return "接口请求异常（网络或超时，详见 TikHub app.log）"
    code = raw.get("code", 200)
    if code == 200 and isinstance(raw.get("data"), dict):
        return None
    detail = raw.get("detail") if isinstance(raw.get("detail"), dict) else raw
    code = detail.get("code", code)
    message = str(detail.get("message_zh") or detail.get("message") or detail.get("detail") or "").strip()
    return f"TikHub 返回 {code}" + (f"：{message[:120]}" if message else "")


def resolve_video_info(client, url, abort, on_retry, clean_url=None):
    """Resolve one share link, retrying TikHub refusals and timeouts.

    TikHub answers HTTP 400 for some links that work moments later (every
    "未获取到视频地址" in October matched a 400 in app.log).  Each attempt is a
    read-only lookup, so retrying cannot duplicate a download.  The final
    error keeps the original "未获取到视频地址" text that callers match on.
    """
    if clean_url is None:
        from downloader.utils.utils import extract_and_clean_url as clean_url
    attempts = len(RESOLVE_RETRY_DELAYS) + 1
    reason = ""
    for attempt in range(1, attempts + 1):
        raw = client.get_data(url, clean_data=False)
        reason = describe_resolve_failure(raw)
        if reason is None:
            api = client.douyin_api if "douyin" in clean_url(url) else client.tiktok_api
            info = api.clean_one_video_data(raw)
            if info and info.get("video_urls"):
                return info
            reason = "接口返回中没有视频地址"
        if attempt == attempts:
            break
        delay = RESOLVE_RETRY_DELAYS[attempt - 1]
        on_retry(attempt, attempts, delay, reason)
        if abort.wait(delay):
            break
    raise RuntimeError(f"未获取到视频地址（{reason}；已尝试 {attempt} 次）")


def main(request_path):
    global RELAY
    os.chdir(PORTABLE_ROOT)
    sys.path.insert(0, str(SOURCE_ROOT))
    from downloader.apis.api_client import MainAPIClient
    from downloader.config import Config
    from downloader.core.downloader import VideoDownloader

    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    urls = list(dict.fromkeys(request.get("urls") or []))
    output = Path(request["download_path"])
    output.mkdir(parents=True, exist_ok=True)
    # A prefetch runs before its project has started; it must not move the
    # project's Dashboard card.  The later normal run reports every item.
    if request.get("statusRelay", True):
        RELAY = StatusRelay(request)
        RELAY.flush()
    checkpoint = Checkpoint(str(request.get("taskId") or ""))
    for url in urls:
        emit("task", url=url, stage="waiting", status="waiting", progress=0, message="等待处理")

    config = Config()
    route = choose_network_route(config.get("api_base_url"))
    emit("info", message="网络：TikHub 与视频直连（不经系统代理）" if route == "direct"
         else "网络：TikHub 直连失败，沿用系统代理")
    client = MainAPIClient(api_key=config.get("api_key"), base_url=config.get("api_base_url"), proxy=config.get("proxy", None))
    if not client.is_configured:
        raise RuntimeError("TikHub API Key 无效，请先在原 TikHub 设置中更新。")
    mode = request.get("mode", "ai-sttn")
    workers = max(1, min(8, int(request.get("download_workers", 3) or 3)))
    subtitle_worker = None
    if bool(request.get("subtitle_enabled", True)) and mode == "ai-sttn":
        subtitle_worker = PersistentSubtitleClient(
            PORTABLE_ROOT, APP_DIR / "ai_subtitle_worker.py",
            event_callback=lambda kind, **payload: emit(kind, **payload),
        )
        # Initialization failure occurs before downloads and is never retried
        # per video or downgraded to CPU.
        subtitle_worker.start()

    abort = threading.Event()
    repair_items = {
        str(item.get("url") or "").strip(): item
        for item in (request.get("repairOnly") or [])
        if isinstance(item, dict) and str(item.get("url") or "").strip()
    }

    def process_repair_url(url, entry):
        """Repair only the existing STTN artifact; never download or rerun STTN."""
        if subtitle_worker is None:
            emit("task", url=url, stage="failed", status="failed", progress=100,
                 message="字幕兜底修复需要持久化CUDA工作进程", errorCode="SUBTITLE_REPAIR_WORKER_UNAVAILABLE",
                 phase="subtitle_processing", files=[str(entry.get("input") or "")])
            return False

        class RepairDownloader:
            subtitle_ai_outcome = {}

            def _emit_pipeline(self, stage, progress=None, message=None, status="running", detail=None):
                emit("task", url=url, stage=stage, progress=progress, status=status,
                     message=message or "", detail=detail)

        helper = RepairDownloader()
        input_path = Path(str(entry.get("input") or ""))
        output_path = Path(str(entry.get("output") or entry.get("input") or ""))
        repaired, error, created = subtitle_worker.repair_blur(entry, helper)
        outcome = helper.subtitle_ai_outcome if isinstance(helper.subtitle_ai_outcome, dict) else {}
        if created and repaired:
            final_path = str(repaired)
            verification = outcome.get("verification") if isinstance(outcome.get("verification"), dict) else {}
            checkpoint.update(
                url, "completed", outputPath=final_path, downloadStatus="completed",
                subtitleStatus="completed", subtitleOutcome="processed",
                verification=verification,
                verificationStatus=str(verification.get("verificationStatus") or "verified_clean"),
                repairMode=str(outcome.get("subtitleTreatment") or "blur_fallback"),
                fallbackAttempt=int(outcome.get("fallbackAttempt") or 1),
                files=[final_path],
                leadingTrim=entry.get("leadingTrim") or {},
                subtitleTreatment=str(outcome.get("subtitleTreatment") or "blur_fallback"),
                originalSubtitleBands=outcome.get("originalSubtitleBands") or entry.get("originalSubtitleBands") or [],
                captionPlacement=str(outcome.get("captionPlacement") or "company_default"),
                blurSegments=outcome.get("blurSegments") or [],
            )
            sidecar_path = Path(final_path).with_suffix(Path(final_path).suffix + ".subtitle-verification.json")
            atomic_json(sidecar_path, {
                "schemaVersion": 1, "taskId": str(request.get("taskId") or ""),
                "batchId": str(request.get("batchId") or ""),
                "productionDate": str(request.get("productionDate") or request.get("date") or ""),
                "url": url, "outputPath": final_path,
                "subtitleTreatment": str(outcome.get("subtitleTreatment") or "blur_fallback"),
                "repairMode": str(outcome.get("subtitleTreatment") or "blur_fallback"),
                "fallbackAttempt": int(outcome.get("fallbackAttempt") or 1),
                "originalSubtitleBands": outcome.get("originalSubtitleBands") or entry.get("originalSubtitleBands") or [],
                "captionPlacement": str(outcome.get("captionPlacement") or "company_default"),
                "blurSegments": outcome.get("blurSegments") or [],
                "verification": verification,
                "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            })
            emit("task", url=url, stage="complete", status="done", progress=100,
                 message="已复用STTN结果完成模糊兜底与字幕复检", files=[final_path],
                 subtitleTreatment=str(outcome.get("subtitleTreatment") or "blur_fallback"),
                 captionPlacement=str(outcome.get("captionPlacement") or "company_default"),
                 verification=verification)
            return True

        message = str(error or outcome.get("message") or "字幕模糊兜底修复失败")
        error_code = str(outcome.get("errorCode") or "SUBTITLE_BLUR_FAILED")
        phase = str(outcome.get("phase") or "subtitle_processing")
        files = [str(input_path)] if input_path.is_file() else []
        checkpoint.update(
            url, "failed", error=message, errorCode=error_code, phase=phase,
            downloadStatus="completed" if files else "failed", subtitleStatus="failed",
            files=files, verification=outcome.get("verification") if isinstance(outcome.get("verification"), dict) else {},
            fallbackAttempt=int(outcome.get("fallbackAttempt") or 1),
        )
        emit("task", url=url, stage="failed", status="failed", progress=100, message=message,
             errorCode=error_code, phase=phase, files=files,
             downloadStatus="completed" if files else "failed", subtitleStatus="failed",
             verification=outcome.get("verification") if isinstance(outcome.get("verification"), dict) else {})
        return False

    def process_url(url):
        if abort.is_set():
            return None
        repair_entry = repair_items.get(str(url).strip())
        if repair_entry:
            return process_repair_url(url, repair_entry)
        existing = checkpoint.completed_output(url)
        if existing:
            emit("task", url=url, stage="complete", status="done", progress=100,
                 message="断点恢复：已验证既有成片", files=[existing])
            return True
        emit("task", url=url, stage="resolve", status="running", progress=5, message="正在解析链接")
        files = []
        downloader = None
        try:
            info = resolve_video_info(
                client, url, abort,
                lambda attempt, attempts, delay, reason: emit(
                    "task", url=url, stage="resolve", status="running", progress=5,
                    message=f"解析失败（{reason}），{delay} 秒后重试（{attempt + 1}/{attempts}）"),
            )
            info.setdefault("media_type", "video")

            def pipeline(event):
                emit("task", url=url, **event)

            def progress(current, maximum):
                percent = int(current / maximum * 100) if maximum else 0
                emit("task", url=url, stage="download", status="running", progress=percent, message=f"视频下载 {percent}%")

            downloader = VideoDownloader(
                download_path=str(output), skip_existing=True, max_workers=1,
                # The lead-trim result is a new, registered derivative.  Keep the raw
                # download until the existing, identity-aware cleanup flow releases it.
                delete_original_after_processing=False,
                subtitle_cover_enabled=bool(request.get("subtitle_enabled", True)), subtitle_cover_mode=mode,
                subtitle_cover_workspace=config.get("subtitle_cover_workspace", ""),
                subtitle_cover_detect_fps=config.get("subtitle_cover_detect_fps", 1.5),
                subtitle_cover_min_score=config.get("subtitle_cover_min_score", 0.38),
                subtitle_cover_blur_kernel=config.get("subtitle_cover_blur_kernel", 41),
                subtitle_cover_white_alpha=config.get("subtitle_cover_white_alpha", 0.88),
                subtitle_ai_workspace=config.get("subtitle_ai_workspace", str(PORTABLE_ROOT / "AI_Subtitle_Backend")),
                subtitle_ai_auto_detect=config.get("subtitle_ai_auto_detect", True), subtitle_ai_require_cuda=True,
                subtitle_ai_quality=config.get("subtitle_ai_quality", 18),
                subtitle_ai_processor=subtitle_worker.process_video if subtitle_worker else None,
                pipeline_callback=pipeline,
            )
            result = downloader.main_downloader(info, progress_callback=progress, max_retries=3)
            if not result.get("success"):
                raise RuntimeError("；".join(result.get("errors", [])) or "下载失败")
            files = result.get("files", [])
            subtitle_outcome = getattr(downloader, "subtitle_ai_outcome", {})
            if bool(request.get("subtitle_enabled", True)):
                if not isinstance(subtitle_outcome, dict) or not subtitle_outcome.get("accepted"):
                    detail = str((subtitle_outcome or {}).get("message") or "")
                    raise RuntimeError(detail or "AI 去字幕未产生已验证的素材")
                final_path = str(subtitle_outcome.get("outputPath") or "")
                trim = subtitle_outcome.get("leadingTrim") or {}
            else:
                final_path = next((str(value) for value in reversed(files) if isinstance(value, str) and Path(value).is_file()), "")
                emit("task", url=url, stage="trim", status="running", progress=100,
                     message=f"正在固定裁剪前导 {LEADING_TRIM_SECONDS:g} 秒")
                trim = trim_leading_cover(final_path, FFMPEG_PATH, FFPROBE_PATH, LEADING_TRIM_SECONDS)
                final_path = str(trim["outputPath"])
                subtitle_outcome = {"outcome": "not_requested", "leadingTrim": trim}
                emit("task", url=url, stage="trim", status="done", progress=100,
                     message=(f"已固定裁剪前导 {LEADING_TRIM_SECONDS:g} 秒："
                              f"{trim['sourceDuration']:.3f}s → {trim['outputDuration']:.3f}s"),
                     files=[final_path])
            if not final_path or Path(final_path).stat().st_size < 1024:
                raise RuntimeError("最终去字幕文件不存在或无效")
            files = [value for value in files if str(value) != final_path] + [final_path]
            checkpoint.update(
                url,
                "completed",
                outputPath=final_path,
                downloadStatus="completed",
                subtitleStatus="completed",
                subtitleOutcome=str(subtitle_outcome.get("outcome") or "processed"),
                verification=(subtitle_outcome.get("verification") if isinstance(subtitle_outcome, dict) else {}),
                verificationStatus=str(
                    ((subtitle_outcome.get("verification") or {}).get("verificationStatus")
                     if isinstance(subtitle_outcome.get("verification"), dict) else "") or "verified_clean"
                ),
                repairMode=str(subtitle_outcome.get("subtitleTreatment") or "sttn_clean"),
                fallbackAttempt=int(subtitle_outcome.get("fallbackAttempt") or (
                    1 if str(subtitle_outcome.get("subtitleTreatment") or "") == "blur_fallback" else 0
                )),
                files=[str(value) for value in files if isinstance(value, str)],
                leadingTrim=(subtitle_outcome.get("leadingTrim") if isinstance(subtitle_outcome, dict) else {}),
                subtitleTreatment=str(subtitle_outcome.get("subtitleTreatment") or ""),
                originalSubtitleBands=subtitle_outcome.get("originalSubtitleBands") or [],
                captionPlacement=str(subtitle_outcome.get("captionPlacement") or "company_default"),
                blurSegments=subtitle_outcome.get("blurSegments") or [],
                sourceVideoWidth=int(subtitle_outcome.get("sourceVideoWidth") or 0),
                sourceVideoHeight=int(subtitle_outcome.get("sourceVideoHeight") or 0),
            )
            sidecar_path = Path(final_path).with_suffix(Path(final_path).suffix + ".subtitle-verification.json")
            atomic_json(sidecar_path, {
                "schemaVersion": 1,
                "taskId": str(request.get("taskId") or ""),
                "batchId": str(request.get("batchId") or ""),
                "productionDate": str(request.get("productionDate") or request.get("date") or ""),
                "url": url,
                "outputPath": final_path,
                "subtitleTreatment": str(subtitle_outcome.get("subtitleTreatment") or subtitle_outcome.get("outcome") or ""),
                "repairMode": str(subtitle_outcome.get("subtitleTreatment") or "sttn_clean"),
                "fallbackAttempt": int(subtitle_outcome.get("fallbackAttempt") or 0),
                "verificationStatus": str(
                    ((subtitle_outcome.get("verification") or {}).get("verificationStatus")
                     if isinstance(subtitle_outcome.get("verification"), dict) else "") or "verified_clean"
                ),
                "originalSubtitleBands": subtitle_outcome.get("originalSubtitleBands") or [],
                "captionPlacement": str(subtitle_outcome.get("captionPlacement") or "company_default"),
                "blurSegments": subtitle_outcome.get("blurSegments") or [],
                "sourceVideoWidth": int(subtitle_outcome.get("sourceVideoWidth") or 0),
                "sourceVideoHeight": int(subtitle_outcome.get("sourceVideoHeight") or 0),
                "verification": subtitle_outcome.get("verification") or {},
                "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            })
            emit("task", url=url, stage="complete", status="done", progress=100,
                 message="下载与去字幕完成", files=files,
                 subtitleTreatment=str(subtitle_outcome.get("subtitleTreatment") or ""),
                 captionPlacement=str(subtitle_outcome.get("captionPlacement") or "company_default"),
                 verification=subtitle_outcome.get("verification") or {})
            return True
        except Exception as exc:
            failure_outcome = getattr(locals().get("downloader", None), "subtitle_ai_outcome", {})
            existing_files = [
                str(value) for value in (files or [])
                if isinstance(value, str) and Path(value).is_file()
            ]
            subtitle_failed = bool(request.get("subtitle_enabled", True)) and (
                str((failure_outcome or {}).get("phase") or "").startswith("subtitle")
                or str((failure_outcome or {}).get("errorCode") or "").startswith(("AI_SUBTITLE", "SUBTITLE"))
                or "AI subtitle" in str(exc)
            )
            checkpoint.update(
                url,
                "failed",
                error=str(exc),
                errorCode=str((failure_outcome or {}).get("errorCode") or "DOWNLOAD_OR_SUBTITLE_FAILED"),
                phase=str((failure_outcome or {}).get("phase") or "download_or_subtitle"),
                downloadStatus="completed" if existing_files or subtitle_failed else "failed",
                subtitleStatus="failed" if subtitle_failed else ("not_requested" if not request.get("subtitle_enabled", True) else "not_started"),
                fallbackAttempt=int((failure_outcome or {}).get("fallbackAttempt") or 0),
                files=existing_files,
                verification=(failure_outcome or {}).get("verification") if isinstance((failure_outcome or {}).get("verification"), dict) else {},
            )
            error_code = str((failure_outcome or {}).get("errorCode") or "DOWNLOAD_OR_SUBTITLE_FAILED")
            phase = str((failure_outcome or {}).get("phase") or "download_or_subtitle")
            if "未获取到视频地址" in str(exc):
                error_code, phase = "VIDEO_ADDRESS_UNAVAILABLE", "resolve"
            # Keep successful downloads visible even when the subtitle stage
            # failed.  The UI can then report Step 1 and Step 2 independently
            # instead of turning one subtitle error into two failures.
            download_status = "completed" if existing_files else "failed"
            subtitle_status = "failed" if subtitle_failed else ("not_requested" if not request.get("subtitle_enabled", True) else "not_started")
            emit("task", url=url, stage="failed", status="failed", progress=100, message=str(exc),
                 errorCode=error_code, phase=phase, files=existing_files,
                 downloadStatus=download_status, subtitleStatus=subtitle_status,
                 verification=(failure_outcome or {}).get("verification") if isinstance(failure_outcome, dict) else {})
            if subtitle_worker and subtitle_worker.fatal_error:
                abort.set()
            return False

    succeeded = completed = failed = 0
    emit("info", message=f"并发下载 {workers} 个；全队列复用一个常驻 PP-OCRv5 会话")
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(process_url, url): url for url in urls}
            for future in as_completed(futures):
                result = future.result()
                if result is None:
                    continue
                completed += 1
                if result:
                    succeeded += 1
                else:
                    failed += 1
                emit("overall", current=completed, total=len(urls), succeeded=succeeded, failed=failed)
                if abort.is_set():
                    for pending in futures:
                        pending.cancel()
                    raise RuntimeError(subtitle_worker.fatal_error or "Persistent subtitle worker failed")
    finally:
        if subtitle_worker:
            subtitle_worker.close()
    emit("complete", total=len(urls), succeeded=succeeded, failed=failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("request")
    args = parser.parse_args()
    try:
        main(args.request)
    except Exception as exc:
        emit("fatal", message=str(exc), detail=traceback.format_exc(),
             errorCode="DOWNLOAD_BRIDGE_RUNTIME_FAILED", phase="bridge_runtime")
        raise SystemExit(1)
