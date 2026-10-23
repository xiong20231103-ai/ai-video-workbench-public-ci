"""Build the writing prompt for one week from rules.json.

Usage: python make_prompt.py --week 2026-W44 [--material 净白板] > prompt.txt
The same rules file feeds copy_week_check.py, so the prompt and the checker never drift apart.
"""
from __future__ import annotations

import argparse
import sys

import copy_week_check as checker

LETTERS = "ABCDEFG"


def hooks_for_week(week: str) -> list[str]:
    """Six of the seven hook types; the one left out rotates every week."""
    number = int(checker.week_tag(week)[-2:])
    skipped = LETTERS[number % len(LETTERS)]
    return [letter for letter in LETTERS if letter != skipped]


def hook_variants(text: str) -> tuple[str, list[str]]:
    name, _, rest = text.partition("。")
    rest = rest.split("（", 1)[0].replace("可换说法：", "")
    return name, [part.strip() for part in rest.split(" / ") if part.strip()]


def assigned_phrasing(rules: dict, week: str, material: str, letter: str) -> str:
    """Rotate phrasings by material and week, because each material is written in its own call
    and cannot see what the others used."""
    _name, variants = hook_variants(rules["hooks"][letter])
    order = list(rules["materials"]).index(material)
    week_number = int(checker.week_tag(week)[-2:])
    return variants[(order + week_number) % len(variants)] if variants else ""


def build(week: str, rules: dict | None = None, material: str | None = None) -> str:
    full = rules or checker.load_rules()
    chosen = [material] if material else list(full["materials"])
    for name in chosen:
        if name not in full["materials"]:
            raise ValueError(f"未知材料：{name}")
    tag = checker.week_tag(week)
    hooks = hooks_for_week(week)
    repetition = full.get("repetition", {})
    lines = [
        f"你是外贸建材短视频的配音文案作者。请为 {week} 这一周写配音文案（" + "、".join(chosen) + "），输出 CSV。",
        "",
        "## 结构（每条 3～5 句）",
        "① 钩子：第一句，抓注意力，同时作为标题（title 列 = 这一句）。",
        "② 亮出产品：This is our [产品英文名]. 或 Our [产品英文名] ...",
        "③ 外观效果 + 实用好处：看起来像什么、比传统材料省什么事。",
        "④ 适用场景（可省）：只能从下面列出的场景里选。",
        "⑤ 固定结尾：见下表，必须一字不差、放在最后、只出现一次。",
        f"英语每条 {full['englishWords']['min']}～{full['englishWords']['max']} 词（约 15～20 秒）。",
        "",
        "## 写法要求",
        "- 卖点用自己的话自然地说，不要照抄下面材料表里的英文短语；同一个卖点在 6 条里要换不同说法。",
        f"- 同一句话在本周所有文案里最多出现 {repetition.get('maxSameSentencePerWeek', 2)} 次（产品介绍句和固定结尾除外）。",
        "- 场景句不要每条都写，6 条里最多 3 条写场景，且场景要换着用。",
        "- 产品不是传统材料：不能写成 real stone / glass mirror / solid wood that…；可以说 stone that can bend、a stone look、it looks like marble。",
        "- 句式里的 [占位] 换成本材料的内容；墙板写 wall，地板写 floor；没有对标材料时（如金属板）用 a plain wall / an ordinary finish 代替 [compareTo]。",
        "- 口语、简短、像真人在短视频里说话，不要堆砌形容词。",
    ]
    for name in chosen:
        info = full["materials"][name]
        facts = "；".join(info["benefits"] + info["exclusiveFacts"])
        compare = f"，对标 {info['compareTo']}" if info["compareTo"] else ""
        scenes = full.get("allowedScenes", {}).get(name, full.get("allowedScenes", {}).get("default", []))
        forbidden = full.get("forbiddenClaims", {}).get(name, [])
        lines += ["", f"## 材料：{name}（只能使用这里的事实，不得新增数据）",
                  f"- 英文名 {info['en']}{compare}",
                  f"- 外观：{info['look']}",
                  f"- 好处：{facts}",
                  f"- 可写的场景：{'、'.join(scenes)}"]
        others = [word for owner, words in full.get("exclusiveWords", {}).items() if owner != name for word in words]
        if forbidden or others:
            lines.append(f"- 禁止写：{'、'.join(forbidden + others)}（其中部分是其他材料独有的卖点，本材料没有）")
        lines.append("- 6 条依次使用的钩子与参考句式（照句式的意思写，不必逐字，换成本材料的内容）：")
        for number, letter in enumerate(hooks, start=1):
            kind, _ = hook_variants(full["hooks"][letter])
            lines.append(f"  {number}. {letter} {kind}：{assigned_phrasing(full, week, name, letter)}")
    lines += [
        "",
        "## 禁止",
        "- 不点名竞争对手，不说价格、折扣、包邮。",
        "- 不用绝对化或无法证明的说法：" + "、".join(full["bannedEnglish"]) + "。",
        "- 「service life of more than 20 years」只能用于塑木地板。",
        "- 不要照抄以前的文案，开头句必须是新的说法。",
        "",
        "## 语言与产品名",
        "先写英语，再逐条翻译成下列语言；同一编号的各语言是同一条文案的译文，意思一致、自然口语化。",
        "各语言介绍产品时，产品名必须沿用下面这句旧文案里的说法（不要换回英文名）：",
    ]
    intros = full.get("productIntroByLanguage", {})
    for name in chosen:
        for language in checker.languages_for(full, name):
            sample = intros.get(name, {}).get(language)
            if sample:
                lines.append(f"- {name}/{language}：{sample}")
    used = {language for name in chosen for language in checker.languages_for(full, name)}
    lines += ["", "## 固定结尾（各语言，逐字照抄）"]
    for language, ending in full["fixedEndings"].items():
        if language in used:
            lines.append(f"- {language}：{ending}")
    lines += [
        "",
        "## 输出格式（只输出 CSV，不要任何解释）",
        "第一行表头：" + ",".join(full["columns"]),
        f"- source 固定为 weekly-{week}",
        "- material、language 用上面的中文名称",
        f"- copyId 为 材料-语言-{tag}-编号，编号 1～6，例如 {chosen[0]}-英语-{tag}-1",
        "- title 为该条的第一句（钩子）",
        "- text 为完整正文，以固定结尾结束",
        "- 字段中有逗号或引号时按 CSV 规则用双引号包起来",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", required=True)
    parser.add_argument("--material", help="只为一种材料生成（推荐，每次 60 条）")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    print(build(args.week, material=args.material), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
