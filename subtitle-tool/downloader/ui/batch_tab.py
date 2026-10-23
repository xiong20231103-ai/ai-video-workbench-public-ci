"""
Enhanced batch download tab for TikHub Downloader with modern UI, improved UX and multithreaded downloads
"""

import threading
import tkinter as tk
from tkinter import messagebox, filedialog
import queue
import time
import os
import json
import sys
from concurrent.futures import ThreadPoolExecutor
import ttkbootstrap as ttk
try:
    from ttkbootstrap.constants import *
    from ttkbootstrap.toast import ToastNotification
    from ttkbootstrap.scrolled import ScrolledText
    HAS_TTKBOOTSTRAP_EXTENSIONS = True
except ImportError:
    # Fallback if extended components are not available
    HAS_TTKBOOTSTRAP_EXTENSIONS = False
import re

from downloader.core.downloader import VideoDownloader
from downloader.utils.utils import extract_and_clean_url, extract_urls_from_text, open_folder
from downloader.utils.logger import logger_instance


class BatchTab:
    """Batch download tab UI and functionality with multithreaded download support"""

    def __init__(self, parent, app):
        """Initialize the batch download tab

        Args:
            parent: Parent widget
            app: Application instance
        """
        self.app = app
        self.frame = ttk.Frame(parent)
        self.translator = app.translator  # Access translator from app

        # Download state tracking
        self.is_downloading = False
        self.download_thread = None
        self.stop_download = False
        self.paused = False

        # Thread coordination
        self.result_queue = queue.Queue()
        self.lock = threading.Lock()

        # Download statistics
        self.completed = 0
        self.failed = 0
        self.successful_urls = []
        self.failed_urls = []
        self.warning_urls = []
        self.download_times = []
        self.download_phase_times = []
        self.postprocess_phase_times = []
        self.start_time = None
        self.currently_downloading = set()
        self.pipeline_state = {}
        self.focus_url = None
        self.subtitle_cover_enabled_var = tk.BooleanVar(value=self.app.config.get('subtitle_cover_enabled', False))
        self.subtitle_cover_workspace_var = tk.StringVar(
            value=self.app.config.get('subtitle_cover_workspace') or self._guess_subtitle_cover_workspace()
        )
        self.subtitle_cover_detect_fps_var = tk.DoubleVar(
            value=float(self.app.config.get('subtitle_cover_detect_fps', 1.5))
        )
        self.subtitle_cover_min_score_var = tk.DoubleVar(
            value=float(self.app.config.get('subtitle_cover_min_score', 0.38))
        )
        self.subtitle_cover_mode_var = tk.StringVar(
            value=self.app.config.get('subtitle_cover_mode', 'blur')
        )
        self.subtitle_cover_blur_kernel_var = tk.IntVar(
            value=int(self.app.config.get('subtitle_cover_blur_kernel', 41))
        )
        self.subtitle_cover_white_alpha_var = tk.DoubleVar(
            value=float(self.app.config.get('subtitle_cover_white_alpha', 0.88))
        )
        self.subtitle_ai_workspace_var = tk.StringVar(
            value=self.app.config.get('subtitle_ai_workspace') or self._guess_ai_subtitle_workspace()
        )
        self.subtitle_ai_preview_first_var = tk.BooleanVar(
            value=bool(self.app.config.get('subtitle_ai_preview_first', True))
        )
        self.custom_download_path = self.app.download_path
        self.batch_subfolder_var = tk.StringVar(value="")

        # Set the logger
        self.logger = logger_instance

        # Default values
        self.max_workers = 5  # Default number of concurrent downloads

        # Safe theme color initialization
        self.theme_color = "#007bff"  # Default primary blue
        try:
            if hasattr(app, 'style') and hasattr(app.style, 'colors'):
                self.theme_color = app.style.colors.get("primary", "#007bff")
            elif hasattr(app, 'root') and isinstance(app.root, ttk.Window):
                # Try to get color from ttk theme
                self.theme_color = app.root.style.colors.get("primary", "#007bff")
        except Exception:
            # Fallback to default if any error occurs
            pass

        # Create icons dictionary
        self._init_icons()

        # Create UI components
        self._create_widgets()
        self.batch_subfolder_var.trace_add("write", lambda *_: self._refresh_save_location_display())
        self._refresh_save_location_display()

    def _init_icons(self):
        """Initialize icons dictionary (using Unicode symbols as fallback)"""
        self.icons = {
            "paste": "📋",
            "clear": "🗑️",
            "extract": "🔍",
            "folder": "📁",
            "download": "⬇️",
            "stop": "⏹️",
            "pause": "⏸️",
            "resume": "▶️",
            "settings": "⚙️",
            "info": "ℹ️",
            "warning": "⚠️",
            "success": "✅",
            "error": "❌"
        }

        # Try to load actual icons if they exist
        try:
            # Check if tk_image exists in app
            if hasattr(self.app, 'tk_images') and isinstance(self.app.tk_images, dict):
                for key in self.icons.keys():
                    if key in self.app.tk_images:
                        self.icons[key] = self.app.tk_images[key]
        except Exception:
            pass  # Fallback to Unicode symbols

    def _create_widgets(self):
        """Create tab UI components"""
        # Main container with two-column layout
        main_container = ttk.Frame(self.frame)
        main_container.pack(fill=tk.BOTH, expand=True, padx=15, pady=10)

        # Left side - URL input and controls
        left_frame = ttk.Frame(main_container)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 7))

        # Right side - Downloads status and statistics
        right_frame = ttk.Frame(main_container)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(7, 0))

        # ----- LEFT SIDE -----

        # Header with title and URL counter
        header_frame = ttk.Frame(left_frame)
        header_frame.pack(fill=tk.X, pady=(0, 5))

        title_label = ttk.Label(
            header_frame,
            text=self.translator.translate("batch_tab", "batch_links"),
            font=("", 11, "bold")
        )
        title_label.pack(side=tk.LEFT)

        self.url_count_var = tk.StringVar(value="0 URLs")
        url_count_label = ttk.Label(header_frame, textvariable=self.url_count_var)
        url_count_label.pack(side=tk.RIGHT)

        # URL input with syntax highlighting
        input_frame = ttk.Frame(left_frame, bootstyle=SECONDARY)
        input_frame.pack(fill=tk.BOTH, expand=True)

        # Use ScrolledText for cleaner scrollbar integration
        self.batch_text = ScrolledText(
            input_frame,
            height=10,
            wrap=tk.WORD,
            autohide=True
        )
        self.batch_text.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        # Bind URL validation and highlighting
        self.batch_text.bind("<KeyRelease>", self._highlight_urls)

        # Control buttons frame with icon buttons
        control_frame = ttk.Frame(left_frame)
        control_frame.pack(fill=tk.X, pady=10)

        # Create modern button group
        btn_group = ttk.Frame(control_frame)
        btn_group.pack(side=tk.LEFT)

        # Buttons with icons and tooltips
        buttons_config = [
            (self.translator.translate("batch_tab", "paste_button"), self._paste_to_batch, "paste",
             self.translator.translate("batch_tab", "paste_tooltip")),
            (self.translator.translate("batch_tab", "clear_button"),
             lambda: [self.batch_text.delete(1.0, tk.END), self._count_urls()], "clear",
             self.translator.translate("batch_tab", "clear_tooltip")),
            (self.translator.translate("batch_tab", "extract_urls_button"), self._extract_urls, "extract",
             self.translator.translate("batch_tab", "extract_tooltip"))
        ]

        for text, command, icon, tooltip in buttons_config:
            btn = ttk.Button(
                btn_group,
                text=f" {text}",
                image=self.icons[icon] if isinstance(self.icons[icon], tk.PhotoImage) else None,
                compound=tk.LEFT,
                command=command,
                bootstyle=OUTLINE
            )
            if not isinstance(self.icons[icon], tk.PhotoImage):
                btn.config(text=f"{self.icons[icon]} {text}")

            btn.pack(side=tk.LEFT, padx=2)
            self._create_tooltip(btn, tooltip)

        # Extract from file button
        file_btn = ttk.Button(
            control_frame,
            text=self.translator.translate("batch_tab", "extract_from_file"),
            command=self._extract_from_file,
            bootstyle=(OUTLINE, INFO)
        )
        file_btn.pack(side=tk.RIGHT)
        self._create_tooltip(
            file_btn,
            self.translator.translate("batch_tab", "extract_from_file_tooltip")
        )

        # Download settings panel
        settings_panel = ttk.LabelFrame(
            left_frame,
            text=self.translator.translate("batch_tab", "download_settings"),
            bootstyle=SECONDARY
        )
        settings_panel.pack(fill=tk.X, pady=(10, 5))

        # Settings grid
        settings_grid = ttk.Frame(settings_panel)
        settings_grid.pack(fill=tk.X, padx=10, pady=10)

        # Row 1: Save location
        save_location_frame = ttk.Frame(settings_grid)
        save_location_frame.pack(fill=tk.X, pady=2)

        self.custom_save_var = tk.BooleanVar(value=False)
        custom_save_check = ttk.Checkbutton(
            save_location_frame,
            text=self.translator.translate("batch_tab", "custom_save_location"),
            variable=self.custom_save_var,
            command=self._toggle_custom_save
        )
        custom_save_check.pack(side=tk.LEFT)

        self.location_display = ttk.Label(
            save_location_frame,
            text=self.app.download_path,
            font=("", 9),
            foreground="gray"
        )
        self.location_display.pack(side=tk.RIGHT)

        subfolder_frame = ttk.Frame(settings_grid)
        subfolder_frame.pack(fill=tk.X, pady=(0, 5))

        ttk.Label(subfolder_frame, text="本批子文件夹").pack(side=tk.LEFT)
        ttk.Entry(
            subfolder_frame,
            textvariable=self.batch_subfolder_var,
            width=36
        ).pack(side=tk.LEFT, padx=(10, 8), fill=tk.X, expand=True)
        ttk.Label(
            subfolder_frame,
            text="可填例如 客户A\\4月21日",
            font=("", 9),
            foreground="gray"
        ).pack(side=tk.RIGHT)

        # Row 2: Thread count and quality settings
        thread_frame = ttk.Frame(settings_grid)
        thread_frame.pack(fill=tk.X, pady=5)

        # Thread count with visual slider
        thread_label = ttk.Label(
            thread_frame,
            text=self.translator.translate("batch_tab", "concurrent_downloads")
        )
        thread_label.pack(side=tk.LEFT)

        self.thread_count_var = tk.IntVar(value=self.max_workers)

        thread_slider = ttk.Scale(
            thread_frame,
            from_=1,
            to=20,
            variable=self.thread_count_var,
            command=lambda val: self.thread_count_display.config(text=f"{int(float(val))}")
        )
        thread_slider.pack(side=tk.LEFT, padx=10, fill=tk.X, expand=True)

        self.thread_count_display = ttk.Label(thread_frame, text=str(self.max_workers))
        self.thread_count_display.pack(side=tk.LEFT, padx=(0, 10))

        # SubtitleCover controls
        cover_panel = ttk.LabelFrame(
            left_frame,
            text="字幕处理参数",
            bootstyle=INFO
        )
        cover_panel.pack(fill=tk.X, pady=(8, 5))

        cover_grid = ttk.Frame(cover_panel)
        cover_grid.pack(fill=tk.X, padx=10, pady=10)

        ttk.Checkbutton(
            cover_grid,
            text="批量下载完成后自动处理字幕",
            variable=self.subtitle_cover_enabled_var
        ).grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 8))

        ttk.Label(cover_grid, text="工作目录").grid(row=1, column=0, sticky=tk.W, pady=4)
        ttk.Entry(cover_grid, textvariable=self.subtitle_cover_workspace_var, width=38).grid(
            row=1, column=1, sticky=tk.EW, pady=4, padx=6
        )
        ttk.Button(
            cover_grid,
            text="浏览",
            command=self._browse_batch_subtitle_cover_workspace,
            bootstyle=OUTLINE
        ).grid(row=1, column=2, sticky=tk.E, pady=4)

        ttk.Label(cover_grid, text="检测 FPS").grid(row=2, column=0, sticky=tk.W, pady=4)
        ttk.Spinbox(
            cover_grid,
            textvariable=self.subtitle_cover_detect_fps_var,
            from_=0.5,
            to=4.0,
            increment=0.5,
            width=10
        ).grid(row=2, column=1, sticky=tk.W, pady=4, padx=6)

        ttk.Label(cover_grid, text="最低分数").grid(row=3, column=0, sticky=tk.W, pady=4)
        ttk.Spinbox(
            cover_grid,
            textvariable=self.subtitle_cover_min_score_var,
            from_=0.2,
            to=0.8,
            increment=0.02,
            width=10
        ).grid(row=3, column=1, sticky=tk.W, pady=4, padx=6)

        ttk.Label(cover_grid, text="模糊强度").grid(row=4, column=0, sticky=tk.W, pady=4)
        ttk.Spinbox(
            cover_grid,
            textvariable=self.subtitle_cover_blur_kernel_var,
            from_=9,
            to=99,
            increment=2,
            width=10
        ).grid(row=4, column=1, sticky=tk.W, pady=4, padx=6)

        ttk.Label(cover_grid, text="白框透明度").grid(row=5, column=0, sticky=tk.W, pady=4)
        ttk.Spinbox(
            cover_grid,
            textvariable=self.subtitle_cover_white_alpha_var,
            from_=0.4,
            to=1.0,
            increment=0.05,
            width=10
        ).grid(row=5, column=1, sticky=tk.W, pady=4, padx=6)

        mode_row = ttk.Frame(cover_grid)
        mode_row.grid(row=6, column=0, columnspan=3, sticky=tk.W, pady=(6, 0))
        ttk.Label(mode_row, text="处理方式").pack(side=tk.LEFT)
        ttk.Radiobutton(mode_row, text="原智能模糊", variable=self.subtitle_cover_mode_var, value="blur").pack(side=tk.LEFT, padx=(10, 8))
        ttk.Radiobutton(mode_row, text="AI去除-STTN", variable=self.subtitle_cover_mode_var, value="ai-sttn").pack(side=tk.LEFT, padx=4)
        ttk.Radiobutton(mode_row, text="AI去除-LaMa", variable=self.subtitle_cover_mode_var, value="ai-lama").pack(side=tk.LEFT, padx=4)
        ttk.Radiobutton(mode_row, text="高速模糊", variable=self.subtitle_cover_mode_var, value="fast-blur").pack(side=tk.LEFT, padx=4)

        ttk.Checkbutton(
            cover_grid,
            text="首条先生成 8 秒预览，确认后再处理整批",
            variable=self.subtitle_ai_preview_first_var,
        ).grid(row=7, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))

        cover_grid.columnconfigure(1, weight=1)

        # Action buttons
        action_frame = ttk.Frame(left_frame)
        action_frame.pack(fill=tk.X, pady=10)

        # Download button
        self.download_button = ttk.Button(
            action_frame,
            text=self.translator.translate("batch_tab", "start_batch_download"),
            command=self._batch_download,
            bootstyle=SUCCESS
        )
        self.download_button.pack(side=tk.LEFT, padx=(0, 5))

        # Control buttons
        control_btns_frame = ttk.Frame(action_frame)
        control_btns_frame.pack(side=tk.LEFT)

        # Pause button
        self.pause_button = ttk.Button(
            control_btns_frame,
            text=self.translator.translate("batch_tab", "pause_download"),
            command=self._toggle_pause,
            state=tk.DISABLED,
            bootstyle=WARNING
        )
        self.pause_button.pack(side=tk.LEFT, padx=2)

        # Stop button
        self.stop_button = ttk.Button(
            control_btns_frame,
            text=self.translator.translate("batch_tab", "stop_download"),
            command=self._stop_download,
            state=tk.DISABLED,
            bootstyle=DANGER
        )
        self.stop_button.pack(side=tk.LEFT, padx=2)

        # ----- RIGHT SIDE -----

        # Progress panel
        progress_panel = ttk.LabelFrame(
            right_frame,
            text=self.translator.translate("batch_tab", "download_progress"),
            padding=10,
            bootstyle=PRIMARY
        )
        progress_panel.pack(fill=tk.X, pady=(0, 10))

        # Progress info
        progress_info_frame = ttk.Frame(progress_panel)
        progress_info_frame.pack(fill=tk.X, pady=(0, 10))

        # Status and count
        self.status_var = tk.StringVar(value=self.translator.translate("batch_tab", "status_ready"))
        status_label = ttk.Label(
            progress_info_frame,
            textvariable=self.status_var,
            font=("", 10, "bold")
        )
        status_label.pack(side=tk.LEFT)

        # Progress counter
        self.progress_var = tk.StringVar(value="0/0")
        progress_label = ttk.Label(
            progress_info_frame,
            textvariable=self.progress_var,
            font=("", 10, "bold")
        )
        progress_label.pack(side=tk.RIGHT)

        # Progress bar with percentage
        progress_bar_frame = ttk.Frame(progress_panel)
        progress_bar_frame.pack(fill=tk.X)

        self.progress = ttk.Progressbar(
            progress_bar_frame,
            maximum=100,
            variable=tk.DoubleVar(value=0),
            bootstyle=(SUCCESS, STRIPED)
        )
        self.progress.pack(fill=tk.X, side=tk.LEFT, expand=True)

        self.percent_var = tk.StringVar(value="0%")
        percent_label = ttk.Label(
            progress_bar_frame,
            textvariable=self.percent_var,
            width=5
        )
        percent_label.pack(side=tk.RIGHT, padx=(5, 0))

        # Stats frame
        stats_frame = ttk.Frame(progress_panel)
        stats_frame.pack(fill=tk.X, pady=(10, 0))

        # Left column - time stats
        time_stats = ttk.Frame(stats_frame)
        time_stats.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Elapsed time
        elapsed_frame = ttk.Frame(time_stats)
        elapsed_frame.pack(fill=tk.X, pady=2)

        ttk.Label(elapsed_frame, text=self.translator.translate("batch_tab", "elapsed_time")).pack(side=tk.LEFT)
        self.elapsed_var = tk.StringVar(value="00:00:00")
        ttk.Label(elapsed_frame, textvariable=self.elapsed_var).pack(side=tk.RIGHT)

        # Estimated time
        eta_frame = ttk.Frame(time_stats)
        eta_frame.pack(fill=tk.X, pady=2)

        ttk.Label(eta_frame, text=self.translator.translate("batch_tab", "estimated_time")).pack(side=tk.LEFT)
        self.eta_var = tk.StringVar(value="--:--:--")
        ttk.Label(eta_frame, textvariable=self.eta_var).pack(side=tk.RIGHT)

        # Right column - count stats
        count_stats = ttk.Frame(stats_frame)
        count_stats.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(20, 0))

        # Success count
        success_frame = ttk.Frame(count_stats)
        success_frame.pack(fill=tk.X, pady=2)

        ttk.Label(
            success_frame,
            text=self.translator.translate("batch_tab", "successful"),
            foreground="green"
        ).pack(side=tk.LEFT)
        self.success_var = tk.StringVar(value="0")
        ttk.Label(
            success_frame,
            textvariable=self.success_var,
            foreground="green"
        ).pack(side=tk.RIGHT)

        # Failed count
        failed_frame = ttk.Frame(count_stats)
        failed_frame.pack(fill=tk.X, pady=2)

        ttk.Label(
            failed_frame,
            text=self.translator.translate("batch_tab", "failed"),
            foreground="red"
        ).pack(side=tk.LEFT)
        self.failed_var = tk.StringVar(value="0")
        ttk.Label(
            failed_frame,
            textvariable=self.failed_var,
            foreground="red"
        ).pack(side=tk.RIGHT)

        # Current pipeline monitor
        pipeline_panel = ttk.LabelFrame(
            right_frame,
            text="当前任务流水线",
            padding=10,
            bootstyle=INFO
        )
        pipeline_panel.pack(fill=tk.X, pady=(0, 10))

        self.focus_url_var = tk.StringVar(value="等待新的下载任务")
        self.pipeline_message_var = tk.StringVar(value="这里会显示当前任务的下载、FFmpeg 和字幕处理进度。")

        ttk.Label(
            pipeline_panel,
            textvariable=self.focus_url_var,
            font=("", 10, "bold")
        ).pack(anchor="w")
        ttk.Label(
            pipeline_panel,
            textvariable=self.pipeline_message_var,
            foreground="gray"
        ).pack(anchor="w", pady=(4, 10))

        self.focus_stage_widgets = {}
        self._create_focus_stage_row(pipeline_panel, "resolve", "链接解析", INFO)
        self._create_focus_stage_row(pipeline_panel, "download", "视频下载", PRIMARY)
        self._create_focus_stage_row(pipeline_panel, "ffmpeg", "FFmpeg 处理", WARNING)
        self._create_focus_stage_row(pipeline_panel, "blur", "字幕处理", DANGER)

        # Currently downloading
        current_panel = ttk.LabelFrame(
            right_frame,
            text=self.translator.translate("batch_tab", "currently_downloading"),
            padding=10
        )
        current_panel.pack(fill=tk.BOTH, expand=True)

        # Live download list using Treeview for better visualization
        self.current_downloads = ttk.Treeview(
            current_panel,
            columns=("url", "stage", "progress", "time"),
            show="headings",
            height=5
        )
        self.current_downloads.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)

        # Configure columns
        self.current_downloads.heading("url", text=self.translator.translate("batch_tab", "video_url"))
        self.current_downloads.heading("stage", text="阶段")
        self.current_downloads.heading("progress", text="进度")
        self.current_downloads.heading("time", text=self.translator.translate("batch_tab", "time"))

        self.current_downloads.column("url", width=180)
        self.current_downloads.column("stage", width=90)
        self.current_downloads.column("progress", width=90)
        self.current_downloads.column("time", width=80)
        self.current_downloads.bind("<<TreeviewSelect>>", self._on_current_download_select)

        # Add scrollbar to treeview
        download_scroll = ttk.Scrollbar(current_panel, command=self.current_downloads.yview)
        self.current_downloads.configure(yscrollcommand=download_scroll.set)
        download_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Results section
        results_frame = ttk.LabelFrame(
            right_frame,
            text=self.translator.translate("batch_tab", "download_results"),
            padding=10
        )
        results_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        # Notebook with tabs for successful and failed downloads
        results_notebook = ttk.Notebook(results_frame)
        results_notebook.pack(fill=tk.BOTH, expand=True)

        # Successful tab
        success_tab = ttk.Frame(results_notebook)
        results_notebook.add(success_tab, text=self.translator.translate("batch_tab", "successful"))

        self.success_list = tk.Text(
            success_tab,
            height=5,
            wrap=tk.WORD,
            state=tk.DISABLED,
            yscrollcommand=tk.Scrollbar(success_tab).set
        )
        # 创建垂直滚动条
        success_scroll = tk.Scrollbar(success_tab, command=self.success_list.yview)
        self.success_list.config(yscrollcommand=success_scroll.set)
        self.success_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        success_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Failed tab
        failed_tab = ttk.Frame(results_notebook)
        results_notebook.add(failed_tab, text=self.translator.translate("batch_tab", "failed"))

        self.failed_list = tk.Text(
            failed_tab,
            height=5,
            wrap=tk.WORD,
            state=tk.DISABLED,
            yscrollcommand=tk.Scrollbar(failed_tab).set
        )
        # 创建垂直滚动条
        failed_scroll = tk.Scrollbar(failed_tab, command=self.failed_list.yview)
        self.failed_list.config(yscrollcommand=failed_scroll.set)
        self.failed_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        failed_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Additional toolbar with utility buttons
        toolbar = ttk.Frame(right_frame)
        toolbar.pack(fill=tk.X, pady=(10, 0))

        open_folder_btn = ttk.Button(
            toolbar,
            text=self.translator.translate("batch_tab", "open_download_folder"),
            command=self._open_active_download_folder
        )
        open_folder_btn.pack(side=tk.LEFT)

        export_btn = ttk.Button(
            toolbar,
            text=self.translator.translate("batch_tab", "export_results"),
            command=self._export_results
        )
        export_btn.pack(side=tk.RIGHT)

        # Start update timers
        self._start_progress_update_timer()
        self._start_url_highlight_timer()

    def _start_progress_update_timer(self):
        """Start a timer to update progress UI periodically"""
        def update_timer():
            if self.is_downloading:
                self._process_results_queue()
                self._update_elapsed_time()
            self.frame.after(100, update_timer)

        self.frame.after(100, update_timer)

    def _start_url_highlight_timer(self):
        """Start a timer to periodically highlight URLs"""
        def highlight_timer():
            if not self.is_downloading:
                self._highlight_urls(None)
            self.frame.after(2000, highlight_timer)

        self.frame.after(2000, highlight_timer)

    def _guess_subtitle_cover_workspace(self):
        candidates = [
            os.path.abspath(os.path.join(os.getcwd(), "..", "SubtitleCoverGUI_Rewrite")),
            os.path.abspath(os.path.join(os.getcwd(), "SubtitleCoverGUI_Rewrite")),
            os.path.abspath(os.path.join(os.path.dirname(os.getcwd()), "SubtitleCoverGUI_Rewrite")),
        ]
        for candidate in candidates:
            if os.path.isfile(os.path.join(candidate, "automation_runner.py")):
                return candidate
        return ""

    def _guess_ai_subtitle_workspace(self):
        executable_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates = [
            os.environ.get("TIKHUB_AI_SUBTITLE_WORKSPACE"),
            os.path.join(executable_dir, "AI_Subtitle_Backend"),
            os.path.join(executable_dir, "vsr_optimized"),
            os.path.abspath(os.path.join(executable_dir, "..", "vsr_optimized")),
            os.path.abspath(os.path.join(executable_dir, "..", "..", "vsr_optimized")),
            os.path.abspath(os.path.join(os.getcwd(), "..", "vsr_optimized")),
            os.path.abspath(os.path.join(os.getcwd(), "vsr_optimized")),
            os.path.abspath(os.path.join(os.path.dirname(os.getcwd()), "AI_Subtitle_Backend")),
            os.path.abspath(os.path.join(os.getcwd(), "AI_Subtitle_Backend")),
        ]
        for candidate in candidates:
            if os.path.isfile(os.path.join(candidate, "automation_runner.py")) or os.path.isfile(os.path.join(candidate, "ai_subtitle_runner.exe")):
                return candidate
        return ""

    def _browse_batch_subtitle_cover_workspace(self):
        folder_path = filedialog.askdirectory(
            initialdir=self.subtitle_cover_workspace_var.get() or os.getcwd()
        )
        if folder_path:
            self.subtitle_cover_workspace_var.set(folder_path)

    def _collect_batch_processing_settings(self):
        subtitle_cover_workspace = self.subtitle_cover_workspace_var.get().strip()
        subtitle_ai_workspace = self.subtitle_ai_workspace_var.get().strip() or self._guess_ai_subtitle_workspace()
        selected_mode = self.subtitle_cover_mode_var.get()
        detect_fps = float(self.subtitle_cover_detect_fps_var.get())
        min_score = float(self.subtitle_cover_min_score_var.get())
        blur_kernel = int(self.subtitle_cover_blur_kernel_var.get())
        white_alpha = float(self.subtitle_cover_white_alpha_var.get())

        if self.subtitle_cover_enabled_var.get():
            if selected_mode in ("ai-sttn", "ai-lama", "fast-blur"):
                if not subtitle_ai_workspace:
                    raise ValueError("找不到 AI 字幕处理后端目录")
                if not (
                    os.path.isfile(os.path.join(subtitle_ai_workspace, "automation_runner.py"))
                    or os.path.isfile(os.path.join(subtitle_ai_workspace, "ai_subtitle_runner.exe"))
                ):
                    raise ValueError("AI 字幕后端缺少 automation_runner.py 或 ai_subtitle_runner.exe")
            else:
                if not subtitle_cover_workspace:
                    raise ValueError("请在批量下载页填写 SubtitleCoverGUI_Rewrite 目录")
                runner_path = os.path.join(subtitle_cover_workspace, "automation_runner.py")
                if not os.path.isfile(runner_path):
                    raise ValueError("批量页选择的 SubtitleCover 目录不正确，缺少 automation_runner.py")

        if detect_fps <= 0:
            raise ValueError("检测 FPS 必须大于 0")
        if min_score < 0 or min_score > 1:
            raise ValueError("最低分数必须在 0 到 1 之间")
        if blur_kernel < 3:
            raise ValueError("模糊强度至少为 3")
        if white_alpha < 0 or white_alpha > 1:
            raise ValueError("白框透明度必须在 0 到 1 之间")

        return {
            'subtitle_cover_enabled': self.subtitle_cover_enabled_var.get(),
            'subtitle_cover_workspace': subtitle_cover_workspace,
            'subtitle_cover_detect_fps': detect_fps,
            'subtitle_cover_min_score': min_score,
            'subtitle_cover_mode': self.subtitle_cover_mode_var.get(),
            'subtitle_cover_blur_kernel': blur_kernel,
            'subtitle_cover_white_alpha': white_alpha,
            'subtitle_ai_workspace': subtitle_ai_workspace,
            'subtitle_ai_auto_detect': True,
            'subtitle_ai_require_cuda': True,
            'subtitle_ai_quality': 18,
            'subtitle_ai_preview_first': self.subtitle_ai_preview_first_var.get(),
            'subtitle_ai_preview_seconds': 8,
        }

    def _create_focus_stage_row(self, parent, key, title, bootstyle):
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=4)

        header = ttk.Frame(row)
        header.pack(fill=tk.X)
        ttk.Label(header, text=title).pack(side=tk.LEFT)
        status_var = tk.StringVar(value="等待")
        ttk.Label(header, textvariable=status_var, bootstyle=bootstyle).pack(side=tk.RIGHT)

        progress_var = tk.DoubleVar(value=0)
        progress_kwargs = {
            "variable": progress_var,
            "maximum": 100,
        }
        if HAS_TTKBOOTSTRAP_EXTENSIONS:
            progress_kwargs["bootstyle"] = (bootstyle, STRIPED)
        ttk.Progressbar(row, **progress_kwargs).pack(fill=tk.X, pady=(4, 2))

        message_var = tk.StringVar(value="尚未开始")
        ttk.Label(row, textvariable=message_var, foreground="gray").pack(anchor="w")

        self.focus_stage_widgets[key] = {
            "status": status_var,
            "progress": progress_var,
            "message": message_var,
        }

    def _reset_focus_panel(self):
        self.focus_url = None
        self.focus_url_var.set("等待新的下载任务")
        self.pipeline_message_var.set("这里会显示当前任务的下载、FFmpeg 和模糊处理进度。")
        for widgets in self.focus_stage_widgets.values():
            widgets["status"].set("等待")
            widgets["progress"].set(0)
            widgets["message"].set("尚未开始")

    def _set_focus_url(self, url):
        self.focus_url = url
        self._refresh_focus_panel()

    def _refresh_focus_panel(self):
        if not self.focus_url or self.focus_url not in self.pipeline_state:
            self._reset_focus_panel()
            return

        record = self.pipeline_state[self.focus_url]
        display_url = self.focus_url[:72] + "..." if len(self.focus_url) > 72 else self.focus_url
        self.focus_url_var.set(display_url)
        self.pipeline_message_var.set(record.get("last_message") or "等待新的阶段更新")

        for stage, widgets in self.focus_stage_widgets.items():
            stage_data = record["stages"].get(stage, {})
            widgets["status"].set(stage_data.get("status", "等待"))
            widgets["progress"].set(stage_data.get("progress", 0))
            widgets["message"].set(stage_data.get("message", "尚未开始"))

    def _ensure_pipeline_record(self, url):
        if url not in self.pipeline_state:
            self.pipeline_state[url] = {
                "last_message": "等待开始",
                "display_stage": "等待",
                "display_progress": 0,
                "stages": {
                    "resolve": {"status": "等待", "progress": 0, "message": "等待解析"},
                    "download": {"status": "等待", "progress": 0, "message": "等待下载"},
                    "ffmpeg": {"status": "等待", "progress": 0, "message": "等待后处理"},
                    "blur": {"status": "等待", "progress": 0, "message": "等待模糊处理"},
                },
            }
        return self.pipeline_state[url]

    def _ensure_pipeline_record_clean(self, url):
        if url not in self.pipeline_state:
            now = time.time()
            self.pipeline_state[url] = {
                "last_message": "等待开始",
                "display_stage": "等待",
                "display_progress": 0,
                "started_at": now,
                "download_done_at": None,
                "completed_at": None,
                "stages": {
                    "resolve": {"status": "等待", "progress": 0, "message": "等待解析"},
                    "download": {"status": "等待", "progress": 0, "message": "等待下载"},
                    "ffmpeg": {"status": "等待", "progress": 0, "message": "等待后处理"},
                    "blur": {"status": "等待", "progress": 0, "message": "等待模糊处理"},
                },
            }
        return self.pipeline_state[url]

    def _pipeline_stage_label(self, stage):
        return {
            "resolve": "解析",
            "download": "下载",
            "ffmpeg": "FFmpeg",
            "blur": "模糊",
        }.get(stage, stage)

    def _pipeline_status_label(self, status):
        return {
            "running": "进行中",
            "done": "完成",
            "error": "异常",
            "skipped": "跳过",
        }.get(status, status or "等待")

    def _handle_pipeline_event(self, url, event):
        record = self._ensure_pipeline_record_clean(url)
        stage = event.get("stage")
        if stage not in record["stages"]:
            return

        status = self._pipeline_status_label(event.get("status"))
        progress = event.get("progress")
        if progress is None:
            progress = record["stages"][stage]["progress"]
        progress = max(0, min(100, float(progress)))
        message = event.get("message") or record["stages"][stage]["message"]

        record["stages"][stage] = {
            "status": status,
            "progress": progress,
            "message": message,
        }
        if stage == "download" and status == "完成" and not record.get("download_done_at"):
            record["download_done_at"] = time.time()
            download_phase = record["download_done_at"] - record.get("started_at", record["download_done_at"])
            if download_phase > 0:
                self.download_phase_times.append(download_phase)
        record["display_stage"] = self._pipeline_stage_label(stage)
        record["display_progress"] = int(progress)
        record["last_message"] = message

        self._update_download_status(
            url,
            status=record["display_stage"],
            progress_text=f"{int(progress)}%",
        )

        if self.focus_url is None or self.focus_url == url or status == "进行中":
            self.focus_url = url
            self._refresh_focus_panel()

    def _on_current_download_select(self, _event):
        selection = self.current_downloads.selection()
        if not selection:
            return
        item_id = selection[0]
        url = getattr(self, f"download_url_{item_id}", None)
        if url:
            self._set_focus_url(url)

    def _create_tooltip(self, widget, text):
        """Create a tooltip for a widget using safer implementation"""
        # Don't create tooltips in this version to avoid UI glitches
        pass

    def _show_tooltip(self, event, text):
        """Show tooltip when mouse enters widget (disabled)"""
        pass

    def _hide_tooltip(self, event):
        """Hide tooltip when mouse leaves widget (disabled)"""
        pass

    def _highlight_urls(self, event=None):
        """Highlight valid URLs in the text input"""
        # Count URLs first
        self._count_urls()

        if not hasattr(self, 'batch_text'):
            return

        # Get all text
        content = self.batch_text.get(1.0, tk.END)

        # Clear existing tags
        self.batch_text.tag_remove("valid_url", "1.0", tk.END)
        self.batch_text.tag_remove("invalid_url", "1.0", tk.END)

        # Configure tags
        self.batch_text.tag_configure("valid_url", foreground="green")
        self.batch_text.tag_configure("invalid_url", foreground="red")

        # Extract URLs from text
        urls = extract_urls_from_text(content)

        # Find and tag all URLs
        for url in urls:
            # Escape special regex characters in the URL
            escaped_url = re.escape(url)

            # Find all occurrences of the URL in the text
            for match in re.finditer(escaped_url, content):
                start_idx = match.start()
                end_idx = match.end()

                # Calculate line and column
                start_line = content[:start_idx].count('\n') + 1
                start_col = start_idx - content[:start_idx].rfind('\n') - 1

                end_line = content[:end_idx].count('\n') + 1
                end_col = end_idx - content[:end_idx].rfind('\n') - 1

                start_pos = f"{start_line}.{start_col}"
                end_pos = f"{end_line}.{end_col}"

                # Always tag as valid_url when extracted by extract_urls_from_text
                self.batch_text.tag_add("valid_url", start_pos, end_pos)

    def _count_urls(self):
        """Count and update the URL counter"""
        if not hasattr(self, 'batch_text') or not hasattr(self, 'url_count_var'):
            return

        content = self.batch_text.get(1.0, tk.END)
        urls = extract_urls_from_text(content)

        # Update the counter
        url_count = len(urls)
        if url_count == 1:
            self.url_count_var.set(f"1 URL")
        else:
            self.url_count_var.set(f"{url_count} URLs")

    def _remove_url_from_batch_input(self, target_url):
        """Remove a successfully completed URL from the batch input box."""
        if not hasattr(self, 'batch_text'):
            return

        content = self.batch_text.get(1.0, tk.END)
        kept_lines = []

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            normalized_line = extract_and_clean_url(line)
            if normalized_line == target_url:
                continue
            kept_lines.append(raw_line)

        self.batch_text.delete(1.0, tk.END)
        if kept_lines:
            self.batch_text.insert(tk.END, "\n".join(kept_lines) + "\n")
        self._highlight_urls()

    def _update_elapsed_time(self):
        """Update the elapsed time display"""
        if self.start_time is None or not self.is_downloading or self.paused:
            return

        # Calculate elapsed time
        elapsed_seconds = int(time.time() - self.start_time)
        hours = elapsed_seconds // 3600
        minutes = (elapsed_seconds % 3600) // 60
        seconds = elapsed_seconds % 60

        # Update display
        self.elapsed_var.set(f"{hours:02d}:{minutes:02d}:{seconds:02d}")
        self.eta_var.set(self._format_time(self._estimate_remaining_seconds()))

    def _format_time(self, seconds):
        """Format seconds into HH:MM:SS"""
        if seconds is None:
            return "--:--:--"

        hours = int(seconds) // 3600
        minutes = (int(seconds) % 3600) // 60
        secs = int(seconds) % 60

        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def _average_or_default(self, values, default_value):
        if values:
            return sum(values) / len(values)
        return default_value

    def _estimate_remaining_seconds(self):
        if not getattr(self, "total_urls", 0):
            return None

        completed_total = self.completed + self.failed
        if completed_total >= self.total_urls:
            return 0

        avg_total = self._average_or_default(self.download_times, 75.0)
        avg_download = self._average_or_default(self.download_phase_times, min(avg_total * 0.35, 20.0))
        avg_post = self._average_or_default(self.postprocess_phase_times, max(avg_total - avg_download, 55.0))

        pending_download = 0
        queued_post = 0
        active_post_fraction = 0.0

        for record in self.pipeline_state.values():
            if record.get("completed_at"):
                continue

            download_done_at = record.get("download_done_at")
            if not download_done_at:
                pending_download += 1
                continue

            ffmpeg_progress = float(record["stages"].get("ffmpeg", {}).get("progress", 0) or 0)
            blur_progress = float(record["stages"].get("blur", {}).get("progress", 0) or 0)
            stage_fraction = max(ffmpeg_progress, blur_progress) / 100.0

            if stage_fraction > 0:
                active_post_fraction = max(active_post_fraction, stage_fraction)
            else:
                queued_post += 1

        effective_download_workers = max(int(getattr(self, "max_workers", 1) or 1), 1)
        remaining_download = (pending_download / effective_download_workers) * avg_download
        remaining_post = queued_post * avg_post
        if active_post_fraction > 0:
            remaining_post += max(avg_post * (1.0 - active_post_fraction), avg_post * 0.1)

        # If there are still videos not yet downloaded, each of them will also need post-processing later.
        remaining_post += pending_download * avg_post

        estimate = remaining_download + remaining_post
        return max(int(estimate), 0)

    def _extract_from_file(self):
        """Extract URLs from a text file"""
        file_path = filedialog.askopenfilename(
            title=self.translator.translate("batch_tab", "select_text_file"),
            filetypes=[
                (self.translator.translate("batch_tab", "text_files"), "*.txt"),
                (self.translator.translate("batch_tab", "all_files"), "*.*")
            ]
        )

        if not file_path:
            return

        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                content = file.read()

            # Extract URLs
            urls = extract_urls_from_text(content)

            if not urls:
                messagebox.showwarning(
                    self.translator.translate("batch_tab", "warning_title"),
                    self.translator.translate("batch_tab", "no_urls_found_in_file")
                )
                return

            # Add URLs to text field
            current_text = self.batch_text.get(1.0, tk.END).strip()
            if current_text:
                self.batch_text.insert(tk.END, "\n")

            self.batch_text.insert(tk.END, "\n".join(urls))
            self._highlight_urls()

            # Show success message
            ToastNotification(
                title=self.translator.translate("batch_tab", "urls_extracted"),
                message=self.translator.translate("batch_tab", "found_urls_in_file").format(count=len(urls)),
                duration=3000
            ).show_toast()

        except Exception as e:
            messagebox.showerror(
                self.translator.translate("batch_tab", "error_title"),
                self.translator.translate("batch_tab", "file_read_error").format(error=str(e))
            )

    def _export_results(self):
        """Export download results to a text file"""
        if not self.successful_urls and not self.failed_urls:
            messagebox.showinfo(
                self.translator.translate("batch_tab", "info_title"),
                self.translator.translate("batch_tab", "no_results_to_export")
            )
            return

        file_path = filedialog.asksaveasfilename(
            title=self.translator.translate("batch_tab", "save_results"),
            defaultextension=".txt",
            filetypes=[
                (self.translator.translate("batch_tab", "text_files"), "*.txt"),
                (self.translator.translate("batch_tab", "all_files"), "*.*")
            ]
        )

        if not file_path:
            return

        try:
            with open(file_path, 'w', encoding='utf-8') as file:
                file.write(f"=== {self.translator.translate('batch_tab', 'download_results')} ===\n\n")
                file.write(f"{self.translator.translate('batch_tab', 'date')}: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                file.write(f"{self.translator.translate('batch_tab', 'total_videos')}: {len(self.successful_urls) + len(self.failed_urls)}\n")
                file.write(f"{self.translator.translate('batch_tab', 'successful')}: {len(self.successful_urls)}\n")
                file.write(f"{self.translator.translate('batch_tab', 'failed')}: {len(self.failed_urls)}\n\n")

                if self.successful_urls:
                    file.write(f"=== {self.translator.translate('batch_tab', 'successful_downloads')} ===\n")
                    for url in self.successful_urls:
                        file.write(f"- {url}\n")
                    file.write("\n")

                if self.failed_urls:
                    file.write(f"=== {self.translator.translate('batch_tab', 'failed_downloads')} ===\n")
                    for url in self.failed_urls:
                        file.write(f"- {url}\n")

            # Show success message
            self._show_toast(
                self.translator.translate("batch_tab", "export_successful"),
                self.translator.translate("batch_tab", "results_saved").format(path=file_path),
                3000
            )

            # Open the file location
            folder_path = file_path.rsplit('/', 1)[0] if '/' in file_path else file_path.rsplit('\\', 1)[0]
            if folder_path:
                open_folder(folder_path)
            else:
                open_folder(".")  # Open current directory as fallback

        except Exception as e:
            messagebox.showerror(
                self.translator.translate("batch_tab", "error_title"),
                self.translator.translate("batch_tab", "export_error").format(error=str(e))
            )

    def update_language(self):
        """Update the UI text when language changes"""
        # Update all LabelFrames
        for child in self.frame.winfo_children():
            if isinstance(child, ttk.Frame):
                for widget in child.winfo_children():
                    if isinstance(widget, ttk.LabelFrame):
                        if "batch_links" in str(widget).lower():
                            widget.configure(text=self.translator.translate("batch_tab", "batch_links"))
                        elif "download_results" in str(widget).lower():
                            widget.configure(text=self.translator.translate("batch_tab", "download_results"))

        # Update all buttons
        for child in self.frame.winfo_children():
            if isinstance(child, ttk.Frame):
                for container in child.winfo_children():
                    if isinstance(container, ttk.Frame):
                        for widget in container.winfo_children():
                            # Control buttons
                            if isinstance(widget, ttk.Button):
                                if "paste" in str(widget).lower():
                                    widget.configure(text=self.translator.translate("batch_tab", "paste_button"))
                                elif "clear" in str(widget).lower():
                                    widget.configure(text=self.translator.translate("batch_tab", "clear_button"))
                                elif "extract" in str(widget).lower():
                                    widget.configure(text=self.translator.translate("batch_tab", "extract_urls_button"))
                                elif "start" in str(widget).lower():
                                    widget.configure(text=self.translator.translate("batch_tab", "start_batch_download"))
                                elif "stop" in str(widget).lower():
                                    widget.configure(text=self.translator.translate("batch_tab", "stop_download"))

                            # Custom save location checkbox
                            elif isinstance(widget, ttk.Checkbutton):
                                widget.configure(text=self.translator.translate("batch_tab", "custom_save_location"))

                            # Thread count label
                            elif isinstance(widget, ttk.Label) and "concurrent" in str(widget).lower():
                                widget.configure(text=self.translator.translate("batch_tab", "concurrent_downloads"))

        # Update status if it's at default
        if self.status_var.get() in ["Ready", "就绪"]:
            self.status_var.set(self.translator.translate("batch_tab", "status_ready"))

    def _toggle_custom_save(self):
        """Toggle custom save location"""
        if self.custom_save_var.get():
            # Open folder selection dialog
            custom_path = filedialog.askdirectory(
                title=self.translator.translate("batch_tab", "select_download_folder"),
                initialdir=self.app.download_path
            )
            if custom_path:
                self.custom_download_path = custom_path
                self._refresh_save_location_display()
            else:
                # Uncheck if no folder selected
                self.custom_save_var.set(False)
                self._refresh_save_location_display()
        else:
            # Reset to default path
            self.custom_download_path = self.app.download_path
            self._refresh_save_location_display()

    def _get_base_download_path(self):
        if self.custom_save_var.get():
            return self.custom_download_path
        return self.app.download_path

    def _resolve_batch_download_path(self):
        base_path = os.path.abspath(self._get_base_download_path())
        subfolder = self.batch_subfolder_var.get().strip()
        if not subfolder:
            return base_path

        normalized_subfolder = os.path.normpath(subfolder)
        if normalized_subfolder in ("", "."):
            return base_path
        if os.path.isabs(normalized_subfolder):
            raise ValueError("本批子文件夹请填写相对路径，不要直接填写完整磁盘路径。")

        resolved_path = os.path.abspath(os.path.join(base_path, normalized_subfolder))
        if os.path.commonpath([base_path, resolved_path]) != base_path:
            raise ValueError("本批子文件夹不能跳出当前下载目录。")
        return resolved_path

    def _refresh_save_location_display(self):
        try:
            display_path = self._resolve_batch_download_path()
        except ValueError:
            base_path = os.path.abspath(self._get_base_download_path())
            raw_subfolder = self.batch_subfolder_var.get().strip()
            display_path = f"{base_path}  ->  {raw_subfolder or '(无效子文件夹)'}"
        self.location_display.config(text=display_path)

    def _open_active_download_folder(self):
        target_path = getattr(self, "batch_download_path", None)
        if not target_path:
            try:
                target_path = self._resolve_batch_download_path()
            except ValueError:
                target_path = self._get_base_download_path()
        open_folder(target_path)

    def _extract_urls(self):
        """Extract and display unique URLs from batch text"""
        text_content = self.batch_text.get(1.0, tk.END).strip()
        if not text_content:
            messagebox.showwarning(
                self.translator.translate("batch_tab", "warning_title"),
                self.translator.translate("batch_tab", "no_text_to_extract")
            )
            return

        # Extract URLs
        urls = extract_urls_from_text(text_content)

        if not urls:
            messagebox.showwarning(
                self.translator.translate("batch_tab", "warning_title"),
                self.translator.translate("batch_tab", "no_urls_found")
            )
            return

        # 更智能的去重和验证
        unique_urls = []
        seen = set()
        for url in urls:
            # 标准化 URL，去除可能的尾部空格和重复查询参数
            normalized_url = extract_and_clean_url(url.strip())
            if normalized_url and normalized_url not in seen:
                unique_urls.append(normalized_url)
                seen.add(normalized_url)

        # 清除并插入唯一 URL
        self.batch_text.delete(1.0, tk.END)
        self.batch_text.insert(tk.END, "\n".join(unique_urls))

        # 显示去重和验证结果
        total_found = len(urls)
        unique_count = len(unique_urls)

        # 根据去重结果显示不同的消息
        if unique_count == total_found:
            messagebox.showinfo(
                self.translator.translate("batch_tab", "urls_extracted"),
                self.translator.translate("batch_tab", "found_unique_urls").format(count=unique_count)
            )
        else:
            messagebox.showinfo(
                self.translator.translate("batch_tab", "urls_processed"),
                self.translator.translate("batch_tab", "urls_processed_details").format(
                    total=total_found,
                    unique=unique_count,
                    removed=total_found - unique_count
                )
            )

    def _paste_to_batch(self):
        """Paste clipboard content to batch text area"""
        try:
            clipboard_content = self.frame.clipboard_get().strip()
            if clipboard_content:
                self.batch_text.insert(tk.INSERT, clipboard_content + "\n")
            else:
                messagebox.showwarning(
                    self.translator.translate("batch_tab", "warning_title"),
                    self.translator.translate("batch_tab", "clipboard_empty")
                )
        except Exception:
            messagebox.showwarning(
                self.translator.translate("batch_tab", "warning_title"),
                self.translator.translate("batch_tab", "clipboard_error")
            )

    def _show_toast(self, title, message, duration=3000, style=None):
        """Show toast notification with fallback to messagebox"""
        if HAS_TTKBOOTSTRAP_EXTENSIONS:
            try:
                ToastNotification(
                    title=title,
                    message=message,
                    duration=duration,
                    bootstyle=style
                ).show_toast()
            except:
                messagebox.showinfo(title, message)
        else:
            messagebox.showinfo(title, message)

    def _toggle_pause(self):
        """Toggle pause/resume download"""
        if not self.is_downloading:
            return

        self.paused = not self.paused

        if self.paused:
            # Update button to show resume
            try:
                self.pause_button.config(
                    text=self.translator.translate("batch_tab", "resume_download"),
                    style="success.TButton" if HAS_TTKBOOTSTRAP_EXTENSIONS else None
                )
            except:
                self.pause_button.config(
                    text=self.translator.translate("batch_tab", "resume_download")
                )

            self.status_var.set(self.translator.translate("batch_tab", "download_paused"))

            # Update progress bar style if possible
            if HAS_TTKBOOTSTRAP_EXTENSIONS:
                try:
                    self.progress.configure(bootstyle=(WARNING, STRIPED))
                except:
                    pass
        else:
            # Update button to show pause
            try:
                self.pause_button.config(
                    text=self.translator.translate("batch_tab", "pause_download"),
                    style="warning.TButton" if HAS_TTKBOOTSTRAP_EXTENSIONS else None
                )
            except:
                self.pause_button.config(
                    text=self.translator.translate("batch_tab", "pause_download")
                )

            self.status_var.set(self.translator.translate("batch_tab", "download_resumed"))

            # Update progress bar style if possible
            if HAS_TTKBOOTSTRAP_EXTENSIONS:
                try:
                    self.progress.configure(bootstyle=(SUCCESS, STRIPED))
                except:
                    pass

    def _batch_download(self):
        """Handle batch download button click"""
        # Check if already downloading
        if self.is_downloading:
            messagebox.showinfo(
                self.translator.translate("batch_tab", "info_title"),
                self.translator.translate("batch_tab", "download_in_progress")
            )
            return

        text_content = self.batch_text.get(1.0, tk.END).strip()
        if not text_content:
            messagebox.showwarning(
                self.translator.translate("batch_tab", "warning_title"),
                self.translator.translate("batch_tab", "enter_video_urls")
            )
            return

        if not self.app.client.is_configured:
            messagebox.showwarning(
                self.translator.translate("batch_tab", "warning_title"),
                self.translator.translate("batch_tab", "setup_api_key_first")
            )
            return

        # Extract URLs from text
        urls = extract_urls_from_text(text_content)

        if not urls:
            messagebox.showwarning(
                self.translator.translate("batch_tab", "warning_title"),
                self.translator.translate("batch_tab", "no_valid_urls")
            )
            return

        # Confirm large downloads
        if len(urls) > 10:
            confirm = messagebox.askyesno(
                self.translator.translate("batch_tab", "confirm_batch_download"),
                self.translator.translate("batch_tab", "confirm_large_download").format(count=len(urls))
            )
            if not confirm:
                return

        # Determine download path
        try:
            download_path = self._resolve_batch_download_path()
        except ValueError as e:
            messagebox.showerror(
                self.translator.translate("batch_tab", "error_title"),
                str(e)
            )
            return

        # Get settings
        settings = {}
        if hasattr(self.app, 'settings_tab'):
            settings = self.app.settings_tab.get_settings()

        try:
            settings.update(self._collect_batch_processing_settings())
        except Exception as e:
            messagebox.showerror(
                self.translator.translate("batch_tab", "error_title"),
                str(e)
            )
            return

        self.app.config.set('subtitle_cover_enabled', settings.get('subtitle_cover_enabled', False))
        self.app.config.set('subtitle_cover_workspace', settings.get('subtitle_cover_workspace', ''))
        self.app.config.set('subtitle_cover_detect_fps', settings.get('subtitle_cover_detect_fps', 1.5))
        self.app.config.set('subtitle_cover_min_score', settings.get('subtitle_cover_min_score', 0.38))
        self.app.config.set('subtitle_cover_mode', settings.get('subtitle_cover_mode', 'blur'))
        self.app.config.set('subtitle_cover_blur_kernel', settings.get('subtitle_cover_blur_kernel', 41))
        self.app.config.set('subtitle_cover_white_alpha', settings.get('subtitle_cover_white_alpha', 0.88))
        self.app.config.set('subtitle_ai_workspace', settings.get('subtitle_ai_workspace', ''))
        self.app.config.set('subtitle_ai_auto_detect', settings.get('subtitle_ai_auto_detect', True))
        self.app.config.set('subtitle_ai_require_cuda', settings.get('subtitle_ai_require_cuda', True))
        self.app.config.set('subtitle_ai_quality', settings.get('subtitle_ai_quality', 18))
        self.app.config.set('subtitle_ai_preview_first', settings.get('subtitle_ai_preview_first', True))
        self.app.config.set('subtitle_ai_preview_seconds', settings.get('subtitle_ai_preview_seconds', 8))

        # Prepare for download
        self.is_downloading = True
        self.stop_download = False
        self.paused = False
        self.start_time = time.time()

        # Update UI
        self.download_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.pause_button.config(state=tk.NORMAL)

        # Try to update progress bar style
        if HAS_TTKBOOTSTRAP_EXTENSIONS:
            try:
                self.progress.configure(bootstyle=(SUCCESS, STRIPED))
            except:
                pass

        # Clear current downloads
        for item in self.current_downloads.get_children():
            self.current_downloads.delete(item)

        # Reset statistics
        self.completed = 0
        self.failed = 0
        self.successful_urls = []
        self.failed_urls = []
        self.warning_urls = []
        self.download_times = []
        self.download_phase_times = []
        self.postprocess_phase_times = []
        self.currently_downloading = set()
        self.pipeline_state = {}
        self._reset_focus_panel()

        # Reset progress
        self._update_progress(0, len(urls))
        self.status_var.set(self.translator.translate("batch_tab", "downloading_videos").format(count=len(urls)))
        self.success_var.set("0")
        self.failed_var.set("0")
        self.elapsed_var.set("00:00:00")
        self.eta_var.set("--:--:--")
        self.percent_var.set("0%")

        # Get thread count from UI
        self.max_workers = self.thread_count_var.get()

        self.batch_download_path = download_path
        self.batch_settings = settings

        # Show notification
        self._show_toast(
            self.translator.translate("batch_tab", "download_started"),
            self.translator.translate("batch_tab", "batch_download_started").format(count=len(urls)),
            3000,
            "success"
        )

        # Start download in background thread
        self.download_thread = threading.Thread(
            target=self._batch_download_thread,
            args=(urls,),
            daemon=True
        )
        self.download_thread.start()

    def _download_single_video(self, url, index, total, preview_gate=False):
        """Download a single video in a worker thread

        Args:
            url: Video URL
            downloader: VideoDownloader instance
            index: Current index in the batch
            total: Total number of URLs

        Returns:
            tuple: (success, url, download_time, error_message)
        """
        if self.stop_download:
            return (False, url, 0, "Download stopped by user")

        # Add to currently downloading list
        self.app.root.after(0, lambda: self._add_to_current_downloads(url))

        # Handle pause state
        while self.paused and not self.stop_download:
            # Update UI to show paused status
            self.app.root.after(0, lambda u=url: self._update_download_status(u, "Paused"))
            time.sleep(0.5)

        if self.stop_download:
            self.app.root.after(0, lambda u=url: self._remove_from_current_downloads(u))
            return (False, url, 0, "Download stopped by user")

        # Start timing
        start_time = time.time()
        try:
            # Update status
            self.app.root.after(0, lambda u=url: self._update_download_status(u, status="解析", progress_text="0%"))
            self.app.root.after(
                0,
                lambda u=url: self._handle_pipeline_event(
                    u,
                    {"stage": "resolve", "status": "running", "progress": 10, "message": "正在获取视频信息"},
                ),
            )

            # Get video info using the refactored API
            video_info = self.app.client.get_data(url, clean_data=True)

            if not video_info:
                self.app.root.after(0, lambda u=url: self._remove_from_current_downloads(u))
                return (False, url, time.time() - start_time, "Failed to retrieve video info")

            # Check if video_urls exists in the response
            if not video_info.get('video_urls'):
                self.app.root.after(0, lambda u=url: self._remove_from_current_downloads(u))
                return (False, url, time.time() - start_time, "No video URLs found in response")

            self.app.root.after(
                0,
                lambda u=url: self._handle_pipeline_event(
                    u,
                    {"stage": "resolve", "status": "done", "progress": 100, "message": "链接解析完成"},
                ),
            )

            settings = getattr(self, "batch_settings", {})
            downloader = VideoDownloader(
                download_path=getattr(self, "batch_download_path", self.app.download_path),
                use_description=settings.get('rename_with_desc', False),
                skip_existing=settings.get('skip_existing', True),
                max_workers=1,
                postprocess_enabled=settings.get('postprocess_enabled', False),
                trim_start_seconds=settings.get('postprocess_trim_start_seconds', 1.5),
                mirror_video=settings.get('postprocess_mirror_video', True),
                delete_original_after_processing=settings.get('postprocess_delete_original', False),
                ffmpeg_path=settings.get('ffmpeg_path'),
                subtitle_cover_enabled=(settings.get('subtitle_cover_enabled', False) and not preview_gate),
                subtitle_cover_workspace=settings.get('subtitle_cover_workspace'),
                subtitle_cover_detect_fps=settings.get('subtitle_cover_detect_fps', 1.5),
                subtitle_cover_min_score=settings.get('subtitle_cover_min_score', 0.38),
                subtitle_cover_mode=settings.get('subtitle_cover_mode', 'blur'),
                subtitle_cover_blur_kernel=settings.get('subtitle_cover_blur_kernel', 41),
                subtitle_cover_white_alpha=settings.get('subtitle_cover_white_alpha', 0.88),
                subtitle_ai_workspace=settings.get('subtitle_ai_workspace'),
                subtitle_ai_auto_detect=settings.get('subtitle_ai_auto_detect', True),
                subtitle_ai_require_cuda=settings.get('subtitle_ai_require_cuda', True),
                subtitle_ai_quality=settings.get('subtitle_ai_quality', 18),
                subtitle_ai_area_cache=settings.get('subtitle_ai_area_cache'),
                pipeline_callback=lambda event, u=url: self.app.root.after(
                    0, lambda e=event, target_url=u: self._handle_pipeline_event(target_url, e)
                ),
            )

            # Create progress callback for this download
            def progress_callback(current, total):
                if total <= 0:
                    return
                percent = int((current / total) * 100)
                self.app.root.after(
                    0,
                    lambda u=url, p=percent: self._handle_pipeline_event(
                        u,
                        {"stage": "download", "status": "running", "progress": p, "message": f"视频下载 {p}%"},
                    ),
                )

            # Ensure media_type is set
            if 'media_type' not in video_info:
                video_info['media_type'] = 'video'  # Default to video type in Batch Tab

            # Download video using main_downloader
            result = downloader.main_downloader(
                video_info,
                progress_callback=progress_callback,
                max_retries=3  # Use default or get from settings
            )

            # Remove from current downloads
            self.app.root.after(0, lambda u=url: self._remove_from_current_downloads(u))

            if result['success'] and result['files']:
                if preview_gate:
                    video_extensions = ('.mp4', '.mov', '.mkv', '.webm', '.avi')
                    source_video = next(
                        (
                            path for path in result['files']
                            if isinstance(path, str)
                            and os.path.isfile(path)
                            and path.lower().endswith(video_extensions)
                        ),
                        None,
                    )
                    if not source_video:
                        return (False, url, time.time() - start_time, "首条预览失败：没有找到已下载的视频文件")

                    preview_path, preview_error = downloader.create_ai_subtitle_preview(
                        source_video,
                        settings.get('subtitle_ai_preview_seconds', 8),
                    )
                    if not preview_path:
                        return (
                            False,
                            url,
                            time.time() - start_time,
                            f"首条预览生成失败，整批尚未启动：{preview_error}",
                        )

                    decision = self._request_ai_preview_decision(
                        preview_path,
                        settings.get('subtitle_cover_mode', 'ai-sttn'),
                    )
                    decision_path = os.path.join(os.path.dirname(preview_path), "preview_decision.json")
                    with open(decision_path, "w", encoding="utf-8") as stream:
                        json.dump(
                            {
                                "preview": preview_path,
                                "decision": decision,
                                "selected_mode_before_preview": settings.get('subtitle_cover_mode'),
                                "original_source_for_full_processing": source_video,
                                "timestamp": time.time(),
                            },
                            stream,
                            ensure_ascii=False,
                            indent=2,
                        )
                    if decision == "cancel":
                        self.stop_download = True
                        return (False, url, time.time() - start_time, "用户在首条预览后取消了整批处理")
                    if decision == "fast-blur":
                        settings['subtitle_cover_mode'] = 'fast-blur'
                        downloader.subtitle_cover_mode = 'fast-blur'
                    if downloader.subtitle_ai_area_cache:
                        settings['subtitle_ai_area_cache'] = downloader.subtitle_ai_area_cache

                    # The full result always starts from the untouched downloaded source,
                    # never from the already processed preview or an AI result.
                    full_output, full_error, _created = downloader._run_ai_subtitle_if_needed(source_video)
                    if not full_output:
                        return (
                            False,
                            url,
                            time.time() - start_time,
                            f"首条完整视频处理失败，整批尚未启动：{full_error}",
                        )
                    result['files'].append(full_output)

                warning_msg = "\n".join(result['errors']) if result['errors'] else None
                return (True, url, time.time() - start_time, warning_msg)
            else:
                error_msg = "\n".join(result['errors']) if result['errors'] else "Failed to save video"
                return (False, url, time.time() - start_time, error_msg)

        except Exception as e:
            print(f"Error downloading video {url}: {e}")
            # Remove from current downloads
            self.app.root.after(0, lambda u=url: self._remove_from_current_downloads(u))
            return (False, url, time.time() - start_time, str(e))

    def _add_to_current_downloads(self, url):
        """Add URL to the currently downloading list"""
        if url in self.currently_downloading:
            return

        # Add to tracking set
        self.currently_downloading.add(url)
        self._ensure_pipeline_record_clean(url)

        # Add to UI
        display_url = url[:40] + "..." if len(url) > 40 else url
        item_id = self.current_downloads.insert("", "end", values=(display_url, "等待", "0%", "0s"))

        # Store the item ID with the URL for later updates
        setattr(self, f"download_item_{hash(url)}", item_id)
        setattr(self, f"download_url_{item_id}", url)
        if self.focus_url is None:
            self._set_focus_url(url)

    def _update_download_status(self, url, status=None, progress_text=None):
        """Update the status of a downloading item"""
        if url not in self.currently_downloading:
            return

        # Get the item ID
        item_id = getattr(self, f"download_item_{hash(url)}", None)
        if not item_id:
            return

        # Calculate elapsed time for this download
        now = time.time()
        start_time = getattr(self, f"download_start_{hash(url)}", now)
        elapsed = int(now - start_time)

        # Store start time if not already set
        if not hasattr(self, f"download_start_{hash(url)}"):
            setattr(self, f"download_start_{hash(url)}", now)

        current_values = self.current_downloads.item(item_id, "values")
        current_stage = status if status is not None else (current_values[1] if len(current_values) > 1 else "等待")
        current_progress = progress_text if progress_text is not None else (current_values[2] if len(current_values) > 2 else "0%")

        # Update the item in the treeview
        self.current_downloads.item(item_id, values=(
            url[:40] + "..." if len(url) > 40 else url,
            current_stage,
            current_progress,
            f"{elapsed}s"
        ))

    def _remove_from_current_downloads(self, url):
        """Remove URL from the currently downloading list"""
        if url not in self.currently_downloading:
            return

        # Remove from tracking set
        self.currently_downloading.remove(url)

        # Remove from UI
        item_id = getattr(self, f"download_item_{hash(url)}", None)
        if item_id:
            self.current_downloads.delete(item_id)
            if hasattr(self, f"download_url_{item_id}"):
                delattr(self, f"download_url_{item_id}")

        # Clean up attributes
        if hasattr(self, f"download_item_{hash(url)}"):
            delattr(self, f"download_item_{hash(url)}")

        if hasattr(self, f"download_start_{hash(url)}"):
            delattr(self, f"download_start_{hash(url)}")

        if self.focus_url == url:
            self.focus_url = next(iter(self.currently_downloading), None)
            self._refresh_focus_panel()

    def _process_results_queue(self):
        """Process results from the queue and update UI"""
        try:
            # Process all available results without blocking
            while not self.result_queue.empty():
                success, url, download_time, error_message = self.result_queue.get_nowait()

                with self.lock:
                    if success:
                        self.completed += 1
                        self.successful_urls.append(url)
                        self.download_times.append(download_time)
                        self._remove_url_from_batch_input(url)
                        if error_message:
                            self.warning_urls.append((url, error_message))
                            self.logger.warning(f"Downloaded with warning {url}: {error_message}")

                        # Update success counter
                        self.success_var.set(str(self.completed))
                    else:
                        self.failed += 1
                        self.failed_urls.append(url)
                        if error_message:
                            self.logger.error(f"Failed to download {url}: {error_message}")

                        # Update failed counter
                        self.failed_var.set(str(self.failed))

                    total = self.completed + self.failed
                    record = self.pipeline_state.get(url)
                    if record and not record.get("completed_at"):
                        record["completed_at"] = time.time()
                        if record.get("download_done_at"):
                            postprocess_phase = record["completed_at"] - record["download_done_at"]
                            if postprocess_phase > 0:
                                self.postprocess_phase_times.append(postprocess_phase)
                    self._update_progress(total, self.total_urls)
                    estimated_remaining_time = self._estimate_remaining_seconds()
                    self.eta_var.set(self._format_time(estimated_remaining_time))
                    status_text = self.translator.translate("batch_tab", "downloading_with_eta").format(
                        active=len(self.currently_downloading),
                        total=self.total_urls,
                        eta=self._format_time(estimated_remaining_time)
                    )
                    self.status_var.set(status_text)

                self.result_queue.task_done()
        except queue.Empty:
            pass

    def _request_ai_preview_decision(self, preview_path, selected_mode):
        """Open the preview on the UI thread and wait for an explicit batch decision."""
        finished = threading.Event()
        answer = {"value": "cancel"}

        def prompt():
            try:
                if os.name == "nt":
                    try:
                        os.startfile(preview_path)
                    except OSError as exc:
                        self.logger.warning("Could not open preview automatically: %s", exc)
                is_ai = selected_mode in ("ai-sttn", "ai-lama")
                detail = (
                    "“是”：使用当前 AI 模式处理首条完整视频和其余视频。\n"
                    "“否”：从每条原视频开始改用高速模糊，不会对 AI 结果再次处理。\n"
                    "“取消”：停止整批，保留预览、原视频和全部日志。"
                    if is_ai else
                    "“是”：继续使用高速模糊处理整批。\n“取消”：停止整批并保留预览和日志。"
                )
                choice = messagebox.askyesnocancel("确认首条字幕预览", f"已打开 8 秒预览：\n{preview_path}\n\n{detail}")
                if choice is True:
                    answer["value"] = "continue"
                elif choice is False and is_ai:
                    answer["value"] = "fast-blur"
                else:
                    answer["value"] = "cancel"
            finally:
                finished.set()

        self.app.root.after(0, prompt)
        finished.wait()
        return answer["value"]

    def _batch_download_thread(self, urls):
        """Background thread function for batch download using thread pool

        Args:
            urls: List of video URLs
            downloader: VideoDownloader instance
        """
        try:
            self.total_urls = len(urls)

            remaining_urls = list(urls)
            preview_first = bool(
                getattr(self, "batch_settings", {}).get('subtitle_ai_preview_first', True)
                and getattr(self, "batch_settings", {}).get('subtitle_cover_enabled', False)
                and getattr(self, "batch_settings", {}).get('subtitle_cover_mode') in ('ai-sttn', 'ai-lama', 'fast-blur')
            )
            if preview_first and remaining_urls:
                first_url = remaining_urls.pop(0)
                first_result = self._download_single_video(first_url, 1, self.total_urls, preview_gate=True)
                self.result_queue.put(first_result)
                if not first_result[0] or self.stop_download:
                    remaining_urls = []

            # Create a thread pool only after the first preview has been confirmed.
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                # Submit all download tasks
                futures = []
                offset = 1 if preview_first else 0
                for i, url in enumerate(remaining_urls):
                    if self.stop_download:
                        break

                    future = executor.submit(
                        self._download_single_video,
                        url,
                        i + 1 + offset,
                        self.total_urls
                    )
                    futures.append(future)

                # Wait for all futures to complete
                for future in futures:
                    if not self.stop_download:
                        try:
                            result = future.result()
                            self.result_queue.put(result)
                        except Exception as e:
                            print(f"Future exception: {e}")

            # Ensure all results are processed
            self.app.root.after(500, self._process_results_queue)

            # Wait for the queue to be fully processed
            self.result_queue.join()

            # Finalize download
            self.app.root.after(500, self._on_batch_download_complete)

        except Exception as e:
            print(f"Batch download error: {e}")
            self.app.root.after(0, lambda:
                messagebox.showerror(
                    self.translator.translate("batch_tab", "download_error"),
                    str(e)
                )
            )

    def _on_batch_download_complete(self):
        """Handle batch download completion"""
        # Reset UI state
        self.is_downloading = False
        self.download_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.pause_button.config(state=tk.DISABLED)

        # Calculate timing stats
        total_time = time.time() - self.start_time if self.start_time else 0

        # Clean up any remaining items in current downloads
        for item in self.current_downloads.get_children():
            self.current_downloads.delete(item)
        self.currently_downloading.clear()

        # Update status and progress
        total = self.total_urls
        completed = self.completed
        failed = self.failed

        self.status_var.set(self.translator.translate("batch_tab", "batch_download_complete_status").format(
            completed=completed,
            failed=failed
        ))
        self.progress_var.set(f"{completed}/{total}")
        self.progress["value"] = 100
        self.percent_var.set("100%")

        # Set progress bar color based on results if possible
        if HAS_TTKBOOTSTRAP_EXTENSIONS:
            try:
                if failed == 0:
                    self.progress.configure(bootstyle=SUCCESS)
                elif completed > failed:
                    self.progress.configure(bootstyle=INFO)
                else:
                    self.progress.configure(bootstyle=WARNING)
            except:
                pass

        # Show toast notification for completion
        toast_style = "success" if failed == 0 else ("info" if completed > failed else "warning")

        download_message = self.translator.translate("batch_tab", "batch_download_complete_toast").format(
            total=total,
            completed=completed,
            failed=failed,
            time=self._format_time(total_time)
        )

        self._show_toast(
            self.translator.translate("batch_tab", "download_complete"),
            download_message,
            5000,
            toast_style
        )

        # Update success and failed lists
        self.success_list.config(state=tk.NORMAL)
        self.failed_list.config(state=tk.NORMAL)

        # Clear previous content
        self.success_list.delete(1.0, tk.END)
        self.failed_list.delete(1.0, tk.END)

        # Add successful download URLs
        if self.successful_urls:
            self.success_list.insert(tk.END, self.translator.translate("batch_tab", "successful_videos") + "\n")
            for url in self.successful_urls:
                self.success_list.insert(tk.END, f"- {url}\n")
        else:
            self.success_list.insert(tk.END, self.translator.translate("batch_tab", "no_successful_videos") + "\n")

        # Add failed download URLs
        if self.failed_urls:
            self.failed_list.insert(tk.END, self.translator.translate("batch_tab", "failed_videos") + "\n")
            for url in self.failed_urls:
                self.failed_list.insert(tk.END, f"- {url}\n")
        else:
            self.failed_list.insert(tk.END, self.translator.translate("batch_tab", "all_videos_successful") + "\n")

        # Disable text widgets to prevent editing
        self.success_list.config(state=tk.DISABLED)
        self.failed_list.config(state=tk.DISABLED)

        # Open folder based on settings
        settings = {}
        if hasattr(self.app, 'settings_tab'):
            settings = self.app.settings_tab.get_settings()

        if completed > 0 and settings.get('auto_open_folder', False):
            download_path = getattr(self, "batch_download_path", self._get_base_download_path())
            open_folder(download_path)

        if self.warning_urls:
            warning_lines = []
            for url, msg in self.warning_urls[:5]:
                warning_lines.append(f"{url}\n{msg}")

            extra_count = len(self.warning_urls) - len(warning_lines)
            warning_text = "\n\n".join(warning_lines)
            if extra_count > 0:
                warning_text += f"\n\n... and {extra_count} more warnings."

            messagebox.showwarning(
                self.translator.translate("batch_tab", "warning_title"),
                f"Some downloads finished, but post-processing had warnings:\n\n{warning_text}"
            )

    def _stop_download(self):
        """Stop the ongoing download"""
        if self.is_downloading:
            # Update UI
            self.stop_download = True
            self.paused = False
            self.stop_button.config(state=tk.DISABLED)
            self.pause_button.config(state=tk.DISABLED)
            self.status_var.set(self.translator.translate("batch_tab", "download_stopped"))

            # Change progress bar style if possible
            if HAS_TTKBOOTSTRAP_EXTENSIONS:
                try:
                    self.progress.configure(bootstyle=(DANGER, STRIPED))
                except:
                    pass

            # Show notification
            self._show_toast(
                self.translator.translate("batch_tab", "download_stopped"),
                self.translator.translate("batch_tab", "batch_download_stopped"),
                3000,
                "danger"
            )

    def _update_progress(self, current, total):
        """Update progress display

        Args:
            current: Current progress
            total: Total items
        """
        if total > 0:
            value = int((current / total) * 100)
        else:
            value = 0

        self.progress_var.set(f"{current}/{total}")
        self.progress["value"] = value
        self.percent_var.set(f"{value}%")

        # Only update progress bar color if ttkbootstrap has advanced features
        if HAS_TTKBOOTSTRAP_EXTENSIONS:
            try:
                if value > 0:
                    if value < 30:
                        self.progress.configure(bootstyle=(SUCCESS, STRIPED))
                    elif value < 70:
                        self.progress.configure(bootstyle=(INFO, STRIPED))
                    else:
                        self.progress.configure(bootstyle=(SUCCESS, STRIPED))
            except:
                pass

        self.app.root.update_idletasks()
