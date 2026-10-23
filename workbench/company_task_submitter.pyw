from __future__ import annotations

import csv
from datetime import datetime, timedelta
import hashlib
import http.client
import json
import re
from pathlib import Path
import threading
import copy
from submission_transport import CloudSubmission,PendingSubmission,SubmissionRejected,load_pending
import time
import tkinter as tk
from tkinter import messagebox, ttk
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from task_inbox import (
    APP_DIR,
    MATERIALS_BY_PROJECT,
    TEMPLATE_FILE,
    build_pipeline_profile,
    atomic_write_json,
    create_task,
    load_templates,
    normalize_urls,
    new_run_group_id,
    selected_projects_from_tasks,
)
from task_contract import (
    FORMAL_BATCH_TOTAL,
    PROJECT_EXPECTED_TOTALS,
    assign_batch_contracts,
    canonical_video_identity,
    formal_batch_issues,
    normalize_selected_projects,
    validate_formal_submission,
)


SENDER_CONFIG_FILE = APP_DIR / "task_submitter_config.json"
SAVED_TASK_BOX_FILE = APP_DIR / "saved_task_box.json"
SUBMISSION_RECEIPT_FILE = APP_DIR / "task_submitter_last_receipt.json"
DEFAULT_SENDER_CONFIG = {"receiverUrl": "http://127.0.0.1:8766", "dashboardUrl": "http://127.0.0.1:8765", "accessToken": ""}
CLOUD_PENDING_FILE = APP_DIR / "task_submitter_pending.json"
SUBMITTER_VERSION = "14"
VOICE_PLAN_CREATE = "新建语音"
VOICE_PLAN_REUSE = "复用已有语音"
AUDIO_BATCH_ROOTS = {
    "ProjectA": Path(r"E:\音频\ProjectA音频"),
    "ProjectC": Path(r"E:\音频\ProjectC音频"),
    "ProjectD": Path(r"E:\音频\ProjectD音频"),
    "SPC地板": Path(r"E:\音频\spc音频\地板"),
    "SPC墙板": Path(r"E:\音频\spc音频\墙板"),
}


def open_home_service(request, timeout, attempts=3):
    """Open a Tailscale/LAN home service without inheriting system proxies.

    Company PCs commonly run V2Ray or another Windows proxy.  Python's
    default urlopen follows that proxy configuration, which incorrectly sends
    Tailscale hostnames and 100.x addresses to the proxy.  Build an explicit
    no-proxy opener for the receiver and Dashboard and retry short-lived
    connection failures with the same idempotent request.
    """
    attempts = max(1, int(attempts))
    last_error = None
    for attempt in range(attempts):
        try:
            return build_opener(ProxyHandler({})).open(request, timeout=timeout)
        except (OSError, URLError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.4 * (attempt + 1))
    raise last_error


def filter_project_status_items(items, selected_date: str, scope: str) -> list[dict]:
    """Never substitute historical projects for an empty current date."""
    clean_items = [dict(item) for item in items if isinstance(item, dict)]
    if scope == "当前生产日期":
        return [item for item in clean_items if str(item.get("date") or "") == selected_date]
    if scope == "历史任务":
        return [item for item in clean_items if str(item.get("date") or "") != selected_date]
    return clean_items


class CompanyTaskSubmitter(tk.Tk):
    """A deliberately confirmation-only task creator for the existing workbench."""

    def __init__(self):
        super().__init__()
        self.title(f"AI视频生产线 · 任务投递器 V{SUBMITTER_VERSION}")
        self.geometry("980x820")
        self.minsize(820, 650)
        self.configure(bg="#F5F7FA")
        self.catalog = load_templates(TEMPLATE_FILE)
        self.templates = {str(item["id"]): item for item in self.catalog["templates"]}
        self.templates_by_project = {str(item["project"]): item for item in self.catalog["templates"]}
        self.voice_materials = self._load_voice_materials()
        self.project_var = tk.StringVar(value="ProjectA")
        now = datetime.now()
        self.date_var = tk.StringVar(value=f"{now.month}.{now.day}")
        self.material_var = tk.StringVar()
        # New voice is the safe default. Reusing an older batch must always be
        # an explicit choice because it can otherwise produce valid-looking
        # videos with the wrong narration.
        self.voice_plan_var = tk.StringVar(value=VOICE_PLAN_CREATE)
        self.audio_batch_var = tk.StringVar()
        self.audio_batch_note_var = tk.StringVar()
        self.expected_count_var = tk.StringVar()
        self.project_contract_note_var = tk.StringVar()
        self.batch_id = self._new_batch_id()
        self.run_group_id = new_run_group_id("formal")
        self.batch_tasks: list[dict] = []
        self.project_selection_vars = {
            project: tk.BooleanVar(value=False)
            for project in PROJECT_EXPECTED_TOTALS
        }
        self.selected_projects_var = tk.StringVar(value="本次未选择项目（加入任务后自动纳入）")
        self.run_group_label_var = tk.StringVar(
            value=f"运行组：{self.run_group_id}（正式/ExampleBrand共用时复制此ID）"
        )
        self.replenish_context: dict | None = None
        self.gap_items_by_row: dict[str, dict] = {}
        self.batch_summary_var = tk.StringVar()
        self.link_status_var = tk.StringVar(value="有效链接 0 条")
        self.status_scope_var = tk.StringVar(value="当前生产日期")
        self.cloud_pending = None
        self.cloud_worker_running = False
        self.submission_locked = False
        self.last_submission_receipt: dict | None = None
        self.sender_config = self._load_sender_config()
        self.info_var = tk.StringVar(value=f"目标接收器：{self.sender_config['receiverUrl']}。可只提交待确认，也可提交后按项目顺序自动执行。")
        self.sync_info_var = tk.StringVar(value="家里项目状态：尚未同步")
        self.dashboard_sync_running = False
        # A submitter can be left open on the company PC.  Alert once for each
        # newly received blocked batch, instead of silently updating the table.
        self.notified_gap_signatures: set[str] = set()
        self._styles()
        self._build()
        self.project_var.trace_add("write", lambda *_: self._refresh_project())
        self.material_var.trace_add("write", lambda *_: self._refresh_preview())
        self.date_var.trace_add("write", lambda *_: self._date_changed())
        self.voice_plan_var.trace_add("write", lambda *_: self._refresh_audio_batch())
        self.audio_batch_var.trace_add("write", lambda *_: self._refresh_preview())
        self.expected_count_var.trace_add("write", lambda *_: self._refresh_preview())
        self.status_scope_var.trace_add("write", lambda *_: self.sync_dashboard_status())
        self._refresh_project()
        if not self._restore_cloud_pending():
            self._load_saved_task_box(silent=True)
        self.after(900, self.sync_dashboard_status)

    def _styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Card.TFrame", background="#FFFFFF")
        style.configure("Title.TLabel", background="#FFFFFF", foreground="#172033", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Body.TLabel", background="#FFFFFF", foreground="#475467", font=("Microsoft YaHei UI", 9))
        style.configure("Stage.TLabel", background="#F7FAFF", foreground="#1D4ED8", font=("Microsoft YaHei UI", 9))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=9)

    def _build(self):
        # A laptop can have less vertical room than the complete task form.
        # Keep every action reachable with a visible scrollbar and wheel input.
        viewport = ttk.Frame(self, style="Card.TFrame")
        viewport.pack(fill="both", expand=True, padx=16, pady=16)
        self.scroll_canvas = tk.Canvas(viewport, bg="#F5F7FA", highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(viewport, orient="vertical", command=self.scroll_canvas.yview)
        self.scroll_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.scroll_canvas.pack(side="left", fill="both", expand=True)
        card = ttk.Frame(self.scroll_canvas, style="Card.TFrame", padding=18)
        self.scroll_window = self.scroll_canvas.create_window((0, 0), window=card, anchor="nw")
        card.bind("<Configure>", self._refresh_scroll_region)
        self.scroll_canvas.bind("<Configure>", self._resize_scroll_content)
        ttk.Label(card, text=f"AI视频生产线 · 任务投递器 V{SUBMITTER_VERSION}", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            card,
            text="项目和材料直接沿用批量语音客户端的目录规则；日期默认今天。家里工作台导入后只填充并检查，不会自动开始下载。",
            style="Body.TLabel",
        ).pack(anchor="w", pady=(4, 12))

        fields = ttk.Frame(card, style="Card.TFrame")
        fields.pack(fill="x")
        ttk.Label(fields, text="生产日期（默认今天）", style="Body.TLabel").grid(row=0, column=0, sticky="w")
        self.date_box = ttk.Combobox(fields, state="readonly", width=13, textvariable=self.date_var)
        self.date_box["values"] = self._recent_dates()
        self.date_box.grid(row=1, column=0, sticky="ew", padx=(0, 12), pady=(5, 0))
        ttk.Label(fields, text="项目", style="Body.TLabel").grid(row=0, column=1, sticky="w")
        self.template_box = ttk.Combobox(fields, state="readonly", width=27, textvariable=self.project_var)
        self.template_box["values"] = list(self.templates_by_project)
        self.template_box.grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=(5, 0))
        ttk.Label(fields, text="材料", style="Body.TLabel").grid(row=0, column=2, sticky="w")
        self.material_box = ttk.Combobox(fields, state="readonly", width=22, textvariable=self.material_var)
        self.material_box.grid(row=1, column=2, sticky="ew", padx=(0, 12), pady=(5, 0))
        ttk.Label(fields, text="音频批次", style="Body.TLabel").grid(row=0, column=3, sticky="w")
        self.audio_batch_box = ttk.Combobox(fields, state="readonly", width=18, textvariable=self.audio_batch_var)
        self.audio_batch_box.grid(row=1, column=3, sticky="ew", pady=(5, 0))
        ttk.Label(fields, text="语音处理", style="Body.TLabel").grid(row=2, column=0, sticky="w", pady=(12, 0))
        self.voice_plan_box = ttk.Combobox(
            fields,
            state="readonly",
            width=18,
            textvariable=self.voice_plan_var,
            values=(VOICE_PLAN_REUSE, VOICE_PLAN_CREATE),
        )
        self.voice_plan_box.grid(row=3, column=0, sticky="ew", padx=(0, 12), pady=(5, 0))
        self.voice_plan_box.bind("<<ComboboxSelected>>", self._voice_plan_selected)
        ttk.Label(fields, textvariable=self.audio_batch_note_var, style="Body.TLabel", wraplength=590).grid(
            row=3, column=2, columnspan=2, sticky="w", pady=(5, 0)
        )
        ttk.Label(fields, text="计划视频数（固定项目合同）", style="Body.TLabel").grid(row=2, column=1, sticky="w", pady=(12, 0))
        count_line = ttk.Frame(fields, style="Card.TFrame")
        count_line.grid(row=3, column=1, sticky="w", pady=(5, 0))
        ttk.Spinbox(count_line, from_=1, to=999, width=10, textvariable=self.expected_count_var).pack(side="left")
        ttk.Label(count_line, textvariable=self.project_contract_note_var, style="Stage.TLabel").pack(side="left", padx=(8, 0))
        for index in range(4):
            fields.columnconfigure(index, weight=1)

        selection = ttk.Frame(card, style="Card.TFrame")
        selection.pack(fill="x", pady=(12, 0))
        ttk.Label(selection, text="本次运行项目（可任意组合）", style="Body.TLabel").pack(anchor="w")
        selection_line = ttk.Frame(selection, style="Card.TFrame")
        selection_line.pack(fill="x", pady=(4, 0))
        for project, variable in self.project_selection_vars.items():
            ttk.Checkbutton(
                selection_line,
                text=project,
                variable=variable,
                command=self._selected_projects_changed,
            ).pack(side="left", padx=(0, 12))
        ttk.Label(selection, textvariable=self.selected_projects_var, style="Stage.TLabel").pack(anchor="w", pady=(4, 0))
        run_group_line = ttk.Frame(selection, style="Card.TFrame")
        run_group_line.pack(fill="x", pady=(3, 0))
        ttk.Label(run_group_line, textvariable=self.run_group_label_var, style="Stage.TLabel").pack(side="left")
        ttk.Button(run_group_line, text="复制运行组ID", command=self._copy_run_group_id).pack(side="left", padx=(10, 0))

        ttk.Label(card, text="六步预设（随此任务保存）", style="Body.TLabel").pack(anchor="w", pady=(12, 4))
        self.preview = tk.Text(card, height=5, bg="#F7FAFF", fg="#1E3A5F", relief="solid", bd=1,
                               font=("Microsoft YaHei UI", 9), wrap="word", padx=10, pady=8)
        self.preview.pack(fill="x")
        self.preview.configure(state="disabled")

        link_header = ttk.Frame(card, style="Card.TFrame")
        link_header.pack(fill="x", pady=(10, 4))
        ttk.Label(link_header, text="视频链接（每行一个；也可直接粘贴一整段文字）", style="Body.TLabel").pack(side="left")
        self.link_status_label = ttk.Label(link_header, textvariable=self.link_status_var, style="Stage.TLabel")
        self.link_status_label.pack(side="right")
        self.links = tk.Text(card, height=4, relief="solid", bd=1, font=("Microsoft YaHei UI", 10), wrap="word")
        self.links.pack(fill="x")
        self.links.bind("<<Modified>>", self._links_modified)

        queue = ttk.Frame(card, style="Card.TFrame")
        queue.pack(fill="x", expand=False, pady=(10, 0))
        queue_header = ttk.Frame(queue, style="Card.TFrame")
        queue_header.pack(fill="x", pady=(0, 6))
        ttk.Label(queue_header, text="当前任务框（可混合多个项目，按列表顺序提交）", style="Body.TLabel").pack(side="left")
        ttk.Label(queue_header, textvariable=self.batch_summary_var, style="Stage.TLabel").pack(side="right")
        columns = ("sequence", "project", "material", "languages", "voice", "audio", "links", "planned")
        self.batch_tree = ttk.Treeview(queue, columns=columns, show="headings", height=3, selectmode="browse")
        for column, label, width in (
            ("sequence", "顺序", 52), ("project", "项目", 90), ("material", "材料", 95),
            ("languages", "最终语言分配", 175),
            ("voice", "语音", 120), ("audio", "音频批次", 90), ("links", "已填", 62), ("planned", "计划", 62),
        ):
            self.batch_tree.heading(column, text=label)
            self.batch_tree.column(column, width=width, anchor="center")
        self.batch_tree.pack(fill="both", expand=True)
        queue_buttons = ttk.Frame(queue, style="Card.TFrame")
        queue_buttons.pack(fill="x", pady=(7, 0))
        ttk.Button(queue_buttons, text="加入任务框", command=self.add_current_task).pack(side="left")
        ttk.Button(queue_buttons, text="删除所选", command=self.remove_selected_task).pack(side="left", padx=(8, 0))
        ttk.Button(queue_buttons, text="上移", command=lambda: self.move_selected_task(-1)).pack(side="left", padx=(8, 0))
        ttk.Button(queue_buttons, text="下移", command=lambda: self.move_selected_task(1)).pack(side="left", padx=(8, 0))
        ttk.Button(queue_buttons, text="清空任务框", command=self.clear_task_box).pack(side="left", padx=(8, 0))
        ttk.Button(queue_buttons, text="新建任务箱", command=self.new_task_box).pack(side="left", padx=(8, 0))
        ttk.Button(queue_buttons, text="重新加载已保存任务", command=self._load_saved_task_box).pack(side="left", padx=(8, 0))
        ttk.Button(queue_buttons, text="提交前完整对账", command=self.show_batch_reconciliation).pack(side="right")

        sync_card = ttk.Frame(card, style="Card.TFrame")
        sync_card.pack(fill="x", expand=False, pady=(10, 0))
        sync_header = ttk.Frame(sync_card, style="Card.TFrame")
        sync_header.pack(fill="x", pady=(0, 6))
        ttk.Label(sync_header, text="家里第1/2步下载与去字幕状态（只读）", style="Body.TLabel").pack(side="left")
        ttk.Button(sync_header, text="刷新同步", command=self.sync_dashboard_status).pack(side="right")
        ttk.Button(sync_header, text="按此缺口补链", command=self.prepare_replenishment).pack(side="right", padx=(0, 8))
        self.status_scope_box = ttk.Combobox(
            sync_header,
            state="readonly",
            width=13,
            textvariable=self.status_scope_var,
            values=("当前生产日期", "历史任务", "全部日期"),
        )
        self.status_scope_box.pack(side="right", padx=(0, 8))
        ttk.Label(sync_header, textvariable=self.sync_info_var, style="Stage.TLabel").pack(side="right", padx=(0, 10))
        gap_columns = ("date", "project", "material", "done", "failed", "missing", "status")
        self.gap_tree = ttk.Treeview(sync_card, columns=gap_columns, show="headings", height=2, selectmode="browse")
        for column, label, width in (
            ("date", "日期", 72), ("project", "项目", 90), ("material", "材料", 100),
            ("done", "完成/计划", 88), ("failed", "失败", 55), ("missing", "缺口", 55), ("status", "状态", 230),
        ):
            self.gap_tree.heading(column, text=label)
            self.gap_tree.column(column, width=width, anchor="center" if column not in {"material", "status"} else "w")
        self.gap_tree.tag_configure("blocked", foreground="#C62828")
        self.gap_tree.tag_configure("completed", foreground="#138A5B")
        self.gap_tree.tag_configure("waiting", foreground="#2563EB")
        self.gap_tree.pack(fill="x")

        footer = ttk.Frame(card, style="Card.TFrame")
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, textvariable=self.info_var, style="Body.TLabel", wraplength=510).pack(side="left", fill="x", expand=True)
        self.submit_button = ttk.Button(footer, text="提交并顺序执行（0）", style="Primary.TButton", command=lambda: self.submit_task_box(True))
        self.submit_button.pack(side="right", padx=(12, 0))
        self.pending_submit_button = ttk.Button(footer, text="只提交待确认", command=lambda: self.submit_task_box(False))
        self.pending_submit_button.pack(side="right")
        self.query_cloud_button=ttk.Button(footer,text="查询云端回执",command=self._query_cloud_receipt)
        self.query_cloud_button.pack(side="right")
        ttk.Button(queue_buttons,text="结束待查询回执",command=self._release_cloud_receipt).pack(side="left",padx=(8,0))
        self.refresh_task_box()
        self._bind_scroll_wheel(card)

    def _refresh_scroll_region(self, _event=None):
        self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all"))

    def _resize_scroll_content(self, event):
        self.scroll_canvas.itemconfigure(self.scroll_window, width=event.width)

    def _bind_scroll_wheel(self, widget):
        """Let a compact laptop scroll the form without hiding footer actions."""
        if not isinstance(widget, tk.Text):
            widget.bind("<MouseWheel>", self._scroll_with_wheel, add="+")
            widget.bind("<Button-4>", lambda _event: self._scroll_by(-1), add="+")
            widget.bind("<Button-5>", lambda _event: self._scroll_by(1), add="+")
        for child in widget.winfo_children():
            self._bind_scroll_wheel(child)

    def _scroll_with_wheel(self, event):
        steps = -int(event.delta / 120) if event.delta else 0
        if steps:
            self._scroll_by(steps)
        return "break"

    def _scroll_by(self, steps):
        self.scroll_canvas.yview_scroll(steps, "units")
        return "break"

    @staticmethod
    def _load_sender_config():
        config = dict(DEFAULT_SENDER_CONFIG)
        try:
            if SENDER_CONFIG_FILE.is_file():
                config.update(json.loads(SENDER_CONFIG_FILE.read_text(encoding="utf-8")))
            else:
                atomic_write_json(SENDER_CONFIG_FILE, config)
        except (OSError, ValueError):
            pass
        return config

    @staticmethod
    def _recent_dates():
        today = datetime.now().date()
        return [f"{(today - timedelta(days=offset)).month}.{(today - timedelta(days=offset)).day}" for offset in range(0, 62)]

    def _links_modified(self, _event=None):
        if self.links.edit_modified():
            self.links.edit_modified(False)
            self._refresh_link_status()

    def _link_analysis(self):
        urls = normalize_urls(self.links.get("1.0", "end-1c")) if hasattr(self, "links") else []
        identities: set[str] = set()
        invalid: list[str] = []
        duplicate_count = 0
        for url in urls:
            identity, error = canonical_video_identity(url)
            if error:
                invalid.append(error)
            elif identity in identities:
                duplicate_count += 1
            else:
                identities.add(identity)
        try:
            planned = max(1, int(self.expected_count_var.get()))
        except (TypeError, ValueError):
            planned = 0
        return {
            "urls": urls,
            "valid": len(identities),
            "invalid": len(invalid),
            "duplicates": duplicate_count,
            "planned": planned,
        }

    def _refresh_link_status(self):
        if not hasattr(self, "link_status_label"):
            return
        result = self._link_analysis()
        details = [f"有效 {result['valid']} / 计划 {result['planned']} 条"]
        if result["duplicates"]:
            details.append(f"重复 {result['duplicates']}")
        if result["invalid"]:
            details.append(f"无效 {result['invalid']}")
        ok = (
            result["planned"] > 0
            and result["valid"] == result["planned"]
            and result["duplicates"] == 0
            and result["invalid"] == 0
        )
        self.link_status_var.set("；".join(details))
        self.link_status_label.configure(foreground="#138A5B" if ok else "#C62828")

    def _load_voice_materials(self):
        """Read the existing BatchVoice directory rules when this is beside the workbench."""
        result = {project: list(materials) for project, materials in MATERIALS_BY_PROJECT.items()}
        path = APP_DIR / "tools" / "BatchVoiceClient-Portable" / "data" / "project_output_dirs.csv"
        if not path.is_file():
            return result
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                for row in csv.DictReader(stream):
                    project = str(row.get("project") or "").strip()
                    material = str(row.get("material") or "").strip()
                    if project not in result:
                        continue
                    if material and material not in result[project]:
                        result[project].append(material)
        except OSError:
            pass
        return result

    def _selected_template(self):
        return self.templates_by_project[self.project_var.get()]

    def _available_audio_batches(self, project):
        root = AUDIO_BATCH_ROOTS.get(project)
        if not root or not root.is_dir():
            return []
        folders = [item for item in root.iterdir() if item.is_dir() and not item.name.startswith(".")]
        folders.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        return [item.name for item in folders]

    def _refresh_audio_batch(self):
        project = self.project_var.get()
        if project == "ProjectB":
            self.audio_batch_box.configure(values=[self.date_var.get()], state="disabled")
            self.audio_batch_var.set(self.date_var.get())
            self.voice_plan_var.set(VOICE_PLAN_CREATE)
            self.voice_plan_box.configure(state="disabled")
            self.audio_batch_note_var.set("ProjectB固定：当天新建语音，音频批次等于生产日期。")
        elif self.voice_plan_var.get() == VOICE_PLAN_CREATE:
            self.voice_plan_box.configure(state="readonly")
            self.audio_batch_box.configure(values=[self.date_var.get()], state="disabled")
            self.audio_batch_var.set(self.date_var.get())
            self.audio_batch_note_var.set("新建本周语音：工作台会启动批量语音客户端；不会复用旧 MP3。")
        else:
            choices = self._available_audio_batches(project)
            previous = self.audio_batch_var.get()
            self.voice_plan_box.configure(state="readonly")
            if choices:
                self.audio_batch_box.configure(values=choices, state="readonly")
                self.audio_batch_var.set(previous if previous in choices else choices[0])
                self.audio_batch_note_var.set("复用已有周音频：工作台检测到 MP3 后不启动语音客户端。")
            else:
                self.voice_plan_var.set(VOICE_PLAN_CREATE)
                self.audio_batch_note_var.set("没有可复用的周音频，已切换为新建语音。")
        self._refresh_preview()

    def _date_changed(self):
        self._refresh_audio_batch()
        if hasattr(self, "gap_tree"):
            self.sync_dashboard_status()

    def _voice_plan_key(self):
        return "create_new" if self.voice_plan_var.get() == VOICE_PLAN_CREATE else "reuse_existing"

    def _voice_plan_selected(self, _event=None):
        """Apply the explicit voice choice to every queued production task.

        The voice selector is a batch-level control in the submitter UI.  Older
        builds only applied it to tasks added after the selection changed, so a
        reloaded first row could silently retain ``reuse_existing`` while the
        rest of the batch used ``create_new``.
        """
        self._refresh_audio_batch()
        if self.submission_locked or not self.batch_tasks:
            return
        selected_voice_plan = self._voice_plan_key()
        for task in self.batch_tasks:
            task_date = str(task.get("productionDate") or task.get("date") or self.date_var.get()).strip()
            task_voice_plan = selected_voice_plan
            audio_batch = task_date if task_voice_plan == "create_new" else str(self.audio_batch_var.get()).strip()
            if str(task.get("project") or "").strip() == "ProjectB":
                task_voice_plan = "create_new"
                audio_batch = task_date
            task["voicePlan"] = task_voice_plan
            task["audioBatch"] = audio_batch
            task["pipelineProfile"] = build_pipeline_profile(
                str(task.get("project") or ""),
                str(task.get("material") or ""),
                task_date,
                audio_batch,
                task_voice_plan,
            )
        self.refresh_task_box()
        self._save_task_box()

    def _refresh_project(self):
        template = self._selected_template()
        materials = list(self.voice_materials.get(self.project_var.get()) or template.get("materials") or [template["material"]])
        self.material_box["values"] = materials
        self.material_var.set(template["material"] if template["material"] in materials else materials[0])
        self.expected_count_var.set(str(template.get("expectedCount") or 1))
        fixed_total = PROJECT_EXPECTED_TOTALS.get(self.project_var.get())
        self.project_contract_note_var.set(
            f"本项目固定总数：{fixed_total}，提交时必须完整"
            if fixed_total else ""
        )
        self._refresh_audio_batch()

    def _selected_projects(self) -> list[str]:
        """Return explicit project scope, or infer it from queued rows."""
        explicit = [
            project for project, variable in self.project_selection_vars.items()
            if bool(variable.get())
        ]
        return explicit or selected_projects_from_tasks(self.batch_tasks)

    def _selected_projects_changed(self):
        selected = self._selected_projects()
        self.selected_projects_var.set(
            "本次运行：" + "、".join(selected)
            if selected else "本次未选择项目（加入任务后自动纳入）"
        )
        self.refresh_task_box()

    def _refresh_run_group_label(self):
        self.run_group_label_var.set(
            f"运行组：{self.run_group_id}（正式/ExampleBrand共用时复制此ID）"
        )

    def _copy_run_group_id(self):
        try:
            self.clipboard_clear()
            self.clipboard_append(self.run_group_id)
            self.update_idletasks()
            self.info_var.set(
                f"已复制运行组ID：{self.run_group_id}。ExampleBrand若要与本次正式项目顺序执行，请填入同一ID。"
            )
        except tk.TclError:
            self.info_var.set(f"运行组ID：{self.run_group_id}（请手动复制）")

    def _refresh_preview(self):
        if not self.project_var.get() or not self.material_var.get():
            return
        template = self._selected_template()
        profile = build_pipeline_profile(
            template["project"], self.material_var.get(), self.date_var.get().strip() or "日期",
            self.audio_batch_var.get(), self._voice_plan_key(),
        )
        voice_action = "新建并启动批量语音客户端" if profile["3_voice"].get("requiresNewGeneration") else "复用现有 MP3，不启动语音客户端"
        lines = [
            "1  下载：导入后人工确认启动。",
            f"2  AI去字幕：{profile['2_subtitle']['mode']}",
            f"3  批量语音：{voice_action}；{profile['3_voice']['cycle']}批次 {profile['3_voice']['audioBatch']} → 家里本机映射：{profile['3_voice']['audioLocation']}",
            f"4  AI素材拼接：{profile['4_stitch']['project']}，Logo {profile['4_stitch']['logo']} → {profile['4_stitch']['outputDirectoryRule']}",
            f"5  CapCut：{profile['5_capcut']['category']}，{profile['5_capcut']['subtitleStyle']}，{profile['5_capcut']['export']} 导出",
            f"6  百度网盘：只上传最终 MP4 → {profile['6_upload']['remoteDirectoryRule']}",
            "OPS：Dashboard 只读跟踪下载、语音、拼接、剪辑、上传状态。",
        ]
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", "\n".join(lines))
        self.preview.configure(state="disabled")
        self._refresh_link_status()

    @staticmethod
    def _new_batch_id():
        return f"dispatch-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    def _current_task(self):
        template_id = self._selected_template()["id"]
        task = create_task(
            template_id,
            self.links.get("1.0", "end-1c"),
            date_label=self.date_var.get(),
            material=self.material_var.get(),
            audio_batch=self.audio_batch_var.get(),
            voice_plan=self._voice_plan_key(),
        )
        task["runGroupId"] = self.run_group_id
        try:
            task["expectedCount"] = max(1, int(self.expected_count_var.get()))
        except (TypeError, ValueError):
            raise ValueError("计划视频数必须是大于 0 的整数")
        if self.replenish_context:
            expected = int(self.replenish_context["expectedCount"])
            completed = int(self.replenish_context["completed"])
            missing = int(self.replenish_context["missingCount"])
            task.update({
                "isReplenishment": True,
                "replenishForTaskId": self.replenish_context["taskId"],
                "canonicalTaskId": self.replenish_context["taskId"],
                "taskRole": "step12_replenishment",
                "replenishmentBaseExpected": expected,
                "replenishmentCompletedBefore": completed,
                "replenishmentMissingBefore": missing,
            })
            if len(task.get("urls") or []) != missing:
                raise ValueError(f"本次补链需要 {missing} 条新链接；当前识别到 {len(task.get('urls') or [])} 条。")
        return task

    def refresh_task_box(self):
        if not hasattr(self, "batch_tree"):
            return
        self.batch_tree.delete(*self.batch_tree.get_children())
        assign_batch_contracts(self.batch_tasks)
        selected_projects = self._selected_projects()
        self.selected_projects_var.set(
            "本次运行：" + "、".join(selected_projects)
            if selected_projects else "本次未选择项目（加入任务后自动纳入）"
        )
        total_links = 0
        project_planned = {project: 0 for project in PROJECT_EXPECTED_TOTALS}
        all_rows_complete = True
        for sequence, task in enumerate(self.batch_tasks, start=1):
            task["batchId"] = self.batch_id
            task["batchKind"] = "formal"
            task["runGroupId"] = self.run_group_id
            task["selectedProjects"] = list(selected_projects)
            task["batchSequence"] = sequence
            task["batchSize"] = len(self.batch_tasks)
            total_links += len(task.get("urls") or [])
            planned = int(task.get("expectedCount") or len(task.get("urls") or []))
            project = str(task.get("project") or "")
            if project in project_planned:
                project_planned[project] += planned
            row_complete = len(task.get("urls") or []) == planned
            all_rows_complete = all_rows_complete and row_complete
            voice = "新建语音" if task.get("voicePlan") == "create_new" else "复用已有语音"
            languages = "、".join(
                f"{item.get('name') or item.get('code')}×{item.get('outputCount')}"
                for item in task.get("languagePlan") or []
                if int(item.get("outputCount") or 0) > 0
            )
            self.batch_tree.insert(
                "", "end", iid=str(task["taskId"]),
                values=(sequence, task.get("project"), task.get("material"), languages, voice, task.get("audioBatch"), len(task.get("urls") or []), planned),
                tags=("complete" if row_complete else "incomplete",),
            )
        count = len(self.batch_tasks)
        total_planned = sum(int(item.get("expectedCount") or len(item.get("urls") or [])) for item in self.batch_tasks)
        project_text = "；".join(
            f"{project}{project_planned[project]}/{expected}"
            for project, expected in PROJECT_EXPECTED_TOTALS.items()
            if project_planned[project] or project in selected_projects
        )
        summary = f"{count} 项 / 已填 {total_links} / 计划 {total_planned} 条"
        self.batch_summary_var.set(f"{summary}；{project_text}" if project_text else summary)
        self.batch_tree.tag_configure("incomplete", foreground="#C62828")
        self.batch_tree.tag_configure("complete", foreground="#172033")
        self.submit_button.configure(text=f"提交并顺序执行（{count}）")
        is_replenishment = bool(self.batch_tasks) and all(bool(item.get("isReplenishment")) for item in self.batch_tasks)
        self.batch_validation_issues = [] if is_replenishment else (
            formal_batch_issues(self.batch_tasks, selected_projects) if count else []
        )
        state = "normal" if count and all_rows_complete and not self.batch_validation_issues and not self.submission_locked else "disabled"
        self.submit_button.configure(state=state)
        self.pending_submit_button.configure(state=state)

    def show_batch_reconciliation(self):
        if not self.batch_tasks:
            return messagebox.showinfo("提交前完整对账", "任务框为空。", parent=self)
        assign_batch_contracts(self.batch_tasks)
        replenishment = all(bool(item.get("isReplenishment")) for item in self.batch_tasks)
        selected_projects = self._selected_projects()
        issues = [] if replenishment else formal_batch_issues(self.batch_tasks, selected_projects)
        rows = []
        for sequence, task in enumerate(self.batch_tasks, start=1):
            planned = int(task.get("expectedCount") or 0)
            actual = len(task.get("urls") or [])
            languages = "、".join(
                f"{item.get('name') or item.get('code')}×{int(item.get('outputCount') or 0)}"
                for item in task.get("languagePlan") or []
                if int(item.get("outputCount") or 0) > 0
            ) or "补链后继承父任务"
            mark = "通过" if actual == planned else "不完整"
            rows.append(
                f"{sequence}. {task.get('project')} / {task.get('material')}："
                f"链接 {actual}/{planned}，语言 {languages}，{mark}"
            )
        if issues:
            title = f"对账未通过（{len(issues)} 项）"
            detail = "\n".join(f"• {item}" for item in issues)
            messagebox.showwarning("提交前完整对账", f"{title}\n\n{detail}\n\n" + "\n".join(rows), parent=self)
            return
        totals = {project: 0 for project in PROJECT_EXPECTED_TOTALS}
        for task in self.batch_tasks:
            if task.get("project") in totals:
                totals[task["project"]] += int(task.get("expectedCount") or 0)
        total_text = "；".join(
            f"{project} {totals[project]}/{expected}"
            for project, expected in PROJECT_EXPECTED_TOTALS.items()
            if project in selected_projects
        )
        heading = "补链任务身份与链接数通过" if replenishment else (
            f"正式任务箱通过：{total_text}，合计 "
            f"{sum(totals.get(project, 0) for project in selected_projects)} 条"
        )
        messagebox.showinfo("提交前完整对账", heading + "\n\n" + "\n".join(rows), parent=self)

    def add_current_task(self):
        if self.submission_locked:
            return messagebox.showinfo("任务箱已提交", "当前任务箱是只读接收回执；请先点击“新建任务箱”。", parent=self)
        try:
            task = self._current_task()
        except (KeyError, ValueError) as exc:
            return messagebox.showwarning("无法加入任务框", str(exc), parent=self)
        if task.get("voicePlan") == "create_new":
            same_voice_batch = next(
                (
                    item for item in self.batch_tasks
                    if item.get("voicePlan") == "create_new"
                    and item.get("project") == task.get("project")
                    and item.get("material") == task.get("material")
                    and item.get("audioBatch") == task.get("audioBatch")
                ),
                None,
            )
            if same_voice_batch:
                return messagebox.showwarning(
                    "语音批次重复",
                    "任务框中已经有一项会新建同一个项目、材料和音频批次的语音。\n"
                    "请把链接合并到该项，避免工作台重复生成同一批音频。",
                    parent=self,
                )
        self.batch_tasks.append(task)
        project = str(task.get("project") or "").strip()
        if project in self.project_selection_vars:
            self.project_selection_vars[project].set(True)
        self._selected_projects_changed()
        replenish_note = ""
        if task.get("isReplenishment"):
            replenish_note = f"；补充原任务 {task.get('replenishForTaskId')}"
            self.replenish_context = None
        self.links.delete("1.0", "end")
        self.refresh_task_box()
        self.info_var.set(
            f"已加入任务框：{task['project']} / {task['material']} / "
            f"已填 {len(task['urls'])} / 计划 {task.get('expectedCount')} 条{replenish_note}。"
        )

    def _selected_batch_index(self):
        selected = self.batch_tree.selection()
        if not selected:
            return None
        task_id = str(selected[0])
        return next((index for index, item in enumerate(self.batch_tasks) if item.get("taskId") == task_id), None)

    def remove_selected_task(self):
        if self.submission_locked:
            return messagebox.showinfo("任务箱已提交", "当前任务箱是只读接收回执；请先点击“新建任务箱”。", parent=self)
        index = self._selected_batch_index()
        if index is None:
            return messagebox.showinfo("请选择任务", "请先在任务框中选中一项。", parent=self)
        task = self.batch_tasks.pop(index)
        self.refresh_task_box()
        self.info_var.set(f"已从任务框移除：{task.get('project')} / {task.get('material')}。尚未发送到家里电脑。")

    def move_selected_task(self, offset):
        if self.submission_locked:
            return messagebox.showinfo("任务箱已提交", "当前任务箱是只读接收回执；请先点击“新建任务箱”。", parent=self)
        index = self._selected_batch_index()
        if index is None:
            return messagebox.showinfo("请选择任务", "请先在任务框中选中一项。", parent=self)
        target = index + offset
        if target < 0 or target >= len(self.batch_tasks):
            return
        self.batch_tasks[index], self.batch_tasks[target] = self.batch_tasks[target], self.batch_tasks[index]
        selected_id = str(self.batch_tasks[target]["taskId"])
        self.refresh_task_box()
        self.batch_tree.selection_set(selected_id)
        self.batch_tree.focus(selected_id)

    def clear_task_box(self):
        if self.submission_locked:
            return messagebox.showinfo("任务箱已提交", "请使用“新建任务箱”开始下一批；接收回执不会被误当成未提交任务。", parent=self)
        if not self.batch_tasks:
            return
        if not messagebox.askyesno("清空任务框", "只清空尚未提交的本地任务框，不会影响已经发送的任务。确定清空吗？", parent=self):
            return
        self.batch_tasks.clear()
        self.batch_id = self._new_batch_id()
        self.run_group_id = new_run_group_id("formal")
        self._refresh_run_group_label()
        for variable in self.project_selection_vars.values():
            variable.set(False)
        self.refresh_task_box()
        self.info_var.set("任务框已清空。")

    def new_task_box(self):
        if self.cloud_pending:
            return messagebox.showinfo("仍有待查询回执","请先查询云端回执；撤销或人工对账后用“结束待查询回执”。不会覆盖原编号。",parent=self)
        if self.batch_tasks and not messagebox.askyesno(
            "新建任务箱",
            "将清空当前本地任务框。已经提交的远程任务不会受影响，是否继续？",
            parent=self,
        ):
            return
        self.batch_tasks.clear()
        self.batch_id = self._new_batch_id()
        self.run_group_id = new_run_group_id("formal")
        self._refresh_run_group_label()
        for variable in self.project_selection_vars.values():
            variable.set(False)
        self.submission_locked = False
        self.last_submission_receipt = None
        self.links.delete("1.0", "end")
        self.refresh_task_box()
        self.info_var.set("已建立新的本地任务箱。")

    @staticmethod
    def _saved_task_blueprint(task):
        """Keep only repeatable input fields, never old progress or IDs."""
        return {
            "date": str(task.get("productionDate") or task.get("date") or "").strip(),
            "templateId": str(task.get("templateId") or "").strip(),
            "project": str(task.get("project") or "").strip(),
            "material": str(task.get("material") or "").strip(),
            "urls": [str(item).strip() for item in task.get("urls") or [] if str(item).strip()],
            "expectedCount": int(task.get("expectedCount") or len(task.get("urls") or []) or 1),
            "audioBatch": str(task.get("audioBatch") or "").strip(),
            "voicePlan": str(task.get("voicePlan") or "").strip(),
        }

    def _save_task_box(self):
        if not self.batch_tasks:
            return
        payload = {
            "schemaVersion": 1,
            "savedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "runGroupId": self.run_group_id,
            "totalExpected": sum(int(item.get("expectedCount") or 0) for item in self.batch_tasks),
            "selectedProjects": self._selected_projects(),
            "tasks": [self._saved_task_blueprint(item) for item in self.batch_tasks],
        }
        atomic_write_json(SAVED_TASK_BOX_FILE, payload)

    def _load_saved_task_box(self, silent=False):
        if self.cloud_pending:
            if not silent:messagebox.showinfo("待查询回执","先处理云端待查询记录；不会创建新的任务编号。",parent=self)
            return
        try:
            payload = json.loads(SAVED_TASK_BOX_FILE.read_text(encoding="utf-8"))
            rows = payload.get("tasks") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or not rows:
                if not silent:
                    messagebox.showinfo("没有已保存任务", "当前版本没有保存的任务箱。", parent=self)
                return
            today = datetime.now()
            today_label = f"{today.month}.{today.day}"
            saved_dates = {
                str(row.get("date") or "").strip()
                for row in rows
                if isinstance(row, dict) and str(row.get("date") or "").strip()
            }
            if silent and saved_dates and saved_dates != {today_label}:
                self.info_var.set(
                    f"今天是 {today_label}；已保存的 {'、'.join(sorted(saved_dates))} 任务未自动载入。"
                    "如需重跑旧批次，请点击“重新加载已保存任务”。"
                )
                return
            restored = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("保存任务包含无效项目")
                task = create_task(
                    str(row.get("templateId") or ""),
                    row.get("urls") or [],
                    date_label=str(row.get("date") or "").strip(),
                    material=str(row.get("material") or "").strip() or None,
                    audio_batch=str(row.get("audioBatch") or "").strip() or None,
                    voice_plan=str(row.get("voicePlan") or "").strip() or None,
                    source="saved_task_box",
                )
                task["expectedCount"] = max(1, int(row.get("expectedCount") or len(task.get("urls") or [])))
                restored.append(task)
            restored_projects = list(
                normalize_selected_projects(payload.get("selectedProjects"))
                or selected_projects_from_tasks(restored)
            )
            valid, reason = validate_formal_submission(restored, restored_projects)
            if not valid:
                raise ValueError(reason)
            self.batch_tasks = restored
            saved_run_group = str(payload.get("runGroupId") or "").strip()
            self.run_group_id = (
                saved_run_group
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}", saved_run_group)
                else new_run_group_id("formal")
            )
            self._refresh_run_group_label()
            for project, variable in self.project_selection_vars.items():
                variable.set(project in restored_projects)
            restored_voice_plans = {
                str(item.get("voicePlan") or "").strip()
                for item in restored
                if str(item.get("project") or "").strip() != "ProjectB"
            }
            restored_voice_plans.discard("")
            if len(restored_voice_plans) == 1:
                restored_plan = next(iter(restored_voice_plans))
                self.voice_plan_var.set(VOICE_PLAN_CREATE if restored_plan == "create_new" else VOICE_PLAN_REUSE)
            self.batch_id = self._new_batch_id()
            self.submission_locked = False
            self.last_submission_receipt = None
            self.refresh_task_box()
            if len(restored_voice_plans) > 1:
                self.info_var.set(
                    "已加载的任务框混有“新建语音”和“复用已有语音”。"
                    "请重新选择一次语音处理模式后再提交；V13会同步整个任务框。"
                )
            else:
                fixed_total = sum(PROJECT_EXPECTED_TOTALS.get(project, 0) for project in restored_projects)
                self.info_var.set(
                    f"已加载保存任务：{len(restored)} 项，所选项目固定合同总计 {fixed_total} 条。可直接重新提交。"
                )
        except FileNotFoundError:
            if not silent:
                messagebox.showinfo("没有已保存任务", "当前版本没有保存的任务箱。", parent=self)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            if not silent:
                messagebox.showerror("加载保存任务失败", str(exc), parent=self)

    def submit_task_box(self, start_first=False):
        if self.cloud_pending:return self._query_cloud_receipt()
        if not self.batch_tasks:
            return messagebox.showwarning("任务框为空", "请先把至少一项链接加入任务框。", parent=self)
        try:
            selected_voice_plan = self._voice_plan_key()
            inconsistent = [
                task for task in self.batch_tasks
                if str(task.get("voicePlan") or "").strip() != selected_voice_plan
                and str(task.get("project") or "").strip() != "ProjectB"
            ]
            if inconsistent:
                raise ValueError(
                    "任务框语音模式与当前选择不一致。请重新选择一次“新建语音”或“复用已有语音”，"
                    "让该选择同步到任务框后再提交。"
                )
            assign_batch_contracts(self.batch_tasks)
            is_replenishment = any(bool(task.get("isReplenishment")) for task in self.batch_tasks)
            selected_projects = self._selected_projects()
            if not is_replenishment:
                valid, reason = validate_formal_submission(self.batch_tasks, selected_projects)
                if not valid:
                    raise ValueError(reason)
            self.refresh_task_box()
            payload = {
                "schemaVersion": 2,
                "batchId": self.batch_id,
                "batchKind": "formal",
                "runGroupId": self.run_group_id,
                "selectedProjects": selected_projects,
                "projectExpectedCount": sum(PROJECT_EXPECTED_TOTALS.get(project, 0) for project in selected_projects),
                # Keep the legacy wire value so an already-running receiver
                # accepts the task immediately.  The current workbench treats
                # it as a safe ordered queue (including gap replenishment).
                "startMode": "auto_start_first" if start_first else "pending_confirmation",
                "voicePlanIntent": selected_voice_plan,
                "audioBatchIntent": str(self.audio_batch_var.get()).strip(),
                "tasks": self.batch_tasks,
            }
            payload["clientSubmissionId"] = self.batch_id
            payload["payloadHash"] = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            # Preserve the exact repeatable inputs before network submission.
            # The next load creates fresh task and batch IDs, so old runtime
            # progress can never be resubmitted accidentally.
            self._save_task_box()
            content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            token = str(self.sender_config.get("accessToken") or "")
            if token:
                headers["X-Task-Token"] = token
            receiver = str(self.sender_config.get("receiverUrl") or DEFAULT_SENDER_CONFIG["receiverUrl"]).strip().rstrip("/")
            if receiver.lower().startswith("https://"):
                return self._submit_cloud(receiver,payload)
            health_request = Request(receiver + "/api/health", headers={"Accept": "application/json"})
            with open_home_service(health_request, timeout=5) as response:
                receiver_health = json.loads(response.read().decode("utf-8"))
            receiver_total = receiver_health.get("formalBatchTotal")
            receiver_projects = receiver_health.get("projectTotals")
            receiver_compatible = (
                receiver_total == FORMAL_BATCH_TOTAL
                and isinstance(receiver_projects, dict)
                and "ProjectD" in receiver_projects
            )
            if not receiver_compatible:
                shown_total = "未知（旧版）" if receiver_total is None else str(receiver_total)
                raise ValueError(
                    f"家里接收器任务合同未升级：接收器总数 {shown_total}，V{SUBMITTER_VERSION} 要求 {FORMAL_BATCH_TOTAL}，"
                    "并需要包含ProjectD。请先重启家里任务接收器，再重新提交；任务箱已保存，未写入生产队列。"
                )
            validation_request = Request(
                receiver + "/api/batches/validate",
                data=content,
                headers=headers,
                method="POST",
            )
            try:
                with open_home_service(validation_request, timeout=8) as response:
                    validation = json.loads(response.read().decode("utf-8"))
                if not validation.get("ok"):
                    raise ValueError(str(validation.get("message") or "接收器对账未通过"))
            except HTTPError as exc:
                if exc.code != 404:
                    raw = exc.read().decode("utf-8", errors="replace")
                    try:
                        detail = json.loads(raw).get("detail", raw)
                    except (ValueError, AttributeError):
                        detail = raw
                    raise ValueError(f"接收器对账失败：{detail}") from exc
            url = receiver + "/api/batches"
            request = Request(url, data=content, headers=headers, method="POST")
            with open_home_service(request, timeout=8) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not result.get("ok"):
                raise OSError("接收器没有确认任务框")
        except (KeyError, ValueError, OSError, URLError) as exc:
            return messagebox.showerror("统一提交失败", str(exc), parent=self)
        self._accept_submission(payload,result,start_first)

    def _accept_submission(self,payload,result,start_first):
        count = len(payload["tasks"])
        links = sum(len(task.get("urls") or []) for task in payload["tasks"])
        submitted_batch_id = payload["batchId"]
        self.submission_locked = True
        self.last_submission_receipt = {
            "schemaVersion": 1,
            "submittedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "batchId": submitted_batch_id,
            "runGroupId": payload.get("runGroupId"),
            "selectedProjects": list(payload.get("selectedProjects") or []),
            "clientSubmissionId": payload["clientSubmissionId"],
            "payloadHash": payload["payloadHash"],
            "projectCount": count,
            "videoCount": links,
            "taskIds": list(result.get("taskIds") or []),
            "duplicate": bool(result.get("duplicate")),
            "workbenchLaunchRequested": bool(result.get("workbenchLaunchRequested")),
            "workbenchLaunchError": str(result.get("workbenchLaunchError") or ""),
        }
        atomic_write_json(SUBMISSION_RECEIPT_FILE, self.last_submission_receipt)
        self.refresh_task_box()
        mode_text = "家里工作台将自动导入并启动首项。" if start_first else "家里工作台等待人工导入。"
        launch_note = ""
        if result.get("workbenchLaunchError"):
            launch_note = f" 工作台启动失败：{result['workbenchLaunchError']}"
        elif result.get("workbenchLaunchRequested"):
            launch_note = " 工作台启动请求已确认。"
        self.info_var.set(
            f"接收回执：{submitted_batch_id}，{count} 项 / {links} 条。{mode_text}{launch_note} "
            "任务框已锁定；点击“新建任务箱”后再录入下一批。"
        )
        messagebox.showinfo(
            "任务框已提交",
            f"接收器已保存 {count} 项任务、共 {links} 条链接。\n\n{mode_text}{launch_note}\n"
            "本页已保留只读回执，不会自动清空。",
            parent=self,
        )

    def _restore_cloud_pending(self):
        try:pending=load_pending(CLOUD_PENDING_FILE)
        except (OSError,ValueError):
            # A corrupt receipt must never fall through to fresh-ID submission.
            self.cloud_pending={"invalid":True};self.submission_locked=True
            self.info_var.set("待查询回执无法读取；保留文件并人工核对，禁止重新递交。")
            return True
        if not pending:return False
        self.cloud_pending=pending;payload=pending["payload"]
        self.batch_id=payload["batchId"];self.run_group_id=payload.get("runGroupId",self.run_group_id)
        self.batch_tasks=copy.deepcopy(payload["tasks"]);self.submission_locked=True
        for project,variable in self.project_selection_vars.items():variable.set(project in payload.get("selectedProjects",[]))
        self._refresh_run_group_label();self.refresh_task_box()
        self.info_var.set("已恢复原任务编号与待查询回执。点击“查询云端回执”；不会自动重发 POST。")
        return True

    def _query_cloud_receipt(self):
        if not self.cloud_pending or self.cloud_pending.get("invalid"):
            return messagebox.showinfo("回执","没有可自动查询的回执；无效文件请保留并人工核对。",parent=self)
        self._submit_cloud(self.cloud_pending["origin"],self.cloud_pending["payload"])

    def _cloud_phase(self,phase):
        labels={"submitting":"正在递交云端；家里尚未确认", "cloud_received":"云端已收件，家里待接收", "waiting_home":"已发家里，等待真实回执", "receipt_lookup":"收件回包待确认，正在按原编号查询", "unknown":"结果不明，请在云端网页核对，禁止重复递交", "home_received":"家里已返回真实接收回执", "rejected":"家里已拒绝，请核对回执"}
        self.info_var.set(labels.get(phase,"云端状态："+str(phase)))

    def _submit_cloud(self,receiver,payload):
        if self.cloud_worker_running:return
        payload=copy.deepcopy(payload)
        try:
            transport=CloudSubmission(receiver,CLOUD_PENDING_FILE)
            self.cloud_pending=transport.prepare(payload)
        except (OSError,ValueError,PendingSubmission) as exc:
            return messagebox.showerror("云端递交未开始",str(exc),parent=self)
        self.cloud_worker_running=True;self.submission_locked=True;self.refresh_task_box()
        self.query_cloud_button.configure(state="disabled")
        def worker():
            try:
                result=transport.run(payload,notify=lambda phase:self.after(0,lambda phase=phase:self._cloud_phase(phase)),expected_total=FORMAL_BATCH_TOTAL)
            except (OSError,ValueError,PendingSubmission,http.client.HTTPException) as exc:
                message=str(exc)
                self.after(0,lambda message=message:self._cloud_pending_error(message))
            else:self.after(0,lambda:self._cloud_success(transport,payload,result))
        threading.Thread(target=worker,daemon=True,name="https-submitter").start()

    def _cloud_pending_error(self,message):
        self.cloud_worker_running=False;self.query_cloud_button.configure(state="normal")
        self.info_var.set(message+" 输入与请求编号已保存；只查询，不重复提交。")

    def _cloud_success(self,transport,payload,result):
        self.cloud_worker_running=False;self.query_cloud_button.configure(state="normal")
        self._accept_submission(payload,result,payload.get("startMode")=="auto_start_first")
        # Save the real receipt before deleting the resumable input/IDs.
        transport.complete();self.cloud_pending=None

    def _release_cloud_receipt(self):
        if self.cloud_worker_running or not self.cloud_pending or self.cloud_pending.get("invalid"):return
        if not messagebox.askyesno("结束回执","先在云端网页核对并撤销未发送命令，或完成人工对账。确认只结束已终结回执？不会重试原命令。",parent=self):return
        pending=copy.deepcopy(self.cloud_pending);self.cloud_worker_running=True
        def worker():
            try:
                transport=CloudSubmission(pending["origin"],CLOUD_PENDING_FILE);transport.release()
            except (OSError,ValueError,PendingSubmission,http.client.HTTPException) as exc:
                message=str(exc);self.after(0,lambda message=message:self._cloud_pending_error(message))
            else:self.after(0,self._cloud_release_done)
        threading.Thread(target=worker,daemon=True,name="https-receipt-close").start()

    def _cloud_release_done(self):
        self.cloud_worker_running=False;self.cloud_pending=None;self.submission_locked=True
        self.query_cloud_button.configure(state="normal");self.refresh_task_box()
        self.info_var.set("原回执已终结。下一批请点“新建任务箱”；不会重试原任务。")

    def sync_dashboard_status(self):
        """Read home status only; all remote control still goes to the receiver."""
        if str(self.sender_config.get("receiverUrl") or "").lower().startswith("https://"):
            self.sync_info_var.set("云端模式：请在网页登录后查看项目状态")
            return
        if self.dashboard_sync_running:
            return
        self.dashboard_sync_running = True
        self.sync_info_var.set("正在读取家里 Dashboard…")
        dashboard = self._dashboard_url()
        scope = self.status_scope_var.get()
        selected_date = self.date_var.get().strip()
        query = urlencode({"date": selected_date}) if scope == "当前生产日期" and selected_date else ""
        status_url = dashboard + "/api/project-gaps" + (f"?{query}" if query else "")

        def worker():
            try:
                request = Request(status_url, headers={"Accept": "application/json"})
                with open_home_service(request, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                items = payload.get("items") if isinstance(payload, dict) else []
                if not isinstance(items, list):
                    items = []
                self.after(0, lambda: self._apply_dashboard_gaps(items))
            except (OSError, URLError, ValueError):
                self.after(0, self._dashboard_sync_failed)

        threading.Thread(target=worker, daemon=True).start()

    def _dashboard_url(self):
        """Use the home receiver's Tailscale host automatically when configured."""
        configured = str(self.sender_config.get("dashboardUrl") or "").strip().rstrip("/")
        receiver = str(self.sender_config.get("receiverUrl") or "").strip()
        receiver_parts = urlsplit(receiver)
        if receiver_parts.scheme == "https":return receiver.rstrip("/")
        is_default_local = configured in {"", DEFAULT_SENDER_CONFIG["dashboardUrl"]}
        if is_default_local and receiver_parts.hostname and receiver_parts.hostname not in {"127.0.0.1", "localhost"}:
            scheme = receiver_parts.scheme or "http"
            return f"{scheme}://{receiver_parts.hostname}:8765"
        return configured or DEFAULT_SENDER_CONFIG["dashboardUrl"]

    def _apply_dashboard_gaps(self, items):
        self.dashboard_sync_running = False
        self.gap_tree.delete(*self.gap_tree.get_children())
        self.gap_items_by_row.clear()
        selected_date = self.date_var.get().strip()
        scope = self.status_scope_var.get()
        filtered = filter_project_status_items(items, selected_date, scope)
        new_blocked_items = []
        for item in filtered:
            try:
                expected = int(item.get("expectedCount") or 0)
                completed = int(item.get("completed") or 0)
                stored_missing = item.get("missingCount")
                missing = int(stored_missing) if stored_missing not in (None, "") else max(0, expected - completed)
            except (TypeError, ValueError):
                expected, completed, missing = 0, 0, 0
            status = str(item.get("stateText") or item.get("status") or "--")
            status_key = str(item.get("status") or "")
            gap_active = bool(item.get("gapActive")) or status_key == "blocked_missing_links"
            not_started = bool(item.get("notStarted")) or status_key in {"planned", "waiting_download"}
            tag = "blocked" if gap_active else ("waiting" if not_started else "completed")
            if tag == "blocked":
                signature = "|".join((
                    str(item.get("taskId") or ""),
                    str(item.get("gapRevision") or "1"),
                    str(completed),
                    str(missing),
                ))
                if signature not in self.notified_gap_signatures:
                    self.notified_gap_signatures.add(signature)
                    new_blocked_items.append((item, completed, expected, missing))
            row_id = str(item.get("taskId") or f"gap-{len(self.gap_items_by_row)}")
            if row_id in self.gap_items_by_row:
                row_id = f"{row_id}-{len(self.gap_items_by_row)}"
            self.gap_items_by_row[row_id] = dict(item)
            self.gap_tree.insert("", "end", iid=row_id, values=(
                item.get("date") or "--", item.get("project") or "--", item.get("material") or "--",
                f"{completed}/{expected}", item.get("failed") or 0, missing, status,
            ), tags=(tag,))
        blocked = sum(
            1 for item in filtered
            if bool(item.get("gapActive")) or str(item.get("status") or "") == "blocked_missing_links"
        )
        if not filtered and scope == "当前生产日期":
            self.sync_info_var.set(f"{selected_date} 暂无家里已接收任务")
        else:
            self.sync_info_var.set(f"已同步 {len(filtered)} 项；待补链 {blocked} 项")
        if new_blocked_items and scope == "当前生产日期":
            item, completed, expected, missing = new_blocked_items[0]
            failure_items = item.get("failureItems") if isinstance(item.get("failureItems"), list) else []
            urls = [str(row.get("url") or "") for row in failure_items if isinstance(row, dict) and row.get("url")]
            detail = "\n".join(urls[:8]) or "请在下方“家里项目进度 / 缺口”表查看详情。"
            messagebox.showwarning(
                "下载缺口提醒",
                f"{item.get('date') or '--'} / {item.get('project') or '--'} / {item.get('material') or '--'}\n"
                f"已完成 {completed} / 计划 {expected}，还差 {missing} 条。\n\n"
                f"需要补的链接：\n{detail}\n\n"
                "请补齐链接后作为新任务加入任务框并提交；不会重跑已完成的视频。",
                parent=self,
            )
        self.after(8000, self.sync_dashboard_status)

    def prepare_replenishment(self):
        """Turn one Dashboard gap row into an exact replacement-link task."""
        selection = self.gap_tree.selection()
        if not selection:
            return messagebox.showinfo("请选择缺口", "请先选中下方需要补链的项目行。", parent=self)
        item = self.gap_items_by_row.get(str(selection[0]))
        if not item:
            return messagebox.showwarning("缺口读取失败", "没有找到这条项目缺口的详细信息，请先点击“刷新同步”。", parent=self)
        try:
            expected = max(1, int(item.get("expectedCount") or 0))
            completed = max(0, int(item.get("completed") or 0))
            stored_missing = item.get("missingCount")
            missing = max(0, int(stored_missing)) if stored_missing not in (None, "") else max(0, expected - completed)
        except (TypeError, ValueError):
            return messagebox.showwarning("缺口数据无效", "无法识别该项目需要补几条链接。", parent=self)
        if not missing:
            return messagebox.showinfo("无需补链", "这个项目没有待补的视频。", parent=self)
        project = str(item.get("project") or "").strip()
        material = str(item.get("material") or "").strip()
        if project not in self.templates_by_project:
            return messagebox.showwarning("项目无法补链", f"投递器没有“{project}”的项目模板。", parent=self)
        self.date_var.set(str(item.get("date") or self.date_var.get()).strip())
        self.project_var.set(project)
        self.material_var.set(material)
        self.expected_count_var.set(str(missing))
        self.replenish_context = {
            "taskId": str(item.get("taskId") or ""),
            "expectedCount": expected,
            "completed": completed,
            "missingCount": missing,
        }
        self.links.delete("1.0", "end")
        self.links.focus_set()
        self.info_var.set(
            f"补链模式：{project} / {material}，原计划 {expected}，已完成 {completed}。"
            f"请粘贴 {missing} 条新链接，再点“加入任务框”。"
        )

    def _dashboard_sync_failed(self):
        self.dashboard_sync_running = False
        self.sync_info_var.set("家里状态无法同步（不会把旧数据当成最新）")
        self.after(12000, self.sync_dashboard_status)


if __name__ == "__main__":
    CompanyTaskSubmitter().mainloop()
