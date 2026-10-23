from __future__ import annotations

"""Local-only import bridge shared by the V13.3 submitters.

The bridge deliberately never talks to the home receiver.  It only accepts a
signed-by-identity (session/run-group) import envelope on loopback, persists
it before acknowledging the request, and lets the unified launcher/child
submitters consume the envelope exactly once.
"""

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import tempfile
import threading
import uuid
from urllib.parse import urlsplit


BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 18766
PROTOCOL_VERSION = "v13.3"
APP_VERSION = "13.3"
RUN_GROUP_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}")
IMPORT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,159}")


def _local_state_root() -> Path:
    candidates = []
    for value in (os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA"), tempfile.gettempdir()):
        if value:
            candidates.append(Path(value) / "AI-Video-Task-Submitter" / "V13")
    candidates.append(Path.home() / ".ai-video-task-submitter" / "V13")
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_probe"
            probe.write_text("ok", encoding="ascii")
            probe.unlink()
            return candidate
        except OSError:
            continue
    # The system temp directory is expected to be writable even when a
    # locked-down company profile denies AppData and the home directory.
    return Path(tempfile.gettempdir()) / "AI-Video-Task-Submitter" / "V13"


STATE_ROOT = _local_state_root()
IMPORTS_ROOT = STATE_ROOT / "imports"
SESSION_FILE = STATE_ROOT / "session.json"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return default


def ensure_session(run_group_id: str) -> dict:
    """Create or update the current launcher session."""
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    existing = _read_json(SESSION_FILE, {})
    # A process restart deliberately rotates the session ID.  Pending import
    # files remain durable for the child submitters, while an old browser tab
    # cannot accidentally submit against a new launcher/run group.
    session_id = f"session-{uuid.uuid4().hex}"
    payload = {
        "protocolVersion": PROTOCOL_VERSION,
        "appVersion": APP_VERSION,
        "sessionId": session_id,
        "runGroupId": run_group_id,
        "startedAt": str(existing.get("startedAt") or _now()),
        "updatedAt": _now(),
        "pid": os.getpid(),
    }
    atomic_write_json(SESSION_FILE, payload)
    return payload


def load_session() -> dict:
    payload = _read_json(SESSION_FILE, {})
    return payload if isinstance(payload, dict) else {}


def import_file(import_id: str) -> Path:
    return IMPORTS_ROOT / f"{import_id}.json"


def import_status_file(import_id: str) -> Path:
    return IMPORTS_ROOT / f"{import_id}.status.json"


def claim_file(import_id: str, kind: str) -> Path:
    safe_kind = re.sub(r"[^A-Za-z0-9_-]+", "-", kind).strip("-") or "child"
    return IMPORTS_ROOT / f"{import_id}.{safe_kind}.claim"


def write_import_status(import_id: str, status: dict) -> dict:
    current = _read_json(import_status_file(import_id), {})
    if not isinstance(current, dict):
        current = {}
    merged = {**current, **status, "importId": import_id, "updatedAt": _now()}
    atomic_write_json(import_status_file(import_id), merged)
    return merged


def read_import(import_id: str) -> dict | None:
    payload = _read_json(import_file(import_id))
    return payload if isinstance(payload, dict) else None


def read_import_status(import_id: str) -> dict:
    payload = _read_json(import_status_file(import_id), {})
    return payload if isinstance(payload, dict) else {}


def claim_import(import_id: str, kind: str) -> bool:
    """Claim one child-kind without allowing duplicate imports after polling."""
    marker = claim_file(import_id, kind)
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with marker.open("x", encoding="utf-8") as handle:
            handle.write(_now())
        return True
    except FileExistsError:
        return False
    except OSError:
        return False


def release_claim(import_id: str, kind: str) -> None:
    try:
        claim_file(import_id, kind).unlink()
    except OSError:
        pass


def _validate_envelope(payload: object, session: dict) -> tuple[bool, str, dict | None]:
    if not isinstance(payload, dict):
        return False, "导入信封必须是JSON对象", None
    import_id = str(payload.get("importId") or "").strip()
    session_id = str(payload.get("sessionId") or "").strip()
    run_group_id = str(payload.get("runGroupId") or "").strip()
    production_date = str(payload.get("productionDate") or "").strip()
    payloads = payload.get("payloads")
    if not IMPORT_ID_PATTERN.fullmatch(import_id):
        return False, "importId格式无效", None
    if session_id != str(session.get("sessionId") or ""):
        return False, "sessionId已过期，请重新读取统一递交器会话", None
    if not RUN_GROUP_PATTERN.fullmatch(run_group_id):
        return False, "runGroupId格式无效", None
    if run_group_id != str(session.get("runGroupId") or ""):
        return False, "runGroupId与当前统一递交器不一致", None
    if not production_date:
        return False, "productionDate不能为空", None
    if not isinstance(payloads, list) or not payloads or len(payloads) > 2:
        return False, "payloads必须包含1至2个独立子合同", None
    kinds: set[str] = set()
    seen_urls: set[str] = set()
    normalized_payloads = []
    for child in payloads:
        if not isinstance(child, dict):
            return False, "子合同格式无效", None
        kind = str(child.get("batchKind") or "").strip()
        if kind not in {"formal", "examplebrand-natural-flow"}:
            return False, "未知合同类型：" + (kind or "空"), None
        if kind in kinds:
            return False, "同一次导入不能重复提交同类子合同", None
        kinds.add(kind)
        if str(child.get("runGroupId") or "").strip() != run_group_id:
            return False, f"{kind}子合同的runGroupId不一致", None
        tasks = child.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return False, f"{kind}子合同没有任务", None
        for task in tasks:
            if not isinstance(task, dict):
                return False, "任务记录格式无效", None
            urls = task.get("urls")
            if not isinstance(urls, list) or not urls:
                return False, "任务链接不能为空", None
            for url in urls:
                text = str(url or "").strip()
                parsed = urlsplit(text)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    return False, f"非法URL：{text}", None
                if text in seen_urls:
                    return False, "同一次导入存在重复链接", None
                seen_urls.add(text)
        normalized_payloads.append(child)
    normalized = dict(payload)
    normalized["payloads"] = normalized_payloads
    normalized["protocolVersion"] = PROTOCOL_VERSION
    return True, "", normalized


class _BridgeHandler(BaseHTTPRequestHandler):
    server_version = "AI-Video-Task-Submitter-Bridge/13.3"

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler API
        return

    @property
    def bridge(self):
        return self.server.bridge  # type: ignore[attr-defined]

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 - HTTP handler API
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/api/health":
            self._send(200, self.bridge.health())
            return
        if path == "/api/session":
            self._send(200, self.bridge.session())
            return
        prefix = "/api/imports/"
        if path.startswith(prefix):
            import_id = path[len(prefix):].strip()
            if not IMPORT_ID_PATTERN.fullmatch(import_id):
                self._send(400, {"ok": False, "error": "importId格式无效"})
                return
            status = read_import_status(import_id)
            if not status:
                self._send(404, {"ok": False, "error": "未找到导入记录"})
            else:
                self._send(200, {"ok": True, **status})
            return
        self._send(404, {"ok": False, "error": "not_found"})

    def do_POST(self):  # noqa: N802 - HTTP handler API
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path != "/api/imports":
            self._send(404, {"ok": False, "error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 20 * 1024 * 1024))
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"ok": False, "error": "请求不是有效JSON"})
            return
        result = self.bridge.persist_import(payload)
        self._send(int(result.pop("_status", 200)), result)


class LocalImportBridge:
    def __init__(self, run_group_id: str, on_import=None):
        self._lock = threading.RLock()
        self._run_group_id = run_group_id
        self._session = ensure_session(run_group_id)
        self._on_import = on_import
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def set_run_group(self, run_group_id: str) -> dict:
        with self._lock:
            self._run_group_id = run_group_id
            self._session = ensure_session(run_group_id)
            return dict(self._session)

    def session(self) -> dict:
        with self._lock:
            session = dict(self._session)
            session["bridgeUrl"] = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"
            return {"ok": True, **session}

    def health(self) -> dict:
        with self._lock:
            pending = 0
            for path in IMPORTS_ROOT.glob("*.status.json"):
                status = _read_json(path, {})
                if isinstance(status, dict) and status.get("state") not in {"completed", "blocked", "expired"}:
                    pending += 1
            return {
                "ok": True,
                "protocolVersion": PROTOCOL_VERSION,
                "appVersion": APP_VERSION,
                "listening": self._server is not None,
                "host": BRIDGE_HOST,
                "port": BRIDGE_PORT,
                "pendingImports": pending,
                "sessionId": self._session.get("sessionId"),
                "runGroupId": self._run_group_id,
            }

    def persist_import(self, payload: object) -> dict:
        with self._lock:
            valid, reason, normalized = _validate_envelope(payload, self._session)
            if not valid or normalized is None:
                return {"ok": False, "error": reason, "_status": 400}
            import_id = str(normalized["importId"])
            existing = read_import_status(import_id)
            if existing:
                return {"ok": True, **existing, "duplicate": True}
            IMPORTS_ROOT.mkdir(parents=True, exist_ok=True)
            normalized["receivedAt"] = _now()
            normalized["state"] = "persisted"
            normalized["childStatus"] = {
                str(child.get("batchKind")): {"state": "pending"}
                for child in normalized["payloads"]
            }
            atomic_write_json(import_file(import_id), normalized)
            status = write_import_status(import_id, {
                "state": "persisted",
                "sessionId": normalized["sessionId"],
                "runGroupId": normalized["runGroupId"],
                "productionDate": normalized["productionDate"],
                "childKinds": list(normalized["childStatus"]),
                "childStatus": normalized["childStatus"],
            })
            callback = self._on_import
            if callback is not None:
                try:
                    callback(import_id, normalized)
                except Exception as exc:  # callback must not break HTTP acknowledgement
                    write_import_status(import_id, {"callbackError": str(exc)})
            return {"ok": True, **status}

    def start(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            IMPORTS_ROOT.mkdir(parents=True, exist_ok=True)
            try:
                server = ThreadingHTTPServer((BRIDGE_HOST, BRIDGE_PORT), _BridgeHandler)
            except OSError as exc:
                raise RuntimeError(f"本地导入桥端口 {BRIDGE_PORT} 被占用：{exc}") from exc
            server.daemon_threads = True
            server.bridge = self  # type: ignore[attr-defined]
            self._server = server
            self._thread = threading.Thread(target=server.serve_forever, name="v13-local-import-bridge", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            server, thread = self._server, self._thread
            self._server = None
            self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
