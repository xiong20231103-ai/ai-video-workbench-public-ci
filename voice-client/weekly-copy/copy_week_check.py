"""Validate one week's voice-over copy library before it may be activated.

Usage:
    python copy_week_check.py --week 2026-W44 --csv 净白板.csv --csv 碳岩板.csv ... [--existing copy_library.csv ...] [--merged week.csv] [--json report.json]

Exit code 0 = accepted, 1 = rejected (errors listed), 2 = cannot read input.
Only the standard library is used so the same file runs on the office and home PCs.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

RULES_FILE = Path(__file__).with_name("rules.json")
WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s+")


def load_rules(path: Path = RULES_FILE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def languages_for(rules: dict, material: str) -> list[str]:
    extra = [lang for lang, mats in rules["scopedLanguages"].items() if material in mats]
    return list(rules["baseLanguages"]) + extra


def week_tag(week: str) -> str:
    match = WEEK_RE.match(week)
    if not match:
        raise ValueError(f"周编号格式应为 2026-W44：{week}")
    return f"{match.group(1)}W{match.group(2)}"


def first_sentence(text: str) -> str:
    return SENTENCE_END.split(text.strip(), maxsplit=1)[0].strip().lower()


MISLEADING_HOOK = re.compile(
    r"\b(?:ever seen|this is)\s+(?:a |an )?"
    r"(?:(?:real|genuine|natural)\s+(?:stone|marble|wood\w*)|glass(?:\s+mirror)?|solid\s+wood|ceramic\s+tiles?)\s+that\b",
    re.IGNORECASE,
)


def sentences(text: str) -> list[str]:
    return [part.strip() for part in SENTENCE_END.split(text.strip()) if part.strip()]


def quality_errors(english: dict[tuple[str, str], dict], rules: dict) -> list[str]:
    """Catch the W44 trial's problems: misleading hooks, invented claims, copy-paste repetition."""
    errors: list[str] = []
    ending = rules["fixedEndings"]["英语"]
    limits = rules.get("repetition", {})
    forbidden = rules.get("forbiddenClaims", {})
    sentence_uses: Counter = Counter()
    openings: dict[str, set] = defaultdict(set)
    hook_endings: dict[str, set] = defaultdict(set)
    for (material, _number), row in english.items():
        text = row["text"]
        parts = [part for part in sentences(text) if part != ending]
        if parts and MISLEADING_HOOK.search(parts[0]):
            errors.append(f"{row['copyId']}：钩子把产品说成了传统材料（{parts[0]}）")
        for owner, words in rules.get("exclusiveWords", {}).items():
            if owner == material:
                continue
            for word in words:
                if re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE):
                    errors.append(f"{row['copyId']}：「{word}」是{owner}独有的卖点，{material} 不能写")
        for claim in forbidden.get(material, []):
            if re.search(rf"\b{re.escape(claim)}\b", text, re.IGNORECASE):
                errors.append(f"{row['copyId']}：{material} 不允许写「{claim}」")
        intro = f"this is our {rules['materials'][material]['en'].lower()}."
        for part in parts:
            if part.lower() != intro:
                sentence_uses[part.lower()] += 1
        if parts:
            words = re.findall(r"[a-z']+", parts[0].lower())
            openings[" ".join(words[:4])].add(material)
            hook_endings[" ".join(words[-3:])].add(material)
    limit = int(limits.get("maxSameSentencePerWeek", 2))
    for sentence, count in sentence_uses.items():
        if count > limit:
            errors.append(f"同一句话本周用了 {count} 次（最多 {limit} 次）：{sentence}")
    across = int(limits.get("maxSameHookOpeningAcrossMaterials", 4))
    for label, groups in (("开头", openings), ("钩子结尾", hook_endings)):
        for phrase, used in groups.items():
            if phrase and len(used) > across:
                errors.append(f"{len(used)} 种材料的钩子{label}相同（最多 {across} 种）：{phrase}")
    return errors


def read_rows(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def check(rows: list[dict], header: list[str], week: str, rules: dict, existing: list[dict] | None = None) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    tag = week_tag(week)
    if header != rules["columns"]:
        errors.append(f"表头必须是 {','.join(rules['columns'])}，实际是 {','.join(header)}")
        return {"accepted": False, "errors": errors, "warnings": warnings, "rows": len(rows)}

    materials = rules["materials"]
    per = int(rules["perWeekPerMaterialLanguage"])
    id_re = re.compile(rf"^(?P<m>.+)-(?P<l>[^-]+)-{tag}-(?P<n>[1-{per}])$")
    counts: Counter = Counter()
    seen_ids: set[str] = set()
    english: dict[tuple[str, str], dict] = {}
    by_number: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)

    for index, row in enumerate(rows, start=2):
        where = f"第 {index} 行 {row.get('copyId') or ''}"
        material, language = (row.get("material") or "").strip(), (row.get("language") or "").strip()
        title, text = (row.get("title") or "").strip(), (row.get("text") or "").strip()
        if material not in materials:
            errors.append(f"{where}：未知材料 {material}")
            continue
        if language not in languages_for(rules, material):
            errors.append(f"{where}：{material} 不应有 {language}")
            continue
        if not title or not text:
            errors.append(f"{where}：标题或正文为空")
            continue
        if (row.get("source") or "").strip() != f"weekly-{week}":
            errors.append(f"{where}：source 应为 weekly-{week}")
        copy_id = (row.get("copyId") or "").strip()
        match = id_re.match(copy_id)
        if not match or match.group("m") != material or match.group("l") != language:
            errors.append(f"{where}：copyId 应为 {material}-{language}-{tag}-1～{per}")
            continue
        if copy_id in seen_ids:
            errors.append(f"{where}：copyId 重复")
            continue
        seen_ids.add(copy_id)
        counts[(material, language)] += 1
        by_number[(material, match.group("n"))][language] = row

        ending = rules["fixedEndings"][language]
        if not text.endswith(ending):
            errors.append(f"{where}：结尾必须一字不差为「{ending}」")
        elif text.count(ending) != 1:
            errors.append(f"{where}：固定结尾重复出现")
        if len(title) > 140:
            warnings.append(f"{where}：标题过长（{len(title)} 字符）")

        if language == "英语":
            english[(material, match.group("n"))] = row
            info = materials[material]
            if info["en"] not in text:
                warnings.append(f"{where}：正文没有提到产品英文名 {info['en']}（旧文案也有少数这样写，仅提醒）")
            words = len(re.findall(r"[A-Za-z0-9'’-]+", text))
            if not rules["englishWords"]["min"] <= words <= rules["englishWords"]["max"]:
                errors.append(f"{where}：英语 {words} 词，应在 {rules['englishWords']['min']}～{rules['englishWords']['max']} 之间")
            lowered = text.lower()
            for banned in rules["bannedEnglish"]:
                if re.search(rf"(?<![A-Za-z]){re.escape(banned.lower())}(?![A-Za-z])", lowered):
                    errors.append(f"{where}：含禁用词「{banned}」")
            if re.search(r"\b20\s*years?\b", lowered) and material != "塑木地板":
                errors.append(f"{where}：「20 年以上寿命」只能用于塑木地板")
            if not text.lower().startswith(title.lower()[:15]):
                warnings.append(f"{where}：正文开头与标题不一致")

    for material in materials:
        for language in languages_for(rules, material):
            if counts[(material, language)] != per:
                errors.append(f"{material}/{language}：应为 {per} 条，实际 {counts[(material, language)]} 条")

    old_texts = {(row.get("text") or "").strip() for row in existing or []}
    old_hooks = {first_sentence(row.get("text") or "") for row in existing or [] if row.get("language") == "英语"}
    for material in materials:
        hooks = [first_sentence(english[(material, str(n))]["text"]) for n in range(1, per + 1) if (material, str(n)) in english]
        duplicated = [hook for hook, n in Counter(hooks).items() if n > 1]
        if duplicated:
            errors.append(f"{material}：本周英语开头句重复：{duplicated[0]}")
        for hook in hooks:
            if hook in old_hooks:
                errors.append(f"{material}：开头句与旧文案相同：{hook}")
    for row in rows:
        if (row.get("text") or "").strip() in old_texts:
            errors.append(f"{row.get('copyId')}：正文与旧文案完全相同")

    errors.extend(quality_errors(english, rules))

    intros = rules.get("productIntroByLanguage", {})
    for row in rows:
        language, material = row.get("language"), row.get("material")
        intro = intros.get(material, {}).get(language, "")
        english_name = materials.get(material, {}).get("en", "")
        if language != "英语" and intro and english_name and english_name not in intro and english_name in (row.get("text") or ""):
            errors.append(f"{row.get('copyId')}：产品名未本地化，应参照「{intro}」")

    ratio = rules["otherLanguageLengthRatio"]
    for (material, number), versions in by_number.items():
        source = versions.get("英语")
        if not source:
            continue
        body = len(source["text"]) - len(rules["fixedEndings"]["英语"])
        for language, row in versions.items():
            if language == "英语":
                continue
            if row["text"].strip() == source["text"].strip():
                errors.append(f"{row['copyId']}：未翻译（与英语相同）")
                continue
            other = len(row["text"]) - len(rules["fixedEndings"][language])
            if body > 0 and not ratio["min"] <= other / body <= ratio["max"] and language != "韩语":
                warnings.append(f"{row['copyId']}：长度与英语原文差距较大（{other}/{body}）")

    return {"accepted": not errors, "errors": errors, "warnings": warnings, "rows": len(rows)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", required=True)
    parser.add_argument("--csv", required=True, type=Path, action="append", help="可重复：每种材料一个文件，自动合并")
    parser.add_argument("--existing", action="append", type=Path, default=[])
    parser.add_argument("--json", type=Path)
    parser.add_argument("--merged", type=Path, help="校验通过时，把合并后的全周文案写到这个 CSV")
    args = parser.parse_args(argv)
    try:
        rules = load_rules()
        header, rows = [], []
        for path in args.csv:
            part_header, part_rows = read_rows(path)
            if header and part_header != header:
                raise ValueError(f"{path} 的表头与其他文件不一致")
            header = part_header
            rows.extend(part_rows)
        existing = [row for path in args.existing for row in read_rows(path)[1]]
    except (OSError, ValueError, csv.Error) as exc:
        print(f"无法读取：{exc}", file=sys.stderr)
        return 2
    report = check(rows, header, args.week, rules, existing)
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.merged and report["accepted"]:
        with args.merged.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rules["columns"])
            writer.writeheader()
            writer.writerows({key: row.get(key, "") for key in rules["columns"]} for row in rows)
    print(f"{'通过' if report['accepted'] else '不通过'}：{report['rows']} 行，{len(report['errors'])} 个错误，{len(report['warnings'])} 个提醒")
    for line in report["errors"][:50]:
        print("  错误：" + line)
    for line in report["warnings"][:20]:
        print("  提醒：" + line)
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
