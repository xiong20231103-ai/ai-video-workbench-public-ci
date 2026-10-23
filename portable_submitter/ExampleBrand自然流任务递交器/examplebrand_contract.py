"""Independent ExampleBrand natural-flow Reels contract.

This module is deliberately separate from the 79-item formal contract.  A
ExampleBrand batch is accepted only when it is explicitly marked with
``batchKind=examplebrand-natural-flow``; formal batches never consult this module.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import hashlib
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit


ExampleBrand_BATCH_KIND = "examplebrand-natural-flow"
ExampleBrand_PROJECT = "ExampleBrand"
ExampleBrand_TOTAL = 21
ExampleBrand_ENGLISH_TOTAL = 18
ExampleBrand_SPANISH_TOTAL = 3
ExampleBrand_ACCOUNTS = ("EN-01", "EN-02", "EN-03", "EN-04", "EN-05", "EN-06", "ES-01")
ExampleBrand_ACCOUNT_SLOTS = ExampleBrand_ACCOUNTS * 3
ExampleBrand_PRODUCTS = (
    "清淤泵", "潜污泵", "离心泵", "轴流泵", "粉碎格栅", "一体化预制泵站", "大型泵站", "MaterialPump",
)
ExampleBrand_PRODUCT_SLUGS = {
    "清淤泵": "dredging-pump", "潜污泵": "submersible-sewage-pump",
    "离心泵": "centrifugal-pump", "轴流泵": "axial-flow-pump",
    "粉碎格栅": "grinder-screen", "一体化预制泵站": "prefabricated-pump-station",
    "大型泵站": "large-pump-station", "MaterialPump": "mobile-pump-trailer",
}
# 用户确认的 ExampleBrand 素材复用关系；不使用 G:\AI素材 作为未知回退。
ExampleBrand_ASSET_DIRS = {
    "清淤泵": r"G:\潜污泵AI素材",
    "潜污泵": r"G:\潜污泵AI素材",
    "离心泵": r"G:\离心泵AI素材",
    "轴流泵": r"G:\轴流泵AI素材",
    "粉碎格栅": r"G:\粉碎型格栅AI素材",
    "一体化预制泵站": r"G:\一体化泵站AI素材",
    "大型泵站": r"G:\一体化泵站AI素材",
    "MaterialPump": r"G:\一体化泵车AI素材(1)",
}
ExampleBrand_VOICES = {"en": "chand", "es": "Farid Dieck"}
ExampleBrand_LANGUAGE_NAMES = {"en": "英语", "es": "西班牙语"}
_VIDEO_ID = re.compile(r"(?<!\d)(\d{15,22})(?!\d)")


def _safe(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-") or "item"


def canonical_video_identity(url: str) -> tuple[str, str]:
    raw = str(url or "").strip().rstrip(".,;，。；")
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "", "链接格式无效"
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme.lower() not in {"http", "https"} or not host:
        return "", "链接必须是 http 或 https 地址"
    if not (host == "douyin.com" or host.endswith(".douyin.com") or host.endswith(".iesdouyin.com")):
        return "", "不是受支持的抖音链接"
    query = parse_qs(parts.query)
    for candidate in (parts.path, *(query.get("modal_id") or []), *(query.get("video_id") or [])):
        match = _VIDEO_ID.search(str(candidate or ""))
        if match:
            return f"douyin:{match.group(1)}", ""
    path = "/" + "/".join(part for part in parts.path.split("/") if part)
    if path == "/":
        return "", "链接中没有可识别的视频地址"
    return f"douyin-url:{host}{path.lower()}", ""


def _output_filename(product: str, language: str, account: str, index: int, short_id: str) -> str:
    return "ExampleBrand_{product}_{language}_{account}_{index:02d}_{short}.mp4".format(
        product=ExampleBrand_PRODUCT_SLUGS[product],
        language=language,
        account=_safe(account),
        index=index,
        short=_safe(short_id)[:10],
    )


def validate_examplebrand_rows(rows: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    if not isinstance(rows, list) or not rows:
        return ["ExampleBrand任务至少需要一行产品"]
    seen_products: set[str] = set()
    seen_video_ids: dict[str, str] = {}
    total = 0
    for row_index, row in enumerate(rows, 1):
        product = str(row.get("product") or row.get("material") or "").strip()
        urls = [str(item).strip() for item in (row.get("urls") or []) if str(item).strip()]
        try:
            count = int(row.get("expectedCount") or row.get("count") or len(urls))
        except (TypeError, ValueError):
            count = 0
        label = f"第{row_index}行 {product or '未填写产品'}"
        if product not in ExampleBrand_PRODUCTS:
            issues.append(f"{label}：不是ExampleBrand支持的产品")
        elif product in seen_products:
            issues.append(f"{label}：产品重复；同一批次每种产品只能一行")
        else:
            seen_products.add(product)
        if count < 1:
            issues.append(f"{label}：数量必须大于0")
        if len(urls) != count:
            issues.append(f"{label}：链接 {len(urls)} 条，计划 {count} 条")
        total += max(0, count)
        for url_index, url in enumerate(urls, 1):
            identity, error = canonical_video_identity(url)
            if error:
                issues.append(f"{label}第{url_index}条：{error}")
            elif identity in seen_video_ids:
                issues.append(f"{label}第{url_index}条：与{seen_video_ids[identity]}重复")
            else:
                seen_video_ids[identity] = label
    if total != ExampleBrand_TOTAL:
        issues.append(f"ExampleBrand每批必须正好 {ExampleBrand_TOTAL} 条，当前为 {total} 条")
    return issues


def build_examplebrand_batch(
    rows: list[dict[str, Any]],
    *,
    production_date: str,
    batch_id: str,
    start_mode: str = "pending_confirmation",
    run_group_id: str | None = None,
) -> dict[str, Any]:
    """Build the immutable 21-output ExampleBrand task box sent to the receiver."""
    issues = validate_examplebrand_rows(rows)
    if not str(production_date or "").strip():
        issues.append("生产日期不能为空")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}", str(batch_id or "")):
        issues.append("batchId格式无效")
    if issues:
        raise ValueError("\n".join(issues))

    run_group = str(run_group_id or f"examplebrand-{batch_id}").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}", run_group):
        raise ValueError("runGroupId格式无效")
    tasks: list[dict[str, Any]] = []
    global_index = 0
    for row_index, source_row in enumerate(rows, 1):
        product = str(source_row.get("product") or source_row.get("material") or "").strip()
        urls = [str(item).strip() for item in source_row.get("urls") or [] if str(item).strip()]
        product_slug = ExampleBrand_PRODUCT_SLUGS[product]
        task_id = f"{batch_id}-{product_slug}"
        outputs: list[dict[str, Any]] = []
        language_counts: dict[str, int] = defaultdict(int)
        for local_index, url in enumerate(urls, 1):
            account = ExampleBrand_ACCOUNT_SLOTS[global_index]
            language = "es" if account == "ES-01" else "en"
            output_index = global_index + 1
            canonical_id = f"{task_id}:out-{local_index:02d}"
            outputs.append({
                "sourceUrl": url,
                "product": product,
                "language": language,
                "languageName": ExampleBrand_LANGUAGE_NAMES[language],
                "channelAccount": account,
                "outputIndex": output_index,
                "canonicalTaskId": canonical_id,
                "outputName": _output_filename(product, language, account, output_index, canonical_id),
            })
            language_counts[language] += 1
            global_index += 1
        language_plan = [
            {
                "code": language,
                "name": ExampleBrand_LANGUAGE_NAMES[language],
                "audioCount": 1,
                "outputCount": count,
                "voice": ExampleBrand_VOICES[language],
            }
            for language, count in (("en", language_counts.get("en", 0)), ("es", language_counts.get("es", 0)))
            if count
        ]
        tasks.append({
            "schemaVersion": 2,
            "batchKind": ExampleBrand_BATCH_KIND,
            "runGroupId": run_group,
            "selectedProjects": [ExampleBrand_PROJECT],
            "batchId": batch_id,
            "batchSequence": row_index,
            "batchSize": len(rows),
            "taskId": task_id,
            "canonicalTaskId": task_id,
            "status": "pending_confirmation",
            "createdAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "date": str(production_date).strip(),
            "productionDate": str(production_date).strip(),
            "project": ExampleBrand_PROJECT,
            "material": product,
            "product": product,
            "urls": urls,
            "expectedCount": len(urls),
            "languagePlan": language_plan,
            "outputPlan": outputs,
            "voicePlan": "create_new",
            "audioBatch": str(production_date).strip(),
            "pipelineProfile": {
                "kind": ExampleBrand_BATCH_KIND,
                "logo": "ExampleBrand",
                "finalFolder": "ExampleBrand",
                "remoteDirectoryRule": f"AI视频生产线/{production_date}/ExampleBrand",
                "outputDirectoryRule": f"E:/生成视频/{production_date}/ExampleBrand",
                "requiresExamplebrandAssets": True,
                "aiMaterialDir": ExampleBrand_ASSET_DIRS[product],
            },
            "source": "examplebrand_submitter",
        })
    summary = {
        "total": ExampleBrand_TOTAL,
        "english": ExampleBrand_ENGLISH_TOTAL,
        "spanish": ExampleBrand_SPANISH_TOTAL,
        "accounts": {account: ExampleBrand_ACCOUNT_SLOTS.count(account) for account in ExampleBrand_ACCOUNTS},
    }
    payload = {
        "schemaVersion": 2,
        "batchKind": ExampleBrand_BATCH_KIND,
        "runGroupId": run_group,
        "selectedProjects": [ExampleBrand_PROJECT],
        "batchId": batch_id,
        "productionDate": str(production_date).strip(),
        "startMode": start_mode,
        "tasks": tasks,
        "examplebrandSummary": summary,
    }
    payload["payloadHash"] = hashlib.sha256(
        __import__("json").dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


def validate_examplebrand_batch_payload(payload: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(payload, dict) or payload.get("batchKind") != ExampleBrand_BATCH_KIND:
        return False, "不是ExampleBrand任务箱"
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return False, "ExampleBrand任务箱缺少tasks"
    run_group = str(payload.get("runGroupId") or "").strip()
    if run_group and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}", run_group):
        return False, "runGroupId格式无效"
    rows = [{"product": item.get("product") or item.get("material"), "count": item.get("expectedCount"), "urls": item.get("urls") or []} for item in tasks]
    issues = validate_examplebrand_rows(rows)
    if str(payload.get("productionDate") or "").strip() == "":
        issues.append("生产日期不能为空")
    expected_accounts = {account: 3 for account in ExampleBrand_ACCOUNTS}
    actual_accounts: dict[str, int] = defaultdict(int)
    for task in tasks:
        for output in task.get("outputPlan") or []:
            actual_accounts[str(output.get("channelAccount") or "")] += 1
    if dict(actual_accounts) != expected_accounts:
        issues.append(f"账号分发必须为每个账号3条，当前为 {dict(actual_accounts)}")
    if sum(len(task.get("outputPlan") or []) for task in tasks) != ExampleBrand_TOTAL:
        issues.append("outputPlan必须恰好包含21条")
    return (False, "\n".join(issues)) if issues else (True, "")
