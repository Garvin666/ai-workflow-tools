#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [自研工具] gen_skill_index.py
# 用途：扫描技能库各 SKILL.md 的 frontmatter，生成/校检「技能库索引」（默认 ~/.workbuddy/skills/README.md），使 SKILL.md 阶段 0 的「索引优先」真正可命中
# 适用场景：阶段 0 要按簇定位候选技能时、技能库新增/改名/改 description 后重建索引时用；已明确知道要用哪个技能（不必先建索引）、或只需跑某个脚本时不用
# 作者：ai-workflow 自研（技能增强-ai-workflow-v3.4.0-2026-09-16，2026-09-16）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/gen_skill_index.py
"""gen_skill_index.py - 技能库索引生成器（ai-workflow v3.4.0 / P6）。

------------------------------------------------------------------------------
为什么需要它（起因是实测出来的，不是设计洁癖）
------------------------------------------------------------------------------
`SKILL.md` 阶段 0 第 2 条写着「先读技能库索引（`.workbuddy/skills/README.md`）按簇定位候选，
仅当索引未命中时才全量扫」——而 2026-09-16 实测：**两处索引路径都不存在**。
于是「索引优先」100% 落空，每次任务都退化为全量扫描 25 份 SKILL.md 的 frontmatter
（≈11,976 B ≈ **2,994 token**，还不含 26 次目录遍历）。

那条条文自称「索引正是为消除这笔成本而建的」。**要么把索引建出来，要么把条文改掉** ——
留着一条永远不生效的条文就是文档失真。本脚本选前者：把索引真正建出来。

------------------------------------------------------------------------------
簇的划法（避免"看起来智能、实际乱分"）
------------------------------------------------------------------------------
规则只有一条、可复现：**目录名的第一个连字符前缀**，且该前缀在库内**至少出现 2 次**才成簇；
不足 2 次的各自归入「独立技能」。不做关键词猜测 —— 关键词簇改一次 description 就漂移一次。
簇内按名称排序，簇间按成员数降序。

------------------------------------------------------------------------------
新鲜度（阶段 0 需要一条便宜且**诚实**的判据）
------------------------------------------------------------------------------
`--check` 算「源集合指纹」并与索引里存的那份比对，**不写任何文件**。

⭐⭐ 指纹**只由内容决定**（`目录名 + SKILL.md 内容的 sha1`），**刻意不含 mtime**。两轮实测
逼出来的口径：
  · v1 用「比索引文件自己的 mtime」→ 任一 `SKILL.md` 的 mtime 落在索引之后（时钟偏移/同步
    工具/`touch`）就**永久**判过期，阶段 0 便永远回落全量扫描，把这条优化原地作废。
  · v2 改成「存指纹」，但指纹里**带了 mtime_ns** → 只 `touch` 不改内容也判过期
    （6 态阴性对照的第 4a 态当场抓到，与我在注释里写的"对时钟免疫"**自相矛盾**）。
  · v3（本版）指纹**纯内容**：`touch` 不改内容 → FRESH（正确，确实没变）；改一个字节 →
    STALE。**代码与声明这才对得上。**

已知边界（如实登记）：这与"同秒内同长度改动"无关了 —— 纯内容指纹对任何字节差异都敏感；
唯一的代价是 `--check` 要读一遍各 `SKILL.md`（本机 25 份共 ≈200 KB，毫秒级，可接受）。

用法：
    python scripts/gen_skill_index.py                      # 生成并写入默认索引
    python scripts/gen_skill_index.py --print              # 只打印，不写盘
    python scripts/gen_skill_index.py --check              # 新鲜度校检（只读，退出码 0/1/2）
    python scripts/gen_skill_index.py --index <路径> --roots <根1>,<根2>

退出码：0 = 成功/新鲜；1 = --check 判过期或索引缺失；2 = 用法或环境错误
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_ROOTS = [
    Path.home() / ".workbuddy" / "skills",
]
DEFAULT_INDEX = Path.home() / ".workbuddy" / "skills" / "README.md"
# 索引文件名本身就在技能根下 —— 扫描时不能被当成技能目录
SKIP_DIRS = {"node_modules", "__pycache__", ".git", "_archive", "_backup-v1", "_backup-v2",
             "_backup-v2.3", "_backup-v3.0", "_backup-v3.2.1", "_backup-v3.3.0"}
DESC_MAX = 78


def est_tok(s: str) -> int:
    han = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    return han + (len(s) - han) // 4


def parse_frontmatter(text: str) -> dict:
    """极简 frontmatter 解析：只取顶层 `key: value` + **YAML 块标量**（`>` / `|` 及 `>-` `|-`）。

    不引 pyyaml（索引器要能在裸环境跑）。⚠️ 必须支持块标量：实测库里 `cli-agent-relay` 与
    `dsh-isolated-home` 的 `description` 用的是折叠块，只认「单行 key: value」的解析器会把
    它们的用途读成字面量 `>` —— 索引里出现 `| > |` 就是这类解析器缺陷的典型症状。
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    body = text[3:end].splitlines()
    out: dict = {}
    i = 0
    while i < len(body):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", body[i])
        if not m:
            i += 1
            continue
        key, val = m.group(1), m.group(2).strip()
        if val in (">", "|", ">-", "|-", ">+", "|+"):
            block, j = [], i + 1
            while j < len(body) and (not body[j].strip() or body[j].startswith((" ", "\t"))):
                block.append(body[j].strip())
                j += 1
            val = " ".join(x for x in block if x)
            i = j
        else:
            val = val.strip('"').strip("'")
            i += 1
        out[key] = val
    return out


def collect(roots: list[Path]) -> tuple[list[dict], list[str]]:
    entries: list[dict] = []
    missing: list[str] = []
    for root in roots:
        if not root.is_dir():
            missing.append(str(root))
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name in SKIP_DIRS or d.name.startswith("."):
                continue
            f = d / "SKILL.md"
            if not f.is_file():
                continue
            try:
                fm = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            entries.append({
                "dir": d.name,
                "name": fm.get("name") or d.name,
                "desc": (fm.get("description") or "").replace("\n", " ").strip(),
                "root": str(root),
                "selfbuilt": fm.get("selfbuilt", "").lower() == "true",
            })
    return entries, missing


def cluster_key(name: str, counts: dict) -> str:
    """簇 = 目录名第一个连字符前缀，且出现 >= 2 次；否则 '独立技能'。"""
    if "-" in name:
        pre = name.split("-", 1)[0]
        if counts.get(pre, 0) >= 2:
            return pre + "-*"
    return "独立技能"


def render(entries: list[dict], roots: list[Path]) -> str:
    counts: dict = {}
    for e in entries:
        if "-" in e["dir"]:
            pre = e["dir"].split("-", 1)[0]
            counts[pre] = counts.get(pre, 0) + 1

    groups: dict = {}
    for e in entries:
        groups.setdefault(cluster_key(e["dir"], counts), []).append(e)

    lines = [
        "# 技能库索引（由 `gen_skill_index.py` 自动生成，请勿手改）",
        "",
        f"<!-- fp: {fingerprint(roots)[0]} -->  源集合**内容**指纹（不含 mtime）；`--check` 就是拿它与实时值比对",
        f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M')} ｜ 技能数：**{len(entries)}**（自动跳过 `_backup-*` / `_archive` / `.git`）",
        f"> 扫描根：{'；'.join(str(r) for r in roots)} ｜ 文件位置：各技能的 `<根>/<目录名>/SKILL.md`",
        "> **读法（阶段 0）**：先在本表**按簇**定位候选 → **只读命中候选的 `SKILL.md` frontmatter** →",
        "> 一个都不命中才回落全量 Glob。**索引过期时以全量为准并重跑本脚本重建**（`--check` 可判新鲜度）。",
        "> 本表刻意只放「技能名 + 用途摘要」：全量表 ≈ 各 SKILL.md frontmatter 的 1/4 字节，这正是它的存在理由。",
        "",
    ]
    # 簇间按成员数降序，`独立技能` 固定排最后
    keys = sorted([k for k in groups if k != "独立技能"], key=lambda k: (-len(groups[k]), k))
    if "独立技能" in groups:
        keys.append("独立技能")
    for k in keys:
        members = sorted(groups[k], key=lambda e: e["dir"])
        lines.append(f"## {k}（{len(members)}）")
        lines.append("")
        lines.append("| 技能 | 用途 |")
        lines.append("| --- | --- |")
        for e in members:
            d = e["desc"] or "（frontmatter 无 description）"
            if len(d) > DESC_MAX:
                d = d[:DESC_MAX] + "…"
            mark = " ⭐自研" if e["selfbuilt"] else ""
            lines.append(f"| `{e['dir']}`{mark} | {d} |")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def fingerprint(roots: list[Path]) -> tuple[str, int]:
    """源集合指纹：`目录名 + SKILL.md 内容 sha1` 排序后再取 sha1。**刻意不含 mtime** —— 见文件头。

    返回 (指纹, 技能目录数)。只读 SKILL.md，不解析 frontmatter。
    """
    import hashlib
    items: list[str] = []
    for root in roots:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name in SKIP_DIRS or d.name.startswith("."):
                continue
            f = d / "SKILL.md"
            if not f.is_file():
                continue
            try:
                h = hashlib.sha1(f.read_bytes()).hexdigest()[:12]
            except OSError:
                continue
            items.append(f"{d.name}|{h}")
    items.sort()
    return hashlib.sha1("\n".join(items).encode("utf-8")).hexdigest()[:16], len(items)


FP_RE = re.compile(r"<!--\s*fp:\s*([0-9a-f]{8,})\s*-->")


def is_fresh(index: Path, roots: list[Path]) -> tuple[bool, str]:
    """比对「索引里存的指纹」与「当前源集合指纹」。"""
    if not index.is_file():
        return False, "索引文件不存在：%s" % index
    m = FP_RE.search(index.read_text(encoding="utf-8", errors="replace")[:2000])
    if not m:
        return False, "索引里没有指纹标记（可能是旧版或手改过）→ 请重建"
    cur, n = fingerprint(roots)
    stored = m.group(1)
    if cur != stored:
        return False, "源集合已变化（存 %s / 现 %s）" % (stored, cur)
    return True, "内容指纹一致（%s），%d 个技能目录" % (stored, n)


def main() -> int:
    ap = argparse.ArgumentParser(description="技能库索引生成/校检（ai-workflow v3.4.0 / P6）")
    ap.add_argument("--index", default=str(DEFAULT_INDEX), help="索引输出路径")
    ap.add_argument("--roots", help="扫描根，逗号分隔（默认用户级 ~/.workbuddy/skills）")
    ap.add_argument("--workspace", help="额外把 <工作区>/.workbuddy/skills 纳入扫描根（项目级技能）")
    ap.add_argument("--print", dest="print_only", action="store_true", help="只打印，不写盘")
    ap.add_argument("--check", action="store_true", help="只校检新鲜度（只读），过期/缺失以 1 退出")
    a = ap.parse_args()

    roots = [Path(p.strip()) for p in a.roots.split(",")] if a.roots else list(DEFAULT_ROOTS)
    if a.workspace:
        roots.append(Path(a.workspace) / ".workbuddy" / "skills")
    index = Path(a.index)

    if a.check:
        fresh, why = is_fresh(index, roots)
        print(("[OK  ] 索引新鲜：" if fresh else "[STALE] 索引不可用：") + why)
        if not fresh:
            print("[HINT] 重跑：python scripts/gen_skill_index.py" + (f" --workspace {a.workspace}" if a.workspace else ""))
        return 0 if fresh else 1

    entries, missing = collect(roots)
    for m in missing:
        print(f"[WARN] 扫描根不存在，已跳过：{m}", file=sys.stderr)
    if not entries:
        print("[ERROR] 一个技能都没扫到 —— 检查 --roots 是否正确", file=sys.stderr)
        return 2

    text = render(entries, roots)
    if a.print_only:
        print(text)
        print(f"[INFO] 估算 {est_tok(text)} tok（汉字数 + 非汉字/4 粗估），{len(entries)} 个技能", file=sys.stderr)
        return 0

    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(text, encoding="utf-8")
    print(f"[ OK ] 已写入 {index}")
    print(f"       {len(entries)} 个技能，{len(text.encode('utf-8'))} B ≈ {est_tok(text)} tok（粗估）")
    print(f"       全量对照：逐份读 frontmatter ≈ {est_tok(''.join(e['desc'] for e in entries)) * 3} tok 量级（本表约为其 1/4）")
    fresh, why = is_fresh(index, roots)
    print(("[ OK ] 新鲜度：" if fresh else "[WARN] 新鲜度：") + why)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
