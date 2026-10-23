from __future__ import annotations

import importlib.util
import json
import os
import queue
import re
import sys
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from common_io import pid_is_alive
from task_contract import expected_count as contract_expected_count, language_plan as contract_language_plan


class StitchContractValidationError(RuntimeError):
    """A recoverable Step-4 contract failure with machine-readable details."""

    def __init__(self, message, *, error_code, missing_languages=None, expected=None, actual=None):
        super().__init__(message)
        self.error_code = str(error_code)
        self.missing_languages = list(missing_languages or [])
        self.expected = expected
        self.actual = actual


class EmbeddedWindowHost(tk.Frame):
    """A Frame that supplies the small window API expected by the legacy app."""

    def __init__(self, master):
        super().__init__(master, bg="#F5F7FA", bd=0, highlightthickness=0)
        self._close_callback = None

    def title(self, _value=None):
        return ""

    def geometry(self, _value=None):
        return ""

    def minsize(self, _width=None, _height=None):
        return None

    def protocol(self, name=None, callback=None):
        if name == "WM_DELETE_WINDOW" and callback is not None:
            self._close_callback = callback
        return None


class EmbeddedStitcherController:
    def __init__(self, parent, source_path: Path):
        self.parent = parent
        self.source_path = Path(source_path)
        self.shell = ttk.Frame(parent)
        self.shell.pack(fill="both", expand=True)
        self.shell.rowconfigure(0, weight=1)
        self.shell.columnconfigure(0, weight=1)
        self.page_canvas = tk.Canvas(self.shell, bg="#F5F7FA", highlightthickness=0, borderwidth=0)
        self.page_scrollbar = ttk.Scrollbar(self.shell, orient="vertical", command=self.page_canvas.yview)
        self.page_canvas.configure(yscrollcommand=self.page_scrollbar.set)
        self.page_canvas.grid(row=0, column=0, sticky="nsew")
        self.page_scrollbar.grid(row=0, column=1, sticky="ns")
        self.host = EmbeddedWindowHost(self.page_canvas)
        self.host_window = self.page_canvas.create_window((0, 0), window=self.host, anchor="nw")
        self.host.bind("<Configure>", self._update_scroll_region)
        self.page_canvas.bind("<Configure>", self._resize_embedded_width)
        self.page_canvas.bind("<MouseWheel>", self._scroll_page)
        self.module = self._load_module()
        self.app = self.module.BoardVideoStitcherApp(self.host)
        self._records_callback = None
        self._records_queue = queue.Queue()
        self._task_contract = {}
        self._cover_assignment_ledger_path = None
        self._stitch_lock_path = None
        self._install_records_hook()
        self.parent.after(500, self._poll_records)
        self.host.bind("<MouseWheel>", self._scroll_page, add="+")
        for widget in self._walk(self.host):
            if not isinstance(widget, tk.Canvas):
                widget.bind("<MouseWheel>", self._scroll_page, add="+")
        self.host.update_idletasks()
        self._update_scroll_region()

    def _install_records_hook(self):
        original = self.app.write_records

        def write_records_with_handoff(records):
            if self._task_contract:
                owner = str(
                    self._task_contract.get("canonicalTaskId")
                    or self._task_contract.get("taskId")
                    or ""
                )
                expected = contract_expected_count(self._task_contract)
                records = [
                    {
                        **record,
                        "remotePipelineTaskId": owner,
                        "expectedTaskCount": expected,
                        "materialExpectedCount": expected,
                        "contractVersion": int(self._task_contract.get("contractVersion") or 2),
                        "productionDate": str(
                            self._task_contract.get("productionDate")
                            or self._task_contract.get("date")
                            or ""
                        ),
                        "coverTemplateMode": str(
                            self._task_contract.get("coverTemplateMode") or "classic_a"
                        ),
                        "coverTemplateId": str(
                            (record.get("cover") or {}).get("template_id") or "classic_a"
                        ),
                        "coverTemplateName": str(
                            (record.get("cover") or {}).get("template_name") or "经典金色"
                        ),
                        "coverTemplateSeed": str(
                            (record.get("cover") or {}).get("stable_seed") or ""
                        ),
                    }
                    for record in records
                ]
            original(records)
            if records:
                output_dir = Path(self.app.output_dir.get().strip() or ".")
                record_files = sorted(
                    output_dir.glob("generation_records_*.json"),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
                self._records_queue.put(
                    (
                        self.app.active_project_name,
                        list(records),
                        record_files[0] if record_files else None,
                    )
                )

        self.app.write_records = write_records_with_handoff

    def set_records_callback(self, callback):
        self._records_callback = callback

    def _poll_records(self):
        try:
            while True:
                project, records, path = self._records_queue.get_nowait()
                self._release_stitch_lock()
                if self._records_callback:
                    self._records_callback(project, records, path)
        except queue.Empty:
            pass
        if self.parent.winfo_exists():
            self.parent.after(500, self._poll_records)

    def _stitch_lock_file(self):
        owner = str(
            self._task_contract.get("canonicalTaskId")
            or self._task_contract.get("taskId")
            or ""
        ).strip()
        if not owner:
            return None
        lock_root = self.source_path.parent.parent / "workbench" / "handoff" / "stitch_locks"
        lock_root.mkdir(parents=True, exist_ok=True)
        return lock_root / f"{owner}.lock"

    def _acquire_stitch_lock(self):
        path = self._stitch_lock_file()
        if path is None:
            return
        payload = {
            "pid": os.getpid(),
            "processStartedAt": self._process_started_at(os.getpid()),
            "processCommand": " ".join(str(value) for value in sys.argv),
            "createdAt": datetime.now().isoformat(timespec="seconds"),
            "taskId": str(self._task_contract.get("canonicalTaskId") or self._task_contract.get("taskId") or ""),
        }
        try:
            with path.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
        except FileExistsError as exc:
            if not self._recoverable_stitch_lock(path):
                raise RuntimeError("该远程任务已有另一个工作台正在拼接，已阻止重复启动。") from exc
            try:
                path.unlink(missing_ok=True)
                with path.open("x", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False)
            except OSError as retry_exc:
                raise RuntimeError("无法回收已失效的拼接锁；为保护任务，未启动重复拼接。") from retry_exc
        self._stitch_lock_path = path

    @staticmethod
    def _pid_is_alive(pid):
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        return pid_is_alive(pid)

    @staticmethod
    def _process_started_at(pid):
        try:
            import psutil

            return round(float(psutil.Process(int(pid)).create_time()), 3)
        except Exception:
            return None

    def _recoverable_stitch_lock(self, path: Path):
        """Reclaim only a lock that cannot belong to a running stitcher."""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return True
        owner_pid = payload.get("pid") if isinstance(payload, dict) else None
        if not self._pid_is_alive(owner_pid):
            return True
        owner_pid = int(owner_pid or 0)
        if owner_pid == os.getpid():
            return not self.is_running()

        # A stale lock can outlive its workbench, and Windows may reuse that
        # numeric PID for an unrelated process.  A live PID alone therefore
        # is not proof that another workbench owns this task.
        try:
            import psutil

            process = psutil.Process(owner_pid)
            recorded_start = payload.get("processStartedAt")
            if recorded_start is not None:
                actual_start = float(process.create_time())
                if abs(actual_start - float(recorded_start)) > 2.0:
                    return True
            command = " ".join(process.cmdline()).casefold()
            if command and "workbench.py" not in command:
                return True
        except psutil.NoSuchProcess:
            return True
        except (psutil.AccessDenied, ValueError, TypeError, OSError):
            # When identity cannot be proven, retain the safety block.
            return False
        return False

    def _release_stitch_lock(self):
        path = self._stitch_lock_path
        self._stitch_lock_path = None
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _update_scroll_region(self, _event=None):
        self.page_canvas.configure(scrollregion=self.page_canvas.bbox("all"))

    def _resize_embedded_width(self, event):
        self.page_canvas.itemconfigure(self.host_window, width=max(event.width, 1180))
        self._update_scroll_region()

    def _scroll_page(self, event):
        self.page_canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _load_module(self):
        if not self.source_path.is_file():
            raise FileNotFoundError(f"找不到拼接工具源码：{self.source_path}")
        source_dir = str(self.source_path.parent)
        if source_dir not in sys.path:
            sys.path.insert(0, source_dir)
        spec = importlib.util.spec_from_file_location("workbench_board_video_stitcher", self.source_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载拼接工具源码：{self.source_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def apply_handoff(self, subject_dir: Path, source_files=None):
        subject_dir = Path(subject_dir)
        if not subject_dir.is_dir():
            raise FileNotFoundError(f"主体素材目录不存在：{subject_dir}")
        whitelist = []
        if source_files is not None:
            for value in source_files:
                path = Path(value)
                if not path.is_file():
                    raise FileNotFoundError(f"任务指定的主体视频不存在：{path}")
                try:
                    path.resolve().relative_to(subject_dir.resolve())
                except ValueError as exc:
                    raise ValueError(f"任务指定的视频不属于当前主体目录：{path}") from exc
                whitelist.append(str(path.resolve()))
            if not whitelist:
                raise ValueError("任务素材清单为空，不能用目录扫描猜测主体视频。")
        if source_files is None:
            self.app.segment_file_whitelists = {}
            self.app.segment_file_whitelist_required = False
        else:
            old_segment = self.module.STANDARD_SEGMENT_NAMES[1]
            self.app.segment_file_whitelists = {
                old_segment: tuple(whitelist)
            }
            self.app.segment_file_whitelist_required = True
        self.app.old_folder_var.set(str(subject_dir))
        self.app.scan_segments()
        # This is a per-task runtime handoff.  Persisting it would turn the
        # next remote task into a stale source-path fallback, especially for
        # non-AI materials such as PU stone and mirror panel.

    def refresh_audio(self):
        self.app.refresh_audio_batches(scan_tasks=True, log_result=False)

    def apply_audio_handoff(self, audio_dir: Path, production_date: str = ""):
        """Point the embedded stitcher at the current task's real audio folder.

        Saved stitcher projects may still use a legacy material alias (for
        example ``木纹``), while the voice handoff writes to the canonical
        material folder (``木饰面``).  The remote task directory is
        authoritative and must be applied before the legacy audio rescan.
        """
        folder = Path(audio_dir)
        if not folder.is_dir():
            raise FileNotFoundError(f"任务音频目录不存在：{folder}")
        batch_name = str(production_date or "").strip()
        selected_batch = batch_name if batch_name and folder.name.casefold() == batch_name.casefold() else ""
        material_root = folder.parent if selected_batch else folder

        self.app.audio_material_root_var.set(str(material_root))
        self.app.audio_task_material_root = str(material_root)
        self.app.audio_batch_var.set(selected_batch)
        self.app.audio_auto_latest_var.set(not bool(selected_batch))
        self.app.audio_folder_var.set(str(folder))

        active_name = str(getattr(self.app, "active_project_name", "") or "")
        project = self.app.projects.get(active_name) if active_name else None
        if isinstance(project, dict):
            project.update({
                "audio_material_root": str(material_root),
                "audio_folder": str(folder),
                "audio_batch": selected_batch,
                "audio_auto_latest": not bool(selected_batch),
            })
            self.app.save_projects()

    def apply_task_contract(self, task: dict):
        """Apply the received material count after the legacy audio scan.

        The legacy stitcher stores project-level defaults (13/8/4/24).  A
        remote material task must override those defaults with the exact
        language/output plan entered in the submitter.
        """
        self._task_contract = dict(task)
        self._configure_cover_template_assignments()
        self._apply_task_output_directory()
        self._apply_task_contract_counts()
        self.app.refresh_language_tasks(select_index=0 if self.app.language_tasks else None)

    def _cover_assignment_path(self):
        owner = str(
            self._task_contract.get("canonicalTaskId")
            or self._task_contract.get("taskId")
            or ""
        ).strip()
        if not owner:
            return None
        safe_owner = re.sub(r"[^A-Za-z0-9._-]+", "_", owner)[:120]
        root = self.source_path.parent.parent / "workbench" / "handoff" / "cover_templates"
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{safe_owner}.json"

    @staticmethod
    def _read_json_object(path):
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_cover_assignment(self, assignment_key, assignment):
        path = self._cover_assignment_ledger_path
        if path is None:
            return
        payload = self._read_json_object(path)
        assignments = payload.get("assignments")
        if not isinstance(assignments, dict):
            assignments = {}
        assignments[str(assignment_key)] = dict(assignment)
        task = self._task_contract
        payload.update({
            "schemaVersion": 1,
            "batchId": str(task.get("batchId") or task.get("dispatchBatchId") or ""),
            "canonicalTaskId": str(task.get("canonicalTaskId") or task.get("taskId") or ""),
            "productionDate": str(task.get("productionDate") or task.get("date") or ""),
            "coverTemplateMode": str(task.get("coverTemplateMode") or "classic_a"),
            "updatedAt": datetime.now().isoformat(timespec="seconds"),
            "assignments": assignments,
        })
        temp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
        self._task_contract["coverTemplateAssignments"] = dict(assignments)

    def _configure_cover_template_assignments(self):
        task = self._task_contract
        path = self._cover_assignment_path()
        self._cover_assignment_ledger_path = path
        ledger = self._read_json_object(path) if path else {}
        owner = str(task.get("canonicalTaskId") or task.get("taskId") or "")
        saved_owner = str(ledger.get("canonicalTaskId") or "")
        assignments = ledger.get("assignments") if saved_owner in {"", owner} else {}
        if not isinstance(assignments, dict):
            assignments = {}
        inline = task.get("coverTemplateAssignments")
        if isinstance(inline, dict):
            assignments = {**inline, **assignments}
        self.app.cover_assignment_context = {
            "batchId": str(task.get("batchId") or task.get("dispatchBatchId") or ""),
            "canonicalTaskId": owner,
            "taskId": str(task.get("taskId") or ""),
            "stableTaskId": owner,
            "productionDate": str(task.get("productionDate") or task.get("date") or ""),
            # Old tasks deliberately remain on template A.  Only tasks
            # created after rollout receive auto_balanced explicitly.
            "coverTemplateMode": str(task.get("coverTemplateMode") or "classic_a"),
        }
        self.app.cover_template_assignments = dict(assignments)
        self.app.cover_template_assignment_callback = self._write_cover_assignment

    def _apply_task_output_directory(self):
        """Re-apply the immutable production-date directory at the last gate.

        The embedded stitcher's legacy validation calls
        ``apply_automatic_output_dir()`` using the wall-clock date.  A task
        resumed after midnight must still write to its submitted
        ``productionDate``.  Keeping this in the controller avoids changing
        interactive/manual stitcher behaviour.
        """
        task = self._task_contract
        profile = task.get("pipelineProfile") if isinstance(task.get("pipelineProfile"), dict) else {}
        stitch = profile.get("4_stitch") if isinstance(profile.get("4_stitch"), dict) else {}
        output_rule = str(stitch.get("outputDirectoryRule") or "").strip()
        if output_rule:
            output_dir = Path(output_rule)
            output_dir.mkdir(parents=True, exist_ok=True)
            # This is a per-task runtime override.  Do not save it back into
            # the legacy project template, otherwise a 7.22 task resumed on
            # 7.24 would silently change its production date directory.
            self.app.output_dir.set(str(output_dir))
        return output_rule

    def _apply_task_contract_counts(self):
        """Apply the immutable remote plan after every legacy audio rescan."""
        task = self._task_contract
        plan = contract_language_plan(task)
        planned = {str(item.get("code") or ""): int(item.get("outputCount") or 0) for item in plan}
        expected = contract_expected_count(task)
        owner = str(task.get("canonicalTaskId") or task.get("taskId") or "")
        matched = set()
        for audio_task in self.app.language_tasks:
            code = str(getattr(audio_task, "language_code", "") or "")
            audio_path = str(getattr(audio_task, "audio_path", "") or "")
            belongs = not owner or owner in audio_path or f"remote-{owner}" in audio_path
            if code in planned and code not in matched and belongs:
                audio_task.count = planned[code]
                matched.add(code)
            else:
                # A remote run must never inherit saved/default counts from a
                # different project, material or older handoff.
                audio_task.count = 0
        missing = sorted(code for code, count in planned.items() if count and code not in matched)
        actual = sum(
            int(getattr(audio_task, "count", 0) or 0)
            for audio_task in self.app.language_tasks
            if not owner or owner in str(getattr(audio_task, "audio_path", "") or "")
        )
        if missing:
            raise StitchContractValidationError(
                f"缺少任务合同要求的配音语言：{', '.join(missing)}",
                error_code="STITCH_LANGUAGE_CONFIG_MISSING",
                missing_languages=missing,
                expected=expected,
                actual=actual,
            )
        if actual != expected:
            raise StitchContractValidationError(
                f"拼接数量合同不一致：计划 {expected}，实际 {actual}",
                error_code="STITCH_CONTRACT_COUNT_MISMATCH",
                expected=expected,
                actual=actual,
            )

    def select_project(self, project_name: str):
        if not project_name or project_name not in self.app.projects:
            raise ValueError(f"第四步骤中找不到项目：{project_name}")
        if project_name == self.app.active_project_name:
            return
        self.app.project_name_var.set(project_name)
        self.app.on_project_selected()

    def focus_generation_actions(self):
        self.host.update_idletasks()
        self._update_scroll_region()
        self.page_canvas.yview_moveto(1.0)
        for widget in self._walk(self.host):
            if isinstance(widget, tk.Canvas):
                try:
                    widget.yview_moveto(1.0)
                except tk.TclError:
                    pass

    def is_running(self):
        worker = getattr(self.app, "worker", None)
        return bool(worker and worker.is_alive())

    def start_unattended(self):
        """Validate and begin a received pipeline task without clicking the UI."""
        if self.is_running():
            return False
        original_refresh = self.app.refresh_audio_batches
        original_auto_output = self.app.apply_automatic_output_dir

        def refresh_with_contract(*args, **kwargs):
            result = original_refresh(*args, **kwargs)
            if self._task_contract:
                self._apply_task_contract_counts()
            return result

        def immutable_task_output():
            # ``validate_before_generate`` invokes this after the task
            # contract was applied.  Do not let the current calendar day
            # silently replace the submitted production date.
            if self._task_contract and self._apply_task_output_directory():
                return False
            return original_auto_output()

        try:
            self._acquire_stitch_lock()
            # start_generate validates internally.  Its validation rescans
            # audio, so the temporary wrapper reapplies the remote contract at
            # the last possible moment before the worker snapshots tasks.
            self.app.refresh_audio_batches = refresh_with_contract
            self.app.apply_automatic_output_dir = immutable_task_output
            self.app.start_generate()
            if not self.is_running():
                raise RuntimeError("The stitcher did not start a worker after validation.")
            return True
        except ValueError as exc:
            self._release_stitch_lock()
            raise RuntimeError(str(exc)) from exc
        except Exception:
            self._release_stitch_lock()
            raise
        finally:
            self.app.refresh_audio_batches = original_refresh
            self.app.apply_automatic_output_dir = original_auto_output

    def save(self):
        self.app.save_cover_settings()
        self.app.save_projects()

    def cancel(self):
        self.app.cancel_generate()

    @staticmethod
    def _walk(widget):
        for child in widget.winfo_children():
            yield child
            yield from EmbeddedStitcherController._walk(child)
