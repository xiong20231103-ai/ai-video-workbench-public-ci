"""
Modern single-video dashboard with staged pipeline progress.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox

import ttkbootstrap as ttk

from downloader.core.downloader import VideoDownloader
from downloader.utils.logger import logger_instance
from downloader.utils.utils import format_number, open_folder


class VideoTab:
    """Single video download tab with a richer dashboard-like UI."""

    STAGE_ORDER = ("resolve", "download", "ffmpeg", "blur")

    def __init__(self, parent, app):
        self.app = app
        self.frame = ttk.Frame(parent)
        self.translator = app.translator
        self.logger = logger_instance
        self.is_downloading = False

        self.url_var = tk.StringVar()
        self.custom_save_var = tk.BooleanVar(value=False)
        self.save_path_var = tk.StringVar(value=self.app.download_path)
        self.status_var = tk.StringVar(value="就绪，等待新任务")
        self.overall_progress_var = tk.DoubleVar(value=0)
        self.overall_caption_var = tk.StringVar(value="等待开始")
        self.hero_badge_var = tk.StringVar(value="Idle")
        self.output_var = tk.StringVar(value="还没有生成成品文件")
        self.metric_vars = {
            "author": tk.StringVar(value="-"),
            "duration": tk.StringVar(value="-"),
            "resolution": tk.StringVar(value="-"),
            "engagement": tk.StringVar(value="-"),
        }
        self.stage_rows: dict[str, dict] = {}
        self.stage_last_log: dict[str, str] = {}
        self.stage_last_bucket: dict[str, int | None] = {}

        self._build_ui()
        self._reset_pipeline_state()

    def _t(self, key: str, default: str) -> str:
        text = self.translator.translate("video_tab", key)
        return default if text == key else text

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.frame, padding=18)
        outer.pack(fill=tk.BOTH, expand=True)

        self._build_hero(outer)

        content = ttk.Frame(outer)
        content.pack(fill=tk.BOTH, expand=True, pady=(16, 0))
        content.columnconfigure(0, weight=7)
        content.columnconfigure(1, weight=5)
        content.rowconfigure(0, weight=1)

        left = ttk.Frame(content)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        right = ttk.Frame(content)
        right.grid(row=0, column=1, sticky="nsew")

        self._build_input_card(left)
        self._build_pipeline_card(left)
        self._build_info_card(right)
        self._build_details_card(right)

    def _build_hero(self, parent) -> None:
        hero = ttk.Frame(parent, padding=18, bootstyle="dark")
        hero.pack(fill=tk.X)

        title_col = ttk.Frame(hero, bootstyle="dark")
        title_col.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(
            title_col,
            text="智能联动下载台",
            font=("Microsoft YaHei UI", 22, "bold"),
            bootstyle="inverse-dark",
        ).pack(anchor="w")
        ttk.Label(
            title_col,
            text="把解析、下载、FFmpeg 和字幕处理放进同一条可视化流水线。",
            font=("Microsoft YaHei UI", 10),
            bootstyle="inverse-secondary",
        ).pack(anchor="w", pady=(6, 0))

        badge = ttk.Label(
            hero,
            textvariable=self.hero_badge_var,
            padding=(16, 10),
            font=("Segoe UI Semibold", 11),
            bootstyle="info",
        )
        badge.pack(side=tk.RIGHT)

    def _build_input_card(self, parent) -> None:
        card = ttk.LabelFrame(parent, text="任务入口", padding=16, bootstyle="primary")
        card.pack(fill=tk.X)

        ttk.Label(
            card,
            text="粘贴一个链接后直接启动，处理状态会实时分阶段显示。",
            font=("Microsoft YaHei UI", 10),
        ).pack(anchor="w")

        entry_row = ttk.Frame(card)
        entry_row.pack(fill=tk.X, pady=(12, 10))
        self.url_entry = ttk.Entry(entry_row, textvariable=self.url_var, font=("Consolas", 11))
        self.url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(entry_row, text="粘贴", command=self._paste_url, bootstyle="secondary").pack(side=tk.LEFT, padx=6)
        ttk.Button(entry_row, text="清空", command=lambda: self.url_var.set(""), bootstyle="light").pack(side=tk.LEFT)

        save_row = ttk.Frame(card)
        save_row.pack(fill=tk.X, pady=(0, 12))
        ttk.Checkbutton(
            save_row,
            text="自定义保存位置",
            variable=self.custom_save_var,
            command=self._toggle_custom_save,
            bootstyle="round-toggle",
        ).pack(side=tk.LEFT)
        ttk.Entry(save_row, textvariable=self.save_path_var, font=("Consolas", 10)).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=10
        )
        ttk.Button(save_row, text="浏览", command=self._browse_save_path, bootstyle="outline-secondary").pack(side=tk.LEFT)

        action_row = ttk.Frame(card)
        action_row.pack(fill=tk.X)
        self.start_button = ttk.Button(
            action_row,
            text="开始联动处理",
            command=self._download_video,
            bootstyle="success",
            width=18,
        )
        self.start_button.pack(side=tk.LEFT)
        ttk.Button(
            action_row,
            text="打开下载目录",
            command=lambda: open_folder(self._get_download_path()),
            bootstyle="outline-primary",
        ).pack(side=tk.LEFT, padx=8)
        ttk.Label(action_row, textvariable=self.output_var, font=("Microsoft YaHei UI", 9)).pack(
            side=tk.RIGHT, padx=(10, 0)
        )

    def _build_pipeline_card(self, parent) -> None:
        card = ttk.LabelFrame(parent, text="处理流水线", padding=16, bootstyle="info")
        card.pack(fill=tk.BOTH, expand=True, pady=(14, 0))

        top = ttk.Frame(card)
        top.pack(fill=tk.X)
        ttk.Label(top, textvariable=self.status_var, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        ttk.Label(top, textvariable=self.overall_caption_var, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(4, 0))

        overall_row = ttk.Frame(card)
        overall_row.pack(fill=tk.X, pady=(14, 12))
        self.overall_progress = ttk.Progressbar(
            overall_row,
            variable=self.overall_progress_var,
            maximum=100,
            bootstyle="success-striped",
        )
        self.overall_progress.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.overall_percent_label = ttk.Label(overall_row, text="0%", width=6)
        self.overall_percent_label.pack(side=tk.LEFT, padx=(10, 0))

        self._create_stage_row(card, "resolve", "1. 链接解析", "secondary")
        self._create_stage_row(card, "download", "2. 视频下载", "primary")
        self._create_stage_row(card, "ffmpeg", "3. FFmpeg 处理", "warning")
        self._create_stage_row(card, "blur", "4. 字幕处理", "danger")

        log_wrap = ttk.Frame(card)
        log_wrap.pack(fill=tk.BOTH, expand=True, pady=(14, 0))
        ttk.Label(log_wrap, text="实时事件", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        body = ttk.Frame(log_wrap)
        body.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.log_text = tk.Text(
            body,
            height=10,
            wrap="word",
            font=("Consolas", 10),
            bg="#0f172a",
            fg="#dbeafe",
            insertbackground="#dbeafe",
            relief="flat",
            borderwidth=0,
        )
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.log_text.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=scroll.set, state="disabled")

    def _create_stage_row(self, parent, key: str, title: str, bootstyle: str) -> None:
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=5)

        header = ttk.Frame(row)
        header.pack(fill=tk.X)
        ttk.Label(header, text=title, font=("Microsoft YaHei UI", 10, "bold")).pack(side=tk.LEFT)
        status_var = tk.StringVar(value="等待")
        ttk.Label(header, textvariable=status_var, bootstyle=bootstyle).pack(side=tk.RIGHT)

        progress_var = tk.DoubleVar(value=0)
        progress = ttk.Progressbar(row, variable=progress_var, maximum=100, bootstyle=f"{bootstyle}-striped")
        progress.pack(fill=tk.X, pady=(5, 3))
        message_var = tk.StringVar(value="尚未开始")
        ttk.Label(row, textvariable=message_var, font=("Microsoft YaHei UI", 9)).pack(anchor="w")

        self.stage_rows[key] = {
            "status": status_var,
            "progress": progress_var,
            "message": message_var,
        }

    def _build_info_card(self, parent) -> None:
        card = ttk.LabelFrame(parent, text="视频概览", padding=16, bootstyle="success")
        card.pack(fill=tk.X)

        grid = ttk.Frame(card)
        grid.pack(fill=tk.X)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        items = [
            ("作者", "author"),
            ("时长", "duration"),
            ("分辨率", "resolution"),
            ("互动", "engagement"),
        ]
        for index, (label, key) in enumerate(items):
            cell = ttk.Frame(grid, padding=10, bootstyle="light")
            cell.grid(row=index // 2, column=index % 2, sticky="nsew", padx=4, pady=4)
            ttk.Label(cell, text=label, font=("Microsoft YaHei UI", 9)).pack(anchor="w")
            ttk.Label(cell, textvariable=self.metric_vars[key], font=("Segoe UI Semibold", 12)).pack(anchor="w", pady=(6, 0))

    def _build_details_card(self, parent) -> None:
        card = ttk.LabelFrame(parent, text="详细信息", padding=16, bootstyle="secondary")
        card.pack(fill=tk.BOTH, expand=True, pady=(14, 0))
        self.info_text = tk.Text(
            card,
            wrap="word",
            font=("Consolas", 10),
            bg="#f8fafc",
            fg="#1e293b",
            relief="flat",
            borderwidth=0,
        )
        self.info_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(card, orient="vertical", command=self.info_text.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.info_text.configure(yscrollcommand=scroll.set)

    def update_language(self) -> None:
        if not self.is_downloading:
            self.status_var.set("就绪，等待新任务")

    def _get_download_path(self) -> str:
        if self.custom_save_var.get() and self.save_path_var.get().strip():
            return self.save_path_var.get().strip()
        return self.app.download_path

    def _toggle_custom_save(self) -> None:
        if not self.custom_save_var.get():
            self.save_path_var.set(self.app.download_path)
            return
        self._browse_save_path()

    def _browse_save_path(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.save_path_var.get() or self.app.download_path)
        if folder:
            self.save_path_var.set(folder)
            self.custom_save_var.set(True)

    def _paste_url(self) -> None:
        try:
            clipboard_content = self.frame.clipboard_get().strip()
            if clipboard_content:
                self.url_var.set(clipboard_content)
                return
        except Exception:
            pass
        messagebox.showwarning("提示", "剪贴板里没有可用链接。")

    def _reset_pipeline_state(self) -> None:
        defaults = {
            "resolve": ("等待", 0, "等待解析链接"),
            "download": ("等待", 0, "等待开始下载"),
            "ffmpeg": ("等待", 0, "等待后处理"),
            "blur": ("等待", 0, "等待字幕处理"),
        }
        for key, (status, progress, message) in defaults.items():
            self.stage_rows[key]["status"].set(status)
            self.stage_rows[key]["progress"].set(progress)
            self.stage_rows[key]["message"].set(message)
        self.overall_progress_var.set(0)
        self.overall_caption_var.set("等待开始")
        self.overall_percent_label.configure(text="0%")
        self.hero_badge_var.set("Idle")
        self.output_var.set("还没有生成成品文件")
        self.stage_last_log = {}
        self.stage_last_bucket = {}
        self._clear_log()

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, f"{message}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _set_stage(self, key: str, status: str | None = None, progress: float | None = None, message: str | None = None) -> None:
        row = self.stage_rows[key]
        if status is not None:
            row["status"].set(status)
        if progress is not None:
            row["progress"].set(max(0, min(100, progress)))
        if message is not None:
            row["message"].set(message)
            bucket = int(progress // 5) if progress is not None else None
            last_bucket = self.stage_last_bucket.get(key)
            should_log = bucket is None or bucket != last_bucket or status in {"完成", "异常", "已跳过"}
            if should_log and self.stage_last_log.get(key) != message:
                self.stage_last_log[key] = message
                self.stage_last_bucket[key] = bucket
                self._append_log(message)
        self._refresh_overall_progress()

    def _refresh_overall_progress(self) -> None:
        total = sum(self.stage_rows[key]["progress"].get() for key in self.STAGE_ORDER)
        overall = total / len(self.STAGE_ORDER)
        self.overall_progress_var.set(overall)
        self.overall_percent_label.configure(text=f"{int(overall)}%")

    def _set_pipeline_mode(self, downloading: bool) -> None:
        self.is_downloading = downloading
        self.start_button.configure(state=tk.DISABLED if downloading else tk.NORMAL)
        self.hero_badge_var.set("Running" if downloading else "Idle")

    def _download_video(self) -> None:
        if self.is_downloading:
            messagebox.showinfo("提示", "当前任务还在处理中。")
            return

        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "请先粘贴视频链接。")
            return

        if not self.app.client.is_configured:
            messagebox.showwarning("提示", "请先在设置里填写 API Key。")
            return

        self._reset_pipeline_state()
        self._set_pipeline_mode(True)
        self.status_var.set("准备解析链接")
        self.overall_caption_var.set("流水线已启动")
        self.app.main_window.update_status(f"正在准备任务: {url}")
        self._set_stage("resolve", status="进行中", progress=10, message="开始解析链接")

        settings = {}
        if hasattr(self.app, "settings_tab"):
            settings = self.app.settings_tab.get_settings()

        if not settings.get("postprocess_enabled", False):
            self._set_stage("ffmpeg", status="已跳过", progress=100, message="未启用 FFmpeg 后处理")
        if not settings.get("subtitle_cover_enabled", False):
            self._set_stage("blur", status="已跳过", progress=100, message="未启用字幕处理")

        self.app.run_in_thread(
            self._download_video_thread,
            self._on_download_complete,
            url=url,
            download_path=self._get_download_path(),
            settings=settings,
        )

    def _download_video_thread(self, url, download_path, settings):
        try:
            self.logger.info(f"Getting video info for URL: {url}")
            video_info = self.app.client.get_data(url, clean_data=True)
            if not video_info:
                return False, None, "无法获取视频信息", None

            self.app.root.after(0, lambda: self._display_video_info(video_info))
            self.app.root.after(0, lambda: self._set_stage("resolve", status="完成", progress=100, message="链接解析完成"))
            self.app.root.after(0, lambda: self._set_stage("download", status="进行中", progress=1, message="开始下载视频"))
            self.app.root.after(0, lambda: self.status_var.set("正在下载视频"))

            if not video_info.get("video_urls"):
                return False, None, "未找到视频下载地址", None

            downloader = VideoDownloader(
                download_path=download_path,
                use_description=settings.get("rename_with_desc", False),
                skip_existing=settings.get("skip_existing", True),
                max_workers=settings.get("max_workers", 4),
                postprocess_enabled=settings.get("postprocess_enabled", False),
                trim_start_seconds=settings.get("postprocess_trim_start_seconds", 1.5),
                mirror_video=settings.get("postprocess_mirror_video", True),
                delete_original_after_processing=settings.get("postprocess_delete_original", False),
                ffmpeg_path=settings.get("ffmpeg_path"),
                subtitle_cover_enabled=settings.get("subtitle_cover_enabled", False),
                subtitle_cover_workspace=settings.get("subtitle_cover_workspace"),
                subtitle_cover_detect_fps=settings.get("subtitle_cover_detect_fps", 1.5),
                subtitle_cover_min_score=settings.get("subtitle_cover_min_score", 0.38),
                subtitle_cover_mode=settings.get("subtitle_cover_mode", "blur"),
                subtitle_cover_blur_kernel=settings.get("subtitle_cover_blur_kernel", 41),
                subtitle_cover_white_alpha=settings.get("subtitle_cover_white_alpha", 0.88),
                subtitle_ai_workspace=settings.get("subtitle_ai_workspace"),
                subtitle_ai_auto_detect=settings.get("subtitle_ai_auto_detect", True),
                subtitle_ai_require_cuda=settings.get("subtitle_ai_require_cuda", True),
                subtitle_ai_quality=settings.get("subtitle_ai_quality", 18),
                pipeline_callback=lambda event: self.app.root.after(0, lambda e=event: self._handle_pipeline_event(e)),
            )

            last_download_bucket = {"value": -1}

            def progress_callback(current, total):
                if total > 0:
                    percent = (current / total) * 100
                    bucket = int(percent // 5)
                    message = None
                    if bucket != last_download_bucket["value"]:
                        last_download_bucket["value"] = bucket
                        message = f"下载进度 {int(percent)}%"
                    self.app.root.after(
                        0,
                        lambda p=percent, m=message: self._set_stage("download", status="进行中", progress=p, message=m),
                    )

            if "media_type" not in video_info:
                video_info["media_type"] = "video"

            result = downloader.main_downloader(
                video_info,
                progress_callback=progress_callback,
                max_retries=settings.get("max_retries", 3),
            )

            if result["success"] and result["files"]:
                warning_msg = "\n".join(result["errors"]) if result["errors"] else None
                return True, result["files"][0], None, warning_msg

            error_msg = "\n".join(result["errors"]) if result["errors"] else "下载失败"
            return False, None, error_msg, None
        except Exception as e:
            self.logger.exception(f"Error in download thread: {str(e)}")
            return False, None, str(e), None

    def _handle_pipeline_event(self, event: dict) -> None:
        stage = event.get("stage")
        status = event.get("status") or "running"
        message = event.get("message")
        progress = event.get("progress")

        if stage not in self.stage_rows:
            if message:
                self._append_log(message)
            return

        status_map = {
            "running": "进行中",
            "done": "完成",
            "error": "异常",
            "skipped": "已跳过",
        }
        ui_status = status_map.get(status, "进行中")

        if progress is None:
            progress = self.stage_rows[stage]["progress"].get()
        self._set_stage(stage, status=ui_status, progress=progress, message=message)

        if stage == "blur" and message:
            self.status_var.set(message)
            self.overall_caption_var.set("字幕处理正在推进中")
        elif stage == "ffmpeg" and message:
            self.overall_caption_var.set("FFmpeg 正在处理中")

    def _on_download_complete(self, result) -> None:
        success, file_path, error_message, warning_message = result
        self._set_pipeline_mode(False)

        if success:
            self._set_stage("download", status="完成", progress=100, message="下载阶段完成")
            self.status_var.set("联动处理完成")
            self.overall_caption_var.set("最终成品已生成")
            self.output_var.set(os.path.basename(file_path))
            self.overall_progress_var.set(100)
            self.overall_percent_label.configure(text="100%")
            self.app.main_window.update_status(f"成品已生成: {file_path}")
            self._show_download_complete_dialog(file_path)
            if warning_message:
                messagebox.showwarning("处理提醒", warning_message)
        else:
            self.status_var.set("处理失败")
            self.overall_caption_var.set("请查看错误信息")
            self.app.main_window.update_status("当前任务处理失败")
            self._append_log(error_message or "未知错误")
            messagebox.showerror("下载失败", error_message or "处理失败")

    def _show_download_complete_dialog(self, file_path: str) -> None:
        dialog = tk.Toplevel(self.app.root)
        dialog.title("处理完成")
        dialog.geometry("560x260")
        dialog.minsize(560, 260)
        dialog.transient(self.app.root)
        dialog.grab_set()

        dialog.update_idletasks()
        width = dialog.winfo_width()
        height = dialog.winfo_height()
        x = (self.app.root.winfo_width() - width) // 2 + self.app.root.winfo_x()
        y = (self.app.root.winfo_height() - height) // 2 + self.app.root.winfo_y()
        dialog.geometry(f"{width}x{height}+{x}+{y}")

        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="成品已准备好", font=("Microsoft YaHei UI", 16, "bold"), bootstyle="success").pack(anchor="w")
        ttk.Label(frame, text="下载、后处理、字幕处理都已经走完。").pack(anchor="w", pady=(6, 14))

        path_box = ttk.Entry(frame, font=("Consolas", 10))
        path_box.insert(0, file_path)
        path_box.configure(state="readonly")
        path_box.pack(fill=tk.X)

        btns = ttk.Frame(frame)
        btns.pack(fill=tk.X, pady=(20, 0))
        ttk.Button(
            btns,
            text="打开所在目录",
            bootstyle="primary",
            command=lambda: [open_folder(os.path.dirname(file_path)), dialog.destroy()],
        ).pack(side=tk.LEFT)
        ttk.Button(
            btns,
            text="关闭",
            bootstyle="secondary",
            command=dialog.destroy,
        ).pack(side=tk.RIGHT)

        self.app.root.wait_window(dialog)

    def _display_video_info(self, video_info: dict) -> None:
        self.info_text.delete(1.0, tk.END)

        desc = video_info.get("desc") or "-"
        author = video_info.get("author_name") or "-"
        resolution = video_info.get("resolution") or "-"
        duration_text = "-"
        if "duration" in video_info:
            try:
                duration_seconds = int(video_info["duration"])
                if duration_seconds > 1000:
                    duration_seconds /= 1000
                duration_text = f"{int(duration_seconds // 60)}:{int(duration_seconds % 60):02d}"
            except Exception:
                duration_text = str(video_info.get("duration"))

        likes = format_number(video_info.get("like_count", 0)) if "like_count" in video_info else "-"
        comments = format_number(video_info.get("comment_count", 0)) if "comment_count" in video_info else "-"

        self.metric_vars["author"].set(author)
        self.metric_vars["duration"].set(duration_text)
        self.metric_vars["resolution"].set(resolution)
        self.metric_vars["engagement"].set(f"{likes} 赞 / {comments} 评")

        lines = [
            f"ID: {video_info.get('id', '-')}",
            f"作者: {author}",
            f"描述: {desc}",
            f"发布时间: {video_info.get('create_time', '-')}",
            f"分辨率: {resolution}",
            f"时长: {duration_text}",
            f"点赞: {likes}",
            f"评论: {comments}",
            f"分享: {format_number(video_info.get('share_count', 0)) if 'share_count' in video_info else '-'}",
            f"播放: {format_number(video_info.get('play_count', 0)) if 'play_count' in video_info else '-'}",
            f"作者ID: {video_info.get('author_id', '-')}",
            f"音乐: {video_info.get('music_title', '-')}",
            f"音乐作者: {video_info.get('music_author', '-')}",
        ]
        tags = video_info.get("tags")
        if tags:
            lines.append(f"标签: {', '.join(tags)}")

        self.info_text.insert(tk.END, "\n".join(lines))
