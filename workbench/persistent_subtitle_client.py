"""Client for the machine-scoped persistent PP-OCR/CUDA worker."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from leading_trim import LEADING_TRIM_SECONDS, trim_leading_cover


class PersistentSubtitleClient:
    PORT = 18767

    def __init__(self, portable_root: Path, worker_script: Path, event_callback=None):
        self.portable_root = Path(portable_root)
        self.worker_script = Path(worker_script)
        self.event_callback = event_callback
        self.socket = None
        self.reader = None
        self.writer = None
        self.request_lock = threading.Lock()
        self.fatal_error = ""
        self.ready = {}
        temp_root = Path(tempfile.gettempdir()) / "ai-video-workbench-logs"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.state_path = temp_root / "persistent-ocr-worker.json"
        self.stderr_path = temp_root / "persistent-ocr-worker.stderr.log"
        self.stdout_path = temp_root / "persistent-ocr-worker.stdout.log"

    def _emit(self, kind, **payload):
        if self.event_callback:
            self.event_callback(kind, **payload)

    def _signature(self):
        return hashlib.sha256(self.worker_script.read_bytes()).hexdigest()

    def _load_state(self):
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _stop_stale_worker(self, state):
        """Stop only the worker identified by our own state file.

        The OCR worker is detached on Windows, so an old process can remain
        after the workbench is updated.  Never kill a PID merely because the
        port is occupied: first require the state record to identify the
        process and verify its command line contains this worker script and
        the expected serve port.
        """
        if os.name != "nt":
            return False
        try:
            pid = int(state.get("pid") or 0)
            port = int(state.get("port") or 0)
        except (TypeError, ValueError):
            return False
        if pid <= 0 or port != self.PORT or pid == os.getpid():
            return False
        try:
            command = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            ).stdout.strip().lower()
        except (OSError, subprocess.SubprocessError):
            return False
        script_name = self.worker_script.name.lower()
        if script_name not in command or "--serve" not in command or f"--port {self.PORT}" not in command:
            return False
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True, timeout=10, check=False)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def _connect(self, timeout=1.0):
        connection = socket.create_connection(("127.0.0.1", self.PORT), timeout=timeout)
        connection.settimeout(None)
        self.socket = connection
        self.reader = connection.makefile("r", encoding="utf-8", errors="replace")
        self.writer = connection.makefile("w", encoding="utf-8", newline="\n")
        ready = self._next_event()
        if not ready or ready.get("event") != "ready":
            self.close()
            raise RuntimeError("Persistent OCR worker did not return ready state")
        expected = self._signature()
        actual = ready.get("scriptSignature") or ready.get("workerSignature")
        if actual != expected:
            self.close()
            state = self._load_state()
            self._stop_stale_worker(state)
            raise RuntimeError("Persistent OCR worker signature mismatch; stale worker was not reused")
        self.ready = ready
        return ready

    def start(self, timeout=420):
        try:
            ready = self._connect()
        except (OSError, RuntimeError):
            ready = None
        if ready is None:
            state = self._load_state()
            if state.get("status") == "fatal" and state.get("signature") == self._signature():
                raise RuntimeError(f"Persistent OCR initialization previously failed: {state.get('message')}")
            python = self.portable_root / ".cuda_env" / "Scripts" / "python.exe"
            if not python.is_file() or not self.worker_script.is_file():
                raise FileNotFoundError("Persistent OCR CUDA runtime or worker script is missing")
            stdout_handle = self.stdout_path.open("a", encoding="utf-8")
            stderr_handle = self.stderr_path.open("a", encoding="utf-8")
            kwargs = {
                "cwd": str(self.portable_root),
                "stdin": subprocess.DEVNULL,
                "stdout": stdout_handle,
                "stderr": stderr_handle,
                "env": {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            }
            if os.name == "nt":
                # Not DETACHED_PROCESS: it makes Windows ignore CREATE_NO_WINDOW,
                # and the real interpreter behind the venv launcher then opens a
                # visible console. A hidden console of its own still lets the
                # persistent worker outlive the bridge that started it.
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen([
                str(python), str(self.worker_script), "--serve", "--port", str(self.PORT),
                "--state-file", str(self.state_path),
            ], **kwargs)
            stdout_handle.close()
            stderr_handle.close()
            deadline = time.monotonic() + timeout
            last_error = ""
            while time.monotonic() < deadline:
                state = self._load_state()
                if state.get("status") == "fatal" and state.get("signature") == self._signature():
                    raise RuntimeError(f"Persistent OCR initialization failed: {state.get('message')}")
                try:
                    ready = self._connect(timeout=1.0)
                    break
                except (OSError, RuntimeError) as exc:
                    last_error = str(exc)
                    time.sleep(0.5)
            else:
                raise TimeoutError(f"Persistent OCR initialization timed out: {last_error}")
        self._emit("info", message=(
            f"Persistent PP-OCRv5 ready; OCR={ready.get('ocrDeviceName') or ready.get('ocrDevice')}; "
            f"engine={ready.get('ocrEngine') or 'legacy'}; batch={ready.get('ocrBatchSize') or 1}; "
            f"initialized once in {ready.get('ocrServiceInitSeconds') or ready.get('ocrInitSeconds')} seconds; "
            f"STTN={ready.get('deviceName')}; encoder=h264_nvenc"
        ))
        return ready

    def _next_event(self):
        raw = self.reader.readline() if self.reader else ""
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"event": "diagnostic", "message": raw.strip()}

    def process_video(self, file_path, downloader):
        with self.request_lock:
            downloader.subtitle_ai_area_cache = {}
            downloader.subtitle_ai_outcome = {
                "accepted": False,
                "outcome": "failed",
                "outputPath": "",
            }
            if self.fatal_error:
                return None, f"AI subtitle batch stopped: {self.fatal_error}", False
            if not self.writer or not self.reader:
                return None, "AI subtitle batch stopped: persistent worker is disconnected", False
            original_input_path = Path(file_path)
            ffmpeg_root = self.portable_root / "AI_Subtitle_Backend" / "backend" / "ffmpeg" / "win_x64"
            downloader._emit_pipeline(
                "trim", 0, f"正在固定裁剪前导 {LEADING_TRIM_SECONDS:g} 秒", status="running"
            )
            try:
                leading_trim = trim_leading_cover(
                    original_input_path, ffmpeg_root / "ffmpeg.exe", ffmpeg_root / "ffprobe.exe", LEADING_TRIM_SECONDS
                )
            except Exception as exc:
                detail = f"Leading cover trim failed before AI subtitle processing: {exc}"
                downloader.subtitle_ai_outcome = {
                    "accepted": False, "outcome": "failed", "outputPath": "",
                    "message": detail, "phase": "leading_trim",
                }
                downloader._emit_pipeline("trim", 100, detail, status="failed")
                return None, detail, False
            input_path = Path(str(leading_trim["outputPath"]))
            downloader._emit_pipeline(
                "trim", 100,
                f"已固定裁剪前导 {LEADING_TRIM_SECONDS:g} 秒："
                f"{leading_trim['sourceDuration']:.3f}s → {leading_trim['outputDuration']:.3f}s",
                status="done",
            )
            output_path = input_path.with_name(f"{input_path.stem}_ai_sttn{input_path.suffix or '.mp4'}")
            request_id = uuid.uuid4().hex
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            log_dir = input_path.parent / ".subtitle_ai_logs" / f"{input_path.stem}_{timestamp}"
            log_dir.mkdir(parents=True, exist_ok=False)
            stdout_path = log_dir / "stdout.jsonl"
            manifest_path = log_dir / "run.json"
            manifest = {
                "status": "running", "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "input": str(input_path), "originalInput": str(original_input_path), "output": str(output_path),
                "mode": "sttn", "workerReady": self.ready, "leadingTrim": leading_trim,
            }
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            request = {"requestId": request_id, "input": str(input_path), "output": str(output_path),
                       "quality": downloader.subtitle_ai_quality, "detectFps": downloader.subtitle_cover_detect_fps}
            try:
                self.writer.write(json.dumps(request, ensure_ascii=False) + "\n")
                self.writer.flush()
            except OSError as exc:
                self.fatal_error = f"Persistent worker connection failed before request: {exc}"
                return None, self.fatal_error, False
            result = None
            try:
                with stdout_path.open("w", encoding="utf-8") as raw_log:
                    while True:
                        event = self._next_event()
                        if event is None:
                            self.fatal_error = "Persistent worker disconnected during a video; automatic retry is unsafe"
                            break
                        raw_log.write(json.dumps(event, ensure_ascii=False) + "\n")
                        raw_log.flush()
                        if event.get("requestId") not in {None, "", request_id}:
                            continue
                        kind = event.get("event")
                        if kind == "progress":
                            total = max(int(event.get("total") or 100), 1)
                            current = int(event.get("current") or 0)
                            downloader._emit_pipeline("blur", max(0, min(100, int(current / total * 100))),
                                                      event.get("message"), status="running", detail=event.get("stage"))
                        elif kind == "detection" and event.get("area"):
                            width, height = downloader._probe_video_dimensions(str(input_path))
                            downloader.subtitle_ai_area_cache = {
                                "area": [int(value) for value in event["area"]], "width": width, "height": height,
                                "confidence": event.get("confidence"), "sampled_frames": event.get("sampledFrames"),
                            }
                            manifest["detection"] = dict(downloader.subtitle_ai_area_cache)
                            manifest["detectedBands"] = event.get("areas") or [event.get("area")]
                            manifest["ocrDiagnostics"] = event.get("diagnostics") or {}
                        elif kind == "result":
                            result = event
                            if event.get("fatal"):
                                self.fatal_error = str(event.get("message") or "Persistent subtitle processing failed")
                            break
            finally:
                outcome = str((result or {}).get("outcome") or "processed").strip()
                accepted_path = Path(str((result or {}).get("outputPath") or output_path))
                success = bool(
                    result
                    and result.get("success")
                    and accepted_path.is_file()
                    and accepted_path.stat().st_size > 0
                )
                verification = (result or {}).get("verification")
                manifest.update({"status": outcome if success else "failed",
                                 "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"), "result": result})
                if isinstance(verification, dict):
                    manifest["verification"] = verification
                if success:
                    manifest["subtitleOutcome"] = outcome
                    manifest["outputPath"] = str(accepted_path)
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                downloader.subtitle_ai_area_cache = {}
            if success:
                downloader.subtitle_ai_outcome = {
                    "accepted": True,
                    "outcome": outcome,
                    "outputPath": str(accepted_path),
                    "verification": verification if isinstance(verification, dict) else {},
                    "leadingTrim": leading_trim,
                    "subtitleTreatment": str((result or {}).get("subtitleTreatment") or outcome),
                    "originalSubtitleBands": (result or {}).get("originalSubtitleBands") or [],
                    "captionPlacement": str((result or {}).get("captionPlacement") or "company_default"),
                    "blurSegments": (result or {}).get("blurSegments") or [],
                    "fallbackAttempt": int((result or {}).get("fallbackAttempt") or 0),
                    "sourceVideoWidth": int((result or {}).get("sourceVideoWidth") or 0),
                    "sourceVideoHeight": int((result or {}).get("sourceVideoHeight") or 0),
                }
                if outcome in {"verified_no_subtitle", "minor_residual"} and accepted_path == input_path:
                    downloader._emit_pipeline(
                        "blur", 100,
                        "Verified no subtitles in source; preserving original video",
                        status="skipped",
                    )
                    # ``False`` keeps the verified original rather than
                    # deleting it as an intermediate replacement file.
                    return str(input_path), None, False
                downloader._emit_pipeline("blur", 100, f"Subtitle processing complete: {output_path.name}", status="done")
                return str(output_path), None, True
            message = (result or {}).get("message") or self.fatal_error or "Unknown persistent AI subtitle error"
            downloader.subtitle_ai_outcome = {
                "accepted": False,
                "outcome": "failed",
                "outputPath": "",
                "message": message,
                "errorCode": str((result or {}).get("errorCode") or "AI_SUBTITLE_PROCESSING_FAILED"),
                "phase": str((result or {}).get("phase") or "subtitle_processing"),
                "verification": (result or {}).get("verification") if isinstance((result or {}).get("verification"), dict) else {},
            }
            downloader._emit_pipeline("blur", None, message, status="error")
            return None, f"AI subtitle processing failed: {message}", False

    def repair_blur(self, entry, downloader):
        """Run only the localized blur fallback on an existing STTN artifact."""
        with self.request_lock:
            if self.fatal_error:
                return None, f"AI subtitle repair stopped: {self.fatal_error}", False
            if not self.writer or not self.reader:
                return None, "AI subtitle repair stopped: persistent worker is disconnected", False
            input_path = Path(str(entry.get("input") or ""))
            output_path = Path(str(entry.get("output") or entry.get("input") or ""))
            if not input_path.is_file() or input_path.stat().st_size < 1024:
                return None, f"STTN artifact missing or incomplete: {input_path}", False
            request_id = uuid.uuid4().hex
            request = {
                "requestId": request_id,
                "repairMode": "blur_fallback",
                "input": str(input_path),
                "output": str(output_path),
                "verification": entry.get("verification") if isinstance(entry.get("verification"), dict) else {},
                "originalSubtitleBands": entry.get("originalSubtitleBands") or [],
                "detectFps": float(entry.get("detectFps") or 2.0),
            }
            try:
                self.writer.write(json.dumps(request, ensure_ascii=False) + "\n")
                self.writer.flush()
            except OSError as exc:
                return None, f"AI subtitle repair request failed: {exc}", False
            result = None
            while True:
                event = self._next_event()
                if event is None:
                    self.fatal_error = "Persistent worker disconnected during subtitle repair"
                    break
                if event.get("requestId") not in {None, "", request_id}:
                    continue
                kind = event.get("event")
                if kind == "progress":
                    total = max(int(event.get("total") or 100), 1)
                    current = int(event.get("current") or 0)
                    downloader._emit_pipeline(
                        "blur", max(0, min(100, int(current / total * 100))),
                        event.get("message"), status="running", detail=event.get("stage"),
                    )
                elif kind == "result":
                    result = event
                    break
            accepted_path = Path(str((result or {}).get("outputPath") or output_path))
            success = bool(
                result and result.get("success") and accepted_path.is_file()
                and accepted_path.stat().st_size >= 1024
            )
            verification = (result or {}).get("verification")
            if success:
                outcome = str((result or {}).get("outcome") or "processed")
                downloader.subtitle_ai_outcome = {
                    "accepted": True,
                    "outcome": outcome,
                    "outputPath": str(accepted_path),
                    "verification": verification if isinstance(verification, dict) else {},
                    "leadingTrim": entry.get("leadingTrim") or {},
                    "subtitleTreatment": str((result or {}).get("subtitleTreatment") or "blur_fallback"),
                    "originalSubtitleBands": (result or {}).get("originalSubtitleBands") or entry.get("originalSubtitleBands") or [],
                    "captionPlacement": str((result or {}).get("captionPlacement") or "company_default"),
                    "blurSegments": (result or {}).get("blurSegments") or [],
                    "fallbackAttempt": int((result or {}).get("fallbackAttempt") or 1),
                }
                downloader._emit_pipeline("blur", 100, "Subtitle blur fallback repair complete", status="done")
                return str(accepted_path), None, True
            message = (result or {}).get("message") or self.fatal_error or "Unknown subtitle repair error"
            downloader.subtitle_ai_outcome = {
                "accepted": False,
                "outcome": "failed",
                "outputPath": "",
                "message": message,
                "errorCode": str((result or {}).get("errorCode") or "SUBTITLE_BLUR_FAILED"),
                "phase": str((result or {}).get("phase") or "subtitle_processing"),
                "verification": verification if isinstance(verification, dict) else {},
                "fallbackAttempt": 1,
            }
            downloader._emit_pipeline("blur", None, message, status="error")
            return None, f"AI subtitle repair failed: {message}", False

    def close(self, force=False):
        for handle in (self.writer, self.reader):
            try:
                if handle:
                    handle.close()
            except OSError:
                pass
        try:
            if self.socket:
                self.socket.close()
        except OSError:
            pass
        self.socket = self.reader = self.writer = None
