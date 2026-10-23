from __future__ import annotations

"""Shared receiver configuration with safe legacy migration.

Release archives never include real credentials.  On the company computer we
reuse an existing working submitter configuration and persist only on that
computer, so upgrading the portable UI does not silently fall back to
127.0.0.1.
"""

import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit


HOME_RECEIVER = "http://example-pc:8766"
HOME_DASHBOARD = "http://example-pc:8765"
DEFAULT_CONNECTION = {
    "receiverUrl": HOME_RECEIVER,
    "dashboardUrl": HOME_DASHBOARD,
    "accessToken": "",
}


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _state_root() -> Path:
    candidates = []
    for value in (os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA"), tempfile.gettempdir()):
        if value:
            candidates.append(Path(value) / "AI-Video-Task-Submitter" / "V13")
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".connection_write_probe"
            probe.write_text("ok", encoding="ascii")
            probe.unlink()
            return candidate
        except OSError:
            continue
    return Path(tempfile.gettempdir()) / "AI-Video-Task-Submitter" / "V13"


SHARED_CONNECTION_FILE = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "AI-Video-Task-Submitter" / "V13" / "connection.json"


def _normalized(payload: dict) -> dict:
    return {
        "receiverUrl": str(payload.get("receiverUrl") or "").strip().rstrip("/"),
        "dashboardUrl": str(payload.get("dashboardUrl") or "").strip().rstrip("/"),
        "accessToken": str(payload.get("accessToken") or "").strip(),
    }


def _is_remote_receiver(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "http" and bool(host) and host not in {"127.0.0.1", "localhost", "::1"}


def is_usable_connection(payload: dict) -> bool:
    value = _normalized(payload)
    return _is_remote_receiver(value["receiverUrl"]) and bool(value["accessToken"])


def _package_search_root(app_dir: Path) -> Path | None:
    current = app_dir.resolve()
    for candidate in (current, *current.parents):
        if candidate.name.lower().startswith("ai-video-task-submitter-v13"):
            return candidate.parent
    # Older standalone packages commonly live immediately under the drive or
    # the parent directory of the executable tree.
    parents = list(current.parents)
    return parents[min(3, len(parents) - 1)] if parents else None


def _legacy_candidates(app_dir: Path) -> list[Path]:
    explicit = str(os.environ.get("AI_VIDEO_CONNECTION_CONFIG") or "").strip()
    candidates = [Path(explicit)] if explicit else []
    search_root = _package_search_root(app_dir)
    search_roots = []
    if search_root and search_root.is_dir():
        search_roots.append(search_root)
        if search_root.parent != search_root and search_root.parent.is_dir():
            search_roots.append(search_root.parent)
    for search_root in search_roots:
        patterns = (
            "LaptopTaskSubmitterV*/AI-Video-Task-Submitter/task_submitter_config.json",
            "LaptopTaskSubmitter*/AI-Video-Task-Submitter/task_submitter_config.json",
            "AI-Video-Task-Submitter-V13*/正式任务递交器/程序/AI-Video-Task-Submitter/task_submitter_config.json",
        )
        for pattern in patterns:
            try:
                candidates.extend(search_root.glob(pattern))
            except OSError:
                continue
    unique = []
    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        key = str(resolved).lower()
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return unique


def load_connection(app_dir: Path, config_file: Path) -> tuple[dict, str]:
    """V14 endpoint is program policy; legacy user configuration stays untouched."""
    current = _read_json(config_file)
    result = dict(current)
    result.update(receiverUrl="https://service.tailexample.ts.net", dashboardUrl="https://service.tailexample.ts.net", accessToken="")
    return result, "v14_https_policy"


def connection_error(config: dict) -> str:
    from urllib.parse import urlsplit
    value = str(config.get("receiverUrl") or "").strip()
    parts = urlsplit(value)
    if parts.scheme == "https" and parts.hostname and parts.hostname.endswith(".ts.net") and not parts.username and not parts.password and parts.port in (None,443) and parts.path in ("", "/") and not parts.query and not parts.fragment:
        return ""
    return "V14 请使用 Tailscale HTTPS 根地址；无需递交令牌。"
