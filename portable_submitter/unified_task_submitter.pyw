from __future__ import annotations

import os
import re
import subprocess
import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
import uuid

from local_import_bridge import (
    BRIDGE_PORT,
    LocalImportBridge,
    read_import_status,
    write_import_status,
)
from submitter_instance_lock import acquire_instance_lock


RUN_GROUP_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}")


def new_run_group_id() -> str:
    return f"run-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"


def find_package_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "正式任务递交器").is_dir() and (candidate / "ExampleBrand自然流任务递交器").is_dir():
            return candidate
    return start


class UnifiedTaskSubmitter(tk.Tk):
    """A single entry point that preserves separate formal and ExampleBrand contracts."""

    def __init__(self):
        super().__init__()
        self.title("AI视频任务递交器 V14 · 统一入口")
        self.geometry("780x600")
        self.minsize(700, 520)
        self.configure(bg="#F5F7FA")
        self.package_root = find_package_root(Path(__file__).resolve().parent)
        self.run_group_var = tk.StringVar(value=new_run_group_id())
        self.status_var = tk.StringVar(value="请选择要打开的任务递交器。两类合同仍独立校验。")
        self.path_var = tk.StringVar(value=f"程序目录：{self.package_root}")
        self.bridge_status_var = tk.StringVar(value=f"本地导入桥：启动中（127.0.0.1:{BRIDGE_PORT}）")
        self.bridge_session_var = tk.StringVar(value="会话：启动中")
        self.bridge_pending_var = tk.StringVar(value="待导入：0")
        # Tk itself owns ``children`` for widget bookkeeping; keep process
        # handles under a distinct name so the periodic refresh never sees a
        # Frame as a Popen instance.
        self.child_processes: dict[str, subprocess.Popen] = {}
        self._closing = False
        self._refresh_job = None
        self._bridge_refresh_job = None
        self.bridge: LocalImportBridge | None = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        try:
            self.bridge = LocalImportBridge(self.run_group_var.get(), on_import=self._bridge_import_received)
            self.bridge.start()
            self.bridge_status_var.set(f"本地导入桥：运行中（127.0.0.1:{BRIDGE_PORT}）")
        except RuntimeError as exc:
            self.bridge_status_var.set(f"本地导入桥：端口冲突（{exc}）")
            self.status_var.set("统一递交器已打开，但脚本导入桥启动失败；请关闭占用18766端口的旧程序后重开。")
        self._refresh_job = self.after(1000, self._refresh_children)
        self._bridge_refresh_job = self.after(1000, self._refresh_bridge_status)

    def _build(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Title.TLabel", background="#FFFFFF", foreground="#172033", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Body.TLabel", background="#FFFFFF", foreground="#475467", font=("Microsoft YaHei UI", 10))
        style.configure("Hint.TLabel", background="#F7FAFF", foreground="#1D4ED8", font=("Microsoft YaHei UI", 10))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=9)

        card = ttk.Frame(self, padding=24)
        card.pack(fill="both", expand=True, padx=18, pady=18)
        ttk.Label(card, text="AI视频任务递交器 V14", style="Title.TLabel").pack(anchor="w")
        ttk.Label(card, text="统一入口 · 任意正式项目组合 + ExampleBrand自然流", style="Body.TLabel").pack(anchor="w", pady=(4, 18))

        group_box = ttk.LabelFrame(card, text="本次运行组", padding=12)
        group_box.pack(fill="x", pady=(0, 14))
        ttk.Entry(group_box, textvariable=self.run_group_var, width=48).pack(side="left", fill="x", expand=True)
        ttk.Button(group_box, text="复制ID", command=self._copy_run_group).pack(side="left", padx=(8, 0))
        ttk.Button(group_box, text="重新生成", command=self._regenerate_run_group).pack(side="left", padx=(8, 0))
        ttk.Label(
            group_box,
            text="同一次组合运行请保持同一ID；只运行ExampleBrand也可直接使用此ID。",
            style="Body.TLabel",
        ).pack(anchor="w", pady=(8, 0))

        ttk.Label(card, text="选择递交入口", style="Hint.TLabel").pack(anchor="w", pady=(0, 8))
        actions = ttk.Frame(card)
        actions.pack(fill="x")
        ttk.Button(actions, text="打开正式项目递交器", style="Primary.TButton", command=lambda: self._launch("formal")).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(actions, text="打开 ExampleBrand 递交器", style="Primary.TButton", command=lambda: self._launch("examplebrand")).pack(side="left", fill="x", expand=True, padx=(6, 0))

        bridge_box = ttk.LabelFrame(card, text="抖音脚本本地导入桥", padding=10)
        bridge_box.pack(fill="x", pady=(14, 0))
        ttk.Label(bridge_box, textvariable=self.bridge_status_var, style="Hint.TLabel").pack(anchor="w")
        ttk.Label(bridge_box, textvariable=self.bridge_session_var, style="Body.TLabel").pack(anchor="w", pady=(3, 0))
        ttk.Label(bridge_box, textvariable=self.bridge_pending_var, style="Body.TLabel").pack(anchor="w", pady=(3, 0))
        ttk.Label(
            bridge_box,
            text="抖音脚本请选择“传入统一递交器”。链接只保存在本机导入队列，仍需你在子窗口检查并点击提交。",
            style="Body.TLabel",
            wraplength=700,
        ).pack(anchor="w", pady=(5, 0))

        ttk.Label(
            card,
            text=(
                "使用方式：先在本窗口确认运行组ID，再打开需要的递交器。\n"
                "正式项目可任意勾选，但每个项目仍必须使用固定合同数量；ExampleBrand仍是独立21条合同。\n"
                "抖音脚本通过本地18766导入桥填入任务框；两个入口会自动收到同一个运行组ID，接收器和工作台会按子批次顺序执行。"
            ),
            style="Body.TLabel",
            justify="left",
        ).pack(anchor="w", pady=(18, 12))

        ttk.Label(card, textvariable=self.status_var, style="Hint.TLabel", wraplength=680).pack(anchor="w", fill="x", pady=(4, 8))
        ttk.Label(card, textvariable=self.path_var, style="Body.TLabel", wraplength=680).pack(anchor="w")

    def _valid_run_group(self) -> str:
        value = self.run_group_var.get().strip()
        if not RUN_GROUP_PATTERN.fullmatch(value):
            raise ValueError("运行组ID格式无效，只能使用字母、数字、点、下划线或短横线。")
        return value

    def _copy_run_group(self):
        try:
            value = self._valid_run_group()
            self.clipboard_clear()
            self.clipboard_append(value)
            self.update_idletasks()
            self.status_var.set(f"已复制运行组ID：{value}")
        except ValueError as exc:
            messagebox.showerror("运行组ID无效", str(exc), parent=self)

    def _regenerate_run_group(self):
        active = [kind for kind, process in self.child_processes.items() if process.poll() is None]
        if active:
            messagebox.showwarning("不能更换运行组", "子递交器仍在运行：" + "、".join(active) + "。请关闭后再重新生成。", parent=self)
            return
        self.run_group_var.set(new_run_group_id())
        if self.bridge is not None:
            self.bridge.set_run_group(self.run_group_var.get().strip())
        self.status_var.set("已生成新的运行组ID；尚未提交任何任务。")

    def _bridge_import_received(self, import_id: str, envelope: dict) -> None:
        """Called by the HTTP thread; marshal all Tk work back to the UI loop."""
        if self._closing:
            return
        try:
            self.after(0, lambda: self._handle_bridge_import(import_id, envelope))
        except tk.TclError:
            return

    def _handle_bridge_import(self, import_id: str, envelope: dict) -> None:
        if self._closing:
            return
        payloads = envelope.get("payloads") if isinstance(envelope, dict) else []
        kinds = [
            str(payload.get("batchKind") or "").strip()
            for payload in payloads
            if isinstance(payload, dict) and str(payload.get("batchKind") or "").strip() in {"formal", "examplebrand-natural-flow"}
        ]
        status = read_import_status(import_id)
        child_status = dict(status.get("childStatus") or {})
        launched = []
        for kind in kinds:
            child_kind = "formal" if kind == "formal" else "examplebrand"
            if self._launch(child_kind, imported=True):
                launched.append(child_kind)
                child_status[kind] = {**dict(child_status.get(kind) or {}), "state": "launcher_opened"}
        write_import_status(import_id, {
            "state": "persisted",
            "childStatus": child_status,
            "launcherOpened": launched,
        })
        label = "、".join("正式项目" if item == "formal" else "ExampleBrand" for item in launched) or "对应"
        self.status_var.set(f"脚本导入已保存：{import_id}；已打开{label}递交器，任务框会自动加载。")

    def _refresh_bridge_status(self):
        if self._closing:
            return
        if self.bridge is not None:
            try:
                health = self.bridge.health()
                self.bridge_status_var.set(
                    f"本地导入桥：{'运行中' if health.get('listening') else '未监听'}（127.0.0.1:{BRIDGE_PORT}）"
                )
                self.bridge_session_var.set(
                    f"会话：{health.get('sessionId') or '--'} · 运行组：{health.get('runGroupId') or '--'}"
                )
                self.bridge_pending_var.set(f"待导入：{health.get('pendingImports', 0)}")
            except Exception as exc:
                self.bridge_status_var.set(f"本地导入桥：状态读取失败（{exc}）")
        try:
            self._bridge_refresh_job = self.after(1500, self._refresh_bridge_status)
        except tk.TclError:
            self._bridge_refresh_job = None

    def _child_paths(self, kind: str) -> tuple[Path, Path]:
        if kind == "formal":
            folder = self.package_root / "正式任务递交器"
            return (
                folder / "程序" / "AI-Video-Task-Submitter" / "AI-Video-Task-Submitter.exe",
                folder / "company_task_submitter.pyw",
            )
        folder = self.package_root / "ExampleBrand自然流任务递交器"
        return (
            folder / "程序" / "ExampleBrand-Natural-Flow-Task-Submitter" / "ExampleBrand-Natural-Flow-Task-Submitter.exe",
            folder / "ExampleBrand自然流任务投递器.pyw",
        )

    def _launch(self, kind: str, imported: bool = False) -> bool:
        try:
            run_group = self._valid_run_group()
        except ValueError as exc:
            messagebox.showerror("运行组ID无效", str(exc), parent=self)
            return False
        old = self.child_processes.get(kind)
        if old is not None and old.poll() is None:
            self.status_var.set(f"{kind}递交器已经打开，本次不重复启动。")
            return False

        exe_path, script_path = self._child_paths(kind)
        env = os.environ.copy()
        env["AI_VIDEO_RUN_GROUP_ID"] = run_group
        env["AI_VIDEO_UNIFIED_ENTRY"] = "1"
        try:
            if exe_path.is_file():
                process = subprocess.Popen([str(exe_path)], cwd=str(exe_path.parent), env=env)
            elif script_path.is_file():
                python_exe = Path(sys.executable)
                if python_exe.name.lower() == "python.exe":
                    pythonw = python_exe.with_name("pythonw.exe")
                    if pythonw.is_file():
                        python_exe = pythonw
                process = subprocess.Popen([str(python_exe), str(script_path)], cwd=str(script_path.parent), env=env)
            else:
                raise FileNotFoundError(f"找不到{kind}递交器程序或源码")
        except OSError as exc:
            messagebox.showerror("启动失败", f"无法启动{kind}递交器：{exc}", parent=self)
            return False
        self.child_processes[kind] = process
        self.status_var.set(
            f"已打开{kind}递交器；运行组ID已绑定为 {run_group}。"
            + (" 正在加载脚本导入任务。" if imported else "")
        )
        return True

    def _refresh_children(self):
        if self._closing:
            return
        ended = [kind for kind, process in self.child_processes.items() if process.poll() is not None]
        for kind in ended:
            self.child_processes.pop(kind, None)
        if self.winfo_exists():
            self._refresh_job = self.after(1000, self._refresh_children)

    def _close(self):
        if self._closing:
            return
        self._closing = True
        if self._refresh_job is not None:
            try:
                self.after_cancel(self._refresh_job)
            except tk.TclError:
                pass
            self._refresh_job = None
        if self._bridge_refresh_job is not None:
            try:
                self.after_cancel(self._bridge_refresh_job)
            except tk.TclError:
                pass
            self._bridge_refresh_job = None
        if self.bridge is not None:
            self.bridge.stop()
            self.bridge = None
        # Only close this launcher.  Formal/ExampleBrand child windows keep running.
        self.destroy()


if __name__ == "__main__":
    from portable_selftest import run as self_test
    if self_test("unified",find_package_root(Path(__file__).resolve().parent)):
        raise SystemExit(0)
    # The browser userscript has one fixed loopback endpoint.  Two launchers
    # from different package copies must therefore never run concurrently:
    # otherwise the older process can own port 18766 while the newer UI shows
    # a different run group.  Use one machine-wide mutex for every V14
    # unified entry so a second launch exits before it can create that split
    # identity.
    if not acquire_instance_lock("AI-Video-Task-Submitter-Unified"):
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(
            "统一入口已在运行",
            "已有 V14 统一任务递交器正在运行。请使用现有窗口；如需切换版本，请先关闭现有统一入口后再启动。",
            parent=root,
        )
        root.destroy()
        raise SystemExit(2)
    UnifiedTaskSubmitter().mainloop()
