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


def is_skipped(name: str) -> bool:
    """这个目录名是否不该算技能。

    ⚠ 2026-09-26 实测踩过：白名单只认 `_backup-*` 这种**下划线 + 固定版本号**的形态，
    而运行时自我保护产生的备份是 `ai-workflow.bak-20260926-124022` —— **没有下划线、带时间戳**
    的第三种形态，于是被当成一个技能算进索引（技能数虚高 1，指纹随之漂移）。
    所以这里加一条与命名无关的通用规则：名字里出现 `.bak` / `.backup` 一律跳过。
    """
    if name in SKIP_DIRS or name.startswith("."):
        return True
    return ".bak" in name or ".backup" in name

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
            if not d.is_dir() or is_skipped(d.name):
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


USAGE_LEDGER_NAME = "_usage_ledger.tsv"


def load_usage(root: Path) -> dict:
    """读取同目录下的 `<根>/_usage_ledger.tsv`（技能使用台账），返回 {目录名: (状态, 次数)}。

    ⭐ 2026-10-08 新增（技能库归并梳理任务 · 阶段 3）。**为什么单列一个 sidecar 文件而不写进
    SKILL.md**：本索引的指纹刻意「只由内容决定」，若把 usage 状态写进 SKILL.md 或写进索引
    正文再参与指纹，则**每次台账刷新都会让索引判 STALE**，把「内容变没变」这个诚实的判据
    污染成「台账新不新」。所以：**台账状态只影响渲染，不参与 fingerprint**。

    ⭐ 为什么不写进 SKILL.md frontmatter：102 个技能里 26 个是非自建（市场安装），改它们
    frontmatter 会在下次市场升级时被覆盖 —— 台账会静默丢失。sidecar 文件是市场不管的。

    文件格式（制表符分隔，`#` 开头为注释）：
        技能目录名\t状态\t调用次数
    状态取 `used` / `unused`。缺文件时返回空 dict ⇒ 索引退化为「不标注」的旧行为（向后兼容）。
    """
    f = root / USAGE_LEDGER_NAME
    if not f.is_file():
        return {}
    out: dict = {}
    try:
        for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = ln.split("\t")
            if len(parts) < 2:
                continue
            name, status = parts[0].strip(), parts[1].strip().lower()
            try:
                calls = int(parts[2]) if len(parts) > 2 and parts[2].strip() else 0
            except ValueError:
                calls = 0
            out[name] = (status, calls)
    except OSError:
        return {}
    return out


UNUSED_SECTION = "未启用区"


def _row(e: dict, usage: dict) -> str:
    """把一条技能渲染成表格行。`unused` 只在此处被**显式打标**（主表已不出现它们）。"""
    d = e["desc"] or "（frontmatter 无 description）"
    if len(d) > DESC_MAX:
        d = d[:DESC_MAX] + "…"
    marks = []
    st, calls = usage.get(e["dir"], ("", 0))
    if st == "unused":
        marks.append("⛔未启用")
    elif st == "used":
        marks.append(f"✅{calls}次" if calls else "✅")
    if e["selfbuilt"]:
        marks.append("⭐自研")
    mark = (" " + " ".join(marks)) if marks else ""
    return f"| `{e['dir']}`{mark} | {d} |"


def _table(members: list[dict], usage: dict, lines: list[str]) -> None:
    lines.append("| 技能 | 用途 |")
    lines.append("| --- | --- |")
    for e in members:
        lines.append(_row(e, usage))


def render(entries: list[dict], roots: list[Path], partition: bool = True) -> str:
    """渲染索引正文。

    ⭐ 2026-10-08（技能库归并梳理任务 · 阶段 5 · **档 1「仅索引分区」**）：`partition=True`
    时把台账标为 `unused` 的技能**从各簇表里摘出去**，统一收到末尾的「未启用区」。

    🔴 为什么改：原先只是**就地打 ⛔ 标** —— 结果「真正在用的 18 个」被淹没在 102 行里，
    阶段 0 按簇定位候选时要先把 84 行噪音读完。分区后主表 = 在用技能，信噪比从 18/102 提到 18/18。
    这是**纯显示层**改动：磁盘上没有任何技能被移动/删除，装载状态未变，随时可逆
    （`partition=False` 即回到旧行为）。所以「候选下线池」这个措辞在末尾区块里写得比原先更重。

    ⚠️ 分区**不参与指纹**：指纹只算 `目录名 + SKILL.md 内容`，与渲染布局无关
    （同 `load_usage` 的理由）。所以分区与否不影响 `--check`。
    """
    counts: dict = {}
    for e in entries:
        if "-" in e["dir"]:
            pre = e["dir"].split("-", 1)[0]
            counts[pre] = counts.get(pre, 0) + 1

    groups: dict = {}
    for e in entries:
        groups.setdefault(cluster_key(e["dir"], counts), []).append(e)

    # 台账：分区/打标都靠它。取所有根的并集（通常只有一个根）。
    usage: dict = {}
    for root in roots:
        usage.update(load_usage(root))
    is_unused = lambda d: usage.get(d, ("", 0))[0] == "unused"          # noqa: E731
    n_unused = sum(1 for e in entries if is_unused(e["dir"]))
    n_used = len(entries) - n_unused

    lines = [
        "# 技能库索引（由 `gen_skill_index.py` 自动生成，请勿手改）",
        "",
        f"<!-- fp: {fingerprint(roots)[0]} -->  源集合**内容**指纹（不含 mtime）；`--check` 就是拿它与实时值比对",
        f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M')} ｜ 技能数：**{len(entries)}**（自动跳过 `_backup-*` / `*.bak*` / `*.backup*` / `_archive` / `.git` / 点开头）",
        f"> 扫描根：{'；'.join(str(r) for r in roots)} ｜ 文件位置：各技能的 `<根>/<目录名>/SKILL.md`",
        "> **读法（阶段 0）**：先在本表**按簇**定位候选 → **只读命中候选的 `SKILL.md` frontmatter** →",
        "> 一个都不命中才回落全量 Glob。**索引过期时以全量为准并重跑本脚本重建**（`--check` 可判新鲜度）。",
        "> 本表刻意只放「技能名 + 用途摘要」：全量表 ≈ 各 SKILL.md frontmatter 的 1/4 字节，这正是它的存在理由。",
    ]
    if usage:
        if partition and n_unused:
            lines += [
                f"> 🏷️ **使用标注**（来自 `{USAGE_LEDGER_NAME}`，只影响显示、**不参与指纹**）：",
                f"> 下方**主表只列在用的 {n_used} 个**；`⛔未启用` 的 **{n_unused}** 个已**分区**到文末「{UNUSED_SECTION}」"
                "（仅索引分区，磁盘上**未移动/未删除**任何技能，装载状态不变，可逆）。",
                f"> `✅{'{n}次'}`/`✅` = 有过真实调用；`⭐自研` = frontmatter `selfbuilt: true`。",
            ]
        else:
            lines += [
                f"> 🏷️ **使用标注**（来自 `{USAGE_LEDGER_NAME}`，只影响显示、**不参与指纹**）：",
                f"> `⛔未启用` = 会话记录中零调用（共 **{n_unused}** 个）；无标注 = 有过真实调用；`⭐自研` = frontmatter `selfbuilt: true`。",
                "> ⛔ 类是**候选下线池**，不是「已删除」—— 装载状态未变，仅标注。",
            ]
    lines.append("")

    # 分区：主表只留「在用」；unused 单独成区。台账缺失时不分区（旧行为）。
    do_partition = bool(partition and usage)
    main_groups: dict = {}
    pool: list[dict] = []
    for k, members in groups.items():
        if do_partition:
            keep = [e for e in members if not is_unused(e["dir"])]
            drop = [e for e in members if is_unused(e["dir"])]
            # 一个簇全被摘空时不渲染空簇标题
            if keep:
                main_groups[k] = keep
            pool.extend(drop)
        else:
            main_groups[k] = members

    # 簇间按成员数降序，`独立技能` 固定排最后
    keys = sorted([k for k in main_groups if k != "独立技能"],
                  key=lambda k: (-len(main_groups[k]), k))
    if "独立技能" in main_groups:
        keys.append("独立技能")
    for k in keys:
        members = sorted(main_groups[k], key=lambda e: e["dir"])
        lines.append(f"## {k}（{len(members)}）")
        lines.append("")
        _table(members, usage, lines)
        lines.append("")

    if pool:
        lines += [
            f"## ⛔ {UNUSED_SECTION}（{len(pool)}）",
            "",
            "> **这是候选下线池，不是「已删除」**：以下技能在会话记录里**零真实调用**，因此被"
            "**移出上表、仅在索引里分区**。磁盘上**未移动、未修改、未删除**任何一个技能目录，"
            "装载状态与从前完全一致 —— 随时可回退（删掉本节、把行放回各簇即可）。",
            "> 阶段 0 按簇找候选时**默认不看本节**；只有当全表都不命中、要扩大搜索面时才回看这里。",
            "> 台账口径见 `ai-workflow/scripts/gen_skill_index.py` 的 `load_usage` docstring。",
            "",
        ]
        for e in sorted(pool, key=lambda e: e["dir"]):
            lines.append(f"- `{e['dir']}`{' ⭐自研' if e['selfbuilt'] else ''} — "
                         f"{(e['desc'] or '（frontmatter 无 description）')[:DESC_MAX]}")
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
            if not d.is_dir() or is_skipped(d.name):
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
