from __future__ import annotations

from collections import defaultdict
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qs, urlsplit


CONTRACT_VERSION = 2
RUN_GROUP_SCHEMA_VERSION = 1
FORMAL_BATCH_TOTAL = 79

# These are project-level language totals only.  They are expanded once for
# the whole submitted task box and then sliced across its material tasks.
# Downstream tools must never treat these numbers as a per-material target.
PROJECT_LANGUAGE_TOTALS = {
    "ProjectA": (("es", 6), ("de", 4), ("en", 2), ("id", 2), ("fr", 2),
             ("pt", 2), ("it", 2), ("ru", 2), ("tr", 2)),
    "ProjectB": (("en", 13),),
    "ProjectC": (("it", 2), ("en", 2), ("de", 2), ("ru", 2)),
    "ProjectD": (("es", 4), ("pt", 2), ("id", 2), ("de", 4),
               ("it", 6), ("ko", 2), ("ru", 2), ("en", 4)),
    "SPC地板": (("en", 1), ("de", 1), ("it", 1), ("ru", 1)),
    "SPC墙板": (("en", 1), ("de", 1), ("it", 1), ("ru", 1)),
}

# Languages whose outputs for one project must all land in a single material
# row of a task box.
SINGLE_MATERIAL_LANGUAGES = ("ko",)

PROJECT_EXPECTED_TOTALS = {
    project: sum(int(count) for _code, count in totals)
    for project, totals in PROJECT_LANGUAGE_TOTALS.items()
}

_PROJECT_ALIASES = {
    "ProjectD": "ProjectD",
    "ProjectD项目": "ProjectD",
}


def normalize_project_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\ufeff", "").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    text = "".join(text.split())
    return _PROJECT_ALIASES.get(text, text)

DOUYIN_HOST_SUFFIXES = ("douyin.com", "iesdouyin.com")
VIDEO_ID_PATTERN = re.compile(r"(?<!\d)(\d{15,22})(?!\d)")

LANGUAGE_NAMES = {
    "en": "英语",
    "es": "西班牙语",
    "de": "德语",
    "id": "印尼语",
    "fr": "法语",
    "pt": "葡萄牙语",
    "it": "意大利语",
    "ru": "俄语",
    "tr": "土耳其语",
    "ko": "韩语",
}


def _task_requests_korean(task: dict[str, Any]) -> bool:
    """Detect Korean in both legacy task rows and explicit language plans."""
    language = str(task.get("language") or "").strip().lower()
    if language in {"ko", "korean", "韩语"}:
        return True
    plan = task.get("languagePlan")
    if not isinstance(plan, list):
        return False
    return any(
        isinstance(item, dict)
        and (str(item.get("code") or "").strip().lower() in {"ko", "korean"}
             or str(item.get("name") or "").strip() == "韩语")
        for item in plan
    )


def expected_count(task: dict[str, Any]) -> int:
    try:
        return max(1, int(task.get("expectedCount") or len(task.get("urls") or []) or 1))
    except (TypeError, ValueError):
        return max(1, len(task.get("urls") or []))


def canonical_video_identity(url: str) -> tuple[str, str]:
    """Return a stable video key and a user-facing validation error.

    Direct Douyin URLs are keyed by numeric video ID.  Short-share links that
    do not expose an ID are keyed by their normalized host/path so exact share
    duplicates are still rejected without following redirects at submit time.
    """
    raw = str(url or "").strip().rstrip(".,;，。；")
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "", "链接格式无效"
    host = (parts.hostname or "").lower().rstrip(".")
    if not parts.scheme or parts.scheme.lower() not in {"http", "https"} or not host:
        return "", "链接必须是 http 或 https 地址"
    if not any(host == suffix or host.endswith("." + suffix) for suffix in DOUYIN_HOST_SUFFIXES):
        return "", "不是受支持的抖音链接"
    query = parse_qs(parts.query)
    candidates = [parts.path, *(query.get("modal_id") or []), *(query.get("video_id") or [])]
    for candidate in candidates:
        match = VIDEO_ID_PATTERN.search(str(candidate or ""))
        if match:
            return f"douyin:{match.group(1)}", ""
    normalized_path = "/" + "/".join(segment for segment in parts.path.split("/") if segment)
    if normalized_path == "/":
        return "", "链接中没有可识别的视频地址"
    return f"douyin-url:{host}{normalized_path.lower()}", ""


def normalize_selected_projects(value: Any) -> tuple[str, ...]:
    """Normalize the explicit project scope carried by a new submission.

    An empty value means legacy/full-contract mode.  The tuple is stable so it
    can safely be included in payload hashes and status records.
    """
    if isinstance(value, str):
        # Accept comma- and line-separated values from the UI/import files.
        # The previous ``\\n`` pattern matched a literal backslash+n rather
        # than an actual newline, which could silently collapse a pasted
        # multi-project selection into one invalid project name.
        values = [part for part in re.split(r"[,，、\n]+", value) if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    result = []
    for item in values:
        project = normalize_project_name(item)
        if project and project not in result:
            result.append(project)
    return tuple(result)


def selected_project_expected_total(selected_projects: Any) -> int:
    selected = normalize_selected_projects(selected_projects)
    return sum(PROJECT_EXPECTED_TOTALS.get(project, 0) for project in selected)


def formal_batch_issues(
    tasks: list[dict[str, Any]],
    selected_projects: Any = None,
) -> list[str]:
    """Return deterministic submit-time problems for the requested scope.

    With no explicit scope this preserves the historical 79-item contract.
    With ``selected_projects`` only those projects are required, enabling a
    partial run without weakening row/link/identity validation.
    """
    issues: list[str] = []
    project_totals: dict[str, int] = defaultdict(int)
    explicit_scope = normalize_selected_projects(selected_projects)
    scoped_projects = set(explicit_scope) if explicit_scope else set(PROJECT_EXPECTED_TOTALS)
    if explicit_scope:
        unknown = [project for project in explicit_scope if project not in PROJECT_EXPECTED_TOTALS]
        if unknown:
            issues.append("选择的正式项目无效：" + "、".join(unknown))
        if not scoped_projects:
            issues.append("至少选择一个正式项目")
    dates: set[str] = set()
    seen_videos: dict[str, str] = {}
    for row_index, task in enumerate(tasks, start=1):
        project = normalize_project_name(task.get("project"))
        material = str(task.get("material") or "").strip()
        planned = expected_count(task)
        urls = [str(item).strip() for item in task.get("urls") or [] if str(item).strip()]
        label = f"第{row_index}项 {project or '未填写项目'}/{material or '未填写材料'}"
        if project not in PROJECT_EXPECTED_TOTALS:
            issues.append(f"{label}：项目不在正式任务清单中")
        elif project not in scoped_projects:
            issues.append(f"{label}：不属于本次选择的项目组合")
        else:
            project_totals[project] += planned
        if _task_requests_korean(task) and project != "ProjectD":
            issues.append(f"{label}：韩语只允许用于ProjectD项目")
        if not material:
            issues.append(f"{label}：材料不能为空")
        if len(urls) != planned:
            issues.append(f"{label}：有效链接 {len(urls)} 条，计划 {planned} 条")
        date_value = str(task.get("productionDate") or task.get("date") or "").strip()
        if date_value:
            dates.add(date_value)
        else:
            issues.append(f"{label}：生产日期不能为空")
        for url_index, url in enumerate(urls, start=1):
            identity, error = canonical_video_identity(url)
            if error:
                issues.append(f"{label}第{url_index}条：{error}")
                continue
            previous = seen_videos.get(identity)
            if previous:
                issues.append(f"{label}第{url_index}条：与{previous}重复")
            else:
                seen_videos[identity] = f"{label}"
    for project, expected in PROJECT_EXPECTED_TOTALS.items():
        if project not in scoped_projects:
            continue
        actual = project_totals.get(project, 0)
        if actual != expected:
            issues.append(f"{project}计划总数必须为 {expected}，当前为 {actual}")
    if len(dates) > 1:
        issues.append(f"同一任务箱只能使用一个生产日期，当前包含：{'、'.join(sorted(dates))}")
    total = sum(expected_count(task) for task in tasks)
    expected_total = selected_project_expected_total(explicit_scope) if explicit_scope else FORMAL_BATCH_TOTAL
    if total != expected_total:
        label = "本次选择项目组合" if explicit_scope else "正式任务箱"
        issues.append(f"{label}计划总数必须为 {expected_total}，当前为 {total}")
    return issues


def _expanded_project_languages(project: str) -> list[str]:
    totals = PROJECT_LANGUAGE_TOTALS.get(normalize_project_name(project), ())
    expanded: list[str] = []
    rounds = max((int(count) for _code, count in totals), default=0)
    for round_index in range(rounds):
        for code, count in totals:
            if round_index < int(count):
                expanded.append(code)
    return expanded


def _compress_language_slice(codes: list[str]) -> list[dict[str, Any]]:
    ordered: list[str] = []
    counts: dict[str, int] = defaultdict(int)
    for code in codes:
        if code not in counts:
            ordered.append(code)
        counts[code] += 1
    return [
        {
            "code": code,
            "name": LANGUAGE_NAMES.get(code, code),
            "audioCount": 1,
            "outputCount": counts[code],
        }
        for code in ordered
        if counts[code] > 0
    ]


def _keep_language_in_one_row(rows: list[list[str]], code: str) -> None:
    """Gather every ``code`` slot into one material row, in place.

    Slots are swapped with another language from the receiving row, so each
    row keeps its count and the project keeps its language totals.  The first
    row that already holds ``code`` and has room receives the rest.
    """
    holders = [index for index, codes in enumerate(rows) if code in codes]
    if len(holders) < 2:
        return
    total = sum(codes.count(code) for codes in rows)
    candidates = [index for index in holders if len(rows[index]) >= total]
    candidates += [
        index for index, codes in enumerate(rows)
        if index not in holders and len(codes) >= total
    ]
    if not candidates:
        return
    target = rows[candidates[0]]
    for index in holders:
        source = rows[index]
        if source is target:
            continue
        for position, value in enumerate(source):
            if value != code:
                continue
            swap = max(slot for slot, other in enumerate(target) if other != code)
            source[position], target[swap] = target[swap], code


def assign_batch_contracts(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach a durable material-level language/output contract to a task box.

    Material counts are always the values entered in the submitter.  The
    stable project language totals are used only to allocate those counts
    across material rows in task-box order.
    """
    offsets: dict[str, int] = defaultdict(int)
    project_totals: dict[str, int] = defaultdict(int)
    planned: list[tuple[dict[str, Any], str, list[str]]] = []
    for task in tasks:
        project = normalize_project_name(task.get("project"))
        task["project"] = project
        project_totals[project] += expected_count(task)

    for task in tasks:
        project = normalize_project_name(task.get("project"))
        task["project"] = project
        count = expected_count(task)
        expanded = _expanded_project_languages(project)
        start = offsets[project]
        stop = start + count
        if expanded and project_totals[project] == len(expanded):
            codes = expanded[start:stop]
        elif project == "ProjectB":
            codes = ["en"] * count
        elif expanded:
            # A non-standard/manual batch remains deterministic while the
            # formal task box still uses the exact maintained project totals.
            codes = [expanded[index % len(expanded)] for index in range(start, stop)]
        else:
            codes = ["en"] * count
        offsets[project] = stop
        planned.append((task, project, codes))

    for project in {project for _task, project, _codes in planned}:
        rows = [codes for _task, row_project, codes in planned if row_project == project]
        for code in SINGLE_MATERIAL_LANGUAGES:
            _keep_language_in_one_row(rows, code)

    for task, _project, codes in planned:
        count = len(codes)
        task["contractVersion"] = CONTRACT_VERSION
        task["expectedCount"] = count
        task["expectedCountSource"] = "submitter_material_task"
        task["languagePlan"] = _compress_language_slice(codes)
    return tasks


def language_plan(task: dict[str, Any]) -> list[dict[str, Any]]:
    explicit = task.get("languagePlan")
    if isinstance(explicit, list):
        cleaned = []
        for item in explicit:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            try:
                outputs = max(0, int(item.get("outputCount") or 0))
                audios = max(1, int(item.get("audioCount") or 1))
            except (TypeError, ValueError):
                continue
            if code and outputs:
                cleaned.append({
                    "code": code,
                    "name": str(item.get("name") or LANGUAGE_NAMES.get(code, code)),
                    "audioCount": audios,
                    "outputCount": outputs,
                })
        if cleaned and sum(item["outputCount"] for item in cleaned) == expected_count(task):
            return cleaned

    migrated = [dict(task)]
    assign_batch_contracts(migrated)
    return list(migrated[0].get("languagePlan") or [])


def validate_formal_batch(tasks: list[dict[str, Any]]) -> tuple[bool, str]:
    """Legacy total-only check retained for older workbench callers."""
    total = sum(expected_count(task) for task in tasks)
    if total != FORMAL_BATCH_TOTAL:
        return False, f"正式任务箱计划总数必须为 {FORMAL_BATCH_TOTAL}，当前为 {total}"
    return True, ""


def validate_formal_submission(
    tasks: list[dict[str, Any]], selected_projects: Any = None
) -> tuple[bool, str]:
    """Validate rows, projects, links and production date for one scope."""
    issues = formal_batch_issues(tasks, selected_projects)
    return (False, "\n".join(issues)) if issues else (True, "")
