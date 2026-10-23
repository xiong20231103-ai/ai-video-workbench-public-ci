"""Client for the isolated RTX 50-series PaddleOCR detection service.

The production STTN worker uses PyTorch CUDA 12.8.  Paddle's Windows
Blackwell runtime is CUDA 12.9, so the two libraries must never share a
Python process.  This client owns only a small local JSON-lines socket.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path


class OCRGpuClient:
    PORT = 18768

    def __init__(self, portable_root: Path, service_script: Path, model_name: str, model_dir: Path):
        self.portable_root = Path(portable_root)
        self.service_script = Path(service_script)
        self.model_name = str(model_name)
        self.model_dir = Path(model_dir)
        self.socket = None
        self.reader = None
        self.writer = None
        self.ready = {}
        self.temp_root = Path(tempfile.gettempdir()) / "ai-video-workbench-logs"
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.temp_root / "persistent-gpu-ocr.json"
        self.stderr_path = self.temp_root / "persistent-gpu-ocr.stderr.log"
        self.stdout_path = self.temp_root / "persistent-gpu-ocr.stdout.log"

    def _close_connection(self):
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

    def _connect(self, timeout: float = 1.0):
        self._close_connection()
        connection = socket.create_connection(("127.0.0.1", self.PORT), timeout=timeout)
        connection.settimeout(None)
        self.socket = connection
        self.reader = connection.makefile("r", encoding="utf-8", errors="replace")
        self.writer = connection.makefile("w", encoding="utf-8", newline="\n")
        line = self.reader.readline()
        ready = json.loads(line) if line else {}
        if ready.get("event") != "ready":
            self._close_connection()
            raise RuntimeError(ready.get("message") or "GPU OCR service did not become ready")
        self.ready = ready
        return ready

    def _state(self):
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def start(self, timeout: float = 180.0):
        try:
            return self._connect()
        except OSError:
            pass
        runtime = self.portable_root / ".ocr_gpu_env" / "Scripts" / "python.exe"
        if not runtime.is_file():
            raise FileNotFoundError(
                f"RTX 5080 OCR runtime is missing: {runtime}. Production is paused; CPU fallback is disabled."
            )
        if not self.service_script.is_file() or not self.model_dir.is_dir():
            raise FileNotFoundError("GPU OCR service script or PP-OCRv5 model directory is missing")
        state = self._state()
        if state.get("status") == "fatal":
            raise RuntimeError(f"Previous GPU OCR initialization failed: {state.get('message')}")
        stdout = self.stdout_path.open("a", encoding="utf-8")
        stderr = self.stderr_path.open("a", encoding="utf-8")
        try:
            # The supported Paddle 50-series wheel needs cuDNN 9/CUDA 12
            # DLLs.  The existing Torch CUDA 12.8 runtime already contains
            # that verified Windows DLL set.  We expose it only to this
            # isolated OCR process; neither Python environment is modified.
            torch_dll_dir = self.portable_root / ".cuda_env" / "Lib" / "site-packages" / "torch" / "lib"
            if not torch_dll_dir.is_dir():
                raise FileNotFoundError(f"Required CUDA DLL directory is missing: {torch_dll_dir}")
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
            env["PATH"] = f"{torch_dll_dir}{os.pathsep}{env.get('PATH', '')}"
            env.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
            kwargs = {
                "cwd": str(self.portable_root),
                "stdin": subprocess.DEVNULL,
                "stdout": stdout,
                "stderr": stderr,
                "env": env,
            }
            if os.name == "nt":
                # Not DETACHED_PROCESS: it makes Windows ignore CREATE_NO_WINDOW,
                # and the real interpreter behind the venv launcher then opens a
                # visible console. A hidden console of its own still lets the
                # service outlive the worker that started it.
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen([
                str(runtime), str(self.service_script), "--serve", "--port", str(self.PORT),
                "--state-file", str(self.state_path), "--model-name", self.model_name,
                "--model-dir", str(self.model_dir),
            ], **kwargs)
        finally:
            stdout.close()
            stderr.close()
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            state = self._state()
            if state.get("status") == "fatal":
                raise RuntimeError(f"GPU OCR initialization failed: {state.get('message')}")
            try:
                return self._connect()
            except (OSError, RuntimeError) as exc:
                last_error = str(exc)
                time.sleep(0.5)
        raise TimeoutError(f"GPU OCR initialization timed out: {last_error}")

    def detect_stable_subtitle_band(
        self,
        video_path: str,
        detect_fps: float,
        progress=None,
        minimum_hits: int | None = None,
        scan_mode: str = "subtitle",
        required_consecutive: int = 2,
        minimum_line_width_ratio: float = 0.05,
    ):
        if not self.writer or not self.reader:
            raise RuntimeError("GPU OCR service is not connected")
        request = {"event": "detect", "videoPath": video_path, "detectFps": detect_fps, "scanMode": scan_mode}
        if minimum_hits is not None:
            request["minimumHits"] = max(2, int(minimum_hits))
        request["requiredConsecutive"] = max(1, int(required_consecutive))
        request["minimumLineWidthRatio"] = max(0.0, min(1.0, float(minimum_line_width_ratio)))
        self.writer.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.writer.flush()
        while True:
            line = self.reader.readline()
            if not line:
                self._close_connection()
                raise RuntimeError("GPU OCR service disconnected during detection")
            event = json.loads(line)
            if event.get("event") == "progress":
                if progress:
                    progress(int(event.get("current") or 0), int(event.get("total") or 1), str(event.get("message") or "GPU OCR"))
                continue
            if event.get("event") == "result":
                if not event.get("success"):
                    raise RuntimeError(str(event.get("message") or "GPU OCR detection failed"))
                return event
