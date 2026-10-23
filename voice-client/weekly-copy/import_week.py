"""家里电脑：把公司电脑发来的周文案导入配音客户端（Windows 计划任务每天运行）。

公司电脑周四用 Tailscale 把 weekly-<周>.csv 发到家里，文件落在下载文件夹（重名时
Taildrop 会改成 "weekly-2026-W45 (1).csv"）。本脚本：
  1. 在 --inbox 里找 weekly-YYYY-Www*.csv，每周取最新的一份；
  2. 用 copy_week_check.py 的完整规则校验（--existing 为 ../data 下的两个文案库）；
  3. 通过才写入 ../data/weekly/weekly-<周>.csv 和 weekly-<周>.accepted.json（含 SHA-256）。
配音客户端每批开始时只启用"当前 ISO 周"的已导入文件，所以下周的文案周一才生效。
已经开始使用的本周文案不会被替换；过去的周不导入。不通过的文件只记日志，原文件不动。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent
DATA = KIT.parent / "data"
WEEKLY = DATA / "weekly"
EXISTING = [DATA / "copy_library.csv", DATA / "copy_library_legacy.csv"]
NAME_RE = re.compile(r"^weekly-(\d{4}-W\d{2})(?: \(\d+\))?\.csv$")

sys.path.insert(0, str(KIT))
import copy_week_check as checker  # noqa: E402


def iso_week(day: dt.date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def log(message: str, weekly: Path) -> None:
    weekly.mkdir(parents=True, exist_ok=True)
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(line, flush=True)
    with (weekly / "import.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def atomic_write(path: Path, data: bytes) -> None:
    temp = path.with_name(path.name + ".tmp")
    with temp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def candidates(inbox: Path) -> dict[str, Path]:
    newest: dict[str, Path] = {}
    for path in inbox.glob("weekly-*.csv"):
        match = NAME_RE.match(path.name)
        if match and path.is_file():
            week = match.group(1)
            if week not in newest or path.stat().st_mtime > newest[week].stat().st_mtime:
                newest[week] = path
    return newest


def import_week(week: str, source: Path, *, weekly: Path, today: dt.date, rules: dict, existing: list[dict]) -> str:
    raw = source.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    target = weekly / f"weekly-{week}.csv"
    record_path = weekly / f"weekly-{week}.accepted.json"
    rejected_path = weekly / f"weekly-{week}.rejected.json"
    current = iso_week(today)
    if week < current:
        return "skipped-past"
    if record_path.exists():
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if record.get("sha256") == sha:
            return "unchanged"
        if week == current:
            log(f"{week} 已在使用中，不替换为 {source.name}（{sha[:12]}）", weekly)
            return "refused-in-use"
    if rejected_path.exists() and json.loads(rejected_path.read_text(encoding="utf-8")).get("sha256") == sha:
        return "rejected-before"
    try:
        header, rows = checker.read_rows(source)
        report = checker.check(rows, header, week, rules, existing)
    except (ValueError, UnicodeError, OSError) as exc:
        report = {"accepted": False, "errors": [str(exc)], "warnings": [], "rows": 0}
    if not report["accepted"]:
        weekly.mkdir(parents=True, exist_ok=True)
        atomic_write(rejected_path, json.dumps({"week": week, "sha256": sha, "sourceFile": str(source),
                                                "errors": report["errors"][:50], "checkedAt": dt.datetime.now().isoformat(timespec="seconds")},
                                               ensure_ascii=False, indent=1).encode("utf-8"))
        log(f"{week} 校验不通过（{len(report['errors'])} 个错误），未导入：{source.name}；首条：{report['errors'][0] if report['errors'] else ''}", weekly)
        return "rejected"
    weekly.mkdir(parents=True, exist_ok=True)
    atomic_write(target, raw)  # the record below is what makes it usable
    atomic_write(record_path, json.dumps({"week": week, "sha256": sha, "rows": report["rows"], "sourceFile": str(source),
                                          "importedAt": dt.datetime.now().isoformat(timespec="seconds")},
                                         ensure_ascii=False, indent=1).encode("utf-8"))
    rejected_path.unlink(missing_ok=True)
    log(f"{week} 已导入 {report['rows']} 条（{source.name}，{sha[:12]}），{'本周立即生效' if week == current else '到该周周一生效'}", weekly)
    return "imported"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inbox", type=Path, required=True, help="Taildrop 收件的下载文件夹")
    parser.add_argument("--weekly-dir", type=Path, default=WEEKLY)
    args = parser.parse_args(argv)
    rules = checker.load_rules()
    existing = [row for path in EXISTING for row in checker.read_rows(path)[1]]
    today = dt.date.today()
    results = {week: import_week(week, path, weekly=args.weekly_dir, today=today, rules=rules, existing=existing)
               for week, path in sorted(candidates(args.inbox).items())}
    print(json.dumps(results, ensure_ascii=False))
    return 1 if "rejected" in results.values() else 0


if __name__ == "__main__":
    sys.exit(main())
