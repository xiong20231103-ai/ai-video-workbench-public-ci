"""
Video and media downloader for TikHub Downloader
支持多平台内容的下载，包括视频、图片、音频等多媒体内容
"""

import concurrent.futures
import json
import mimetypes
import os
import random
import shutil
import subprocess
import sys
import time
import threading
import traceback
from datetime import datetime

import httpx
from jinja2 import Template

from downloader.core.fallback_html_template import fallback_album_template, fallback_mixed_template
from downloader.constants import DEFAULT_VIDEO_HEADERS
from downloader.utils.logger import logger_instance
from downloader.utils.utils import sanitize_filename


class VideoDownloader:
    """Handles multimedia downloading functionality for various platforms"""

    POSTPROCESS_LOCK = threading.Lock()

    # HTML templates as class variables to avoid repetition
    ALBUM_PREVIEW_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ album_name }}</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f5f5;
        }
        h1 {
            text-align: center;
            color: #333;
        }
        .gallery {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
            grid-gap: 15px;
            margin-top: 20px;
        }
        .gallery img {
            width: 100%;
            height: auto;
            border-radius: 5px;
            box-shadow: 0 2px 5px rgba(0, 0, 0, 0.1);
            transition: transform 0.3s ease;
        }
        .gallery img:hover {
            transform: scale(1.03);
        }
        .info {
            text-align: center;
            margin-bottom: 20px;
            color: #666;
        }
        .desc {
            margin-top: 15px;
            padding: 10px;
            background-color: #fff;
            border-radius: 5px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
        }
    </style>
</head>
<body>
    <h1>{{ album_name }}</h1>
    <div class="info">
        <p>Platform: {{ platform }}</p>
        <p>Author: {{ author }}</p>
        <p>Album contains {{ image_files|length }} images</p>
    </div>
    {% if description %}
    <div class="desc">{{ description }}</div>
    {% endif %}
    <div class="gallery">
        {% for image in image_files %}
        <img src="{{ image }}" alt="{{ image }}">
        {% endfor %}
    </div>
</body>
</html>
"""

    MIXED_CONTENT_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ content_name }}</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f5f5;
        }
        h1, h2 {
            color: #333;
        }
        h1 {
            text-align: center;
        }
        .info {
            text-align: center;
            margin-bottom: 20px;
            color: #666;
        }
        .section {
            margin-top: 30px;
            padding: 15px;
            background-color: #fff;
            border-radius: 5px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
        }
        .gallery {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
            grid-gap: 15px;
            margin-top: 20px;
        }
        .gallery img {
            width: 100%;
            height: auto;
            border-radius: 5px;
            box-shadow: 0 2px 5px rgba(0, 0, 0, 0.1);
        }
        .media-item {
            margin: 10px 0;
            padding: 10px;
            background-color: #f9f9f9;
            border-radius: 5px;
        }
        .desc {
            margin-top: 15px;
            padding: 10px;
            background-color: #fff;
            border-radius: 5px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
        }
        video, audio {
            width: 100%;
            margin-top: 10px;
        }
        a {
            color: #0066cc;
            text-decoration: none;
        }
        a:hover {
            text-decoration: underline;
        }
    </style>
</head>
<body>
    <h1>{{ content_name }}</h1>
    <div class="info">
        <p>Platform: {{ platform }}</p>
        <p>Author: {{ author }}</p>
    </div>
    
    {% if description %}
    <div class="desc">{{ description }}</div>
    {% endif %}
    
    {% if videos %}
    <div class="section">
        <h2>Videos ({{ videos|length }})</h2>
        {% for video in videos %}
        <div class="media-item">
            <p>{{ video }}</p>
            <video controls>
                <source src="{{ video }}" type="video/mp4">
                Your browser does not support the video tag.
            </video>
            <p><a href="{{ video }}" download>Download Video</a></p>
        </div>
        {% endfor %}
    </div>
    {% endif %}
    
    {% if images %}
    <div class="section">
        <h2>Images ({{ images|length }})</h2>
        <div class="gallery">
            {% for image in images %}
            <a href="{{ image }}" target="_blank"><img src="{{ image }}" alt="{{ image }}"></a>
            {% endfor %}
        </div>
    </div>
    {% endif %}
    
    {% if audio %}
    <div class="section">
        <h2>Audio ({{ audio|length }})</h2>
        {% for audio_file in audio %}
        <div class="media-item">
            <p>{{ audio_file }}</p>
            <audio controls>
                <source src="{{ audio_file }}" type="audio/mpeg">
                Your browser does not support the audio tag.
            </audio>
            <p><a href="{{ audio_file }}" download>Download Audio</a></p>
        </div>
        {% endfor %}
    </div>
    {% endif %}
    
    {% if music %}
    <div class="section">
        <h2>Music ({{ music|length }})</h2>
        {% for music_file in music %}
        <div class="media-item">
            <p>{{ music_file }}</p>
            <audio controls>
                <source src="{{ music_file }}" type="audio/mpeg">
                Your browser does not support the audio tag.
            </audio>
            <p><a href="{{ music_file }}" download>Download Music</a></p>
        </div>
        {% endfor %}
    </div>
    {% endif %}
</body>
</html>
"""

    def __init__(
        self,
        download_path,
        use_description=False,
        skip_existing=True,
        max_workers=4,
        postprocess_enabled=False,
        trim_start_seconds=1.5,
        mirror_video=True,
        delete_original_after_processing=False,
        ffmpeg_path=None,
        subtitle_cover_enabled=False,
        subtitle_cover_workspace=None,
        subtitle_cover_detect_fps=1.5,
        subtitle_cover_min_score=0.38,
        subtitle_cover_mode="blur",
        subtitle_cover_blur_kernel=41,
        subtitle_cover_white_alpha=0.88,
        subtitle_ai_workspace=None,
        subtitle_ai_auto_detect=True,
        subtitle_ai_require_cuda=True,
        subtitle_ai_quality=18,
        subtitle_ai_area_cache=None,
        subtitle_ai_processor=None,
        pipeline_callback=None,
    ):
        """Initialize the downloader

        Args:
            download_path: Path to save downloaded media
            use_description: Whether to use content description as filename
            skip_existing: Whether to skip existing files
            max_workers: Maximum number of parallel downloads
        """
        self.download_path = download_path
        self.use_description = use_description
        self.skip_existing = skip_existing
        self.max_workers = max_workers  # New parameter for parallel downloads
        self.postprocess_enabled = postprocess_enabled
        self.trim_start_seconds = max(float(trim_start_seconds or 0), 0.0)
        self.mirror_video = mirror_video
        self.delete_original_after_processing = delete_original_after_processing
        self.ffmpeg_path = ffmpeg_path
        self.subtitle_cover_enabled = subtitle_cover_enabled
        self.subtitle_cover_workspace = subtitle_cover_workspace
        self.subtitle_cover_detect_fps = max(float(subtitle_cover_detect_fps or 1.5), 0.25)
        self.subtitle_cover_min_score = max(float(subtitle_cover_min_score or 0.38), 0.0)
        self.subtitle_cover_mode = subtitle_cover_mode or "blur"
        self.subtitle_cover_blur_kernel = max(int(subtitle_cover_blur_kernel or 41), 3)
        self.subtitle_cover_white_alpha = max(float(subtitle_cover_white_alpha or 0.88), 0.0)
        self.subtitle_ai_workspace = subtitle_ai_workspace
        self.subtitle_ai_auto_detect = bool(subtitle_ai_auto_detect)
        self.subtitle_ai_require_cuda = bool(subtitle_ai_require_cuda)
        self.subtitle_ai_quality = max(0, min(51, int(subtitle_ai_quality or 18)))
        self.subtitle_ai_area_cache = subtitle_ai_area_cache
        self.subtitle_ai_processor = subtitle_ai_processor
        self.pipeline_callback = pipeline_callback

        # Ensure download directory exists
        os.makedirs(self.download_path, exist_ok=True)

        # Set the logger
        self.logger = logger_instance

        # Initialize template engine
        self._init_templates()

        # Initialize MIME types
        mimetypes.init()

    def _emit_pipeline(self, stage, progress=None, message=None, status="running", detail=None):
        if not self.pipeline_callback:
            return
        try:
            self.pipeline_callback(
                {
                    "stage": stage,
                    "progress": progress,
                    "message": message,
                    "status": status,
                    "detail": detail,
                }
            )
        except Exception as e:
            self.logger.warning(f"Pipeline callback failed: {e}")

    def _guess_subtitle_cover_workspace(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        candidates = [
            self.subtitle_cover_workspace,
            os.path.join(repo_root, "SubtitleCoverGUI_Rewrite"),
            os.path.join(os.getcwd(), "SubtitleCoverGUI_Rewrite"),
            os.path.join(os.path.dirname(os.getcwd()), "SubtitleCoverGUI_Rewrite"),
        ]
        for candidate in candidates:
            if not candidate:
                continue
            if os.path.isfile(os.path.join(candidate, "subtitle_cover_runner.exe")):
                return os.path.abspath(candidate)
            if os.path.isfile(os.path.join(candidate, "subtitle_cover_runner", "subtitle_cover_runner.exe")):
                return os.path.abspath(candidate)
            if os.path.isfile(os.path.join(candidate, "automation_runner.py")):
                return os.path.abspath(candidate)
        return None

    def _resolve_subtitle_cover_command(self, workspace):
        runner_candidates = [
            os.path.join(workspace, "subtitle_cover_runner.exe"),
            os.path.join(workspace, "subtitle_cover_runner", "subtitle_cover_runner.exe"),
        ]
        for runner_executable in runner_candidates:
            if os.path.isfile(runner_executable):
                return [runner_executable], None

        runner_script = os.path.join(workspace, "automation_runner.py")
        if not os.path.isfile(runner_script):
            return None, "SubtitleCover ???????: automation_runner.py / subtitle_cover_runner.exe"

        bundled_python = os.path.join(workspace, ".subtitlecover_env", "Scripts", "python.exe")
        if os.path.isfile(bundled_python):
            return [bundled_python, runner_script], None

        current_executable = sys.executable or ""
        current_name = os.path.basename(current_executable).lower()
        if current_name.startswith("python") and os.path.isfile(current_executable):
            return [current_executable, runner_script], None

        path_python = shutil.which("python.exe") or shutil.which("python")
        if path_python:
            return [path_python, runner_script], None

        return None, "SubtitleCover ????? Python ?????????? subtitle_cover_runner.exe ????"

    def _run_video_pipeline(self, file_path):
        warnings = []
        current_path = file_path

        self._emit_pipeline("download", 100, f"下载完成: {os.path.basename(file_path)}", status="done")
        if self.postprocess_enabled or self.subtitle_cover_enabled:
            self._emit_pipeline("queue", None, "等待进入后台处理队列", status="running")

        with self.POSTPROCESS_LOCK:
            if self.postprocess_enabled or self.subtitle_cover_enabled:
                self._emit_pipeline("queue", None, "已进入后台处理队列", status="done")

            processed_file = self._postprocess_video_if_needed(file_path)
            if processed_file:
                current_path = processed_file
            elif self.postprocess_enabled:
                warnings.append(f"FFmpeg 后处理失败，保留原文件: {os.path.basename(file_path)}")

            covered_file, cover_warning, cover_created = self._run_subtitle_cover_if_needed(current_path)
        if covered_file:
            if self.delete_original_after_processing and cover_created and os.path.abspath(covered_file) != os.path.abspath(current_path):
                try:
                    os.remove(current_path)
                    self.logger.info(f"Removed intermediate file after SubtitleCover success: {current_path}")
                except Exception as e:
                    warnings.append(f"最终成品已生成，但删除中间文件失败: {os.path.basename(current_path)} ({e})")
            current_path = covered_file
        elif cover_warning:
            warnings.append(cover_warning)

        return current_path, warnings

    def _run_subtitle_cover_if_needed(self, file_path):
        if not self.subtitle_cover_enabled:
            self._emit_pipeline("blur", 100, "未启用模糊处理，跳过", status="skipped")
            return None, None, False

        if self.subtitle_cover_mode in ("ai-sttn", "ai-lama", "fast-blur"):
            return self._run_ai_subtitle_if_needed(file_path)

        if not file_path or not os.path.exists(file_path):
            return None, f"SubtitleCover 未执行，文件不存在: {file_path}", False

        workspace = self._guess_subtitle_cover_workspace()
        if not workspace:
            return None, "SubtitleCover 未执行，找不到 SubtitleCoverGUI_Rewrite 目录", False

        command_prefix, resolve_error = self._resolve_subtitle_cover_command(workspace)
        if not command_prefix:
            return None, resolve_error, False

        file_root, file_ext = os.path.splitext(file_path)
        output_path = f"{file_root}_covered{file_ext or '.mp4'}"
        if self.skip_existing and os.path.exists(output_path):
            self.logger.info(f"Skipping existing SubtitleCover output: {output_path}")
            self._emit_pipeline("blur", 100, f"复用已有模糊结果: {os.path.basename(output_path)}", status="done")
            return output_path, None, False

        command = [
            *command_prefix,
            "--input",
            file_path,
            "--output",
            output_path,
            "--workspace",
            workspace,
            "--detect-fps",
            str(self.subtitle_cover_detect_fps),
            "--min-score",
            str(self.subtitle_cover_min_score),
            "--mode",
            str(self.subtitle_cover_mode),
            "--blur-kernel",
            str(self.subtitle_cover_blur_kernel),
            "--white-alpha",
            str(self.subtitle_cover_white_alpha),
        ]

        self._emit_pipeline("blur", 0, "开始模糊处理", status="running")

        try:
            popen_kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "env": {
                    **os.environ,
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1",
                },
            }
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                popen_kwargs["startupinfo"] = startupinfo
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            process = subprocess.Popen(
                command,
                **popen_kwargs,
            )
        except Exception as e:
            self.logger.error(f"Failed to launch SubtitleCover automation: {e}")
            return None, f"SubtitleCover 启动失败: {e}", False

        payload = None
        stdout_lines = []
        if process.stdout:
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                stdout_lines.append(line)
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    self.logger.warning(f"SubtitleCover returned non-JSON output: {line[-500:]}")
                    continue

                event_type = event.get("event")
                if event_type == "progress":
                    total = max(int(event.get("total") or 0), 1)
                    current = int(event.get("current") or 0)
                    percent = max(0, min(100, int((current / total) * 100)))
                    self._emit_pipeline(
                        "blur",
                        percent,
                        event.get("message") or "模糊处理中",
                        status="running",
                        detail=event.get("stage"),
                    )
                elif event_type == "log":
                    self._emit_pipeline(
                        "blur",
                        None,
                        event.get("message"),
                        status="running",
                        detail=event.get("level"),
                    )
                elif event_type == "result":
                    payload = event

        stderr_text = ""
        if process.stderr:
            stderr_text = process.stderr.read().strip()
        completed_returncode = process.wait()

        if completed_returncode == 0 and payload and payload.get("success"):
            final_path = payload.get("output_path") or output_path
            self.logger.info(f"SubtitleCover output created: {final_path}")
            self._emit_pipeline("blur", 100, f"模糊处理完成: {os.path.basename(final_path)}", status="done")
            return final_path, None, True

        message = None
        if payload:
            message = payload.get("message") or payload.get("traceback")
        if not message:
            joined_stdout = "\n".join(stdout_lines).strip()
            message = (stderr_text or joined_stdout or "Unknown error").strip()

        if self._is_no_subtitle_detected_message(message):
            self.logger.info(f"SubtitleCover skipped because no stable subtitle band was found: {file_path}")
            self._emit_pipeline("blur", 100, "未检测到稳定字幕，已自动跳过模糊处理", status="skipped")
            return file_path, None, False

        self.logger.warning(f"SubtitleCover automation did not produce output for {file_path}: {message}")
        self._emit_pipeline("blur", None, message, status="error")
        return None, f"SubtitleCover 未成功处理 {os.path.basename(file_path)}: {message}", False

    def _is_no_subtitle_detected_message(self, message):
        if not message:
            return False

        normalized = str(message).strip()
        known_markers = (
            "未检测到稳定中文字幕带",
            "未检测到稳定字幕带",
            "未检测到稳定字幕",
        )
        return any(marker in normalized for marker in known_markers)

    def _guess_ai_subtitle_workspace(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        executable_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates = [
            self.subtitle_ai_workspace,
            os.environ.get("TIKHUB_AI_SUBTITLE_WORKSPACE"),
            os.path.join(executable_dir, "AI_Subtitle_Backend"),
            os.path.join(executable_dir, "vsr_optimized"),
            os.path.abspath(os.path.join(executable_dir, "..", "vsr_optimized")),
            os.path.abspath(os.path.join(executable_dir, "..", "..", "vsr_optimized")),
            os.path.join(repo_root, "vsr_optimized"),
            os.path.join(repo_root, "AI_Subtitle_Backend"),
            os.path.join(os.getcwd(), "AI_Subtitle_Backend"),
        ]
        for candidate in candidates:
            if not candidate:
                continue
            if os.path.isfile(os.path.join(candidate, "ai_subtitle_runner.exe")):
                return os.path.abspath(candidate)
            if os.path.isfile(os.path.join(candidate, "automation_runner.py")):
                return os.path.abspath(candidate)
        return None

    def _resolve_ai_subtitle_command(self, workspace):
        executable = os.path.join(workspace, "ai_subtitle_runner.exe")
        if os.path.isfile(executable):
            return [executable], None
        script = os.path.join(workspace, "automation_runner.py")
        if not os.path.isfile(script):
            return None, "AI subtitle backend is missing automation_runner.py"
        portable_root = os.path.dirname(workspace)
        portable_base_python = os.path.join(portable_root, "python_runtime", "python.exe")
        portable_venv = os.path.join(portable_root, ".cuda_env")
        portable_venv_python = os.path.join(portable_venv, "Scripts", "python.exe")
        if os.path.isfile(portable_base_python) and os.path.isfile(portable_venv_python):
            # Repair absolute venv paths after the portable folder is moved.
            try:
                portable_cfg = os.path.join(portable_venv, "pyvenv.cfg")
                with open(portable_cfg, "w", encoding="utf-8") as stream:
                    stream.write(
                        f"home = {os.path.dirname(portable_base_python)}\n"
                        "include-system-site-packages = false\n"
                        "version = 3.12.2\n"
                        f"executable = {portable_base_python}\n"
                        f"command = {portable_base_python} -m venv {portable_venv}\n"
                    )
            except Exception:
                self.logger.exception("Could not repair portable CUDA venv paths")

        python_candidates = [
            portable_venv_python,
            os.path.join(workspace, ".cuda_env", "Scripts", "python.exe"),
            os.path.join(os.path.dirname(workspace), ".cuda_env", "Scripts", "python.exe"),
        ]
        current_executable = sys.executable or ""
        if os.path.basename(current_executable).lower().startswith("python"):
            python_candidates.append(current_executable)
        for python_path in python_candidates:
            if python_path and os.path.isfile(python_path):
                return [python_path, script], None
        return None, "AI subtitle CUDA Python runtime was not found"

    def _run_ai_subtitle_if_needed(self, file_path):
        if not file_path or not os.path.exists(file_path):
            return None, f"AI subtitle processing skipped; file not found: {file_path}", False
        if self.subtitle_ai_processor is not None:
            return self.subtitle_ai_processor(file_path, self)
        workspace = self._guess_ai_subtitle_workspace()
        if not workspace:
            return None, "AI subtitle backend workspace was not found", False
        command_prefix, resolve_error = self._resolve_ai_subtitle_command(workspace)
        if not command_prefix:
            return None, resolve_error, False

        mode_map = {"ai-sttn": "sttn", "ai-lama": "lama", "fast-blur": "blur"}
        backend_mode = mode_map[self.subtitle_cover_mode]
        cached_area = self._scaled_cached_subtitle_area(file_path)
        file_root, file_ext = os.path.splitext(file_path)
        suffix = {"sttn": "_ai_sttn", "lama": "_ai_lama", "blur": "_blur"}[backend_mode]
        output_path = f"{file_root}{suffix}{file_ext or '.mp4'}"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        log_dir = os.path.join(os.path.dirname(file_path), ".subtitle_ai_logs", f"{os.path.basename(file_root)}_{timestamp}")
        os.makedirs(log_dir, exist_ok=False)
        raw_stdout_path = os.path.join(log_dir, "stdout.jsonl")
        stderr_path = os.path.join(log_dir, "stderr.log")
        manifest_path = os.path.join(log_dir, "run.json")
        manifest = {
            "status": "running",
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "input": file_path,
            "output": output_path,
            "mode": backend_mode,
            "workspace": workspace,
            "auto_detect": self.subtitle_ai_auto_detect,
            "cached_subtitle_area": cached_area,
            "require_cuda": self.subtitle_ai_require_cuda,
            "quality": self.subtitle_ai_quality,
        }
        with open(manifest_path, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)

        command = [
            *command_prefix,
            "--input", file_path,
            "--output", output_path,
            "--mode", backend_mode,
            "--quality", str(self.subtitle_ai_quality),
            "--detect-fps", str(self.subtitle_cover_detect_fps),
        ]
        if cached_area:
            command.extend(["--subtitle-area", *(str(value) for value in cached_area)])
        elif self.subtitle_ai_auto_detect:
            command.append("--auto-detect")
        if self.subtitle_ai_require_cuda:
            command.append("--require-cuda")

        self._emit_pipeline("blur", 0, f"Starting {backend_mode} subtitle processing", status="running")
        payload = None
        stdout_lines = []
        stderr_text = ""
        returncode = None
        started = time.perf_counter()
        try:
            popen_kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "env": {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            }
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                popen_kwargs["startupinfo"] = startupinfo
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            process = subprocess.Popen(command, **popen_kwargs)
            with open(raw_stdout_path, "w", encoding="utf-8") as raw_log:
                if process.stdout:
                    for raw_line in process.stdout:
                        raw_log.write(raw_line)
                        raw_log.flush()
                        line = raw_line.strip()
                        if not line:
                            continue
                        stdout_lines.append(line)
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            safe_line = line[-500:].encode("gbk", "replace").decode("gbk") if os.name == "nt" else line[-500:]
                            self.logger.info("AI subtitle output: %s", safe_line)
                            continue
                        if event.get("event") == "progress":
                            total = max(int(event.get("total") or 100), 1)
                            current = int(event.get("current") or 0)
                            percent = max(0, min(100, int(current / total * 100)))
                            self._emit_pipeline("blur", percent, event.get("message"), status="running", detail=event.get("stage"))
                        elif event.get("event") == "detection" and event.get("area"):
                            width, height = self._probe_video_dimensions(file_path)
                            self.subtitle_ai_area_cache = {
                                "area": [int(value) for value in event["area"]],
                                "width": width,
                                "height": height,
                                "confidence": event.get("confidence"),
                                "sampled_frames": event.get("sampled_frames"),
                            }
                            manifest["detection"] = self.subtitle_ai_area_cache
                        elif event.get("event") == "result":
                            payload = event
            if process.stderr:
                stderr_text = process.stderr.read()
            returncode = process.wait()
            with open(stderr_path, "w", encoding="utf-8") as stream:
                stream.write(stderr_text)
            success = returncode == 0 and payload and payload.get("success") and os.path.exists(output_path)
            manifest.update(
                {
                    "status": "succeeded" if success else "failed",
                    "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                    "returncode": returncode,
                    "result": payload,
                    "command": command,
                }
            )
            with open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
            if success:
                self._emit_pipeline("blur", 100, f"Subtitle processing complete: {os.path.basename(output_path)}", status="done")
                return output_path, None, True
            message = (payload or {}).get("message") if payload else None
            message = message or stderr_text or "\n".join(stdout_lines[-20:]) or "Unknown AI subtitle error"
            self._emit_pipeline("blur", None, message, status="error")
            return None, f"AI subtitle processing failed: {message}", False
        except Exception as exc:
            manifest.update(
                {
                    "status": "failed",
                    "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                    "returncode": returncode,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                    "command": command,
                }
            )
            with open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
            self.logger.exception("AI subtitle processing failed")
            return None, f"AI subtitle processing failed: {exc}", False

    def _probe_video_dimensions(self, file_path):
        ffprobe = self._find_ffprobe()
        if not ffprobe:
            return None, None
        try:
            completed = subprocess.run(
                [
                    ffprobe,
                    "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height",
                    "-of", "json",
                    file_path,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            streams = json.loads(completed.stdout or "{}").get("streams", [])
            if completed.returncode == 0 and streams:
                return int(streams[0]["width"]), int(streams[0]["height"])
        except Exception:
            self.logger.exception("Could not probe video dimensions for subtitle-area cache")
        return None, None

    def _scaled_cached_subtitle_area(self, file_path):
        cache = self.subtitle_ai_area_cache
        if not isinstance(cache, dict) or not cache.get("area"):
            return None
        source_width = cache.get("width")
        source_height = cache.get("height")
        current_width, current_height = self._probe_video_dimensions(file_path)
        area = [int(value) for value in cache["area"]]
        if not all((source_width, source_height, current_width, current_height)):
            return area
        ymin, ymax, xmin, xmax = area
        scale_y = current_height / source_height
        scale_x = current_width / source_width
        return [
            int(round(ymin * scale_y)),
            int(round(ymax * scale_y)),
            int(round(xmin * scale_x)),
            int(round(xmax * scale_x)),
        ]

    def create_ai_subtitle_preview(self, file_path, seconds=8):
        """Create and process a short copy without ever modifying the original video."""
        if not file_path or not os.path.isfile(file_path):
            return None, f"Preview source was not found: {file_path}"
        ffmpeg = self._find_ffmpeg()
        if not ffmpeg:
            return None, "FFmpeg was not found; cannot create the first-video preview"

        seconds = max(3, min(30, int(seconds or 8)))
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        preview_dir = os.path.join(os.path.dirname(file_path), ".subtitle_ai_preview", timestamp)
        os.makedirs(preview_dir, exist_ok=False)
        stem = os.path.splitext(os.path.basename(file_path))[0]
        clip_path = os.path.join(preview_dir, f"{stem}_source_{seconds}s.mp4")
        manifest_path = os.path.join(preview_dir, "preview.json")
        manifest = {
            "status": "creating_clip",
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "original_input": file_path,
            "clip": clip_path,
            "seconds": seconds,
            "mode": self.subtitle_cover_mode,
            "original_will_not_be_modified": True,
        }
        with open(manifest_path, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)

        command = [
            ffmpeg,
            "-y",
            "-ss", "0",
            "-t", str(seconds),
            "-i", file_path,
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            clip_path,
        ]
        try:
            run_kwargs = {
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "check": False,
            }
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                run_kwargs["startupinfo"] = startupinfo
                run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            completed = subprocess.run(command, **run_kwargs)
            manifest["clip_command"] = command
            manifest["clip_returncode"] = completed.returncode
            manifest["clip_stdout"] = completed.stdout
            manifest["clip_stderr"] = completed.stderr
            if completed.returncode != 0 or not os.path.isfile(clip_path):
                manifest["status"] = "failed"
                manifest["error"] = completed.stderr or "Preview clip was not created"
                return None, manifest["error"]

            manifest["status"] = "processing_preview"
            with open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
            preview_output, warning, _created = self._run_ai_subtitle_if_needed(clip_path)
            manifest["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            manifest["processed_preview"] = preview_output
            manifest["status"] = "succeeded" if preview_output else "failed"
            manifest["error"] = warning
            return preview_output, warning
        except Exception as exc:
            manifest["status"] = "failed"
            manifest["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            manifest["error"] = str(exc)
            manifest["traceback"] = traceback.format_exc()
            return None, str(exc)
        finally:
            with open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)

    def _init_templates(self):
        """Initialize Jinja2 templates"""
        try:
            self.album_template = Template(self.ALBUM_PREVIEW_TEMPLATE)
            self.mixed_template = Template(self.MIXED_CONTENT_TEMPLATE)
            self.logger.info("Templates initialized successfully")
        except Exception as e:
            self.logger.error(f"Failed to initialize templates: {e}")
            self.album_template = None
            self.mixed_template = None

    def parallel_download(self, items, progress_callback=None):
        """Download multiple items in parallel

        Args:
            items: List of data dictionaries to download
            progress_callback: Optional callback for progress updates

        Returns:
            dict: Results with success, files, and errors
        """
        result = {
            "success": False,
            "files": [],
            "errors": []
        }

        if not items:
            return result

        total = len(items)
        completed = 0

        # Function to handle individual download with progress reporting
        def download_item(item_data):
            try:
                # Create custom progress callback for this item
                item_progress = None
                if progress_callback:
                    def item_progress(p, t):
                        # We can't update the UI from worker threads, so we just track progress
                        pass

                # Download the item
                item_result = self.main_downloader(
                    item_data,
                    None,  # Use default output directory
                    item_progress,
                    3  # Default max retries
                )

                return item_result
            except Exception as e:
                self.logger.error(f"Error in parallel download worker: {e}")
                return {"success": False, "files": [], "errors": [str(e)]}

        # Use thread pool for parallel downloads
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_item = {executor.submit(download_item, item): item for item in items}

            # Process results as they complete
            for i, future in enumerate(concurrent.futures.as_completed(future_to_item)):
                try:
                    item_result = future.result()
                    result['files'].extend(item_result['files'])
                    result['errors'].extend(item_result['errors'])

                    # Update overall progress
                    completed += 1
                    if progress_callback:
                        progress_callback(completed, total)

                except Exception as e:
                    result['errors'].append(str(e))
                    completed += 1
                    if progress_callback:
                        progress_callback(completed, total)

        # Set success flag if any files were downloaded
        result['success'] = len(result['files']) > 0

        return result

    def main_downloader(self, data, output_dir=None, progress_callback=None, max_retries=3):
        """Universal media downloader that handles all media types

        Args:
            data: Standardized content information dictionary
            output_dir: Optional custom output directory
            progress_callback: Optional callback for progress updates
            max_retries: Maximum number of retry attempts

        Returns:
            dict: Results containing success, file paths and any errors
        """
        # Use specified output directory or default
        output_dir = output_dir or self.download_path
        os.makedirs(output_dir, exist_ok=True)

        # Initialize results
        result = {
            "success": False,
            "files": [],
            "errors": []
        }

        try:
            # Validate essential data fields
            if not data.get('id'):
                raise ValueError("Missing required field: 'id'")

            # Smart media type detection - determine media type from available URLs if not provided
            if not data.get('media_type'):
                media_type = self._detect_media_type(data)
                if not media_type:
                    raise ValueError("Missing required field: 'media_type' and couldn't determine from data")
                data['media_type'] = media_type

            # Create author directory if needed
            if self.use_description and data.get('author_name'):
                safe_author = sanitize_filename(data['author_name'], max_length=50)
                output_dir = os.path.join(output_dir, safe_author)
                os.makedirs(output_dir, exist_ok=True)

            # Process based on media type using unified interface
            self._process_content(data, output_dir, progress_callback, max_retries, result)

            # Set success flag if any files were downloaded
            result['success'] = len(result['files']) > 0

            # Final progress update
            if progress_callback:
                progress_callback(100, 100)

            return result

        except Exception as e:
            error_msg = f"Error in main_downloader: {str(e)}"
            self.logger.error(error_msg)
            result['errors'].append(error_msg)
            return result

    def _detect_media_type(self, data):
        """Intelligently detect media type from data structure

        Args:
            data: Content data dictionary

        Returns:
            str: Detected media type or None if can't determine
        """
        # Check for multiple media types (mixed content)
        media_types = []
        if data.get('video_urls'):
            media_types.append('video')
        if data.get('image_urls'):
            media_types.append('image')
        if data.get('audio_urls'):
            media_types.append('audio')

        # If multiple types detected, it's mixed content
        if len(media_types) > 1:
            return 'mixed'
        elif len(media_types) == 1:
            return media_types[0]

        # Check platform-specific patterns as fallback
        platform = data.get('platform', '').lower()
        if platform in ['tiktok', 'douyin']:
            # These platforms typically have videos
            return 'video'
        elif platform in ['xiaohongshu']:
            # Red Book can have either images or videos
            return 'mixed'
        elif platform in ['bilibili'] and data.get('music_urls'):
            return 'audio'

        # Can't determine
        return None

    def _process_content(self, data, output_dir, progress_callback, max_retries, result):
        """Unified content processing method that handles all media types

        Args:
            data: Content data dictionary
            output_dir: Directory to save files
            progress_callback: Progress reporting callback
            max_retries: Maximum retry attempts
            result: Result dictionary to be updated
        """
        media_type = data['media_type']

        # Dispatch to appropriate handler based on media type
        if media_type == 'video':
            self._process_video(data, output_dir, progress_callback, max_retries, result)
        elif media_type == 'image':
            self._process_image(data, output_dir, progress_callback, max_retries, result)
        elif media_type == 'audio':
            self._process_audio(data, output_dir, progress_callback, max_retries, result)
        elif media_type == 'mixed':
            self._process_mixed(data, output_dir, progress_callback, max_retries, result)
        else:
            raise ValueError(f"Unsupported media type: {media_type}")

    def _process_video(self, data, output_dir, progress_callback, max_retries, result):
        """Process video type content

        Args:
            data: Content data dictionary
            output_dir: Directory to save files
            progress_callback: Progress reporting callback
            max_retries: Maximum retry attempts
            result: Result dictionary to be updated
        """
        # Check for video URLs
        if not data.get('video_urls'):
            error_msg = "No video URLs found for video content"
            self.logger.error(error_msg)
            result['errors'].append(error_msg)
            return

        # For multiple videos, use parallel downloading
        video_urls = data['video_urls']
        if len(video_urls) > 1 and self.max_workers > 1:
            self._parallel_process_videos(data, video_urls, output_dir, progress_callback, max_retries, result)
        else:
            # Sequential download for single video or if parallel disabled
            for idx, video_url in enumerate(video_urls):
                if not video_url or not isinstance(video_url, str) or not video_url.startswith('http'):
                    self.logger.warning(f"Invalid video URL: {video_url}")
                    continue

                file_path = self._download_media_file(
                    data,
                    video_url,
                    output_dir,
                    '.mp4',  # Default extension, will be updated based on content
                    idx if len(video_urls) > 1 else None,
                    progress_callback,
                    max_retries
                )

                if file_path:
                    final_file, warnings = self._run_video_pipeline(file_path)
                    result['files'].append(final_file or file_path)
                    result['errors'].extend(warnings)
                    self.logger.info(f"Successfully downloaded video to {final_file or file_path}")

    def _parallel_process_videos(self, data, video_urls, output_dir, progress_callback, max_retries, result):
        """Process multiple videos in parallel

        Args:
            data: Content data dictionary
            video_urls: List of video URLs
            output_dir: Directory to save files
            progress_callback: Progress reporting callback
            max_retries: Maximum retry attempts
            result: Result dictionary to be updated
        """
        valid_urls = [url for url in video_urls if url and isinstance(url, str) and url.startswith('http')]
        total_videos = len(valid_urls)

        if not valid_urls:
            self.logger.warning("No valid video URLs found")
            return

        completed = 0

        # Create a progress tracker for overall progress
        def update_progress():
            nonlocal completed
            completed += 1
            if progress_callback:
                progress_callback(min(int((completed / total_videos) * 100), 99), 100)

        # Function to download a single video
        def download_video(idx, url):
            try:
                file_path = self._download_media_file(
                    data,
                    url,
                    output_dir,
                    '.mp4',  # Default extension, will be updated based on content
                    idx if total_videos > 1 else None,
                    None,  # No individual progress callback for parallel downloads
                    max_retries
                )

                update_progress()
                if not file_path:
                    return None, []
                final_file, warnings = self._run_video_pipeline(file_path)
                return final_file or file_path, warnings
            except Exception as e:
                self.logger.error(f"Error downloading video {idx}: {e}")
                update_progress()
                return None, []

        # Initialize progress
        if progress_callback:
            progress_callback(0, 100)

        # Use thread pool for parallel downloads
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.max_workers, total_videos)) as executor:
            # Submit all download tasks
            future_to_idx = {executor.submit(download_video, idx, url): idx
                            for idx, url in enumerate(valid_urls)}

            # Collect results as they complete
            for future in concurrent.futures.as_completed(future_to_idx):
                file_path, warnings = future.result()
                if file_path:
                    result['files'].append(file_path)
                result['errors'].extend(warnings)

        # Final progress update
        if progress_callback:
            progress_callback(100, 100)

    def _process_image(self, data, output_dir, progress_callback, max_retries, result):
        """Process image type content with enhanced parallel downloading for albums

        Args:
            data: Content data dictionary
            output_dir: Directory to save files
            progress_callback: Progress reporting callback
            max_retries: Maximum retry attempts
            result: Result dictionary to be updated
        """
        self.logger.info(f"Processing image content with {len(data.get('image_urls', []))} images")

        # Check for image URLs
        if not data.get('image_urls'):
            error_msg = "No image URLs found for image content"
            self.logger.error(error_msg)
            result['errors'].append(error_msg)
            return

        # Handle multiple images (album)
        if len(data['image_urls']) > 1:
            album_name = self._get_content_name(data)
            album_dir = os.path.join(output_dir, album_name)
            os.makedirs(album_dir, exist_ok=True)

            # Always use parallel downloading for multiple images
            # Adjust worker count based on image count for better efficiency
            effective_workers = min(self.max_workers, len(data['image_urls']))
            self.logger.info(f"Using {effective_workers} workers for {len(data['image_urls'])} images")

            self._parallel_process_images(data, data['image_urls'], album_dir, progress_callback, max_retries, result)

            # Create HTML preview for multiple images if any were downloaded
            if result['files']:
                preview_path = self._create_album_preview(album_dir, data)
                if preview_path:
                    result['files'].append(preview_path)

        else:
            # Single image download
            image_url = data['image_urls'][0]
            if not image_url or not isinstance(image_url, str) or not image_url.startswith('http'):
                error_msg = f"Invalid image URL: {image_url}"
                self.logger.error(error_msg)
                result['errors'].append(error_msg)
                return

            file_path = self._download_media_file(
                data,
                image_url,
                output_dir,
                self._determine_file_extension(image_url, '.jpg'),
                None,
                progress_callback,
                max_retries
            )

            if file_path:
                result['files'].append(file_path)
                self.logger.info(f"Successfully downloaded image to {file_path}")

    def _parallel_process_images(self, data, image_urls, output_dir, progress_callback, max_retries, result):
        """Enhanced parallel processing for multiple images with better resource utilization

        Args:
            data: Content data dictionary
            image_urls: List of image URLs
            output_dir: Directory to save files
            progress_callback: Progress reporting callback
            max_retries: Maximum retry attempts
            result: Result dictionary to be updated
        """
        # Filter invalid URLs first
        valid_urls = [url for url in image_urls if url and isinstance(url, str) and url.startswith('http')]
        total_images = len(valid_urls)

        if not valid_urls:
            self.logger.warning("No valid image URLs found")
            return

        completed = 0
        download_results = []

        # Create a thread-safe progress tracker that prevents UI updates too frequently
        # to avoid overwhelming the main thread
        last_update_time = [0]  # Use list for mutable reference in nested function
        min_update_interval = 0.1  # Minimum seconds between progress updates

        def update_progress():
            nonlocal completed
            completed += 1

            # Throttle UI updates to prevent UI freezing
            current_time = time.time()
            if progress_callback and (
                    current_time - last_update_time[0] >= min_update_interval or completed == total_images):
                progress = min(int((completed / total_images) * 100), 99)
                progress_callback(progress, 100)
                last_update_time[0] = current_time

        # Function to download a single image with better error handling
        def download_image(idx, url):
            try:
                # Determine image extension
                ext = self._determine_file_extension(url, '.jpg')

                # Unique filename based on index
                file_path = self._download_media_file(
                    data,
                    url,
                    output_dir,
                    ext,
                    idx,
                    None,  # No individual progress callback for parallel downloads
                    max_retries
                )

                update_progress()
                return idx, file_path, None  # Success
            except Exception as e:
                self.logger.error(f"Error downloading image {idx}: {e}")
                update_progress()
                return idx, None, str(e)  # Error

        # Initialize progress
        if progress_callback:
            progress_callback(0, 100)

        # Calculate optimal batch size to prevent overwhelming the system
        # This is important for very large image sets
        batch_size = 20  # Default batch size
        max_workers = min(self.max_workers, 8)  # Cap workers at a reasonable number

        # Process in batches for very large image sets
        if total_images > 100:
            self.logger.info(f"Processing {total_images} images in batches of {batch_size}")

            for batch_start in range(0, total_images, batch_size):
                batch_end = min(batch_start + batch_size, total_images)
                batch = valid_urls[batch_start:batch_end]

                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = [executor.submit(download_image, batch_start + i, url) for i, url in enumerate(batch)]

                    for future in concurrent.futures.as_completed(futures):
                        idx, file_path, error = future.result()
                        if file_path:
                            download_results.append((idx, file_path))
                        elif error:
                            result['errors'].append(f"Image {idx}: {error}")

                # Short pause between batches to allow system resources to recover
                time.sleep(0.2)
        else:
            # For smaller image sets, process all at once
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(download_image, idx, url) for idx, url in enumerate(valid_urls)]

                for future in concurrent.futures.as_completed(futures):
                    idx, file_path, error = future.result()
                    if file_path:
                        download_results.append((idx, file_path))
                    elif error:
                        result['errors'].append(f"Image {idx}: {error}")

        # Sort results by index to maintain original order
        download_results.sort(key=lambda x: x[0])

        # Add successfully downloaded files to result
        for _, file_path in download_results:
            result['files'].append(file_path)

        # Final progress update
        if progress_callback:
            progress_callback(100, 100)

        self.logger.info(f"Parallel download completed: {len(download_results)} of {total_images} images downloaded")

    def _process_audio(self, data, output_dir, progress_callback, max_retries, result):
        """Process audio type content with parallel capability

        Args:
            data: Content data dictionary
            output_dir: Directory to save files
            progress_callback: Progress reporting callback
            max_retries: Maximum retry attempts
            result: Result dictionary to be updated
        """
        # Check for audio URLs
        if not data.get('audio_urls'):
            error_msg = "No audio URLs found for audio content"
            self.logger.error(error_msg)
            result['errors'].append(error_msg)
            return

        # For multiple audio files, use parallel downloading
        audio_urls = data['audio_urls']
        if len(audio_urls) > 1 and self.max_workers > 1:
            self._parallel_process_audio(data, audio_urls, output_dir, progress_callback, max_retries, result)
        else:
            # Sequential download
            for idx, audio_url in enumerate(audio_urls):
                if not audio_url or not isinstance(audio_url, str) or not audio_url.startswith('http'):
                    self.logger.warning(f"Invalid audio URL: {audio_url}")
                    continue

                # Determine audio extension
                ext = self._determine_file_extension(audio_url, '.mp3')

                file_path = self._download_media_file(
                    data,
                    audio_url,
                    output_dir,
                    ext,
                    idx if len(audio_urls) > 1 else None,
                    progress_callback,
                    max_retries
                )

                if file_path:
                    result['files'].append(file_path)
                    self.logger.info(f"Successfully downloaded audio to {file_path}")

    def _parallel_process_audio(self, data, audio_urls, output_dir, progress_callback, max_retries, result):
        """Process multiple audio files in parallel

        Args:
            data: Content data dictionary
            audio_urls: List of audio URLs
            output_dir: Directory to save files
            progress_callback: Progress reporting callback
            max_retries: Maximum retry attempts
            result: Result dictionary to be updated
        """
        # Implementation similar to _parallel_process_videos but for audio files
        valid_urls = [url for url in audio_urls if url and isinstance(url, str) and url.startswith('http')]
        total_audio = len(valid_urls)

        if not valid_urls:
            self.logger.warning("No valid audio URLs found")
            return

        completed = 0

        # Create a progress tracker for overall progress
        def update_progress():
            nonlocal completed
            completed += 1
            if progress_callback:
                progress_callback(min(int((completed / total_audio) * 100), 99), 100)

        # Function to download a single audio file
        def download_audio(idx, url):
            try:
                # Determine audio extension
                ext = self._determine_file_extension(url, '.mp3')

                file_path = self._download_media_file(
                    data,
                    url,
                    output_dir,
                    ext,
                    idx if total_audio > 1 else None,
                    None,  # No individual progress callback for parallel downloads
                    max_retries
                )

                update_progress()
                return file_path
            except Exception as e:
                self.logger.error(f"Error downloading audio {idx}: {e}")
                update_progress()
                return None

        # Initialize progress
        if progress_callback:
            progress_callback(0, 100)

        # Use thread pool for parallel downloads
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.max_workers, total_audio)) as executor:
            # Submit all download tasks
            future_to_idx = {executor.submit(download_audio, idx, url): idx
                            for idx, url in enumerate(valid_urls)}

            # Collect results as they complete
            for future in concurrent.futures.as_completed(future_to_idx):
                file_path = future.result()
                if file_path:
                    result['files'].append(file_path)

        # Final progress update
        if progress_callback:
            progress_callback(100, 100)

    def _process_mixed(self, data, output_dir, progress_callback, max_retries, result):
        """Process mixed type content with parallel downloads for different media types

        Args:
            data: Content data dictionary
            output_dir: Directory to save files
            progress_callback: Progress reporting callback
            max_retries: Maximum retry attempts
            result: Result dictionary to be updated
        """
        # Create a directory for mixed content
        mixed_dir = os.path.join(output_dir, self._get_content_name(data))
        os.makedirs(mixed_dir, exist_ok=True)

        # Count total items to download for progress calculation
        total_items = 0
        total_items += len(data.get('video_urls', []))
        total_items += len(data.get('image_urls', []))
        total_items += len(data.get('audio_urls', []))
        total_items += len(data.get('music_urls', [])) if data.get('music_id') else 0

        if total_items == 0:
            error_msg = "No media URLs found in mixed content"
            self.logger.error(error_msg)
            result['errors'].append(error_msg)
            return

        # For mixed content with multiple files, use parallel downloading if enabled
        if total_items > 1 and self.max_workers > 1:
            self._parallel_process_mixed(data, mixed_dir, progress_callback, max_retries, result)
        else:
            # Sequential processing
            current_item = 0

            # Helper function to update progress for mixed content
            def update_mixed_progress(p, t):
                if progress_callback:
                    item_portion = 100 / total_items
                    overall = int(((current_item * item_portion) + (p * item_portion / t)))
                    progress_callback(min(overall, 99), 100)  # Cap at 99% until completely done

            # Process each media type sequentially
            # Process videos
            if data.get('video_urls'):
                for idx, video_url in enumerate(data['video_urls']):
                    if not video_url or not isinstance(video_url, str) or not video_url.startswith('http'):
                        self.logger.warning(f"Invalid video URL: {video_url}")
                        current_item += 1
                        continue

                    file_path = self._download_media_file(
                        data,
                        video_url,
                        mixed_dir,
                        '.mp4',
                        idx if len(data['video_urls']) > 1 else None,
                        update_mixed_progress,
                        max_retries,
                        suffix='_video'
                    )

                    if file_path:
                        result['files'].append(file_path)

                    current_item += 1

            # Process images, audio, and music similarly...
            # (Code omitted for brevity - similar pattern to videos)

        # Create an index.html that links to all downloaded files
        if result['files']:
            index_path = self._create_mixed_content_index(mixed_dir, data, result['files'])
            if index_path:
                result['files'].append(index_path)

    def _parallel_process_mixed(self, data, mixed_dir, progress_callback, max_retries, result):
        """Process mixed content with parallel downloads

        Args:
            data: Content data dictionary
            mixed_dir: Directory to save mixed content
            progress_callback: Progress reporting callback
            max_retries: Maximum retry attempts
            result: Result dictionary to be updated
        """
        # Prepare download tasks for all media types
        download_tasks = []

        # Add video tasks
        for idx, url in enumerate(data.get('video_urls', [])):
            if url and isinstance(url, str) and url.startswith('http'):
                download_tasks.append({
                    'type': 'video',
                    'url': url,
                    'idx': idx,
                    'ext': '.mp4',
                    'suffix': '_video'
                })

        # Add image tasks
        for idx, url in enumerate(data.get('image_urls', [])):
            if url and isinstance(url, str) and url.startswith('http'):
                ext = self._determine_file_extension(url, '.jpg')
                download_tasks.append({
                    'type': 'image',
                    'url': url,
                    'idx': idx,
                    'ext': ext,
                    'suffix': '_image'
                })

        # Add audio tasks
        for idx, url in enumerate(data.get('audio_urls', [])):
            if url and isinstance(url, str) and url.startswith('http'):
                ext = self._determine_file_extension(url, '.mp3')
                download_tasks.append({
                    'type': 'audio',
                    'url': url,
                    'idx': idx,
                    'ext': ext,
                    'suffix': '_audio'
                })

        # Add music tasks
        if data.get('music_id'):
            for idx, url in enumerate(data.get('music_urls', [])):
                if url and isinstance(url, str) and url.startswith('http'):
                    ext = self._determine_file_extension(url, '.mp3')
                    download_tasks.append({
                        'type': 'music',
                        'url': url,
                        'idx': idx,
                        'ext': ext,
                        'suffix': '_music'
                    })

        total_tasks = len(download_tasks)
        if total_tasks == 0:
            self.logger.warning("No valid media URLs found in mixed content")
            return

        completed = 0

        # Progress update function
        def update_progress():
            nonlocal completed
            completed += 1
            if progress_callback:
                progress_callback(min(int((completed / total_tasks) * 100), 99), 100)

        # Function to download a single media item
        def download_media_item(task):
            try:
                file_path = self._download_media_file(
                    data,
                    task['url'],
                    mixed_dir,
                    task['ext'],
                    task['idx'],
                    None,  # No individual progress callback
                    max_retries,
                    task['suffix']
                )

                update_progress()
                return file_path
            except Exception as e:
                self.logger.error(f"Error downloading {task['type']} {task['idx']}: {e}")
                update_progress()
                return None

        # Initialize progress
        if progress_callback:
            progress_callback(0, 100)

        # Use thread pool for parallel downloads
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.max_workers, total_tasks)) as executor:
            # Submit all download tasks
            future_to_task = {executor.submit(download_media_item, task): task
                             for task in download_tasks}

            # Collect results as they complete
            for future in concurrent.futures.as_completed(future_to_task):
                file_path = future.result()
                if file_path:
                    result['files'].append(file_path)

        # Final progress update
        if progress_callback:
            progress_callback(100, 100)

    def _get_content_name(self, data):
        """Generate a standardized content name from data

        Args:
            data: Content data dictionary

        Returns:
            str: Sanitized name for files/folders
        """
        content_id = data['id']
        platform = data.get('platform', 'unknown')

        if self.use_description and data.get('desc'):
            desc = data['desc']
            safe_desc = sanitize_filename(desc, max_length=50)
            if safe_desc:
                return f"{safe_desc}_{content_id}"

        # Include platform and author if available
        author_part = ""
        if data.get('author_name'):
            author_part = f"_{sanitize_filename(data['author_name'], max_length=15)}"

        return f"{platform}{author_part}_{content_id}"

    def _determine_file_extension(self, url, default_ext, content_type=None):
        """Enhanced file extension detection using URL and content-type

        Args:
            url: Media URL
            default_ext: Default extension to use if can't determine
            content_type: Optional HTTP Content-Type header

        Returns:
            str: File extension with leading dot
        """
        # First try to determine from content-type if available
        if content_type:
            content_type = content_type.lower()
            # Map MIME types to extensions
            mime_map = {
                'image/jpeg': '.jpg',
                'image/png': '.png',
                'image/heic': '.heic',
                'image/webp': '.webp',
                'image/gif': '.gif',
                'video/mp4': '.mp4',
                'video/quicktime': '.mov',
                'video/webm': '.webm',
                'audio/mpeg': '.mp3',
                'audio/mp4': '.m4a',
                'audio/wav': '.wav',
                'audio/aac': '.aac'
            }

            for mime, ext in mime_map.items():
                if mime in content_type:
                    return ext

        # If couldn't determine from content-type, try from URL
        if url:
            lower_url = url.lower()

            # Use mimetypes module first
            ext = mimetypes.guess_extension(mimetypes.guess_type(url)[0] or '')
            if ext and ext != '.jpe':  # Skip .jpe which is sometimes returned for .jpg
                return ext

            # Fallback to manual checks
            # Images
            if '.webp' in lower_url:
                return '.webp'
            elif '.png' in lower_url:
                return '.png'
            elif '.jpg' in lower_url or '.jpeg' in lower_url:
                return '.jpg'
            elif '.gif' in lower_url:
                return '.gif'

            # Videos
            elif '.mp4' in lower_url:
                return '.mp4'
            elif '.mov' in lower_url:
                return '.mov'
            elif '.webm' in lower_url:
                return '.webm'

            # Audio
            elif '.mp3' in lower_url:
                return '.mp3'
            elif '.m4a' in lower_url:
                return '.m4a'
            elif '.wav' in lower_url:
                return '.wav'
            elif '.aac' in lower_url:
                return '.aac'

        # Default extension if couldn't determine
        return default_ext

    def _download_media_file(self, data, url, output_dir, extension, index=None, progress_callback=None, max_retries=3,
                             suffix=None):
        """Enhanced media file downloader with improved retry logic and content-type detection

        Args:
            data: Content data dictionary
            url: URL to download
            output_dir: Directory to save the file
            extension: File extension (with dot)
            index: Optional index for multi-file content
            progress_callback: Optional progress reporting callback
            max_retries: Maximum retry attempts
            suffix: Optional suffix to add to filename

        Returns:
            str: Path to downloaded file or None if failed
        """
        # Validate URL
        if not url or not isinstance(url, str) or not url.startswith('http'):
            self.logger.error(f"Invalid URL: {url}")
            return None

        try:
            # Generate base filename
            base_name = self._get_content_name(data)

            # Add index if provided (for multi-file content)
            if index is not None:
                base_name = f"{base_name}_{index + 1:03d}"

            # Add suffix if provided
            if suffix:
                base_name = f"{base_name}{suffix}"

            # Complete filename with extension
            file_name = os.path.join(output_dir, f"{base_name}{extension}")

            # Skip if file exists and skip_existing is True
            if self.skip_existing and os.path.exists(file_name):
                self.logger.info(f"Skipping existing file: {file_name}")
                return file_name

            # Initialize progress if callback provided
            if progress_callback:
                progress_callback(0, 100)

            # Create a temporary file for partial downloads
            temp_file = f"{file_name}.part"

            # Track where to resume download if supported
            resume_position = 0
            if os.path.exists(temp_file):
                resume_position = os.path.getsize(temp_file)

            # Download with retry logic and exponential backoff
            retry_count = 0
            backoff_factor = 2.0

            while retry_count < max_retries:
                try:
                    if extension in ['.mp4', '.mov', '.webm']:
                        timeout_settings = httpx.Timeout(connect=15.0, read=300.0, write=15.0, pool=10.0)
                    else:
                        timeout_settings = httpx.Timeout(connect=8.0, read=30.0, write=8.0, pool=5.0)

                    headers = DEFAULT_VIDEO_HEADERS.copy()
                    if resume_position > 0:
                        headers['Range'] = f'bytes={resume_position}-'

                    if retry_count > 0:
                        user_agents = [
                            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Safari/605.1.15',
                            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/93.0.4577.63 Safari/537.36',
                            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.159 Safari/537.36',
                        ]
                        headers['User-Agent'] = user_agents[retry_count % len(user_agents)]

                    with httpx.Client(timeout=timeout_settings, follow_redirects=True) as client:
                        with client.stream("GET", url, headers=headers) as response:
                            if response.status_code in (200, 206):
                                content_type = response.headers.get('content-type')
                                detected_ext = self._determine_file_extension(url, extension, content_type)

                                if detected_ext != extension:
                                    new_file_name = os.path.join(output_dir, f"{base_name}{detected_ext}")
                                    new_temp_file = f"{new_file_name}.part"

                                    if os.path.exists(temp_file):
                                        os.rename(temp_file, new_temp_file)

                                    file_name = new_file_name
                                    temp_file = new_temp_file
                                    extension = detected_ext

                                total_size = int(response.headers.get('content-length', 0))
                                if response.status_code == 206:
                                    content_range = response.headers.get('content-range', '')
                                    if content_range and '/' in content_range:
                                        try:
                                            total_size = int(content_range.split('/')[1])
                                        except (ValueError, IndexError):
                                            total_size += resume_position
                                    else:
                                        total_size += resume_position

                                if extension in ['.mp4', '.mov', '.webm']:
                                    chunk_size = 16384
                                elif total_size > 5 * 1024 * 1024:
                                    chunk_size = 8192
                                else:
                                    chunk_size = 4096

                                mode = 'ab' if resume_position > 0 else 'wb'
                                with open(temp_file, mode) as f:
                                    downloaded = resume_position
                                    last_progress_update = time.time()

                                    for chunk in response.iter_bytes(chunk_size=chunk_size):
                                        if chunk:
                                            f.write(chunk)
                                            downloaded += len(chunk)

                                            current_time = time.time()
                                            if progress_callback and total_size > 0 and (
                                                    current_time - last_progress_update >= 0.1):
                                                progress = int((downloaded / total_size) * 100)
                                                progress_callback(progress, 100)
                                                last_progress_update = current_time

                                os.replace(temp_file, file_name)

                                if progress_callback:
                                    progress_callback(100, 100)

                                return file_name

                            elif response.status_code == 429:
                                retry_delay = (backoff_factor ** retry_count) * 5 + (random.random() * 2)
                                self.logger.warning(
                                    f"Rate limited. Retrying in {retry_delay:.1f}s... ({retry_count + 1}/{max_retries})")
                                time.sleep(retry_delay)

                            elif response.status_code == 504:
                                retry_delay = (backoff_factor ** retry_count) * 3
                                self.logger.warning(
                                    f"Gateway timeout. Retrying in {retry_delay:.1f}s... ({retry_count + 1}/{max_retries})")
                                time.sleep(retry_delay)

                            elif 500 <= response.status_code < 600:
                                retry_delay = (backoff_factor ** retry_count) * 2
                                self.logger.warning(
                                    f"Server error {response.status_code}. Retrying in {retry_delay:.1f}s... ({retry_count + 1}/{max_retries})")
                                time.sleep(retry_delay)

                            else:
                                self.logger.error(f"HTTP error {response.status_code} when downloading media")
                                break

                except (httpx.TimeoutException, httpx.ConnectTimeout) as e:
                    if os.path.exists(temp_file):
                        resume_position = os.path.getsize(temp_file)

                    retry_delay = (backoff_factor ** retry_count) * 3 + (random.random() * 1.5)
                    self.logger.warning(
                        f"Timeout error: {e}. Retrying in {retry_delay:.1f}s... ({retry_count + 1}/{max_retries})")
                    time.sleep(retry_delay)

                except (httpx.NetworkError, httpx.ProtocolError) as e:
                    if os.path.exists(temp_file):
                        resume_position = os.path.getsize(temp_file)

                    retry_delay = (backoff_factor ** retry_count) * 2 + (random.random() * 1)
                    self.logger.warning(
                        f"Network error: {e}. Retrying in {retry_delay:.1f}s... ({retry_count + 1}/{max_retries})")
                    time.sleep(retry_delay)

                except Exception as e:
                    self.logger.error(f"Unexpected error downloading media: {e}")
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
                    return None

                retry_count += 1

            self.logger.error(f"Failed to download media after {max_retries} attempts")
            if os.path.exists(temp_file):
                os.remove(temp_file)
            return None

        except Exception as e:
            self.logger.error(f"Error in _download_media_file: {e}")
            temp_file = f"{file_name}.part" if 'file_name' in locals() else None
            if temp_file and os.path.exists(temp_file):
                os.remove(temp_file)
            return None

    def _postprocess_video_if_needed(self, file_path):
        """Optionally create a processed video based on the current post-processing settings."""
        if not self.postprocess_enabled:
            self._emit_pipeline("ffmpeg", 100, "未启用 FFmpeg 后处理，跳过", status="skipped")
            return None

        ffmpeg_executable = self._find_ffmpeg()
        if not ffmpeg_executable:
            self.logger.warning("Post-processing skipped because ffmpeg was not found")
            self._emit_pipeline("ffmpeg", None, "未找到 ffmpeg，跳过后处理", status="error")
            return None

        if not file_path or not os.path.exists(file_path):
            self.logger.warning(f"Post-processing skipped because file does not exist: {file_path}")
            self._emit_pipeline("ffmpeg", None, "源文件不存在，无法进行 FFmpeg 后处理", status="error")
            return None

        file_root, _ = os.path.splitext(file_path)
        suffix_parts = ["processed"]
        if self.mirror_video:
            suffix_parts.append("mirror")
        if self.trim_start_seconds > 0:
            trim_label = str(self.trim_start_seconds).replace('.', '_')
            suffix_parts.append(f"after_{trim_label}s")
        output_path = f"{file_root}_{'_'.join(suffix_parts)}.mp4"

        if self.skip_existing and os.path.exists(output_path):
            self.logger.info(f"Skipping existing post-processed file: {output_path}")
            self._emit_pipeline("ffmpeg", 100, f"复用已有 FFmpeg 结果: {os.path.basename(output_path)}", status="done")
            return output_path

        command = [ffmpeg_executable, "-y"]
        if self.trim_start_seconds > 0:
            command.extend(["-ss", str(self.trim_start_seconds)])
        command.extend(["-i", file_path])

        if self.mirror_video:
            command.extend(["-vf", "hflip"])

        command.extend([
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "18",
            "-c:a", "aac",
            "-b:a", "192k",
            output_path,
        ])

        try:
            self._emit_pipeline("ffmpeg", 5, "开始 FFmpeg 后处理", status="running")
            run_kwargs = {
                "capture_output": True,
                "text": True,
                "check": False,
            }
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                run_kwargs["startupinfo"] = startupinfo
                run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            completed = subprocess.run(command, **run_kwargs)
            if completed.returncode != 0:
                self.logger.error(
                    "Video post-processing failed for %s: %s",
                    file_path,
                    completed.stderr.strip() or completed.stdout.strip()
                )
                self._emit_pipeline("ffmpeg", None, completed.stderr.strip() or "FFmpeg 后处理失败", status="error")
                return None

            self.logger.info(f"Created processed video: {output_path}")
            self._emit_pipeline("ffmpeg", 100, f"FFmpeg 后处理完成: {os.path.basename(output_path)}", status="done")
            if self.delete_original_after_processing:
                try:
                    os.remove(file_path)
                    self.logger.info(f"Deleted original file after processing: {file_path}")
                except Exception as e:
                    self.logger.warning(f"Processed video created, but failed to delete original file {file_path}: {e}")
            return output_path
        except Exception as e:
            self.logger.error(f"Unexpected error during video post-processing: {e}")
            self._emit_pipeline("ffmpeg", None, f"FFmpeg 后处理异常: {e}", status="error")
            return None

    def _find_ffmpeg(self):
        """Resolve ffmpeg executable path from settings or PATH."""
        if self.ffmpeg_path:
            configured_path = self.ffmpeg_path.strip().strip('"')
            if os.path.isfile(configured_path):
                return configured_path

        path_ffmpeg = shutil.which("ffmpeg")
        if path_ffmpeg:
            return path_ffmpeg

        workspace = self._guess_ai_subtitle_workspace()
        candidates = [
            os.path.join(workspace, "backend", "ffmpeg", "win_x64", "ffmpeg.exe") if workspace else None,
            r"C:\Users\ExampleUser\Documents\New project\SubtitleCoverGUI_Rewrite\ffmpeg\bin\ffmpeg.exe",
            r"D:\resources\backend\ffmpeg\win_x64\ffmpeg.exe",
        ]
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                return candidate
        return None

    def _find_ffprobe(self):
        ffmpeg = self._find_ffmpeg()
        extension = ".exe" if os.name == "nt" else ""
        candidates = [
            os.path.join(os.path.dirname(ffmpeg), f"ffprobe{extension}") if ffmpeg else None,
            r"C:\Users\ExampleUser\Documents\New project\SubtitleCoverGUI_Rewrite\ffmpeg\bin\ffprobe.exe",
            shutil.which("ffprobe"),
        ]
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                return candidate
        return None

    def _create_album_preview(self, album_dir, data):
        """Create an HTML preview for downloaded album using Jinja2 templates

        Args:
            album_dir: Directory containing the downloaded images
            data: Content data dictionary

        Returns:
            str: Path to the created HTML file or None if failed
        """
        try:
            # Get all image files in the album directory
            image_files = self._get_image_files(album_dir)

            if not image_files:
                self.logger.warning(f"No image files found in album directory: {album_dir}")
                return None

            # Prepare template context
            context = {
                'album_name': self._get_content_name(data),
                'platform': data.get('platform', 'unknown'),
                'author': data.get('author_name', 'Unknown Author'),
                'description': data.get('desc', ''),
                'image_files': image_files
            }

            # Render template
            if self.album_template:
                html_content = self.album_template.render(**context)
            else:
                # Fallback to direct string template (less ideal)
                self.logger.warning("Using fallback template rendering for album preview")
                html_content = fallback_album_template(context)

            # Write HTML file
            html_path = os.path.join(album_dir, "preview.html")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)

            return html_path

        except Exception as e:
            self.logger.error(f"Error creating album preview: {e}")
            return None

    def _get_image_files(self, directory):
        """Get all image files in the directory

        Args:
            directory: Directory to scan for image files

        Returns:
            list: List of image filenames
        """
        image_files = []
        for file in os.listdir(directory):
            lower_file = file.lower()
            if (lower_file.endswith('.jpg') or lower_file.endswith('.jpeg') or
                lower_file.endswith('.png') or lower_file.endswith('.webp') or
                lower_file.endswith('.gif')):
                image_files.append(file)

        # Sort image files numerically if possible
        image_files.sort()
        return image_files

    def _create_mixed_content_index(self, mixed_dir, data, file_paths):
        """Create an HTML index for mixed content using Jinja2 templates

        Args:
            mixed_dir: Directory containing the mixed content
            data: Content data dictionary
            file_paths: List of downloaded file paths

        Returns:
            str: Path to the created HTML file or None if failed
        """
        try:
            # Group files by media type
            media_files = self._group_files_by_type(mixed_dir, file_paths)

            if not media_files:
                self.logger.warning(f"No media files found for index in directory: {mixed_dir}")
                return None

            # Prepare template context
            context = {
                'content_name': self._get_content_name(data),
                'platform': data.get('platform', 'unknown'),
                'author': data.get('author_name', 'Unknown Author'),
                'description': data.get('desc', ''),
                'videos': media_files.get('videos', []),
                'images': media_files.get('images', []),
                'audio': media_files.get('audio', []),
                'music': media_files.get('music', []),
                'other': media_files.get('other', [])
            }

            # Render template
            if self.mixed_template:
                html_content = self.mixed_template.render(**context)
            else:
                # Fallback to direct string template (less ideal)
                self.logger.warning("Using fallback template rendering for mixed content index")
                html_content = fallback_mixed_template(context)

            # Write HTML file
            html_path = os.path.join(mixed_dir, "index.html")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)

            return html_path

        except Exception as e:
            self.logger.error(f"Error creating mixed content index: {e}")
            return None

    def _group_files_by_type(self, directory, file_paths):
        """Group files by their media type

        Args:
            directory: Directory containing the files
            file_paths: List of file paths to group

        Returns:
            dict: Dictionary with media types as keys and lists of files as values
        """
        media_files = {}  # Group by type

        for file_path in file_paths:
            if directory in file_path and os.path.exists(file_path):
                file_name = os.path.basename(file_path)
                lower_file = file_name.lower()

                # Skip HTML files
                if lower_file.endswith('.html'):
                    continue

                # Categorize by file type
                if lower_file.endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
                    media_type = 'images'
                elif lower_file.endswith(('.mp4', '.mov', '.webm')):
                    media_type = 'videos'
                elif lower_file.endswith(('.mp3', '.m4a', '.wav', '.aac')):
                    if '_music' in lower_file:
                        media_type = 'music'
                    else:
                        media_type = 'audio'
                else:
                    media_type = 'other'

                if media_type not in media_files:
                    media_files[media_type] = []

                media_files[media_type].append(file_name)

        # Sort each category
        for media_type in media_files:
            media_files[media_type].sort()

        return media_files

