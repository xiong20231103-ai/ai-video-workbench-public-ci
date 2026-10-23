from __future__ import annotations

import copy
import http.client
from submission_transport import CloudSubmission,PendingSubmission,load_pending
from submission_transport import SubmissionRejected as CloudRejected
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener
import uuid

from examplebrand_contract import (
    ExampleBrand_ACCOUNT_SLOTS,
    ExampleBrand_PRODUCTS,
    ExampleBrand_TOTAL,
    build_examplebrand_batch,
    validate_examplebrand_rows,
)
from submitter_instance_lock import acquire_instance_lock
from local_import_bridge import (
    IMPORTS_ROOT,
    claim_import,
    release_claim,
    read_import,
    read_import_status,
    write_import_status,
)
from submitter_connection import DEFAULT_CONNECTION, connection_error, load_connection


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "task_submitter_config.json"
CLOUD_PENDING_FILE = APP_DIR / "task_submitter_pending.json"
SAVED_TASK_BOX_FILE = APP_DIR / "examplebrand_saved_task_box.json"
SUBMISSION_FILE = APP_DIR / "examplebrand-last-submission.json"
SUBMISSION_LOG_FILE = APP_DIR / "examplebrand-submission-log.jsonl"
HISTORY_DIR = APP_DIR / "examplebrand-history"
RUN_GROUP_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}")
URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
SUBMISSION_DEADLINE_SECONDS = 30
QUERY_DEADLINE_SECONDS = 18


class SubmissionRejected(Exception):
    """The receiver gave a definite validation or business rejection."""


class SubmissionUncertain(Exception):
    """The request outcome cannot be known from the company-side response."""


def new_run_group_id() -> str:
    return f"run-examplebrand-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"


def new_batch_id() -> str:
    return f"examplebrand-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"


def parse_links(text: str) -> list[str]:
    """Accept one URL per line or a pasted paragraph containing URLs."""
    raw = str(text or "").replace("\r", "")
    matches = URL_PATTERN.findall(raw)
    if matches:
        return [item.rstrip(".,;，。；、)】]）") for item in matches]
    return [line.strip().rstrip(".,;，。；、)】]）") for line in raw.splitlines() if line.strip()]


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def append_json_log(event: dict) -> None:
    """Best-effort local audit log; secrets are never included in event data."""
    try:
        SUBMISSION_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with SUBMISSION_LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def _receiver_error(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            detail = json.loads(raw).get("detail", raw)
        except (OSError, ValueError, TypeError, AttributeError):
            detail = str(exc)
        return f"HTTP {exc.code}: {detail}"
    return str(exc) or exc.__class__.__name__


def _request_json(url: str, *, data: bytes | None = None, method: str = "GET", timeout: int = 10, token: str = "") -> dict:
    request = Request(
        url,
        data=data,
        headers={
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {}),
            **({"X-Task-Token": token} if token else {}),
        },
        method=method,
    )
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=max(1, int(timeout))) as response:
            raw = response.read()
        payload = json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        if 400 <= exc.code < 500 and exc.code not in {408, 429}:
            raise SubmissionRejected(_receiver_error(exc)) from exc
        raise SubmissionUncertain(_receiver_error(exc)) from exc
    except (URLError, TimeoutError, socket.timeout, OSError, IncompleteRead, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SubmissionUncertain(_receiver_error(exc)) from exc
    if not isinstance(payload, dict):
        raise SubmissionUncertain("接收器返回了无法识别的JSON")
    return payload


def post_json(url: str, payload: dict, timeout: int = 10, token: str = "") -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    result = _request_json(url.rstrip("/") + "/api/batches", data=data, method="POST", timeout=timeout, token=token)
    if result.get("ok") is False:
        raise SubmissionRejected(str(result.get("message") or result.get("detail") or "接收器拒绝任务框"))
    return result


def validate_remote(url: str, payload: dict, timeout: int = 10, token: str = "") -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        result = _request_json(url.rstrip("/") + "/api/batches/validate", data=data, method="POST", timeout=timeout, token=token)
    except SubmissionRejected as exc:
        # Older receivers may not expose the optional validation endpoint;
        # the idempotent /api/batches call remains authoritative in that case.
        if str(exc).startswith("HTTP 404"):
            return {"ok": True, "validationSkipped": True}
        raise
    if result.get("ok") is False:
        raise SubmissionRejected(str(result.get("message") or result.get("detail") or "接收器对账未通过"))
    return result


def business_fingerprint(payload: dict | None = None, rows: list[dict] | None = None, production_date: str = "") -> str:
    """Hash only business inputs so duplicate imports with fresh IDs coalesce."""
    if isinstance(payload, dict):
        tasks = payload.get("tasks") or []
        production_date = str(payload.get("productionDate") or production_date).strip()
        normalized_rows = [
            {
                "product": str(item.get("product") or item.get("material") or "").strip(),
                "expectedCount": int(item.get("expectedCount") or len(item.get("urls") or [])),
                "urls": [str(url).strip() for url in item.get("urls") or [] if str(url).strip()],
            }
            for item in tasks
            if isinstance(item, dict)
        ]
    else:
        normalized_rows = [
            {
                "product": str(item.get("product") or item.get("material") or "").strip(),
                "expectedCount": int(item.get("expectedCount") or item.get("count") or len(item.get("urls") or [])),
                "urls": [str(url).strip() for url in item.get("urls") or [] if str(url).strip()],
            }
            for item in rows or []
            if isinstance(item, dict)
        ]
    identity = {"productionDate": str(production_date or "").strip(), "rows": normalized_rows}
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def ensure_payload_hash(payload: dict) -> dict:
    """Attach the stable contract hash without hashing the hash field itself."""
    result = copy.deepcopy(payload)
    existing = str(result.get("payloadHash") or "").strip()
    if existing:
        return result
    canonical = copy.deepcopy(result)
    canonical.pop("payloadHash", None)
    result["payloadHash"] = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return result


def query_remote(url: str, payload: dict, timeout: int = 10, token: str = "") -> dict:
    """Query active receiver records; an absent active item remains unknown."""
    response = _request_json(url.rstrip("/") + "/api/tasks", timeout=timeout, token=token)
    items = response.get("items") if isinstance(response.get("items"), list) else []
    task_ids = {str(item.get("taskId") or "") for item in payload.get("tasks") or []}
    batch_id = str(payload.get("batchId") or "")
    run_group_id = str(payload.get("runGroupId") or "")
    payload_hash = str(payload.get("payloadHash") or "")
    matches = [
        item for item in items
        if isinstance(item, dict)
        and (
            str(item.get("taskId") or "") in task_ids
            or (batch_id and str(item.get("batchId") or "") == batch_id)
            or (payload_hash and str(item.get("payloadHash") or "") == payload_hash)
            or (run_group_id and str(item.get("runGroupId") or "") == run_group_id and str(item.get("batchKind") or "") == "examplebrand-natural-flow")
        )
    ]
    return {"matched": bool(matches), "items": matches, "activeCount": len(items)}


class ExamplebrandSubmitter(tk.Tk):
    """ExampleBrand entry form using the same task-box workflow as formal projects."""

    def __init__(self):
        super().__init__()
        self.title("AI视频生产线 · ExampleBrand自然流任务递交器 V14")
        self.geometry("1180x820")
        self.minsize(980, 700)
        self.configure(bg="#F5F7FA")
        self.rows: list[dict] = []
        self.batch_id = new_batch_id()
        self.date_var = tk.StringVar(value=f"{datetime.now().month}.{datetime.now().day}")
        self.receiver_var = tk.StringVar(value=DEFAULT_CONNECTION["receiverUrl"])
        self.run_group_var = tk.StringVar(value=new_run_group_id())
        self.product_var = tk.StringVar(value=ExampleBrand_PRODUCTS[0])
        self.expected_count_var = tk.StringVar(value="1")
        self.current_link_status_var = tk.StringVar(value="有效链接 0 / 计划 1")
        self.total_var = tk.StringVar(value=f"计划 0 / {ExampleBrand_TOTAL}")
        self.status_var = tk.StringVar(value="独立ExampleBrand任务箱；可按产品分行加入，合计21条后提交。")
        self.task_summary_var = tk.StringVar(value="0个产品行 / 0条")
        self.access_token = ""
        self.cloud_pending = None
        self.submission_locked = False
        self.submit_in_progress = False
        self.query_in_progress = False
        self.submission_state = "draft"
        self.active_payload: dict | None = None
        self.last_submission_receipt: dict | None = None
        self.current_operation_id = 0
        self._stale_saved_submission: dict | None = None
        self._closing = False
        self._run_group_preset = False
        self._bridge_poll_job = None
        self._load_config()
        self._build()
        self._restore_cloud_pending()
        if isinstance(self.active_payload, dict):
            # Migrate contracts written by pre-hash builds during startup so
            # a restart preserves the exact payloadHash used for queries.
            self.active_payload = ensure_payload_hash(self.active_payload)
            self._save_task_box()
        if self._stale_saved_submission:
            self._archive_state(self._stale_saved_submission, "stale-run-group")
            self._save_task_box()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._bridge_poll_job = self.after(1200, self._poll_local_imports)

    def _load_config(self):
        payload, _source = load_connection(APP_DIR, CONFIG_FILE)
        try:
            self.receiver_var.set(str(payload.get("receiverUrl") or self.receiver_var.get()))
            self.access_token = str(payload.get("accessToken") or "")
            configured_run_group = str(payload.get("runGroupId") or "").strip()
            if RUN_GROUP_PATTERN.fullmatch(configured_run_group):
                self.run_group_var.set(configured_run_group)
                self._run_group_preset = True
        except (OSError, ValueError, TypeError):
            pass
        preset_run_group = str(os.environ.get("AI_VIDEO_RUN_GROUP_ID") or "").strip()
        if RUN_GROUP_PATTERN.fullmatch(preset_run_group):
            self.run_group_var.set(preset_run_group)
            self._run_group_preset = True
        try:
            saved = json.loads(SAVED_TASK_BOX_FILE.read_text(encoding="utf-8"))
            saved_run_group = str(saved.get("runGroupId") or "").strip()
            saved_submitted = bool(saved.get("submitted"))
            saved_state = str(saved.get("submissionState") or ("accepted" if saved_submitted else "draft"))
            current_run_group = self.run_group_var.get().strip()
            has_explicit_run_group = self._run_group_preset
            stale = bool(
                has_explicit_run_group
                and saved_run_group
                and current_run_group
                and saved_run_group != current_run_group
            )
            if stale:
                self._stale_saved_submission = dict(saved)
                # Carry the matching historical receiver receipt into the
                # archive so the UI can distinguish an old accepted batch
                # from a new run group without reusing that identity.
                try:
                    previous = json.loads(SUBMISSION_FILE.read_text(encoding="utf-8"))
                    previous_payload = previous.get("payload") if isinstance(previous, dict) else None
                    if isinstance(previous_payload, dict) and str(previous_payload.get("batchId") or "") == str(saved.get("batchId") or ""):
                        self._stale_saved_submission["receipt"] = previous.get("receipt")
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    pass
                self.status_var.set(
                    f"发现旧运行组的已保存任务：{saved_run_group}；当前运行组为 {current_run_group}，旧任务已归档，等待载入当前导入。"
                )
                self.rows = []
                self.batch_id = new_batch_id()
                self.active_payload = None
                self.last_submission_receipt = None
                self.submission_locked = False
                self.submission_state = "draft"
                return
            if not self._run_group_preset and RUN_GROUP_PATTERN.fullmatch(saved_run_group):
                self.run_group_var.set(saved_run_group)
            saved_date = str(saved.get("productionDate") or "").strip()
            if saved_date:
                self.date_var.set(saved_date)
            saved_rows = saved.get("rows")
            if isinstance(saved_rows, list):
                restored = []
                for item in saved_rows:
                    if not isinstance(item, dict):
                        continue
                    product = str(item.get("product") or "").strip()
                    urls = [str(url).strip() for url in item.get("urls") or [] if str(url).strip()]
                    count = int(item.get("expectedCount") or len(urls))
                    restored.append({"product": product, "expectedCount": count, "urls": urls})
                issues = validate_examplebrand_rows(restored)
                if not issues or (len(issues) == 1 and "必须正好 21 条" in issues[0]):
                    self.rows = restored
                    self.batch_id = str(saved.get("batchId") or self.batch_id).strip() or self.batch_id
                    contract = saved.get("contract")
                    if isinstance(contract, dict):
                        self.active_payload = ensure_payload_hash(contract)
                    self.last_submission_receipt = saved.get("receipt") if isinstance(saved.get("receipt"), dict) else None
                    self.submission_locked = saved_submitted
                    self.submission_state = saved_state
                    if self.submission_state in {"submitting", "querying"}:
                        self.submission_state = "receipt_pending"
                        self.submission_locked = False
                        self.status_var.set("上次操作在程序退出前未完成：接收状态待确认，请查询家里接收器。")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        if self.active_payload is None and SUBMISSION_FILE.is_file():
            try:
                last = json.loads(SUBMISSION_FILE.read_text(encoding="utf-8"))
                contract = last.get("payload") if isinstance(last, dict) else None
                if (
                    isinstance(contract, dict)
                    and str(contract.get("batchId") or "") == self.batch_id
                    and str(contract.get("runGroupId") or "") == self.run_group_var.get().strip()
                ):
                    self.active_payload = ensure_payload_hash(contract)
                    self.last_submission_receipt = last.get("receipt") if isinstance(last.get("receipt"), dict) else None
                    self.submission_state = str(last.get("submissionState") or self.submission_state)
                    if self.submission_state in {"submitting", "querying"}:
                        self.submission_state = "receipt_pending"
                        self.submission_locked = False
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass

    def _styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Card.TFrame", background="#FFFFFF")
        style.configure("Title.TLabel", background="#FFFFFF", foreground="#172033", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Body.TLabel", background="#FFFFFF", foreground="#475467", font=("Microsoft YaHei UI", 9))
        style.configure("Stage.TLabel", background="#F7FAFF", foreground="#1D4ED8", font=("Microsoft YaHei UI", 9))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=9)

    def _build(self):
        self._styles()
        viewport = ttk.Frame(self, style="Card.TFrame")
        viewport.pack(fill="both", expand=True, padx=18, pady=18)
        scrollbar = ttk.Scrollbar(viewport, orient="vertical")
        canvas = tk.Canvas(viewport, highlightthickness=0, background="#FFFFFF", yscrollcommand=scrollbar.set)
        scrollbar.configure(command=canvas.yview)
        card = ttk.Frame(canvas, style="Card.TFrame", padding=18)
        card.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=card, anchor="nw")
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        ttk.Label(card, text="AI视频生产线 · ExampleBrand自然流任务递交器 V13", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            card,
            text="ExampleBrand独立21条任务箱；按建材递交器的任务框方式录入多个产品，提交后不会自动启动工作台。",
            style="Body.TLabel",
        ).pack(anchor="w", pady=(4, 14))

        fields = ttk.Frame(card, style="Card.TFrame")
        fields.pack(fill="x")
        ttk.Label(fields, text="生产日期", style="Body.TLabel").grid(row=0, column=0, sticky="w")
        self.date_entry = ttk.Entry(fields, textvariable=self.date_var, width=13)
        self.date_entry.grid(row=1, column=0, sticky="w", pady=(3, 0))
        ttk.Label(fields, text="家里接收器", style="Body.TLabel").grid(row=0, column=1, sticky="w", padx=(18, 0))
        self.receiver_entry = ttk.Entry(fields, textvariable=self.receiver_var, width=38)
        self.receiver_entry.grid(row=1, column=1, sticky="w", padx=(18, 0), pady=(3, 0))
        ttk.Label(fields, text="运行组ID（自动绑定）", style="Body.TLabel").grid(row=0, column=2, sticky="w", padx=(18, 0))
        self.run_group_entry = ttk.Entry(fields, textvariable=self.run_group_var, width=34)
        self.run_group_entry.grid(row=1, column=2, sticky="w", padx=(18, 0), pady=(3, 0))
        ttk.Button(fields, text="复制运行组ID", command=self._copy_run_group).grid(row=1, column=3, padx=(8, 0), pady=(3, 0))
        ttk.Label(fields, textvariable=self.total_var, style="Stage.TLabel").grid(row=1, column=4, sticky="e", padx=(18, 0), pady=(3, 0))

        ttk.Label(
            card,
            text="ExampleBrand可单独生成运行组ID；若和正式项目组合执行，请使用统一入口自动传入的同一ID。英语18条、西语3条，7个账号各3条。",
            style="Body.TLabel",
        ).pack(anchor="w", pady=(8, 12))

        entry = ttk.LabelFrame(card, text="加入产品任务", padding=10)
        entry.pack(fill="x", pady=(0, 12))
        entry_fields = ttk.Frame(entry)
        entry_fields.pack(fill="x")
        ttk.Label(entry_fields, text="产品").pack(side="left")
        self.product_box = ttk.Combobox(entry_fields, state="readonly", width=23, values=ExampleBrand_PRODUCTS, textvariable=self.product_var)
        self.product_box.pack(side="left", padx=(6, 16))
        ttk.Label(entry_fields, text="计划数量").pack(side="left")
        self.count_box = ttk.Spinbox(entry_fields, from_=1, to=ExampleBrand_TOTAL, width=8, textvariable=self.expected_count_var, command=self._refresh_current_link_status)
        self.count_box.pack(side="left", padx=(6, 16))
        ttk.Label(entry_fields, textvariable=self.current_link_status_var, style="Stage.TLabel").pack(side="left", padx=(6, 12))
        self.entry_add_button = ttk.Button(entry_fields, text="加入任务框", style="Primary.TButton", command=self.add_current_task)
        self.entry_add_button.pack(side="right")
        self.links = tk.Text(entry, height=5, wrap="none", font=("Consolas", 9))
        self.links.pack(fill="x", expand=True, pady=(8, 0))
        self.links.bind("<KeyRelease>", lambda _event: self._refresh_current_link_status())
        ttk.Label(entry, text="链接可每行一条，也可以直接粘贴一整段文字；加入任务框后可继续选择其他产品。", style="Body.TLabel").pack(anchor="w", pady=(5, 0))

        queue = ttk.Frame(card, style="Card.TFrame")
        queue.pack(fill="both", expand=True)
        queue_header = ttk.Frame(queue, style="Card.TFrame")
        queue_header.pack(fill="x")
        ttk.Label(queue_header, text="当前ExampleBrand任务框（可添加多个产品，按列表顺序分配账号）", style="Body.TLabel").pack(side="left")
        ttk.Label(queue_header, textvariable=self.task_summary_var, style="Stage.TLabel").pack(side="right")
        columns = ("sequence", "product", "language", "accounts", "filled", "planned")
        self.task_tree = ttk.Treeview(queue, columns=columns, show="headings", height=7, selectmode="browse")
        for key, label, width in (
            ("sequence", "顺序", 52),
            ("product", "产品", 150),
            ("language", "最终语言分配", 150),
            ("accounts", "账号分配", 260),
            ("filled", "已填", 70),
            ("planned", "计划", 70),
        ):
            self.task_tree.heading(key, text=label)
            self.task_tree.column(key, width=width, anchor="center")
        self.task_tree.pack(fill="x", pady=(6, 6))
        buttons = ttk.Frame(queue, style="Card.TFrame")
        buttons.pack(fill="x")
        self.add_button = ttk.Button(buttons, text="加入任务框", command=self.add_current_task)
        self.add_button.pack(side="left")
        self.remove_button = ttk.Button(buttons, text="删除所选", command=self.remove_selected_task)
        self.remove_button.pack(side="left", padx=(8, 0))
        self.up_button = ttk.Button(buttons, text="上移", command=lambda: self.move_selected_task(-1))
        self.up_button.pack(side="left", padx=(8, 0))
        self.down_button = ttk.Button(buttons, text="下移", command=lambda: self.move_selected_task(1))
        self.down_button.pack(side="left", padx=(8, 0))
        self.clear_button = ttk.Button(buttons, text="清空任务框", command=self.clear_task_box)
        self.clear_button.pack(side="left", padx=(8, 0))
        self.new_button = ttk.Button(buttons, text="新建任务箱", command=self.new_task_box)
        self.new_button.pack(side="left", padx=(8, 0))
        self.reload_button = ttk.Button(buttons, text="重新加载已保存任务", command=self.reload_saved_task_box)
        self.reload_button.pack(side="left", padx=(8, 0))
        self.preview_button = ttk.Button(buttons, text="提交前完整对账", command=self._preview)
        self.preview_button.pack(side="right")

        footer = ttk.Frame(card, style="Card.TFrame")
        footer.pack(fill="x", pady=(14, 0))
        ttk.Label(footer, textvariable=self.status_var, style="Body.TLabel", wraplength=760).pack(side="left", fill="x", expand=True)
        self.submit_button = ttk.Button(footer, text="提交到家里接收器（待确认）", style="Primary.TButton", command=self._submit)
        self.submit_button.pack(side="right", padx=(12, 0))
        ttk.Button(footer,text="结束待查询回执",command=self._release_cloud_receipt).pack(side="right")
        ttk.Button(footer,text="继续未提交任务",command=self._resume_cloud_submission).pack(side="right")
        self.query_button = ttk.Button(footer, text="查询接收状态", command=self._query_receipt)
        self.query_button.pack(side="right", padx=(12, 0))
        self.date_var.trace_add("write", self._identity_field_changed)
        self.run_group_var.trace_add("write", self._identity_field_changed)
        self._refresh_current_link_status()
        self.refresh_task_box()

    def _identity_field_changed(self, *_args):
        if not getattr(self, "task_tree", None):
            return
        if not self.submission_locked and not self.submit_in_progress and not self.query_in_progress:
            self.active_payload = None
            if self.rows:
                self._save_task_box()

    def _copy_run_group(self):
        value = self.run_group_var.get().strip()
        if not RUN_GROUP_PATTERN.fullmatch(value):
            return messagebox.showerror("运行组ID无效", "运行组ID格式无效。", parent=self)
        self.clipboard_clear()
        self.clipboard_append(value)
        self.update_idletasks()
        self.status_var.set(f"已复制运行组ID：{value}")

    def _set_bridge_child_status(self, import_id: str, state: str, **extra) -> None:
        status = read_import_status(import_id)
        child_status = dict(status.get("childStatus") or {})
        child_status["examplebrand-natural-flow"] = {
            **dict(child_status.get("examplebrand-natural-flow") or {}),
            "state": state,
            **extra,
        }
        write_import_status(import_id, {"childStatus": child_status})

    def _poll_local_imports(self):
        """Consume persisted ExampleBrand imports exactly once after the window is ready."""
        if self._closing:
            return
        try:
            for path in sorted(IMPORTS_ROOT.glob("*.json"), key=lambda item: item.stat().st_mtime):
                import_id = path.name[:-5]
                if not import_id or import_id.endswith(".status"):
                    continue
                payload = read_import(import_id)
                status = read_import_status(import_id)
                child_status = dict(status.get("childStatus") or {})
                if "examplebrand-natural-flow" not in child_status:
                    continue
                child = dict(child_status.get("examplebrand-natural-flow") or {})
                child_state = str(child.get("state") or "")
                # A prior build used ``blocked`` for the stale submitted-lock
                # condition.  Revisit only blocked imports belonging to the
                # active run group so that those imports can be migrated to
                # the new duplicate-safe state machine; unrelated historical
                # blocked imports remain untouched.
                retryable_blocked = child_state == "blocked" and str(status.get("runGroupId") or "") == self.run_group_var.get().strip()
                if not payload or child_state in {"loaded", "duplicate", "error", "completed"} or (child_state == "blocked" and not retryable_blocked):
                    continue
                if retryable_blocked:
                    # The previous build left a claim marker even though it
                    # never loaded the import. Release that stale marker once
                    # before handing the record to the migration path.
                    release_claim(import_id, "examplebrand")
                if not claim_import(import_id, "examplebrand"):
                    continue
                try:
                    self._apply_local_import(import_id, payload)
                except Exception as exc:
                    self._set_bridge_child_status(import_id, "error", error=str(exc))
                    self.status_var.set(f"脚本导入失败：{exc}")
        except OSError:
            pass
        try:
            self._bridge_poll_job = self.after(1200, self._poll_local_imports)
        except tk.TclError:
            self._bridge_poll_job = None

    def _apply_local_import(self, import_id: str, envelope: dict) -> None:
        if self.cloud_pending:
            self._set_bridge_child_status(import_id,"blocked",error="仍有云端待查询回执")
            return
        run_group_id = str(envelope.get("runGroupId") or "").strip()
        if run_group_id != self.run_group_var.get().strip():
            self._set_bridge_child_status(import_id, "blocked", error="runGroupId与当前ExampleBrand递交器不一致")
            return
        payload = next(
            (
                item for item in envelope.get("payloads") or []
                if isinstance(item, dict) and str(item.get("batchKind") or "") == "examplebrand-natural-flow"
            ),
            None,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list) or not payload["tasks"]:
            self._set_bridge_child_status(import_id, "blocked", error="ExampleBrand子合同缺少任务")
            return
        payload = ensure_payload_hash(payload)
        incoming = [copy.deepcopy(item) for item in payload["tasks"] if isinstance(item, dict)]
        if len(incoming) != len(payload["tasks"]):
            self._set_bridge_child_status(import_id, "blocked", error="ExampleBrand任务记录格式无效")
            return
        incoming_products = [str(item.get("product") or item.get("material") or "").strip() for item in incoming]
        if any(not product for product in incoming_products) or len(set(incoming_products)) != len(incoming_products):
            self._set_bridge_child_status(import_id, "blocked", error="ExampleBrand子合同包含重复或缺失产品行")
            return
        incoming_fingerprint = business_fingerprint(payload=payload)
        current_fingerprint = business_fingerprint(
            rows=self.rows,
            production_date=self.date_var.get().strip(),
        ) if self.rows else ""
        if self.active_payload and incoming_fingerprint == business_fingerprint(payload=self.active_payload):
            self._set_bridge_child_status(
                import_id,
                "duplicate",
                duplicateOfBatchId=self.batch_id,
                duplicateOfTaskIds=[str(item.get("taskId") or "") for item in self.active_payload.get("tasks") or []],
                duplicateAt=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
            self.status_var.set(f"已忽略重复导入：当前批次 {self.batch_id} 保留原任务身份。")
            return
        if current_fingerprint and incoming_fingerprint == current_fingerprint:
            self._set_bridge_child_status(
                import_id,
                "duplicate",
                duplicateOfBatchId=self.batch_id,
                duplicateAt=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
            self.status_var.set(f"已忽略重复导入：当前批次 {self.batch_id} 保留原任务身份。")
            return
        existing_urls = {
            str(url).strip()
            for row in self.rows
            for url in row.get("urls") or []
            if str(url).strip()
        }
        incoming_urls = {
            str(url).strip()
            for item in incoming
            for url in item.get("urls") or []
            if str(url).strip()
        }
        if existing_urls & incoming_urls:
            self._set_bridge_child_status(import_id, "blocked", error="当前ExampleBrand任务框已有相同链接")
            return
        if self.submission_locked:
            self._set_bridge_child_status(import_id, "blocked", error="当前ExampleBrand任务框已接收；请先新建任务箱载入下一批")
            return
        if self.rows:
            self._set_bridge_child_status(import_id, "blocked", error="当前ExampleBrand任务框非空，请先处理已有任务")
            return
        self.rows = [
            {
                "product": product,
                "expectedCount": int(item.get("expectedCount") or len(item.get("urls") or [])),
                "urls": [str(url).strip() for url in item.get("urls") or [] if str(url).strip()],
            }
            for product, item in zip(incoming_products, incoming)
        ]
        self.batch_id = str(payload.get("batchId") or self.batch_id).strip() or self.batch_id
        self.run_group_var.set(run_group_id)
        self.submission_locked = False
        self.submission_state = "draft"
        self.active_payload = copy.deepcopy(payload)
        self.last_submission_receipt = None
        self.refresh_task_box()
        self._save_task_box()
        self.status_var.set(f"脚本导入已加载：ExampleBrand {len(self.rows)} 个产品行；请检查21条合同后手动提交。")
        self._set_bridge_child_status(import_id, "loaded", loadedTaskCount=len(incoming), loadedAt=datetime.now().astimezone().isoformat(timespec="seconds"))

    def _current_count(self) -> int:
        try:
            return max(1, int(self.expected_count_var.get() or 0))
        except (TypeError, ValueError):
            return 0

    def _refresh_current_link_status(self):
        count = self._current_count()
        urls = parse_links(self.links.get("1.0", "end")) if hasattr(self, "links") else []
        self.current_link_status_var.set(f"有效链接 {len(urls)} / 计划 {count or '--'}")

    def _rows_payload(self) -> list[dict]:
        return [
            {"product": row["product"], "expectedCount": int(row["expectedCount"]), "urls": list(row["urls"])}
            for row in self.rows
        ]

    def _archive_state(self, state: dict, reason: str) -> None:
        try:
            HISTORY_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            batch = str(state.get("batchId") or (state.get("payload") or {}).get("batchId") or "unknown")
            target = HISTORY_DIR / f"{stamp}-{reason}-{batch}.json"
            atomic_write_json(target, {"archivedAt": datetime.now().astimezone().isoformat(timespec="seconds"), "reason": reason, **dict(state)})
        except OSError:
            pass

    def _save_task_box(self) -> bool:
        payload = {
            "schemaVersion": 1,
            "savedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "runGroupId": self.run_group_var.get().strip(),
            "productionDate": self.date_var.get().strip(),
            "batchId": self.batch_id,
            "submitted": self.submission_locked,
            "submissionState": self.submission_state,
            "rows": self._rows_payload(),
        }
        if isinstance(self.active_payload, dict):
            payload["contract"] = copy.deepcopy(self.active_payload)
        if isinstance(self.last_submission_receipt, dict):
            payload["receipt"] = copy.deepcopy(self.last_submission_receipt)
        try:
            atomic_write_json(SAVED_TASK_BOX_FILE, payload)
            return True
        except OSError:
            self.status_var.set("任务框已在内存更新，但本地保存失败；请检查递交器目录权限。")
            return False

    def _persist_submission(self, payload: dict, *, state: str, operation_id: int, phase: str = "", error: str = "", receipt: dict | None = None) -> bool:
        record = {
            "schemaVersion": 2,
            "savedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "submissionState": state,
            "operationId": operation_id,
            "phase": phase,
            "error": error,
            "payloadHash": str(payload.get("payloadHash") or ""),
            "payload": copy.deepcopy(payload),
        }
        if receipt is not None:
            record["receipt"] = copy.deepcopy(receipt)
        try:
            atomic_write_json(SUBMISSION_FILE, record)
            return True
        except OSError:
            return False

    def _log_event(self, operation_id: int, phase: str, payload: dict | None = None, **extra) -> None:
        contract = payload or self.active_payload or {}
        append_json_log({
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "operationId": operation_id,
            "phase": phase,
            "batchId": str(contract.get("batchId") or self.batch_id),
            "taskIds": [str(item.get("taskId") or "") for item in contract.get("tasks") or [] if isinstance(item, dict)],
            "runGroupId": str(contract.get("runGroupId") or self.run_group_var.get().strip()),
            "payloadHash": str(contract.get("payloadHash") or ""),
            **extra,
        })

    def reload_saved_task_box(self):
        if self.cloud_pending:return messagebox.showinfo("待查询回执","先查询并核对原回执，不会创建新编号。",parent=self)
        try:
            payload = json.loads(SAVED_TASK_BOX_FILE.read_text(encoding="utf-8"))
            rows = payload.get("rows") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise ValueError("保存文件中没有任务行")
            restored = []
            for item in rows:
                if not isinstance(item, dict):
                    raise ValueError("保存任务行格式无效")
                restored.append({
                    "product": str(item.get("product") or "").strip(),
                    "expectedCount": int(item.get("expectedCount") or 0),
                    "urls": [str(url).strip() for url in item.get("urls") or [] if str(url).strip()],
                })
            issues = validate_examplebrand_rows(restored)
            if issues and not (len(issues) == 1 and "必须正好 21 条" in issues[0]):
                raise ValueError("；".join(issues))
            self.rows = restored
            saved_group = str(payload.get("runGroupId") or "").strip()
            if not self._run_group_preset and RUN_GROUP_PATTERN.fullmatch(saved_group):
                self.run_group_var.set(saved_group)
            saved_date = str(payload.get("productionDate") or "").strip()
            if saved_date:
                self.date_var.set(saved_date)
            self.batch_id = str(payload.get("batchId") or "").strip() or new_batch_id()
            self.active_payload = ensure_payload_hash(payload.get("contract")) if isinstance(payload.get("contract"), dict) else None
            self.last_submission_receipt = copy.deepcopy(payload.get("receipt")) if isinstance(payload.get("receipt"), dict) else None
            self.submission_locked = bool(payload.get("submitted"))
            self.submission_state = str(payload.get("submissionState") or ("accepted" if self.submission_locked else "draft"))
            self.status_var.set(
                f"已加载保存任务：{len(restored)}个产品行；"
                + ("该任务箱已接收并锁定；可新建下一批。" if self.submission_locked else "可继续编辑，尚未提交。")
            )
            self.refresh_task_box()
        except FileNotFoundError:
            messagebox.showinfo("没有已保存任务", "当前没有可加载的ExampleBrand任务框。", parent=self)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            messagebox.showerror("加载任务框失败", str(exc), parent=self)

    def _candidate_issues(self, rows: list[dict]) -> list[str]:
        return [issue for issue in validate_examplebrand_rows(rows) if "必须正好 21 条" not in issue]

    def add_current_task(self):
        if self.submission_locked:
            return messagebox.showinfo("任务箱已接收", "当前任务箱已接收并锁定，请点击“新建任务箱”录入下一批。", parent=self)
        if self.submit_in_progress or self.query_in_progress or self.submission_state == "receipt_pending":
            return messagebox.showinfo("提交状态待确认", "请先完成接收状态查询，再修改任务框。", parent=self)
        product = self.product_var.get().strip()
        count = self._current_count()
        urls = parse_links(self.links.get("1.0", "end"))
        if product not in ExampleBrand_PRODUCTS:
            return messagebox.showerror("产品无效", "请选择ExampleBrand支持的产品。", parent=self)
        if count < 1:
            return messagebox.showerror("数量无效", "计划数量必须是大于0的整数。", parent=self)
        if not urls or len(urls) != count:
            return messagebox.showwarning("链接数量不符", f"{product}需要 {count} 条链接，当前识别到 {len(urls)} 条。", parent=self)
        if any(row.get("product") == product for row in self.rows):
            return messagebox.showwarning("产品重复", "同一批次每种产品只能加入一行；请删除旧行后重新加入。", parent=self)
        candidate = self._rows_payload() + [{"product": product, "expectedCount": count, "urls": urls}]
        issues = self._candidate_issues(candidate)
        if issues:
            return messagebox.showerror("ExampleBrand任务行未通过校验", "\n".join(issues), parent=self)
        self.rows.append({"product": product, "expectedCount": count, "urls": urls})
        self.active_payload = None
        self.links.delete("1.0", "end")
        self._save_task_box()
        self.refresh_task_box()
        self.status_var.set(f"已加入任务框：{product} {count}条；可继续添加其他产品。")
        self.links.focus_set()

    def _selected_index(self) -> int | None:
        selected = self.task_tree.selection()
        if not selected:
            return None
        try:
            return int(self.task_tree.item(selected[0], "values")[0]) - 1
        except (TypeError, ValueError, IndexError):
            return None

    def remove_selected_task(self):
        if self.submission_locked or self.submit_in_progress or self.query_in_progress or self.submission_state == "receipt_pending":
            return
        index = self._selected_index()
        if index is None or not (0 <= index < len(self.rows)):
            return messagebox.showinfo("请选择任务", "请先在任务框中选中一项。", parent=self)
        removed = self.rows.pop(index)
        self.active_payload = None
        self._save_task_box()
        self.refresh_task_box()
        self.status_var.set(f"已从任务框移除：{removed['product']}。")

    def move_selected_task(self, direction: int):
        if self.submission_locked or self.submit_in_progress or self.query_in_progress or self.submission_state == "receipt_pending":
            return
        index = self._selected_index()
        if index is None:
            return messagebox.showinfo("请选择任务", "请先在任务框中选中一项。", parent=self)
        target = index + direction
        if not (0 <= target < len(self.rows)):
            return
        self.rows[index], self.rows[target] = self.rows[target], self.rows[index]
        self.active_payload = None
        self._save_task_box()
        self.refresh_task_box()
        self.task_tree.selection_set(self.task_tree.get_children()[target])

    def clear_task_box(self):
        if self.submission_locked or self.submit_in_progress or self.query_in_progress or self.submission_state == "receipt_pending" or not self.rows:
            return
        if not messagebox.askyesno("清空任务框", "只清空尚未提交的ExampleBrand任务框，不影响家里已保存任务。确定吗？", parent=self):
            return
        self.rows = []
        self.active_payload = None
        self.last_submission_receipt = None
        self.submission_state = "draft"
        self._save_task_box()
        self.refresh_task_box()
        self.status_var.set("ExampleBrand任务框已清空。")

    def new_task_box(self):
        if self.cloud_pending:return messagebox.showinfo("待查询回执","先查询并核对原回执，不会创建新编号。",parent=self)
        if self.submit_in_progress or self.query_in_progress:
            return messagebox.showinfo("操作进行中", "请等待当前提交或接收状态查询结束。", parent=self)
        if self.submission_state == "receipt_pending":
            return messagebox.showwarning("接收状态待确认", "请先点击“查询接收状态”，确认家里是否已收到当前任务。", parent=self)
        if self.rows and not self.submission_locked and not messagebox.askyesno("新建任务箱", "当前任务框尚未提交，确定新建并归档当前草稿吗？", parent=self):
            return
        if self.rows or self.active_payload or self.last_submission_receipt:
            self._archive_state({
                "runGroupId": self.run_group_var.get().strip(),
                "productionDate": self.date_var.get().strip(),
                "batchId": self.batch_id,
                "submitted": self.submission_locked,
                "submissionState": self.submission_state,
                "rows": self._rows_payload(),
                "contract": self.active_payload,
                "receipt": self.last_submission_receipt,
            }, "new-task-box")
        self.rows = []
        self.batch_id = new_batch_id()
        self.submission_locked = False
        self.submission_state = "draft"
        self.active_payload = None
        self.last_submission_receipt = None
        self._clear_entry_widgets()
        self._save_task_box()
        self.refresh_task_box()
        self.status_var.set("已新建ExampleBrand任务箱；请输入产品数量和链接。")

    def _clear_entry_widgets(self):
        if hasattr(self, "links"):
            self.links.delete("1.0", "end")
        self.expected_count_var.set("1")
        self.product_var.set(ExampleBrand_PRODUCTS[0])

    @staticmethod
    def _account_ranges(accounts: list[str]) -> str:
        unique = []
        for account in accounts:
            if account not in unique:
                unique.append(account)
        return "、".join(unique) if unique else "--"

    def _row_preview(self, row: dict, offset: int) -> tuple[str, str, int]:
        count = int(row.get("expectedCount") or 0)
        slots = list(ExampleBrand_ACCOUNT_SLOTS[offset: offset + count])
        english = sum(1 for account in slots if account.startswith("EN-"))
        spanish = sum(1 for account in slots if account == "ES-01")
        language = f"英语{english} / 西语{spanish}"
        return language, self._account_ranges(slots), offset + count

    def refresh_task_box(self):
        if self.cloud_pending:self.submission_locked=True
        for item in self.task_tree.get_children():
            self.task_tree.delete(item)
        total = 0
        offset = 0
        for sequence, row in enumerate(self.rows, 1):
            planned = int(row.get("expectedCount") or 0)
            filled = len(row.get("urls") or [])
            language, accounts, offset = self._row_preview(row, offset)
            total += planned
            tag = "complete" if planned == filled else "incomplete"
            self.task_tree.insert("", "end", values=(sequence, row.get("product"), language, accounts, filled, planned), tags=(tag,))
        self.task_tree.tag_configure("complete", foreground="#16794A")
        self.task_tree.tag_configure("incomplete", foreground="#B42318")
        self.total_var.set(f"计划 {total} / {ExampleBrand_TOTAL}")
        self.task_summary_var.set(f"{len(self.rows)}个产品行 / {total}条")
        editable = (
            not self.submission_locked
            and not self.submit_in_progress
            and not self.query_in_progress
            and self.submission_state != "receipt_pending"
        )
        for widget in (self.add_button, self.entry_add_button, self.remove_button, self.up_button, self.down_button, self.clear_button, self.preview_button):
            widget.configure(state="normal" if editable else "disabled")
        # A received task is read-only, but starting a new task box must remain available.
        self.new_button.configure(state="normal" if not self.submit_in_progress and not self.query_in_progress else "disabled")
        self.reload_button.configure(state="normal" if not self.submit_in_progress and not self.query_in_progress else "disabled")
        self.submit_button.configure(state="normal" if editable else "disabled")
        self.query_button.configure(
            state="normal" if self.active_payload and self.submission_state in {"receipt_pending", "accepted", "cloud_pending"} and not self.query_in_progress and not self.submit_in_progress else "disabled"
        )
        input_state = "normal" if editable else "disabled"
        for widget in (self.date_entry, self.receiver_entry, self.run_group_entry, self.product_box, self.count_box, self.links):
            try:
                widget.configure(state=input_state)
            except tk.TclError:
                pass

    def _build_payload(self) -> dict:
        run_group_id = self.run_group_var.get().strip()
        if not RUN_GROUP_PATTERN.fullmatch(run_group_id):
            raise ValueError("运行组ID格式无效")
        if isinstance(self.active_payload, dict):
            contract = self.active_payload
            if (
                str(contract.get("batchId") or "") == self.batch_id
                and str(contract.get("runGroupId") or "") == run_group_id
                and str(contract.get("productionDate") or "") == self.date_var.get().strip()
                and business_fingerprint(payload=contract) == business_fingerprint(rows=self.rows, production_date=self.date_var.get().strip())
            ):
                return ensure_payload_hash(contract)
        return ensure_payload_hash(build_examplebrand_batch(
            self._rows_payload(),
            production_date=self.date_var.get().strip(),
            batch_id=self.batch_id,
            start_mode="pending_confirmation",
            run_group_id=run_group_id,
        ))

    def _preview(self):
        try:
            payload = self._build_payload()
        except ValueError as exc:
            self.status_var.set(str(exc))
            messagebox.showerror("ExampleBrand合同未通过", str(exc), parent=self)
            return
        rows = []
        offset = 0
        for index, row in enumerate(payload.get("tasks") or [], 1):
            language, accounts, offset = self._row_preview(row, offset)
            rows.append(f"{index}. {row.get('product')}：{row.get('expectedCount')}条，{language}，{accounts}")
        summary = payload["examplebrandSummary"]
        self.status_var.set(f"校验通过：21条，英语{summary['english']}，西语{summary['spanish']}，每个账号3条；尚未提交。")
        messagebox.showinfo("ExampleBrand提交前完整对账", "\n".join(rows) + "\n\n合同校验通过，尚未提交。", parent=self)

    def _submit(self):
        if self.cloud_pending:return self._query_receipt()
        if self.submit_in_progress or self.submission_locked or self.query_in_progress:
            return
        try:
            payload = self._build_payload()
        except ValueError as exc:
            self.status_var.set(str(exc))
            messagebox.showerror("ExampleBrand合同未通过", str(exc), parent=self)
            return
        receiver = self.receiver_var.get().strip()
        if receiver.startswith("https://"):
            return self._submit_cloud(receiver,payload)
        token = self.access_token
        if not receiver:
            return messagebox.showerror("接收器地址为空", "请填写家里接收器地址。", parent=self)
        config_problem = connection_error({"receiverUrl": receiver, "accessToken": token})
        if config_problem:
            return messagebox.showerror("接收器配置缺失", config_problem, parent=self)
        self.active_payload = copy.deepcopy(payload)
        self.current_operation_id += 1
        operation_id = self.current_operation_id
        self.submit_in_progress = True
        self.submission_state = "submitting"
        self._save_task_box()
        self._persist_submission(payload, state="submitting", operation_id=operation_id, phase="before-validate")
        self._log_event(operation_id, "before-validate", payload)
        self.status_var.set("正在向家里接收器提交合同；窗口仍可关闭，不会启动工作台。")
        self.refresh_task_box()

        def worker():
            started = time.monotonic()
            deadline = started + SUBMISSION_DEADLINE_SECONDS
            try:
                validate_timeout = min(8, max(1, int(deadline - time.monotonic())))
                validate_remote(receiver, payload, timeout=validate_timeout, token=token)
                self._log_event(operation_id, "validated", payload, elapsedMs=int((time.monotonic() - started) * 1000))
                remaining = int(deadline - time.monotonic())
                if remaining <= 0:
                    raise SubmissionUncertain("提交总时限已到，无法确认接收结果")
                result = post_json(receiver, payload, timeout=min(12, remaining), token=token)
                expected_task_ids = {str(item.get("taskId") or "") for item in payload.get("tasks") or []}
                response_task_ids = {str(item) for item in result.get("taskIds") or []}
                # A successful HTTP response without the current identity is
                # not proof of receipt. Leave it queryable instead of allowing
                # a second submission with a different contract.
                if not str(result.get("batchId") or "") or not response_task_ids:
                    raise SubmissionUncertain("鎺ユ敹鍥炴墽缂哄皯褰撳墠 batchId 鎴栦浠诲姟ID")
                if result.get("batchId") not in {None, "", payload.get("batchId")}:
                    raise SubmissionRejected("接收回执的batchId与当前合同不一致")
                if response_task_ids and not expected_task_ids.issubset(response_task_ids):
                    raise SubmissionRejected("接收回执缺少当前任务ID")
                outcome = ("accepted", payload, result, "")
                self._log_event(operation_id, "received", payload, elapsedMs=int((time.monotonic() - started) * 1000), duplicate=bool(result.get("duplicate")))
            except SubmissionRejected as exc:
                outcome = ("rejected", payload, None, str(exc))
                self._log_event(operation_id, "rejected", payload, error=str(exc), elapsedMs=int((time.monotonic() - started) * 1000))
            except SubmissionUncertain as exc:
                outcome = ("uncertain", payload, None, str(exc))
                self._log_event(operation_id, "receipt-pending", payload, error=str(exc), elapsedMs=int((time.monotonic() - started) * 1000))
            except Exception as exc:
                outcome = ("uncertain", payload, None, f"后台线程异常：{exc}")
                self._log_event(operation_id, "receipt-pending", payload, error=str(exc), elapsedMs=int((time.monotonic() - started) * 1000))
            try:
                self.after(0, lambda: self._finish_submit(operation_id, outcome))
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True, name="examplebrand-submit").start()

    def _finish_submit(self, operation_id: int, outcome):
        if self._closing:
            return
        if operation_id != self.current_operation_id:
            return
        kind, payload, result, error = outcome
        self.submit_in_progress = False
        if kind == "rejected":
            self.submission_state = "rejected"
            self.submission_locked = False
            self._persist_submission(payload, state="rejected", operation_id=operation_id, phase="complete", error=error)
            self._save_task_box()
            self.status_var.set(f"接收器明确拒绝：{error}")
            self.refresh_task_box()
            messagebox.showerror("ExampleBrand提交被拒绝", error, parent=self)
            return
        if kind == "uncertain":
            self.submission_state = "receipt_pending"
            self.submission_locked = False
            persisted = self._persist_submission(payload, state="receipt_pending", operation_id=operation_id, phase="complete", error=error)
            self._save_task_box()
            suffix = "" if persisted else " 本地状态保存异常。"
            self.status_var.set(f"接收状态待确认：请求可能已被家里接收，请点击“查询接收状态”。{suffix}")
            self.refresh_task_box()
            messagebox.showwarning("接收状态待确认", "请求结果暂时无法确认，家里可能已经接收。\n请先查询接收状态，不要重复提交。", parent=self)
            return
        self.submission_state = "accepted"
        self.submission_locked = True
        self.last_submission_receipt = dict(result or {})
        self.last_submission_receipt["verifiedBatchId"] = payload.get("batchId")
        self.last_submission_receipt["verifiedRunGroupId"] = payload.get("runGroupId")
        persisted = self._persist_submission(payload, state="accepted", operation_id=operation_id, phase="complete", receipt=self.last_submission_receipt)
        saved = self._save_task_box()
        if not persisted or not saved:
            self.status_var.set("家里已接收，本地回执保存异常；当前任务已锁定，请保留窗口并导出备份。")
        else:
            self.status_var.set(f"已接收、等待启动：{payload['batchId']}；任务框已锁定，可新建下一批。")
        self.refresh_task_box()
        messagebox.showinfo("ExampleBrand任务箱已接收", "家里接收器已保存21条任务，状态为“已接收、等待启动”。\n本程序不会自动启动生产；点击“新建任务箱”可录入下一批。", parent=self)

    def _query_receipt(self):
        if self.cloud_pending:
            if self.cloud_pending.get("invalid"):return
            return self._submit_cloud(self.cloud_pending["origin"],self.cloud_pending["payload"],query_only=True)
        if self.receiver_var.get().startswith("https://"):
            return messagebox.showinfo("历史回执","没有云端待查询编号；已接收任务请在云端网页登录查看，不会重新递交。",parent=self)
        if self.query_in_progress or self.submit_in_progress or not isinstance(self.active_payload, dict):
            return
        receiver = self.receiver_var.get().strip()
        token = self.access_token
        if not receiver:
            self.status_var.set("接收状态待确认：接收器地址为空，无法查询。")
            return
        self.current_operation_id += 1
        operation_id = self.current_operation_id
        payload = copy.deepcopy(self.active_payload)
        self.query_in_progress = True
        self.submission_state = "querying"
        self._log_event(operation_id, "query-start", payload)
        self.status_var.set("正在查询家里接收器状态；不会重新提交或启动任务。")
        self.refresh_task_box()

        def worker():
            started = time.monotonic()
            try:
                result = query_remote(receiver, payload, timeout=QUERY_DEADLINE_SECONDS, token=token)
                outcome = ("matched", result, "") if result.get("matched") else ("unknown", result, "活动任务列表未命中当前身份")
                self._log_event(operation_id, "query-complete", payload, matched=bool(result.get("matched")), elapsedMs=int((time.monotonic() - started) * 1000))
            except SubmissionUncertain as exc:
                outcome = ("unknown", None, str(exc))
                self._log_event(operation_id, "query-unknown", payload, error=str(exc), elapsedMs=int((time.monotonic() - started) * 1000))
            except Exception as exc:
                outcome = ("unknown", None, f"后台查询异常：{exc}")
                self._log_event(operation_id, "query-unknown", payload, error=str(exc), elapsedMs=int((time.monotonic() - started) * 1000))
            try:
                self.after(0, lambda: self._finish_query(operation_id, payload, outcome))
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True, name="examplebrand-receipt-query").start()

    def _finish_query(self, operation_id: int, payload: dict, outcome):
        if self._closing or operation_id != self.current_operation_id:
            return
        self.query_in_progress = False
        kind, result, error = outcome
        if kind == "matched":
            self.submission_state = "accepted"
            self.submission_locked = True
            self.last_submission_receipt = {
                "ok": True,
                "source": "active-task-query",
                "queriedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                "batchId": payload.get("batchId"),
                "runGroupId": payload.get("runGroupId"),
                "payloadHash": payload.get("payloadHash"),
                "taskIds": [str(item.get("taskId") or "") for item in payload.get("tasks") or []],
                "matches": result.get("items") or [],
            }
            persisted = self._persist_submission(payload, state="accepted", operation_id=operation_id, phase="query-matched", receipt=self.last_submission_receipt)
            saved = self._save_task_box()
            self.status_var.set(
                "已接收、等待启动：查询已找到同一任务身份。"
                if persisted and saved else
                "家里已接收，本地回执保存异常；当前任务已锁定。"
            )
        else:
            self.submission_state = "receipt_pending"
            self.submission_locked = False
            self._persist_submission(payload, state="receipt_pending", operation_id=operation_id, phase="query-unknown", error=error)
            self._save_task_box()
            self.status_var.set(f"暂时无法确认接收状态：{error or '家里活动列表未命中当前身份'}；未自动重试。")
        self.refresh_task_box()

    def _restore_cloud_pending(self):
        try:
            pending=load_pending(CLOUD_PENDING_FILE)
        except (OSError,ValueError):
            self.cloud_pending={"invalid":True};self.submission_locked=True
            self.status_var.set("待查询回执无法读取；保留文件人工核对，禁止新建编号。")
            return
        if not pending:return
        self.cloud_pending=pending;payload=pending["payload"]
        self.active_payload=copy.deepcopy(payload);self.batch_id=payload["batchId"]
        self.run_group_var.set(payload["runGroupId"])
        self.date_var.set(payload["productionDate"])
        self.rows=copy.deepcopy(payload["tasks"])
        self.submission_state="cloud_pending";self.submission_locked=True
        self.refresh_task_box()
        self.status_var.set("已恢复原任务身份；仅查询不会提交；需首次递交请用“继续未提交任务”。")

    def _resume_cloud_submission(self):
        if (self.submit_in_progress or self.query_in_progress) or not self.cloud_pending or self.cloud_pending.get("invalid"):return
        if not messagebox.askyesno("继续未提交任务","这会使用已保存的原输入与编号，检查健康并首次递交尚未提交的阶段；已尝试的阶段只查询，结果不明不会重发。是否继续？",parent=self):return
        self._submit_cloud(self.cloud_pending["origin"],self.cloud_pending["payload"])

    def _submit_cloud(self,receiver,payload,query_only=False):
        if self.submit_in_progress or self.query_in_progress:return
        if self.cloud_pending and self.cloud_pending.get("invalid"):return
        payload=copy.deepcopy(payload)
        try:
            transport=CloudSubmission(receiver,CLOUD_PENDING_FILE)
            self.cloud_pending=load_pending(CLOUD_PENDING_FILE) if query_only else transport.prepare(payload)
            if not self.cloud_pending:raise PendingSubmission("没有已保存的递交记录；仅查询不会新建或提交。")
        except (OSError,ValueError,PendingSubmission) as exc:
            return messagebox.showerror("云端递交未开始",str(exc),parent=self)
        self.active_payload=payload;self.submission_locked=True;self.submit_in_progress=True
        self.submission_state="cloud_pending";self.current_operation_id+=1
        operation=self.current_operation_id;self.refresh_task_box()
        def worker():
            try:
                result=transport.run(payload,query_only=query_only,notify=lambda phase:self.after(0,lambda phase=phase:self.status_var.set("云端状态："+phase+"；输入和原编号已保存")))
                expected={str(t["taskId"]) for t in payload["tasks"]}
                if result.get("batchId")!=payload["batchId"] or not expected.issubset({str(t) for t in result.get("taskIds",[])}):
                    raise PendingSubmission("真实回执身份缺失或不符，请人工核对，禁止重发")
            except (OSError,ValueError,PendingSubmission,CloudRejected,http.client.HTTPException) as exc:
                message=str(exc)
                self.after(0,lambda message=message:self._cloud_failed(operation,message))
            else:
                self.after(0,lambda:self._cloud_accepted(operation,payload,result,transport))
        threading.Thread(target=worker,daemon=True,name="examplebrand-https-submit").start()

    def _cloud_failed(self,operation,message):
        if self._closing or operation!=self.current_operation_id:return
        self.submit_in_progress=False;self.submission_state="cloud_pending"
        self.refresh_task_box()
        self.status_var.set(message+"；原输入与编号保留；仅查询不会提交，需首次递交请用“继续未提交任务”。")

    def _cloud_accepted(self,operation,payload,result,transport):
        if self._closing or operation!=self.current_operation_id:return
        # Durable, identity-checked real receipt before removing resumable state.
        receipt=dict(result,verifiedBatchId=payload["batchId"],verifiedRunGroupId=payload["runGroupId"])
        if not self._persist_submission(payload,state="accepted",operation_id=operation,phase="cloud-home-received",receipt=receipt):
            return self._cloud_failed(operation,"家里已接收，但本地回执保存失败；请保留原记录")
        self.submission_locked=True;self.submission_state="accepted";self.last_submission_receipt=receipt
        if not self._save_task_box():return self._cloud_failed(operation,"家里已接收，但任务箱保存失败")
        transport.complete();self.cloud_pending=None;self.submit_in_progress=False
        self.refresh_task_box();self.status_var.set("家里已接收、等待启动；不会自动启动生产。")

    def _release_cloud_receipt(self):
        if self.submit_in_progress or self.query_in_progress or not self.cloud_pending or self.cloud_pending.get("invalid"):return
        if not messagebox.askyesno("结束回执","先在云端撤销未发送请求或完成人工对账。确认查询已终结后结束原回执？",parent=self):return
        pending=copy.deepcopy(self.cloud_pending);self.query_in_progress=True
        def worker():
            try:CloudSubmission(pending["origin"],CLOUD_PENDING_FILE).release()
            except (OSError,ValueError,PendingSubmission,http.client.HTTPException) as exc:
                message=str(exc);self.after(0,lambda message=message:self._cloud_release_finished(message))
            else:self.after(0,lambda:self._cloud_release_finished(None))
        threading.Thread(target=worker,daemon=True,name="examplebrand-close-receipt").start()

    def _cloud_release_finished(self,error):
        if self._closing:return
        self.query_in_progress=False
        if not error:self.cloud_pending=None;self.submission_state="accepted";self.submission_locked=True
        self.refresh_task_box();self.status_var.set(error or "原回执已终结；下一批请显式点新建任务箱。")

    def _close(self):
        if self._closing:
            return
        self._closing = True
        if self._bridge_poll_job is not None:
            try:
                self.after_cancel(self._bridge_poll_job)
            except tk.TclError:
                pass
            self._bridge_poll_job = None
        # Closing this window never terminates another submitter or deletes the saved task box.
        self.destroy()


if __name__ == "__main__":
    from portable_selftest import run as self_test
    if self_test("examplebrand",None):
        raise SystemExit(0)
    if not acquire_instance_lock("AI-Video-Task-Submitter-ExampleBrand"):
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("递交器已在运行", "ExampleBrand递交器已经打开，请使用现有窗口。", parent=root)
        root.destroy()
        raise SystemExit(2)
    ExamplebrandSubmitter().mainloop()
