from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Callable

from common_io import atomic_write_json, now_iso


APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "baidu_upload_config.json"
SECRET_FILE = APP_DIR / "baidu_upload_secrets.dat"
STATE_FILE = APP_DIR / "handoff" / "upload" / "upload_status.json"
HISTORICAL_VERIFY_REQUEST_FILE = (
    APP_DIR / "handoff" / "upload" / "historical_cloud_verify_request.json"
)
HISTORICAL_VERIFY_STATUS_FILE = (
    APP_DIR / "handoff" / "upload" / "historical_cloud_verify_status.json"
)

DEFAULT_CONFIG = {
    "enabled": False,
    "app_name": "AI视频生产线",
    "records_root": str(APP_DIR / "交接记录"),
    "state_file": str(STATE_FILE),
    "scan_interval_seconds": 5,
    "stable_seconds": 30,
    "stable_checks": 3,
    "retry_base_seconds": 30,
    "retry_max_seconds": 1800,
    "request_timeout_seconds": 90,
    "cleanup_after_upload": True,
    "keep_final_until_download_confirmed": False,
    "protected_sync_roots": [],
    # Empty means all future uploads; a date such as 7.20 protects earlier history.
    "cleanup_from_date": "",
}

PROJECT_DIRS = {
    "ProjectA": "ProjectA",
    "ProjectB": "ProjectB",
    "ProjectC": "ProjectC",
    "ProjectD": "ProjectD",
    "SPC地板": "SPC",
    "SPC墙板": "SPC",
    # ExampleBrand is an independent task box, but its verified final MP4 files use
    # the same flat date/project cloud layout as formal projects.  Account and
    # output ownership stay in the immutable filename/manifest; no account
    # subdirectory is introduced here.
    "ExampleBrand": "ExampleBrand",
}


def load_json(path: Path, default):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return default
    return value


def load_config(path: Path = CONFIG_FILE) -> dict:
    config = dict(DEFAULT_CONFIG)
    saved = load_json(path, {})
    if isinstance(saved, dict):
        config.update({key: value for key, value in saved.items() if key in config})
    return config


def save_config(config: dict, path: Path = CONFIG_FILE) -> None:
    content = dict(DEFAULT_CONFIG)
    content.update({key: value for key, value in config.items() if key in content})
    atomic_write_json(path, content)


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, object]:
    buffer = ctypes.create_string_buffer(data)
    return (
        _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))),
        buffer,
    )


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("百度网盘密钥只能在 Windows 上使用 DPAPI 保存")
    source, source_buffer = _blob(data)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "AI Video Pipeline",
        None,
        None,
        None,
        0,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("百度网盘密钥只能在 Windows 上使用 DPAPI 读取")
    source, source_buffer = _blob(data)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


class SecretStore:
    """Stores credentials encrypted for the current Windows user with DPAPI."""

    def __init__(self, path: Path = SECRET_FILE):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            encrypted = base64.b64decode(self.path.read_bytes(), validate=True)
            payload = json.loads(_unprotect(encrypted).decode("utf-8"))
        except (OSError, ValueError, UnicodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def save(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(base64.b64encode(_protect(data)))
        os.replace(temporary, self.path)

    def update(self, **changes) -> dict:
        payload = self.load()
        payload.update(changes)
        self.save(payload)
        return payload


class BaiduApiError(RuntimeError):
    def __init__(self, message: str, code=None, payload=None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


def upload_failure_code(error: Exception) -> str:
    """Return a stable code without treating auth or cloud conflicts as retryable."""
    if isinstance(error, ValueError) and "不支持的项目" in str(error):
        return "UPLOAD_PROJECT_UNSUPPORTED"
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "UPLOAD_TIMEOUT"
    if isinstance(error, (urllib.error.URLError, ConnectionError, ConnectionResetError, BrokenPipeError)):
        return "UPLOAD_NETWORK_ERROR"
    if isinstance(error, BaiduApiError):
        code = str(getattr(error, "code", "") or "").lower()
        if code in {"6", "110", "111", "invalid_grant", "expired_token", "unauthorized"}:
            return "UPLOAD_CREDENTIAL_INVALID"
        return "UPLOAD_API_ERROR"
    return "UPLOAD_FAILED"


# Baidu Netdisk is a mainland service, but the machine-wide HTTPS_PROXY (local
# v2rayN) routed every request through the proxy: d.pcs.baidu.com took 4.3 s
# per connection through it versus 0.14 s direct (measured 2026-10-20), and
# each uploaded chunk is its own request.  Connect directly; use the system
# proxy only when a direct connection cannot be opened at all.
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_PROXY_FALLBACK = False


def _connect_failed(error: urllib.error.URLError) -> bool:
    """True only for failures before any request byte was sent."""
    reason = getattr(error, "reason", error)
    if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
        return True
    return isinstance(reason, OSError) and getattr(reason, "errno", None) in {101, 113, 10051, 10065}


def _open(request: urllib.request.Request, timeout: int):
    global _PROXY_FALLBACK
    if not _PROXY_FALLBACK:
        try:
            return _DIRECT_OPENER.open(request, timeout=timeout)
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError as exc:
            # A timeout or reset may mean Baidu already received the request,
            # so only a refused/unresolvable connection is retried via proxy.
            if not _connect_failed(exc):
                raise
            _PROXY_FALLBACK = True
    return urllib.request.urlopen(request, timeout=timeout)


def _json_request(
    url: str,
    *,
    data: dict | None = None,
    body: bytes | None = None,
    headers: dict | None = None,
    timeout: int = 90,
) -> dict:
    request_headers = {"User-Agent": "pan.baidu.com"}
    request_headers.update(headers or {})
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    request = urllib.request.Request(url, data=body, headers=request_headers)
    try:
        with _open(request, timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            payload = {}
        message = payload.get("error_description") or payload.get("errmsg") or str(exc)
        raise BaiduApiError(message, payload.get("errno") or payload.get("error_code"), payload) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise BaiduApiError(f"网络请求失败：{exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise BaiduApiError("百度网盘返回了无法识别的数据") from exc
    if payload.get("error"):
        message = payload.get("error_description") or payload.get("error")
        raise BaiduApiError(str(message), payload.get("error"), payload)
    code = payload.get("errno")
    if code is None:
        code = payload.get("error_code")
    if code not in (None, 0, "0"):
        message = (
            payload.get("error_description")
            or payload.get("error_msg")
            or payload.get("errmsg")
            or f"百度网盘接口错误 {code}"
        )
        raise BaiduApiError(str(message), code, payload)
    return payload


class BaiduNetdiskClient:
    def __init__(self, secret_store: SecretStore, timeout: int = 90):
        self.secret_store = secret_store
        self.timeout = int(timeout)

    def _credentials(self) -> dict:
        credentials = self.secret_store.load()
        if not credentials.get("app_key") or not credentials.get("secret_key"):
            raise BaiduApiError("尚未配置 App Key 和 Secret Key")
        return credentials

    def request_device_code(self) -> dict:
        credentials = self._credentials()
        query = urllib.parse.urlencode(
            {
                "response_type": "device_code",
                "client_id": credentials["app_key"],
                "scope": "basic,netdisk",
            }
        )
        return _json_request(
            f"https://openapi.baidu.com/oauth/2.0/device/code?{query}",
            timeout=self.timeout,
        )

    def poll_device_token(
        self,
        device_code: str,
        interval: int = 5,
        expires_in: int = 300,
    ) -> dict:
        credentials = self._credentials()
        deadline = time.time() + max(30, int(expires_in))
        wait = max(5, int(interval))
        while time.time() < deadline:
            query = urllib.parse.urlencode(
                {
                    "grant_type": "device_token",
                    "code": device_code,
                    "client_id": credentials["app_key"],
                    "client_secret": credentials["secret_key"],
                }
            )
            try:
                token = _json_request(
                    f"https://openapi.baidu.com/oauth/2.0/token?{query}",
                    timeout=self.timeout,
                )
            except BaiduApiError as exc:
                pending = str(exc.payload.get("error") or exc.code or "").lower()
                if pending in {
                    "authorization_pending",
                    "slow_down",
                    "expired_token",
                    "invalid_grant",
                }:
                    if pending == "slow_down":
                        wait += 2
                    elif pending in {"expired_token", "invalid_grant"}:
                        raise
                    time.sleep(wait)
                    continue
                raise
            self._save_token(token)
            return token
        raise BaiduApiError("百度网盘授权码已过期，请重新发起授权")

    def _save_token(self, token: dict) -> None:
        expires_in = max(60, int(token.get("expires_in") or 2592000))
        self.secret_store.update(
            access_token=str(token.get("access_token") or ""),
            refresh_token=str(token.get("refresh_token") or ""),
            expires_at=int(time.time()) + expires_in,
            authorized_at=now_iso(),
        )

    def ensure_access_token(self) -> str:
        credentials = self._credentials()
        token = str(credentials.get("access_token") or "")
        expires_at = int(credentials.get("expires_at") or 0)
        if token and expires_at > int(time.time()) + 3600:
            return token
        refresh_token = str(credentials.get("refresh_token") or "")
        if not refresh_token:
            raise BaiduApiError("百度网盘尚未授权，请先在工作台扫码授权")
        query = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": credentials["app_key"],
                "client_secret": credentials["secret_key"],
            }
        )
        token_payload = _json_request(
            f"https://openapi.baidu.com/oauth/2.0/token?{query}",
            timeout=self.timeout,
        )
        # Refresh tokens are single-use. Persist the replacement immediately.
        self._save_token(token_payload)
        return str(token_payload["access_token"])

    def user_info(self) -> dict:
        access_token = self.ensure_access_token()
        query = urllib.parse.urlencode(
            {"method": "uinfo", "access_token": access_token, "vip_version": "v2"}
        )
        return _json_request(
            f"https://pan.baidu.com/rest/2.0/xpan/nas?{query}",
            timeout=self.timeout,
        )

    @staticmethod
    def chunk_size_for_vip(vip_type: int) -> int:
        return {2: 32, 1: 16}.get(int(vip_type or 0), 4) * 1024 * 1024

    @staticmethod
    def file_limit_for_vip(vip_type: int) -> int:
        return {2: 20, 1: 10}.get(int(vip_type or 0), 4) * 1024 * 1024 * 1024

    def _precreate(
        self,
        remote_path: str,
        size: int,
        block_md5s: list[str],
        upload_id: str = "",
    ) -> dict:
        token = self.ensure_access_token()
        query = urllib.parse.urlencode({"method": "precreate", "access_token": token})
        data = {
            "path": remote_path,
            "size": str(size),
            "isdir": "0",
            "autoinit": "1",
            "rtype": "2",
            "block_list": json.dumps(block_md5s, ensure_ascii=False),
        }
        if upload_id:
            data["uploadid"] = upload_id
        return _json_request(
            f"https://pan.baidu.com/rest/2.0/xpan/file?{query}",
            data=data,
            timeout=self.timeout,
        )

    def _upload_domain(self, remote_path: str, upload_id: str) -> str:
        token = self.ensure_access_token()
        query = urllib.parse.urlencode(
            {
                "method": "locateupload",
                "appid": "250528",
                "access_token": token,
                "path": remote_path,
                "uploadid": upload_id,
                "upload_version": "2.0",
            }
        )
        payload = _json_request(
            f"https://d.pcs.baidu.com/rest/2.0/pcs/file?{query}",
            timeout=self.timeout,
        )
        choices = [
            str(item.get("server") or "")
            for item in payload.get("servers") or []
            if str(item.get("server") or "").startswith("https://")
        ]
        if not choices:
            server = str(payload.get("host") or "").strip()
            if server:
                choices.append("https://" + server.lstrip("/"))
        if not choices:
            raise BaiduApiError("百度网盘没有返回可用的上传服务器")
        return choices[0].rstrip("/")

    def _upload_part(
        self,
        domain: str,
        remote_path: str,
        upload_id: str,
        part_number: int,
        chunk: bytes,
    ) -> dict:
        token = self.ensure_access_token()
        query = urllib.parse.urlencode(
            {
                "method": "upload",
                "access_token": token,
                "type": "tmpfile",
                "path": remote_path,
                "uploadid": upload_id,
                "partseq": str(part_number),
            }
        )
        boundary = "----AIVideoPipeline" + uuid.uuid4().hex
        prefix = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="chunk.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("ascii")
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        return _json_request(
            f"{domain}/rest/2.0/pcs/superfile2?{query}",
            body=prefix + chunk + suffix,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=max(self.timeout, 300),
        )

    def _create(
        self,
        remote_path: str,
        size: int,
        block_md5s: list[str],
        upload_id: str,
    ) -> dict:
        token = self.ensure_access_token()
        query = urllib.parse.urlencode({"method": "create", "access_token": token})
        return _json_request(
            f"https://pan.baidu.com/rest/2.0/xpan/file?{query}",
            data={
                "path": remote_path,
                "size": str(size),
                "isdir": "0",
                "rtype": "2",
                "uploadid": upload_id,
                "block_list": json.dumps(block_md5s, ensure_ascii=False),
                "mode": "3",
            },
            timeout=self.timeout,
        )

    def rename_file(self, remote_path: str, new_name: str) -> str:
        token = self.ensure_access_token()
        query = urllib.parse.urlencode(
            {
                "method": "filemanager",
                "access_token": token,
                "opera": "rename",
            }
        )
        payload = _json_request(
            f"https://pan.baidu.com/rest/2.0/xpan/file?{query}",
            data={
                "async": "0",
                "filelist": json.dumps(
                    [{"path": remote_path, "newname": new_name}],
                    ensure_ascii=False,
                ),
            },
            timeout=self.timeout,
        )
        for result in payload.get("info") or []:
            code = result.get("errno")
            if code not in (None, 0, "0"):
                raise BaiduApiError(
                    f"百度网盘重命名失败 {code}",
                    code,
                    result,
                )
        parent = remote_path.rsplit("/", 1)[0]
        return f"{parent}/{new_name}"

    def file_metadata(self, fs_id: str) -> dict:
        """Read the completed cloud object back from Baidu before local cleanup."""
        token = self.ensure_access_token()
        query = urllib.parse.urlencode(
            {
                "method": "filemetas",
                "access_token": token,
                "fsids": json.dumps([int(fs_id)]),
                "dlink": "0",
            }
        )
        payload = _json_request(
            f"https://pan.baidu.com/rest/2.0/xpan/multimedia?{query}",
            timeout=self.timeout,
        )
        entries = payload.get("list") or []
        if len(entries) != 1:
            raise BaiduApiError("百度网盘无法确认已上传文件仍然存在")
        entry = dict(entries[0])
        if str(entry.get("fs_id") or "") != str(fs_id):
            raise BaiduApiError("百度网盘返回的文件身份与本条任务不一致")
        return entry

    def upload_file(
        self,
        local_path: Path,
        remote_path: str,
        *,
        progress: Callable[[dict], None] | None = None,
        resume: dict | None = None,
    ) -> dict:
        local_path = Path(local_path)
        info = self.user_info()
        vip_type = int(info.get("vip_type") or 0)
        chunk_size = self.chunk_size_for_vip(vip_type)
        size = local_path.stat().st_size
        if size > self.file_limit_for_vip(vip_type):
            raise BaiduApiError("视频超过当前百度网盘账号的单文件上传上限")

        block_md5s: list[str] = []
        with local_path.open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                block_md5s.append(hashlib.md5(chunk).hexdigest())
        if not block_md5s:
            raise BaiduApiError("不允许上传空文件")
        if len(block_md5s) > 1024:
            raise BaiduApiError("视频分片数量超过百度网盘限制")

        resume = dict(resume or {})
        if resume.get("size") != size or resume.get("blockMd5s") != block_md5s:
            resume = {}
        upload_id = str(resume.get("uploadId") or "")
        try:
            precreate = self._precreate(remote_path, size, block_md5s, upload_id)
        except BaiduApiError:
            if not upload_id:
                raise
            upload_id = ""
            precreate = self._precreate(remote_path, size, block_md5s)
        upload_id = str(precreate.get("uploadid") or upload_id)
        if not upload_id:
            raise BaiduApiError("预上传没有返回 uploadid")
        required = precreate.get("block_list")
        if not isinstance(required, list):
            required = list(range(len(block_md5s)))
        required_parts = {int(value) for value in required}
        domain = self._upload_domain(remote_path, upload_id)
        uploaded = {int(value) for value in resume.get("uploadedParts") or []}
        uploaded -= required_parts

        if progress:
            progress(
                {
                    "phase": "uploading",
                    "uploadId": upload_id,
                    "size": size,
                    "chunkSize": chunk_size,
                    "blockMd5s": block_md5s,
                    "uploadedParts": sorted(uploaded),
                    "vipType": vip_type,
                    "account": str(info.get("netdisk_name") or info.get("baidu_name") or ""),
                }
            )

        with local_path.open("rb") as handle:
            for part_number in range(len(block_md5s)):
                if part_number not in required_parts:
                    uploaded.add(part_number)
                    continue
                if part_number in uploaded:
                    continue
                handle.seek(part_number * chunk_size)
                chunk = handle.read(chunk_size)
                result = self._upload_part(
                    domain, remote_path, upload_id, part_number, chunk
                )
                remote_md5 = str(result.get("md5") or "").lower()
                if remote_md5 and remote_md5 != block_md5s[part_number]:
                    raise BaiduApiError(f"第 {part_number + 1} 个分片校验失败")
                uploaded.add(part_number)
                if progress:
                    progress(
                        {
                            "phase": "uploading",
                            "uploadId": upload_id,
                            "size": size,
                            "chunkSize": chunk_size,
                            "blockMd5s": block_md5s,
                            "uploadedParts": sorted(uploaded),
                            "vipType": vip_type,
                            "account": str(
                                info.get("netdisk_name") or info.get("baidu_name") or ""
                            ),
                        }
                    )

        created = self._create(remote_path, size, block_md5s, upload_id)
        if int(created.get("size") or -1) != size:
            raise BaiduApiError("云端文件大小与本地文件不一致")
        if not created.get("fs_id"):
            raise BaiduApiError("百度网盘没有返回最终文件ID")
        return {
            "fsId": str(created["fs_id"]),
            "path": str(created.get("path") or remote_path),
            "size": int(created.get("size") or size),
            "md5": str(created.get("md5") or ""),
            "vipType": vip_type,
            "chunkSize": chunk_size,
            "account": str(info.get("netdisk_name") or info.get("baidu_name") or ""),
        }


def safe_remote_component(value: str) -> str:
    cleaned = str(value or "").strip().replace("/", "_").replace("\\", "_")
    return cleaned.strip(". ") or "未命名"


def remote_path_for(task: dict, app_name: str) -> str:
    project = str(task.get("project") or "").strip()
    category = PROJECT_DIRS.get(project)
    if not category:
        raise ValueError(f"不支持的项目：{project or '空'}")
    date_value = safe_remote_component(str(task.get("productionDate") or task.get("date") or ""))
    output_path = str(
        task.get("finalOutputPath")
        or task.get("localPath")
        or ""
    )
    filename = safe_remote_component(Path(output_path).name)
    return "/".join(
        (
            "",
            "apps",
            safe_remote_component(app_name),
            date_value,
            category,
            filename,
        )
    )


def is_final_mp4(path: Path) -> tuple[bool, str]:
    path = Path(path)
    if path.suffix.lower() != ".mp4":
        return False, "只允许上传最终 MP4"
    try:
        size = path.stat().st_size
        if size <= 1024:
            return False, "视频文件过小"
        with path.open("rb") as handle:
            header = handle.read(64)
    except OSError as exc:
        return False, f"无法读取成片：{exc}"
    if b"ftyp" not in header:
        return False, "文件不是有效的 MP4 容器"
    return True, ""


class _ShellFileOperation(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def recycle_file(path: Path) -> None:
    """Move one explicit local file to the Windows Recycle Bin, never delete it directly."""
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(str(path))
    operation = _ShellFileOperation()
    operation.wFunc = 3  # FO_DELETE
    operation.pFrom = str(path) + "\0\0"
    operation.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400  # undo + no UI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result or operation.fAnyOperationsAborted:
        raise OSError(result or 1, f"Unable to move to Recycle Bin: {path}")


def _empty_state() -> dict:
    return {
        "schemaVersion": 1,
        "configured": False,
        "authorized": False,
        "running": False,
        "updatedAt": "",
        "account": "",
        "vipType": None,
        "chunkSizeMB": 0,
        "tasks": {},
    }


class UploadCoordinator:
    def __init__(
        self,
        config_path: Path = CONFIG_FILE,
        secret_store: SecretStore | None = None,
        client_factory: Callable[[SecretStore, int], BaiduNetdiskClient] = BaiduNetdiskClient,
        historical_verify_request_path: Path | None = None,
        historical_verify_status_path: Path | None = None,
    ):
        self.config_path = Path(config_path)
        self.config = load_config(self.config_path)
        self.secret_store = secret_store or SecretStore()
        self.client_factory = client_factory
        self.state_path = Path(str(self.config.get("state_file") or STATE_FILE))
        self.historical_verify_request_path = Path(
            historical_verify_request_path
            or self.state_path.parent / HISTORICAL_VERIFY_REQUEST_FILE.name
        )
        self.historical_verify_status_path = Path(
            historical_verify_status_path
            or self.state_path.parent / HISTORICAL_VERIFY_STATUS_FILE.name
        )
        loaded = load_json(self.state_path, {})
        self.state = loaded if isinstance(loaded, dict) else {}
        baseline = _empty_state()
        baseline.update(self.state)
        if not isinstance(baseline.get("tasks"), dict):
            baseline["tasks"] = {}
        self.state = baseline

    def save_state(self) -> None:
        self.state["updatedAt"] = now_iso()
        atomic_write_json(self.state_path, self.state)

    def discover(self) -> int:
        records_root = Path(str(self.config["records_root"]))
        discovered = 0
        if not records_root.is_dir():
            return discovered
        for path in records_root.glob("*/第四步到第五步.json"):
            payload = load_json(path, {})
            if not isinstance(payload, dict):
                continue
            for task in payload.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                capcut_status = self._capcut_status(task)
                if not self._capcut_completed(task, capcut_status):
                    continue
                task_id = str(task.get("taskId") or "").strip()
                final_path = Path(str(task.get("finalOutputPath") or ""))
                if not task_id or not final_path.is_file():
                    continue
                item = self.state["tasks"].get(task_id)
                if item is None:
                    item = {
                        "taskId": task_id,
                        "cleanupPolicyVersion": int(payload.get("cleanupPolicyVersion") or 0),
                        "batchId": str(payload.get("batchId") or ""),
                        "manifestId": str(payload.get("manifestId") or ""),
                        "attemptId": str(payload.get("attemptId") or ""),
                        "canonicalTaskId": str(
                            task.get("remotePipelineTaskId")
                            or payload.get("remotePipelineTaskId")
                            or ""
                        ),
                        "expectedTaskCount": int(
                            task.get("expectedTaskCount")
                            or payload.get("expectedTaskCount")
                            or 0
                        ),
                        "date": str(task.get("productionDate") or task.get("date") or payload.get("productionDate") or payload.get("date") or ""),
                        "productionDate": str(task.get("productionDate") or task.get("date") or payload.get("productionDate") or payload.get("date") or ""),
                        "project": str(task.get("project") or ""),
                        "material": str(task.get("material") or ""),
                        "language": str(task.get("language") or ""),
                        "localPath": str(final_path.resolve()),
                        # These paths originate from the fourth-to-fifth-step handoff.
                        # Only explicitly recorded paths are eligible for automatic recycling.
                        "sourceVideoPath": str(task.get("videoPath") or ""),
                        "temporaryOutputPath": str(
                            (capcut_status or {}).get("temporaryOutputPath")
                            or task.get("temporaryOutputPath")
                            or ""
                        ),
                        "postProcessWorkingPath": str(
                            (capcut_status or {}).get("postProcessWorkingPath")
                            or task.get("postProcessWorkingPath")
                            or ""
                        ),
                        "preparedSamplePath": str(
                            (capcut_status or {}).get("preparedSamplePath")
                            or task.get("preparedSamplePath")
                            or ""
                        ),
                        "originalSamplePath": str(
                            (capcut_status or {}).get("originalSamplePath")
                            or task.get("originalSamplePath")
                            or ""
                        ),
                        "audioPath": str(task.get("audioPath") or ""),
                        "subtitleSourcePaths": list(task.get("subtitleSourcePaths") or []),
                        "remotePath": "",
                        "status": "waiting_validation",
                        "progress": 0,
                        "attempts": 0,
                        "error": "",
                        "nextRetryAt": 0,
                        "stableSince": 0,
                        "observedSize": 0,
                        "observedMtimeNs": 0,
                        "resume": {},
                        "discoveredAt": now_iso(),
                        "cleanup": {"state": "waiting_upload", "final": "kept"},
                    }
                    self.state["tasks"][task_id] = item
                    discovered += 1
                else:
                    item.update(
                        {
                            "date": str(task.get("productionDate") or task.get("date") or payload.get("productionDate") or payload.get("date") or ""),
                            "productionDate": str(task.get("productionDate") or task.get("date") or payload.get("productionDate") or payload.get("date") or ""),
                            "project": str(task.get("project") or ""),
                            "material": str(task.get("material") or ""),
                            "language": str(task.get("language") or ""),
                            "cleanupPolicyVersion": int(payload.get("cleanupPolicyVersion") or 0),
                            "batchId": str(payload.get("batchId") or ""),
                            "manifestId": str(payload.get("manifestId") or ""),
                            "attemptId": str(payload.get("attemptId") or ""),
                            "canonicalTaskId": str(
                                task.get("remotePipelineTaskId")
                                or payload.get("remotePipelineTaskId")
                                or item.get("canonicalTaskId")
                                or ""
                            ),
                            "expectedTaskCount": int(
                                task.get("expectedTaskCount")
                                or payload.get("expectedTaskCount")
                                or item.get("expectedTaskCount")
                                or 0
                            ),
                            "localPath": str(final_path.resolve()),
                            "sourceVideoPath": str(task.get("videoPath") or ""),
                            "temporaryOutputPath": str(
                                (capcut_status or {}).get("temporaryOutputPath")
                                or task.get("temporaryOutputPath")
                                or item.get("temporaryOutputPath")
                                or ""
                            ),
                            "postProcessWorkingPath": str(
                                (capcut_status or {}).get("postProcessWorkingPath")
                                or task.get("postProcessWorkingPath")
                                or item.get("postProcessWorkingPath")
                                or ""
                            ),
                            "preparedSamplePath": str(
                                (capcut_status or {}).get("preparedSamplePath")
                                or task.get("preparedSamplePath")
                                or item.get("preparedSamplePath")
                                or ""
                            ),
                            "originalSamplePath": str(
                                (capcut_status or {}).get("originalSamplePath")
                                or task.get("originalSamplePath")
                                or item.get("originalSamplePath")
                                or ""
                            ),
                            "audioPath": str(task.get("audioPath") or ""),
                            "subtitleSourcePaths": list(task.get("subtitleSourcePaths") or []),
                        }
                    )
        return discovered

    @staticmethod
    def _capcut_status(task: dict) -> dict | None:
        status_file = Path(str(task.get("statusFile") or ""))
        if not status_file.is_file():
            return None
        payload = load_json(status_file, {})
        if not isinstance(payload, dict):
            return None
        task_id = str(task.get("taskId") or "")
        return next(
            (
                item
                for item in payload.get("tasks") or []
                if isinstance(item, dict)
                and str(item.get("taskId") or "") == task_id
            ),
            None,
        )

    @staticmethod
    def _capcut_completed(task: dict, status_item: dict | None = None) -> bool:
        if task.get("status") == "completed":
            return True
        return bool(status_item and str(status_item.get("state") or "") == "Completed")

    def _stability_ready(self, item: dict) -> bool:
        path = Path(item["localPath"])
        try:
            stat = path.stat()
        except OSError as exc:
            item["status"] = "failed"
            item["error"] = f"成片不存在：{exc}"
            item["errorCode"] = "UPLOAD_LOCAL_FINAL_MISSING"
            return False
        signature_changed = (
            int(item.get("observedSize") or 0) != stat.st_size
            or int(item.get("observedMtimeNs") or 0) != stat.st_mtime_ns
        )
        if signature_changed:
            item["observedSize"] = stat.st_size
            item["observedMtimeNs"] = stat.st_mtime_ns
            item["stableSince"] = int(time.time())
            item["stableChecks"] = 1
            item["status"] = "waiting_validation"
            item["error"] = ""
            return False
        item["stableChecks"] = int(item.get("stableChecks") or 1) + 1
        stable_seconds = int(self.config.get("stable_seconds", 30))
        stable_checks = int(self.config.get("stable_checks", 3))
        return (
            time.time() - int(item.get("stableSince") or int(time.time()))
            >= stable_seconds
            and int(item["stableChecks"]) >= stable_checks
        )

    def _progress(self, item: dict, progress: dict) -> None:
        item["resume"] = {
            key: progress[key]
            for key in ("uploadId", "size", "chunkSize", "blockMd5s", "uploadedParts")
            if key in progress
        }
        total_parts = len(progress.get("blockMd5s") or [])
        completed_parts = len(progress.get("uploadedParts") or [])
        item["progress"] = (
            min(99, round(completed_parts * 100 / total_parts)) if total_parts else 0
        )
        item["status"] = "uploading"
        item["error"] = ""
        self.state["account"] = str(progress.get("account") or self.state.get("account") or "")
        self.state["vipType"] = progress.get("vipType")
        self.state["chunkSizeMB"] = round(int(progress.get("chunkSize") or 0) / 1024 / 1024)
        self.save_state()

    @staticmethod
    def _same_path(left: str, right: str) -> bool:
        try:
            return Path(left).resolve() == Path(right).resolve()
        except OSError:
            return str(left).casefold() == str(right).casefold()

    def _recycle_explicit_file(self, item: dict, field: str, label: str, release_retained: bool = False) -> None:
        cleanup = item.setdefault("cleanup", {})
        key = f"{field}State"
        if cleanup.get(key) in {"recycled", "not_registered"}:
            return
        if cleanup.get(key) == "retained_weekly" and not release_retained:
            return
        raw_path = str(item.get(field) or "").strip()
        if not raw_path:
            cleanup[key] = "not_registered"
            return
        if self._same_path(raw_path, str(item.get("localPath") or "")):
            cleanup[key] = "blocked_same_as_final"
            return
        path = Path(raw_path).resolve()
        if not path.is_file():
            cleanup[key] = "already_missing"
            return
        try:
            recycle_file(path)
            cleanup[key] = "recycled"
            cleanup[f"{field}RecycledAt"] = now_iso()
        except Exception as exc:
            cleanup[key] = "failed"
            cleanup[f"{field}Error"] = f"{label}: {exc}"

    def _recycle_registered_subtitles(self, item: dict) -> None:
        cleanup = item.setdefault("cleanup", {})
        if cleanup.get("subtitleSourcesState") in {"recycled", "not_registered"}:
            return
        values = item.get("subtitleSourcePaths") or []
        if not isinstance(values, list) or not values:
            cleanup["subtitleSourcesState"] = "not_registered"
            return
        failures = []
        for raw_path in values:
            path = Path(str(raw_path or "").strip()).resolve()
            if not path.is_file():
                continue
            if self._same_path(str(path), str(item.get("localPath") or "")):
                failures.append("subtitle path is final MP4")
                continue
            try:
                recycle_file(path)
            except Exception as exc:
                failures.append(str(exc))
        if failures:
            cleanup["subtitleSourcesState"] = "failed"
            cleanup["subtitleSourcesError"] = "; ".join(failures[:3])
        else:
            cleanup["subtitleSourcesState"] = "recycled"
            cleanup["subtitleSourcesRecycledAt"] = now_iso()

    def _entity_audio_groups_ready(self) -> list[list[dict]]:
        groups: dict[tuple[str, str], list[dict]] = {}
        for item in self.state["tasks"].values():
            if str(item.get("project") or "") != "ProjectB" or not self._cleanup_eligible(item):
                continue
            audio_path = str(item.get("audioPath") or "").strip()
            if audio_path:
                groups.setdefault((str(item.get("date") or ""), audio_path.casefold()), []).append(item)
        return [items for items in groups.values() if items and all(x.get("status") == "uploaded" for x in items)]

    @staticmethod
    def _date_key(value: str) -> tuple[int, int]:
        try:
            month, day = (int(part) for part in str(value).split(".", 1))
            return month, day
        except (TypeError, ValueError):
            return 0, 0

    def _cleanup_eligible(self, item: dict) -> bool:
        minimum = str(self.config.get("cleanup_from_date") or "").strip()
        return not minimum or self._date_key(str(item.get("date") or "")) >= self._date_key(minimum)

    def _weekly_audio_groups_ready(self) -> list[list[dict]]:
        """Release an old reusable audio only after a later weekly audio has produced an uploaded final."""
        groups: dict[tuple[str, str], list[dict]] = {}
        for item in self.state["tasks"].values():
            project = str(item.get("project") or "")
            audio_path = str(item.get("audioPath") or "").strip()
            if project == "ProjectB" or not audio_path or not self._cleanup_eligible(item):
                continue
            groups.setdefault((project, audio_path.casefold()), []).append(item)
        ready = []
        for (project, old_audio), old_group in groups.items():
            if not all(item.get("status") == "uploaded" for item in old_group):
                continue
            old_last_date = max(self._date_key(str(item.get("date") or "")) for item in old_group)
            replacement_ready = any(
                other_project == project
                and other_audio != old_audio
                and any(item.get("status") == "uploaded" for item in other_group)
                and max(self._date_key(str(item.get("date") or "")) for item in other_group) > old_last_date
                for (other_project, other_audio), other_group in groups.items()
            )
            if replacement_ready:
                ready.append(old_group)
        return ready

    def _is_protected_sync_path(self, path: Path) -> bool:
        resolved = path.resolve()
        configured = self.config.get("protected_sync_roots") or []
        for raw_root in configured:
            try:
                resolved.relative_to(Path(str(raw_root)).resolve())
                return True
            except (OSError, ValueError):
                pass
        folded_parts = [part.casefold() for part in resolved.parts]
        sync_markers = (
            "baidunetdisk",
            "baidusyncdisk",
            "百度网盘",
            "百度云同步",
            "百度同步盘",
        )
        return any(marker in part for part in folded_parts for marker in sync_markers)

    def _cloud_cleanup_confirmed(self, item: dict, client) -> bool:
        cleanup = item.setdefault("cleanup", {})
        if cleanup.get("cloudVerifiedAt") and int(cleanup.get("cloudVerifiedSize") or -1) >= 0:
            return True
        if item.get("status") != "uploaded" or not item.get("completedAt"):
            cleanup["state"] = "blocked_upload_not_completed"
            return False
        final_path = Path(str(item.get("localPath") or "")).resolve()
        if not final_path.is_file():
            cleanup["state"] = "blocked_local_final_missing"
            return False
        local_size = final_path.stat().st_size
        fs_id = str(item.get("fsId") or "").strip()
        if not fs_id or int(item.get("cloudSize") or -1) != local_size:
            cleanup["state"] = "blocked_cloud_size_unconfirmed"
            return False
        try:
            metadata = client.file_metadata(fs_id)
        except Exception as exc:
            cleanup["state"] = "blocked_cloud_lookup_failed"
            cleanup["cloudVerificationError"] = str(exc)
            return False
        if int(metadata.get("size") or -1) != local_size:
            cleanup["state"] = "blocked_cloud_size_mismatch"
            cleanup["cloudVerificationError"] = (
                f"云端 {metadata.get('size')} 字节，本地 {local_size} 字节"
            )
            return False
        remote_path = str(item.get("remotePath") or "")
        metadata_path = str(metadata.get("path") or "")
        if remote_path and metadata_path and remote_path != metadata_path:
            cleanup["state"] = "blocked_cloud_path_mismatch"
            cleanup["cloudVerificationError"] = "云端文件路径与本条任务不一致"
            return False
        cleanup["cloudVerifiedAt"] = now_iso()
        cleanup["cloudVerifiedSize"] = local_size
        cleanup["state"] = "cloud_verified"
        cleanup.pop("cloudVerificationError", None)
        return True

    def verify_historical_cloud_records(
        self,
        client,
        task_ids: list[str] | None = None,
        progress: Callable[[dict], None] | None = None,
    ) -> dict:
        """Verify legacy uploads without enabling cleanup or deleting local files."""
        requested = {
            str(value).strip() for value in (task_ids or []) if str(value).strip()
        }
        candidates = []
        for item in self.state["tasks"].values():
            task_id = str(item.get("taskId") or "").strip()
            cleanup = item.setdefault("cleanup", {})
            if requested and task_id not in requested:
                continue
            if item.get("status") != "uploaded":
                continue
            if self._cleanup_policy_enabled(item):
                continue
            if cleanup.get("cloudVerifiedAt"):
                continue
            candidates.append(item)

        result = {
            "total": len(candidates),
            "processed": 0,
            "verified": 0,
            "failed": 0,
            "skipped": 0,
            "failures": [],
        }
        for item in candidates:
            task_id = str(item.get("taskId") or "")
            cleanup = item.setdefault("cleanup", {})
            verification = {
                "state": "running",
                "startedAt": now_iso(),
                "taskId": task_id,
            }
            cleanup["historyVerification"] = verification
            error_code = ""
            error_message = ""
            local_size = -1
            final_path = Path(str(item.get("localPath") or "")).resolve()
            try:
                if not item.get("completedAt"):
                    error_code = "upload_not_completed"
                    error_message = "上传记录没有完成时间"
                elif not final_path.is_file():
                    error_code = "local_final_missing"
                    error_message = "本地正式成片不存在，无法进行大小对账"
                else:
                    local_size = final_path.stat().st_size
                    fs_id = str(item.get("fsId") or "").strip()
                    cloud_size = int(item.get("cloudSize") or -1)
                    if not fs_id:
                        error_code = "cloud_fs_id_missing"
                        error_message = "历史上传记录缺少百度云文件 ID"
                    elif cloud_size != local_size:
                        error_code = "recorded_size_mismatch"
                        error_message = (
                            f"上传记录 {cloud_size} 字节，本地 {local_size} 字节"
                        )
                    else:
                        metadata = client.file_metadata(fs_id)
                        metadata_size = int(metadata.get("size") or -1)
                        remote_path = str(item.get("remotePath") or "")
                        metadata_path = str(metadata.get("path") or "")
                        if metadata_size != local_size:
                            error_code = "cloud_size_mismatch"
                            error_message = (
                                f"云端 {metadata_size} 字节，本地 {local_size} 字节"
                            )
                        elif (
                            remote_path
                            and metadata_path
                            and remote_path != metadata_path
                        ):
                            error_code = "cloud_path_mismatch"
                            error_message = "云端文件路径与本条历史任务不一致"
            except Exception as exc:
                error_code = "cloud_lookup_failed"
                error_message = str(exc)

            result["processed"] += 1
            verification["finishedAt"] = now_iso()
            if error_code:
                verification.update(
                    {
                        "state": "failed",
                        "errorCode": error_code,
                        "message": error_message,
                    }
                )
                result["failed"] += 1
                result["failures"].append(
                    {
                        "taskId": task_id,
                        "errorCode": error_code,
                        "message": error_message,
                    }
                )
            else:
                verified_at = now_iso()
                cleanup["cloudVerifiedAt"] = verified_at
                cleanup["cloudVerifiedSize"] = local_size
                cleanup["state"] = "legacy_retained"
                cleanup["reason"] = "cleanup_policy_v2_not_registered"
                cleanup.pop("cloudVerificationError", None)
                verification.update(
                    {
                        "state": "verified",
                        "verifiedAt": verified_at,
                        "verifiedSize": local_size,
                    }
                )
                result["verified"] += 1
            self.save_state()
            if progress:
                progress(dict(result))
        return result

    def process_historical_cloud_verification_request(self) -> dict | None:
        request = load_json(self.historical_verify_request_path, {})
        if not isinstance(request, dict):
            return None
        command_id = str(request.get("commandId") or "").strip()
        if not command_id:
            return None
        existing = load_json(self.historical_verify_status_path, {})
        if (
            isinstance(existing, dict)
            and str(existing.get("commandId") or "") == command_id
            and str(existing.get("state") or "")
            in {"completed", "partial", "blocked", "failed"}
        ):
            return existing

        status = {
            "commandId": command_id,
            "state": "requested",
            "requestedAt": str(request.get("requestedAt") or now_iso()),
            "updatedAt": now_iso(),
            "total": 0,
            "processed": 0,
            "verified": 0,
            "failed": 0,
            "message": "历史百度云核验命令已接收",
        }
        atomic_write_json(self.historical_verify_status_path, status)
        if not self.state.get("configured") or not self.state.get("authorized"):
            status.update(
                {
                    "state": "blocked",
                    "updatedAt": now_iso(),
                    "message": "百度网盘尚未配置或登录已失效，历史核验未执行",
                }
            )
            atomic_write_json(self.historical_verify_status_path, status)
            return status

        status.update(
            {
                "state": "running",
                "startedAt": now_iso(),
                "updatedAt": now_iso(),
                "message": "正在逐条查询百度云文件并核对路径和大小",
            }
        )
        atomic_write_json(self.historical_verify_status_path, status)

        def update_progress(result: dict) -> None:
            status.update(
                {
                    key: result.get(key, status.get(key))
                    for key in ("total", "processed", "verified", "failed")
                }
            )
            status["updatedAt"] = now_iso()
            atomic_write_json(self.historical_verify_status_path, status)

        try:
            client = self.client_factory(
                self.secret_store,
                int(self.config.get("request_timeout_seconds") or 90),
            )
            result = self.verify_historical_cloud_records(
                client,
                task_ids=request.get("taskIds")
                if isinstance(request.get("taskIds"), list)
                else None,
                progress=update_progress,
            )
            status.update(result)
            status["state"] = "completed" if not result["failed"] else "partial"
            status["finishedAt"] = now_iso()
            status["updatedAt"] = now_iso()
            status["message"] = (
                f"历史核验完成：通过 {result['verified']} 条，"
                f"失败 {result['failed']} 条；未上传、未删除任何文件"
            )
        except Exception as exc:
            status.update(
                {
                    "state": "failed",
                    "finishedAt": now_iso(),
                    "updatedAt": now_iso(),
                    "message": f"历史百度云核验失败：{exc}",
                }
            )
        atomic_write_json(self.historical_verify_status_path, status)
        return status

    @staticmethod
    def _cleanup_policy_enabled(item: dict) -> bool:
        return int(item.get("cleanupPolicyVersion") or 0) >= 2

    def _protected_task_paths(self, item: dict) -> list[Path]:
        protected = []
        for field in ("audioPath", "originalSamplePath"):
            raw = str(item.get(field) or "").strip()
            if raw:
                protected.append(Path(raw).resolve())
        return protected

    def _preflight_exclusive_cleanup(self, item: dict) -> tuple[list[tuple[str, Path]], list[str]]:
        """Resolve registered task files without deleting anything."""
        fields = (
            "sourceVideoPath",
            "temporaryOutputPath",
            "postProcessWorkingPath",
            "preparedSamplePath",
            "localPath",
        )
        final_raw = str(item.get("localPath") or "").strip()
        final_parent = Path(final_raw).resolve().parent if final_raw else None
        protected = self._protected_task_paths(item)
        candidates: list[tuple[str, Path]] = []
        blockers: list[str] = []
        seen: set[str] = set()
        for field in fields:
            raw = str(item.get(field) or "").strip()
            if not raw:
                continue
            try:
                path = Path(raw).resolve()
            except OSError:
                blockers.append(f"{field}:invalid_path")
                continue
            folded = str(path).casefold()
            if folded in seen:
                continue
            seen.add(folded)
            if any(self._same_path(str(path), str(value)) for value in protected):
                blockers.append(f"{field}:permanent_keep")
                continue
            if self._is_protected_sync_path(path):
                blockers.append(f"{field}:baidu_sync_directory")
                continue
            if field in {"temporaryOutputPath", "postProcessWorkingPath", "localPath"}:
                if final_parent is None or path.parent != final_parent:
                    blockers.append(f"{field}:outside_registered_output_root")
                    continue
            if field == "preparedSamplePath" and path.parent.name.casefold() != "prepared-samples":
                blockers.append(f"{field}:not_task_prepared_sample")
                continue
            candidates.append((field, path))
        return candidates, blockers

    def _delete_registered_candidates(self, item: dict, candidates: list[tuple[str, Path]]) -> bool:
        cleanup = item.setdefault("cleanup", {})
        # Keep the local final until every other registered file was removed.
        ordered = sorted(candidates, key=lambda row: row[0] == "localPath")
        for field, path in ordered:
            state_key = f"{field}State"
            if cleanup.get(state_key) in {
                "permanently_deleted",
                "already_missing",
                "not_registered",
            }:
                continue
            if not path.is_file():
                cleanup[state_key] = "already_missing"
                continue
            try:
                path.unlink()
                cleanup[state_key] = "permanently_deleted"
                cleanup[f"{field}DeletedAt"] = now_iso()
            except Exception as exc:
                cleanup[state_key] = "failed"
                cleanup[f"{field}Error"] = str(exc)
                return False
        return True

    def _cleanup_task_exclusive_files(self, item: dict, client) -> None:
        cleanup = item.setdefault("cleanup", {})
        cleanup["audioPathState"] = "permanent_keep"
        cleanup["originalSamplePathState"] = "permanent_keep"
        cleanup["publicAssetsState"] = "permanent_keep"
        if not self._cleanup_policy_enabled(item):
            cleanup["state"] = "legacy_retained"
            cleanup["reason"] = "cleanup_policy_v2_not_registered"
            return
        if cleanup.get("state") == "cleanup_completed":
            return
        if not self._cloud_cleanup_confirmed(item, client):
            return
        candidates, blockers = self._preflight_exclusive_cleanup(item)
        if blockers:
            cleanup["state"] = "blocked_candidate_preflight"
            cleanup["blockedReasons"] = blockers
            return
        cleanup["state"] = "cleaning_task_files"
        cleanup.pop("blockedReasons", None)
        if not self._delete_registered_candidates(item, candidates):
            cleanup["state"] = "blocked_task_file_delete_failed"
            return
        cleanup["taskFilesCleanedAt"] = now_iso()
        cleanup["state"] = "waiting_shared_release"

    def _shared_source_referenced_elsewhere(self, owner: str, path: Path) -> bool:
        for other in self.state["tasks"].values():
            if str(other.get("canonicalTaskId") or "") == owner:
                continue
            for raw in other.get("subtitleSourcePaths") or []:
                if raw and self._same_path(str(path), str(raw)):
                    if str(other.get("cleanup", {}).get("state") or "") != "cleanup_completed":
                        return True
        return False

    def _cleanup_shared_sources(self) -> None:
        groups: dict[str, list[dict]] = {}
        for item in self.state["tasks"].values():
            if not self._cleanup_policy_enabled(item):
                continue
            owner = str(item.get("canonicalTaskId") or "").strip()
            if owner:
                groups.setdefault(owner, []).append(item)
        for owner, items in groups.items():
            expected_values = {int(item.get("expectedTaskCount") or 0) for item in items}
            expected = next(iter(expected_values), 0) if len(expected_values) == 1 else 0
            unique_ids = {str(item.get("taskId") or "") for item in items}
            ready = (
                expected > 0
                and len(unique_ids) == expected
                and all(item.get("status") == "uploaded" for item in items)
                and all(item.get("cleanup", {}).get("cloudVerifiedAt") for item in items)
                and all(
                    item.get("cleanup", {}).get("state")
                    in {"waiting_shared_release", "cleanup_completed"}
                    for item in items
                )
            )
            if not ready:
                for item in items:
                    cleanup = item.setdefault("cleanup", {})
                    if cleanup.get("state") == "waiting_shared_release":
                        cleanup["sharedState"] = "waiting_project_uploads"
                continue

            sources: dict[str, Path] = {}
            blockers: list[str] = []
            for item in items:
                for raw in item.get("subtitleSourcePaths") or []:
                    if str(raw or "").strip():
                        path = Path(str(raw)).resolve()
                        sources[str(path).casefold()] = path
            for path in sources.values():
                if self._is_protected_sync_path(path):
                    blockers.append("shared_source:baidu_sync_directory")
                elif self._shared_source_referenced_elsewhere(owner, path):
                    blockers.append("shared_source:referenced_by_another_project")
                elif any(
                    self._same_path(str(path), str(protected))
                    for item in items
                    for protected in self._protected_task_paths(item)
                ):
                    blockers.append("shared_source:permanent_keep")
            if blockers:
                for item in items:
                    cleanup = item.setdefault("cleanup", {})
                    cleanup["state"] = "blocked_shared_preflight"
                    cleanup["blockedReasons"] = sorted(set(blockers))
                continue

            failure = ""
            for path in sources.values():
                if not path.is_file():
                    continue
                try:
                    path.unlink()
                except Exception as exc:
                    failure = str(exc)
                    break
            for item in items:
                cleanup = item.setdefault("cleanup", {})
                if failure:
                    cleanup["state"] = "blocked_shared_delete_failed"
                    cleanup["sharedDeleteError"] = failure
                else:
                    cleanup["subtitleSourcesState"] = (
                        "permanently_deleted" if sources else "not_registered"
                    )
                    cleanup["sharedFilesCleanedAt"] = now_iso()
                    cleanup["state"] = "cleanup_completed"
                    cleanup["completedAt"] = now_iso()

    def _apply_post_upload_cleanup(self, client=None) -> None:
        """Clean verified task files, then release shared project sources."""
        if not self.config.get("cleanup_after_upload", True):
            return
        if client is None:
            client = self.client_factory(
                self.secret_store,
                int(self.config.get("request_timeout_seconds") or 90),
            )
        for item in self.state["tasks"].values():
            if not self._cleanup_eligible(item):
                continue
            self._cleanup_task_exclusive_files(item, client)
        self._cleanup_shared_sources()

    def confirm_downloaded_and_recycle_final(self, task_ids: list[str]) -> dict:
        """Compatibility entry point; cleanup still requires a live cloud verification."""
        requested = {str(value) for value in task_ids if str(value).strip()}
        result = {"recycled": [], "skipped": [], "failed": []}
        self._apply_post_upload_cleanup()
        for task_id in requested:
            item = self.state["tasks"].get(task_id)
            if not item:
                result["skipped"].append(task_id)
                continue
            cleanup = item.setdefault("cleanup", {})
            if cleanup.get("state") == "cleanup_completed":
                result["recycled"].append(task_id)
            elif str(cleanup.get("state") or "").startswith("blocked_"):
                result["skipped"].append(task_id)
            else:
                result["failed"].append(task_id)
        self.save_state()
        return result

    def run_once(self) -> dict:
        self.config = load_config(self.config_path)
        credentials = self.secret_store.load()
        self.state["configured"] = bool(
            self.config.get("enabled")
            and credentials.get("app_key")
            and credentials.get("secret_key")
        )
        self.state["authorized"] = bool(
            credentials.get("access_token") or credentials.get("refresh_token")
        )
        self.state["running"] = True
        self.discover()
        self.save_state()
        self.process_historical_cloud_verification_request()
        if not self.state["configured"] or not self.state["authorized"]:
            return self.state

        # A task can have accumulated exponential backoff while an older
        # client did not know its project mapping (ExampleBrand was the first such
        # case).  Once the current build can deterministically construct the
        # remote path, that old configuration error is no longer a real
        # upload failure.  Re-arm only this exact error class; network,
        # credential, cloud-verification and local-media failures retain their
        # original retry history.
        for item in self.state["tasks"].values():
            if item.get("status") != "failed":
                continue
            old_error = str(item.get("error") or "")
            old_code = str(item.get("errorCode") or "")
            if old_code != "UPLOAD_PROJECT_UNSUPPORTED" and "不支持的项目" not in old_error:
                continue
            try:
                item["remotePath"] = remote_path_for(
                    item, str(self.config["app_name"])
                )
            except ValueError:
                continue
            item.update(
                {
                    "status": "waiting_validation",
                    "progress": 0,
                    "error": "",
                    "errorCode": "",
                    "attempts": 0,
                    "nextRetryAt": 0,
                    "resume": {},
                    "configurationMigratedAt": now_iso(),
                }
            )
        self.save_state()

        # Recoverable post-upload cleanup: only runs for tasks already marked uploaded.
        self._apply_post_upload_cleanup()
        self.save_state()

        now = int(time.time())
        candidates = sorted(
            self.state["tasks"].values(),
            key=lambda item: (str(item.get("date") or ""), str(item.get("taskId") or "")),
        )
        for item in candidates:
            if item.get("status") == "uploaded":
                current_name = str(item.get("remotePath") or "").rsplit("/", 1)[-1]
                expected_name = Path(str(item.get("localPath") or "")).name
                if (
                    current_name.startswith("未命名")
                    and expected_name.lower().endswith(".mp4")
                ):
                    try:
                        client = self.client_factory(
                            self.secret_store,
                            int(self.config.get("request_timeout_seconds") or 90),
                        )
                        item["remotePath"] = client.rename_file(
                            str(item["remotePath"]),
                            expected_name,
                        )
                        item["renameError"] = ""
                        item["renamedAt"] = now_iso()
                        self.save_state()
                    except Exception as exc:
                        item["renameError"] = str(exc)
                        self.save_state()
                # Post-upload cleanup was already reconciled once before this
                # candidate loop. Re-running the whole task ledger for every
                # historical uploaded row turns a scan into O(n²) work and can
                # starve a new batch behind years of history.
                continue
            if int(item.get("nextRetryAt") or 0) > now:
                continue
            path = Path(str(item.get("localPath") or ""))
            valid, error = is_final_mp4(path)
            if not valid:
                item["status"] = "failed"
                item["error"] = error
                item["errorCode"] = "UPLOAD_LOCAL_FINAL_INVALID"
                item["nextRetryAt"] = now + int(self.config["retry_max_seconds"])
                self.save_state()
                continue
            if not self._stability_ready(item):
                self.save_state()
                continue
            try:
                remote_path = remote_path_for(item, str(self.config["app_name"]))
                item["remotePath"] = remote_path
                item["status"] = "uploading"
                item["startedAt"] = now_iso()
                self.save_state()
                client = self.client_factory(
                    self.secret_store,
                    int(self.config.get("request_timeout_seconds") or 90),
                )
                result = client.upload_file(
                    path,
                    remote_path,
                    progress=lambda value: self._progress(item, value),
                    resume=item.get("resume") or {},
                )
                item.update(
                    {
                        "status": "uploaded",
                        "progress": 100,
                        "error": "",
                        "completedAt": now_iso(),
                        "fsId": result["fsId"],
                        "remotePath": result["path"],
                        "cloudSize": result["size"],
                        "cloudMd5": result.get("md5", ""),
                        "resume": {},
                        "nextRetryAt": 0,
                    }
                )
                self.state["account"] = result.get("account", self.state.get("account", ""))
                self.state["vipType"] = result.get("vipType")
                self.state["chunkSizeMB"] = round(
                    int(result.get("chunkSize") or 0) / 1024 / 1024
                )
                if self._cleanup_policy_enabled(item):
                    self._apply_post_upload_cleanup(client)
                else:
                    # Legacy/current manually-cleaned jobs still need a live
                    # cloud query so Dashboard can safely offer them for manual
                    # recycling.  Verification must never opt them into the
                    # automatic deletion policy.
                    if self._cloud_cleanup_confirmed(item, client):
                        cleanup = item.setdefault("cleanup", {})
                        cleanup["state"] = "legacy_retained"
                        cleanup["reason"] = "cleanup_policy_v2_not_registered"
            except Exception as exc:
                attempts = int(item.get("attempts") or 0) + 1
                delay = min(
                    int(self.config["retry_max_seconds"]),
                    int(self.config["retry_base_seconds"]) * (2 ** min(attempts - 1, 6)),
                )
                item.update(
                    {
                        "status": "failed",
                        "error": str(exc),
                        "errorCode": upload_failure_code(exc),
                        "attempts": attempts,
                        "nextRetryAt": int(time.time()) + delay,
                        "failedAt": now_iso(),
                    }
                )
            self.save_state()
            break
        return self.state

    def summary(self) -> dict:
        tasks = list((self.state.get("tasks") or {}).values())
        completed = sum(1 for item in tasks if item.get("status") == "uploaded")
        failed = sum(1 for item in tasks if item.get("status") == "failed")
        uploading = sum(1 for item in tasks if item.get("status") == "uploading")
        waiting = len(tasks) - completed - failed - uploading
        current = next(
            (item for item in tasks if item.get("status") == "uploading"),
            next((item for item in tasks if item.get("status") == "failed"), None),
        )
        return {
            "configured": bool(self.state.get("configured")),
            "authorized": bool(self.state.get("authorized")),
            "running": bool(self.state.get("running")),
            "total": len(tasks),
            "completed": completed,
            "failed": failed,
            "uploading": uploading,
            "waiting": waiting,
            "progress": round(
                sum(int(item.get("progress") or 0) for item in tasks) / max(len(tasks), 1)
            ),
            "current": current or {},
            "updatedAt": self.state.get("updatedAt", ""),
            "account": self.state.get("account", ""),
            "vipType": self.state.get("vipType"),
            "chunkSizeMB": self.state.get("chunkSizeMB", 0),
        }
