"""每周四生成下周配音文案（公司电脑 OpenClaw 定时任务调用）。

流程按本目录 README.md：
  1. 每种材料用 make_prompt.py 生成提示词，交给 luna（openai/gpt-5.6-luna），只取回复里的 CSV；
  2. 单种材料先校验，不通过就把错误原文附在提示词后面重写；
  3. 全周合并预检：跨材料的错误（同一句话用太多次、太多材料用同一钩子开头）不写材料名，
     按材料顺序归给超出限额的材料重写；每种材料两个阶段合计最多重写 2 次；
  4. copy_week_check.py 正式合并校验（--existing 指向 ../data 下的 copy_library.csv 和 copy_library_legacy.csv）；
  5. 通过时在结果目录写 READY，由 Windows 端 send_week_to_home.ps1 用 Tailscale 发到家里；
  6. 有飞书配置文件时发飞书通知。这里不提交到家里的生产。

用法：
  python3 run_weekly_copy.py --runs-dir ~/.openclaw/workspace/weekly-copy/runs                 # 生成下一周（ISO 周）
  python3 run_weekly_copy.py --runs-dir ~/.openclaw/workspace/weekly-copy/runs --week 2026-W45 # 指定周
结果目录默认取环境变量 WEEKLY_COPY_RUNS，没有时为本目录下的 runs/（已在 .gitignore 中）。
已有 SENT（已发到家里）的周直接跳过，不会被重新生成覆盖；确需重写加 --force。
"""
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import hashlib
import hmac
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

KIT =Path(__file__).resolve().parent
DATA = KIT.parent / "data"
SOURCE_FILE = KIT.parent.parent / "SOURCE.txt"  # written when the kit is copied into OpenClaw
EXISTING = [DATA / "copy_library.csv", DATA / "copy_library_legacy.csv"]
FEISHU_CONFIG = Path.home() / ".openclaw" / "credentials" / "feishu-weekly-copy.json"
MODEL = "openai/gpt-5.6-luna"
MAX_REWRITES = 2
CALL_TIMEOUT = 1200
WORKERS = 3

sys.path.insert(0, str(KIT))
import copy_week_check as checker  # noqa: E402
import make_prompt  # noqa: E402


def next_week(today: dt.date | None = None) -> str:
    year, week, _ = ((today or dt.date.today()) + dt.timedelta(days=7)).isocalendar()
    return f"{year}-W{week:02d}"


def log(run_dir: Path, message: str) -> None:
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(line, flush=True)
    with (run_dir / "run.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def ask_luna(prompt: str) -> str:
    """One-shot model turn through OpenClaw; returns the reply text."""
    result = subprocess.run(
        ["openclaw", "infer", "model", "run", "--model", MODEL, "--json", "--prompt", prompt],
        capture_output=True, text=True, encoding="utf-8", timeout=CALL_TIMEOUT,
    )
    output = result.stdout + "\n" + result.stderr
    start = output.find('{\n  "ok"')
    if start < 0:
        raise RuntimeError(f"luna 没有返回 JSON（退出码 {result.returncode}）：{output[-500:]}")
    payload, _ = json.JSONDecoder().raw_decode(output[start:])
    if not payload.get("ok"):
        raise RuntimeError(f"luna 调用失败：{json.dumps(payload, ensure_ascii=False)[:500]}")
    return "".join(item.get("text") or "" for item in payload.get("outputs", []))


def extract_csv(reply: str, columns: list[str]) -> str:
    """Keep only the CSV: drop code fences and anything before the header line."""
    text = re.sub(r"^```[a-zA-Z]*\s*$", "", reply, flags=re.MULTILINE)
    header = ",".join(columns)
    start = text.find(header)
    if start < 0:
        raise ValueError(f"回复里找不到表头 {header}")
    return text[start:].strip() + "\n"


def check_material(csv_text: str, week: str, material: str, rules: dict, existing: list[dict]) -> dict:
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = [dict(row) for row in reader]
    scoped = {**rules, "materials": {material: rules["materials"][material]}}
    return checker.check(rows, list(reader.fieldnames or []), week, scoped, existing)


def rewrite_prompt(base: str, errors: list[str]) -> str:
    return (
        base
        + "\n## 上一次输出没有通过校验，错误原文如下\n"
        + "\n".join(f"- {line}" for line in errors)
        + "\n\n请逐条改正上面的错误，重新输出这种材料的完整 CSV（全部条目，不只改错的行），仍然只输出 CSV。\n"
    )


def run_material(week: str, material: str, rules: dict, existing: list[dict], run_dir: Path,
                 first_attempt: int = 0, errors: list[str] | None = None) -> dict:
    """Generate (or rewrite, from first_attempt on) one material; MAX_REWRITES counts across both phases."""
    attempts_dir = run_dir / "attempts"
    base = make_prompt.build(week, rules=rules, material=material)
    errors, warnings = errors or [], []
    prompt = rewrite_prompt(base, errors) if errors else base
    for attempt in range(first_attempt, MAX_REWRITES + 1):
        stem = attempts_dir / f"{material}-{attempt}"
        stem.with_suffix(".prompt.txt").write_text(prompt, encoding="utf-8")
        label = "生成" if attempt == 0 else f"第 {attempt} 次重写"
        log(run_dir, f"{material}：{label}")
        reply = ""
        for call in range(2):
            try:
                reply = ask_luna(prompt)
                break
            except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                log(run_dir, f"{material}：调用失败（第 {call + 1} 次）：{exc}")
                time.sleep(30)
        stem.with_suffix(".reply.txt").write_text(reply, encoding="utf-8")
        try:
            csv_text = extract_csv(reply, rules["columns"])
            report = check_material(csv_text, week, material, rules, existing)
        except (ValueError, csv.Error) as exc:
            csv_text, report = "", {"accepted": False, "errors": [str(exc)], "warnings": [], "rows": 0}
        errors, warnings = report["errors"], report["warnings"]
        stem.with_suffix(".errors.txt").write_text("\n".join(errors), encoding="utf-8")
        if csv_text:
            (run_dir / f"{material}.csv").write_text(csv_text, encoding="utf-8")
        log(run_dir, f"{material}：{report['rows']} 行，{len(errors)} 个错误，{len(warnings)} 个提醒")
        if report["accepted"]:
            return {"material": material, "accepted": True, "rewrites": attempt, "errors": [], "warnings": warnings}
        prompt = rewrite_prompt(base, errors)
    return {"material": material, "accepted": False, "rewrites": MAX_REWRITES, "errors": errors, "warnings": warnings}


def read_week(materials: list[str], run_dir: Path) -> tuple[list[str], list[dict]]:
    header, rows = [], []
    for material in materials:
        header, part = checker.read_rows(run_dir / f"{material}.csv")
        rows.extend(part)
    return header, rows


def blame(errors: list[str], rows: list[dict], rules: dict) -> dict[str, list[str]]:
    """Map merged-check errors to the materials that should be rewritten.

    Cross-material errors (same sentence too often, same hook opening/ending in too many materials)
    name no material: the earliest materials keep the phrase up to the limit, later ones rewrite."""
    order = list(rules["materials"])
    ending = rules["fixedEndings"]["英语"].lower()
    english: dict[str, list[list[str]]] = {name: [] for name in order}
    for row in sorted(rows, key=lambda r: r.get("copyId") or ""):
        if row.get("language") == "英语" and row.get("material") in english:
            parts = [p.lower() for p in checker.sentences(row.get("text") or "") if p.lower() != ending]
            english[row["material"]].append(parts)
    found: dict[str, list[str]] = {}
    for error in errors:
        plain = re.sub(r"^第 \d+ 行 ", "", error)
        direct = next((n for n in sorted(order, key=len, reverse=True)
                       if plain.startswith((n + "-", n + "：", n + "/"))), None)
        if direct:
            found.setdefault(direct, []).append(error)
            continue
        sentence = re.match(r"同一句话本周用了 \d+ 次（最多 (\d+) 次）：(.*)$", error)
        phrase = re.match(r"\d+ 种材料的钩子(.+?)相同（最多 (\d+) 种）：(.*)$", error)
        if sentence:
            limit, text, total = int(sentence.group(1)), sentence.group(2).strip(), 0
            for name in order:
                uses = sum(parts.count(text) for parts in english[name])
                total += uses
                if uses and total > limit:
                    found.setdefault(name, []).append(error + "（其他材料已在用这句话，本材料请换一种说法）")
        elif phrase:
            kind, limit, text = phrase.group(1), int(phrase.group(2)), phrase.group(3).strip()
            users = []
            for name in order:
                for parts in english[name]:
                    words = re.findall(r"[a-z']+", parts[0]) if parts else []
                    key = " ".join(words[:4]) if kind == "开头" else " ".join(words[-3:])
                    if key == text:
                        users.append(name)
                        break
            for name in users[limit:]:
                found.setdefault(name, []).append(error + f"（其他材料已在用这个{kind}，本材料的钩子请换一种说法）")
    return found


def final_check(week: str, materials: list[str], run_dir: Path) -> tuple[int, str]:
    command = [sys.executable, str(KIT / "copy_week_check.py"), "--week", week]
    for material in materials:
        command += ["--csv", str(run_dir / f"{material}.csv")]
    for path in EXISTING:
        command += ["--existing", str(path)]
    command += ["--merged", str(run_dir / f"weekly-{week}.csv"), "--json", str(run_dir / f"report-{week}.json")]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    return result.returncode, (result.stdout + result.stderr).strip()


def notify_feishu(text: str) -> str:
    """Feishu custom bot; webhook (and optional signing secret) live only in the local credentials file."""
    if not FEISHU_CONFIG.exists():
        return f"未发送：没有 {FEISHU_CONFIG}"
    config = json.loads(FEISHU_CONFIG.read_text(encoding="utf-8"))
    body: dict = {"msg_type": "text", "content": {"text": text}}
    if config.get("secret"):
        timestamp = str(int(time.time()))
        key = f"{timestamp}\n{config['secret']}".encode()
        body["timestamp"] = timestamp
        body["sign"] = base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()
    request = urllib.request.Request(
        config["webhook"], data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        answer = json.loads(response.read().decode() or "{}")
    ok = answer.get("code", answer.get("StatusCode", 0)) == 0
    return "已发送" if ok else f"发送失败：{answer}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--week", help="ISO 周，默认下一周")
    parser.add_argument("--material", action="append", help="只跑这几种材料（调试用，不做全周合并）")
    parser.add_argument("--runs-dir", type=Path, default=Path(os.environ.get("WEEKLY_COPY_RUNS") or KIT / "runs"))
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--force", action="store_true", help="已发到家里的周也重新生成")
    args = parser.parse_args(argv)

    week = args.week or next_week()
    checker.week_tag(week)
    rules = checker.load_rules()
    materials = args.material or list(rules["materials"])
    run_dir = args.runs_dir.expanduser() / week
    if (run_dir / "SENT").exists() and not args.force:
        print(f"{week} 已发到家里（{run_dir / 'SENT'}），不再重新生成；确需重写请加 --force")
        return 0
    if run_dir.exists():  # keep an earlier unsent run aside instead of mixing its files in
        run_dir.rename(run_dir.with_name(f"{week}-{dt.datetime.now():%Y%m%d-%H%M%S}"))
    (run_dir / "attempts").mkdir(parents=True)
    existing = [row for path in EXISTING for row in checker.read_rows(path)[1]]
    source = SOURCE_FILE.read_text(encoding="utf-8").strip() if SOURCE_FILE.exists() else "仓库工作副本"
    log(run_dir, f"开始 {week}，材料 {len(materials)} 种，模型 {MODEL}，脚本来源 {source}")

    with ThreadPoolExecutor(WORKERS) as pool:
        by_material = {r["material"]: r for r in pool.map(lambda m: run_material(week, m, rules, existing, run_dir), materials)}

    # Cross-material rules only show up once all materials are merged: rewrite the materials to blame.
    while not args.material and all(r["accepted"] for r in by_material.values()):
        header, rows = read_week(materials, run_dir)
        report = checker.check(rows, header, week, rules, existing)
        if report["accepted"]:
            break
        targets = blame(report["errors"], rows, rules)
        log(run_dir, f"合并预检：{len(report['errors'])} 个错误，归到 {('、'.join(targets) or '无法归到具体材料')}")
        retry = {m: e for m, e in targets.items() if by_material[m]["rewrites"] < MAX_REWRITES}
        for m in set(targets) - set(retry):
            by_material[m] = {**by_material[m], "accepted": False, "errors": targets[m]}
        if not retry:
            break
        with ThreadPoolExecutor(WORKERS) as pool:
            for r in pool.map(lambda m: run_material(week, m, rules, existing, run_dir,
                                                     by_material[m]["rewrites"] + 1, retry[m]), retry):
                by_material[r["material"]] = r
    results = [by_material[m] for m in materials]
    failed = [r for r in results if not r["accepted"]]

    lines = [f"配音文案 {week}（luna 生成）"]
    for r in results:
        state = "通过" if r["accepted"] else "不通过"
        lines.append(f"- {r['material']}：{state}，重写 {r['rewrites']} 次，提醒 {len(r['warnings'])} 条")
    if failed:
        lines.append("")
        lines.append(f"{len(failed)} 种材料重写 {MAX_REWRITES} 次后仍不通过，本周不换文案，家里继续用上一周。")
        for r in failed:
            lines += [f"{r['material']} 最后一次错误："] + [f"  {e}" for e in r["errors"][:8]]
        code = 1
    elif args.material:
        lines.append("（只跑了部分材料，未做全周合并）")
        code = 0
    else:
        code, output = final_check(week, materials, run_dir)
        log(run_dir, "合并校验：" + output.replace("\n", " | ")[:2000])
        lines.append("")
        if code == 0:
            lines.append(f"全周合并校验通过：{output.splitlines()[0]}")
            lines.append(f"结果在公司电脑 {run_dir}，随后由 Windows 发送任务用 Tailscale 发到家里；不提交到家里的生产。")
            (run_dir / "READY").write_text(f"weekly-{week}.csv\nreport-{week}.json\n", encoding="utf-8")
        else:
            lines.append("全周合并校验不通过，本周不换文案：")
            lines += output.splitlines()[:20]
    message = "\n".join(lines)
    (run_dir / "summary.txt").write_text(message + "\n", encoding="utf-8")
    log(run_dir, "结果：\n" + message)
    if not args.no_notify:
        try:
            log(run_dir, "飞书通知：" + notify_feishu(message))
        except Exception as exc:  # noqa: BLE001 - notification must not hide the run result
            log(run_dir, f"飞书通知失败：{exc}")
    return code


if __name__ == "__main__":
    sys.exit(main())
