# -*- coding: utf-8 -*-
# [自研工具] memory_handoff.py
# 用途：ai-workflow 记忆写入与交接引擎 —— 把任务收尾整理出的交接文档 / 会话记忆落位到知识库 40-记忆 区
#       （含结构校验、幂等备份），并重建记忆索引、输出自升级统计。
# 适用场景：ai-workflow L2 任务阶段 6 收尾（见 references/memory-handoff.md），手动触发。
# 作者：ai-workflow 自研（机制-记忆管理与交接自升级-2026-10-16，2026-10-16）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/memory_handoff.py
# ⚠️ 标注头**写两处**是刻意的，不是冗余：顶上这组 `#` 注释行才是**机器判据**
#    （`scripts/push_router.py::scan_selftool_header` 只认前 40 行内的注释行），
#    下面 docstring 里的同名块是给人读的复述。**改一处必须同步另一处**，否则分流会被判「无标注头」。
"""[自研工具] memory_handoff —— ai-workflow 记忆写入与交接引擎
用途：把任务收尾整理出的交接文档 / 会话记忆落位到知识库 40-记忆 区（含结构校验、幂等备份），
     并重建记忆索引、输出自升级统计。语义（内容撰写）归模型，算术与判据（路径/校验/备份/计数）归本脚本。
适用场景：ai-workflow L2 任务阶段 6 收尾（手动触发，见 references/memory-handoff.md）
仓库：https://github.com/Garvin666/ai-workflow-tools

子命令：
  place   落位一篇交接/会话记忆（结构校验 → 备份 → 写入）
  index   重建 40-记忆/00-记忆索引.md（按项目分组，幂等）
  stats   输出统计 JSON（供自升级决策）
  backup  备份指定文件（.bak-<ts>）
  selftest  自测（含阴性对照）
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

DEFAULT_ROOT = Path(r"E:\ChatGPT\知识库\40-记忆")
KIND_DIR = {"handoff": "交接", "memory": "会话记忆"}

# 结构判据：按顺序出现的关键节（正则，容忍「一、二、三」编号与变体措辞）
HANDOFF_SECTIONS = [r"^##\s+.*内容", r"^##\s+.*结果", r"^##\s+.*后续"]
MEMORY_SECTIONS = [
    r"^##\s+.*任务与结果",
    r"^##\s+.*关键决策",
    r"^##\s+.*偏好信号",
    r"^##\s+.*未决",
    r"^##\s+.*重要事实",
]
PLACEHOLDER_RE = re.compile(r"<[^<>\n]{1,20}>|TODO|TBD|xxx|XXX|待填")


def ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def read_exact(p: Path) -> str:
    """按字节保真读文本（newline="" 禁行尾转换）。3.12/3.13 双兼容。

    ⚠️ 用 open(...) 而非 Path.read_text(newline=...)：`newline` 参数是 **Python 3.13+ 独有**，
    在 3.12（本机 Anaconda 默认解释器）下抛 TypeError ⇒ `index` 路径整体崩栈
    （2026-10-16 实测：3.12 下 rc=1、3.13 venv 才通。属「环境依赖型崩栈」——本机默认解释器
    恰好是旧版，任何按默认解释器调用的会话都会撞上）。
    """
    with open(p, "r", encoding="utf-8", newline="") as f:
        return f.read()


def check_structure(text: str, kind: str) -> list[str]:
    """返回问题列表（空 = 通过）。判据：各关键节按顺序各出现 ≥1 次。"""
    pats = HANDOFF_SECTIONS if kind == "handoff" else MEMORY_SECTIONS
    problems: list[str] = []
    last = -1
    lines = text.splitlines()
    for pat in pats:
        rx = re.compile(pat)
        pos = next((i for i, ln in enumerate(lines) if rx.match(ln)), -1)
        if pos == -1:
            problems.append(f"缺节或有误：/{pat}/ 未按预期出现")
        elif pos <= last:
            problems.append(f"节序错误：/{pat}/ 出现位置不在前一节之后")
        else:
            last = pos
    if not text.strip():
        problems.append("内容为空")
    return problems


def check_placeholders(text: str) -> list[str]:
    hits = PLACEHOLDER_RE.findall(text)
    # 排除合法的 HTML/双链等：仅当命中片段像占位符时报警（保守：直接报但限长）
    return [h for h in hits if not h.startswith("[[")][:5]


def backup_file(p: Path) -> Path | None:
    if not p.exists():
        return None
    b = p.with_name(f"{p.name}.bak-{ts()}")
    shutil.copy2(p, b)
    return b


def cmd_place(args) -> int:
    root = Path(args.root)
    src = Path(args.src)
    if not src.is_file():
        print(f"[ERR] 源文件不存在：{src}")
        return 2
    if args.kind not in KIND_DIR:
        print(f"[ERR] --kind 只能是 {sorted(KIND_DIR)}")
        return 2
    with open(src, "r", encoding="utf-8", newline="") as f:
        text = f.read()  # ⚠️ newline="" 保真：Windows 默认会把 \n 转成 \r\n（2026-10-16 实测踩中）
    problems = check_structure(text, args.kind)
    if problems:
        print("[FAIL] 结构校验未过：")
        for p in problems:
            print(f"  - {p}")
        return 1
    ph = check_placeholders(text)
    if ph:
        print(f"[WARN] 疑似占位符 {ph} —— 请确认这是正文内容而非未填模板（不阻断）")
    when = args.date or datetime.now().strftime("%Y-%m-%d")
    hhmm = args.time or datetime.now().strftime("%H%M")
    dest_dir = root / KIND_DIR[args.kind] / args.project
    dest = dest_dir / f"{when}-{hhmm}-{args.task}.md"
    # 同日同任务、异时间 ⇒ 可能是「想更新却写成新增」：显式 WARN（不阻断）。同名同时间 = 更新（走 .bak）。
    if dest_dir.is_dir():
        siblings = [p.name for p in sorted(dest_dir.glob(f"{when}-*-{args.task}.md"))
                    if p.name != dest.name and ".bak" not in p.name]
        if siblings:
            print(f"[WARN] 当日同任务已有 {len(siblings)} 篇：{'、'.join(siblings)}"
                  f" ⇒ 若意图是**更新**既有篇目，请传 --date {when} --time <原HHmm>；"
                  f"否则本次将**新增一篇**（同主题多次归档的既定语义）")
    if args.dry_run:
        print(f"[DRY] 将写入：{dest}（校验已过；备份：{'有' if dest.exists() else '无'}）")
        return 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    b = backup_file(dest)
    with open(dest, "w", encoding="utf-8", newline="") as f:
        f.write(text)  # ⚠️ newline=""：逐字节保真，禁行尾转换（同读侧，2026-10-16 修）
    line_count = len(text.splitlines())
    print(f"[OK] 已写入：{dest}")
    print(f"     行数 {line_count} ｜ 字节 {dest.stat().st_size}"
          + (f" ｜ 旧版备份：{b.name}" if b else " ｜ 无旧版"))
    return 0


def scan(root: Path) -> dict:
    """扫描 40-记忆：分层新文件 + 平铺存量。"""
    out: dict = {"交接": {}, "会话记忆": {}, "存量平铺": []}
    for kind_dir in ("交接", "会话记忆"):
        d = root / kind_dir
        if not d.is_dir():
            continue
        for proj in sorted(p for p in d.iterdir() if p.is_dir()):
            files = sorted(f for f in proj.glob("*.md")
                           if ".bak" not in f.name)
            if files:
                out[kind_dir][proj.name] = [f.name for f in files]
    legacy = root / "会话记忆"
    if legacy.is_dir():
        out["存量平铺"] = sorted(
            f.name for f in legacy.glob("*.md") if ".bak" not in f.name)
    return out


def cmd_index(args) -> int:
    root = Path(args.root)
    data = scan(root)
    lines = [
        "# 40-记忆 · 记忆索引（自动生成，勿手改）",
        "",
        # ⚠️ 头部刻意**不含时间戳**：这段内容要拿去做「与磁盘逐字比较」的幂等判据，
        #    掺入任何随时间变化的字段（如 now()）会让该判据恒假 ⇒ 每次运行都重写 + 堆 .bak
        #    （2026-10-16 实测：一日内累积 9 个 .bak；同仓 gen_skill_index.py 的指纹亦刻意
        #     不含 mtime —— 同一口径）。最后重建时间看本文件 mtime 即可。
        "> 由 ai-workflow `memory_handoff.py index` 重建 ｜ 内容＝文件清单的纯函数（不含时间戳，清单不变则不重写）。",
        "> 分层规则：`<类型>/<项目>/<日期>-<任务>.md`；项目名 = 任务工作区名。",
        "",
    ]
    for kind_dir in ("交接", "会话记忆"):
        lines.append(f"## {kind_dir}")
        lines.append("")
        groups = data.get(kind_dir, {})
        if not groups:
            lines.append("（暂无）")
            lines.append("")
            continue
        for proj, files in groups.items():
            lines.append(f"### {proj}（{len(files)}）")
            lines.append("")
            for f in files:
                date = f[:10]
                stem = f[:-3]
                lines.append(f"- {date} ｜ [[{kind_dir}/{proj}/{stem}\\|{stem[11:]}]]")
            lines.append("")
    legacy = data.get("存量平铺", [])
    if legacy:
        lines.append("## 存量平铺（2026-10-16 前，未分层，原位保留）")
        lines.append("")
        for f in legacy:
            stem = f[:-3]
            lines.append(f"- [[会话记忆/{stem}\\|{f}]]")
        lines.append("")
    if args.dry_run:
        print(f"[DRY] 索引将含：交接 {sum(len(v) for v in data.get('交接', {}).values())} 篇 ｜ "
              f"会话记忆 {sum(len(v) for v in data.get('会话记忆', {}).values())} 篇 ｜ "
              f"存量 {len(legacy)} 篇")
        return 0
    dest = root / "00-记忆索引.md"
    new = "\n".join(lines) + "\n"
    if dest.exists() and read_exact(dest) == new:
        print(f"[OK] 索引无变化（内容相同，未重写）：{dest}")
        return 0
    b = backup_file(dest)
    with open(dest, "w", encoding="utf-8", newline="") as f:
        f.write(new)  # newline=""：生成物恒为 LF（与 会话记忆 存量行尾一致）
    print(f"[OK] 索引已重建：{dest}"
          + (f"（旧版备份 {b.name}）" if b else "（首建）"))
    return 0


def cmd_stats(args) -> int:
    root = Path(args.root)
    data = scan(root)
    handoff_n = sum(len(v) for v in data.get("交接", {}).values())
    memory_n = sum(len(v) for v in data.get("会话记忆", {}).values())
    legacy_n = len(data.get("存量平铺", []))
    correction = root / "纠错账本.md"
    corr_lines = 0
    if correction.exists():
        corr_lines = sum(1 for ln in correction.read_text(encoding="utf-8").splitlines()
                         if ln.strip().startswith("|") and "---" not in ln)
    profile = list((root / "画像").glob("*.md")) if (root / "画像").is_dir() else []
    out = {
        "root": str(root),
        "交接": {"projects": len(data.get("交接", {})), "files": handoff_n},
        "会话记忆": {"projects": len(data.get("会话记忆", {})), "files": memory_n},
        "存量平铺": legacy_n,
        "纠错账本": {"exists": correction.exists(), "table_rows": corr_lines},
        "画像": [p.name for p in profile],
        "thresholds": {"会话记忆单项目": 10, "交接单项目": 10},
        "suggestions": [],
    }
    if memory_n + legacy_n >= 20 and not (root / "00-记忆索引.md").exists():
        out["suggestions"].append("记忆合计 ≥20 篇且无索引 ⇒ 跑 index 重建")
    for proj, files in data.get("交接", {}).items():
        if len(files) >= out["thresholds"]["交接单项目"]:
            out["suggestions"].append(f"交接/{proj} 已达 {len(files)} 篇 ⇒ 归属/归档回顾提案")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_backup(args) -> int:
    p = Path(args.file)
    b = backup_file(p)
    if b is None:
        print(f"[ERR] 文件不存在：{p}")
        return 2
    print(f"[OK] 已备份：{b}")
    return 0


def cmd_selftest(_args) -> int:
    fails: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mh-selftest-") as td:
        root = Path(td)
        # fixture 1：合法交接 → place 成功
        good = root / "src-good.md"
        good.write_text(
            "# 交接｜样例（2026-10-16）\n\n## 一、本次工作的内容\n\n做了 A。\n\n"
            "## 二、结果\n\n产出 B。\n\n## 三、后续事项\n\n下一步 C。\n",
            encoding="utf-8")
        rc = cmd_place(argparse.Namespace(
            root=str(root), src=str(good), kind="handoff", project="样例项目",
            task="样例任务", date="2026-10-16", time="1200", dry_run=False))
        if rc != 0:
            fails.append("fixture1 合法交接 place 应 rc=0")
        dest = root / "交接" / "样例项目" / "2026-10-16-1200-样例任务.md"
        if not dest.is_file():
            fails.append("fixture1 产物未落位")

        # fixture 2（阴性对照）：缺「后续」节 → 必须 rc=1 且不落位
        bad = root / "src-bad.md"
        bad.write_text("# 交接\n\n## 一、本次工作的内容\n\nX\n\n## 二、结果\n\nY\n",
                       encoding="utf-8")
        rc2 = cmd_place(argparse.Namespace(
            root=str(root), src=str(bad), kind="handoff", project="样例项目",
            task="坏样例", date="2026-10-16", time="1200", dry_run=False))
        if rc2 != 1:
            fails.append(f"fixture2 缺节应 rc=1，实得 {rc2}")
        if (root / "交接" / "样例项目" / "2026-10-16-1200-坏样例.md").exists():
            fails.append("fixture2 非法内容不应落位")

        # fixture 3：五段式会话记忆合法
        m = root / "src-mem.md"
        m.write_text(
            "# 会话记忆｜样例\n\n## 一、任务与结果\nA\n\n## 二、关键决策与理由\nB\n\n"
            "## 三、用户的偏好信号\nC\n\n## 四、未决事项\nD\n\n## 五、重要事实与来源\nE\n",
            encoding="utf-8")
        rc3 = cmd_place(argparse.Namespace(
            root=str(root), src=str(m), kind="memory", project="样例项目",
            task="样例记忆", date="2026-10-16", time="1201", dry_run=False))
        if rc3 != 0:
            fails.append("fixture3 合法五段 place 应 rc=0")

        # fixture 4：幂等覆盖 → 旧版备份存在
        rc4 = cmd_place(argparse.Namespace(
            root=str(root), src=str(m), kind="memory", project="样例项目",
            task="样例记忆", date="2026-10-16", time="1201", dry_run=False))
        baks = list((root / "会话记忆" / "样例项目").glob("*.bak-*"))
        if rc4 != 0 or not baks:
            fails.append("fixture4 二次 place 应成功且产生 .bak 备份")

        # fixture 5：index 幂等（连跑两次第二次报无变化）+ 含条目
        rc5 = cmd_index(argparse.Namespace(root=str(root), dry_run=False))
        rc6 = cmd_index(argparse.Namespace(root=str(root), dry_run=False))
        idx = root / "00-记忆索引.md"
        if rc5 != 0 or rc6 != 0 or not idx.is_file():
            fails.append("fixture5 index 连跑两次应 rc=0 且产出索引")
        if idx.is_file() and "样例任务" not in idx.read_text(encoding="utf-8"):
            fails.append("fixture5 索引应含样例条目")

        # fixture 6：stats 输出合法 JSON
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc7 = cmd_stats(argparse.Namespace(root=str(root)))
        try:
            json.loads(buf.getvalue())
        except Exception:
            fails.append("fixture6 stats 输出应为合法 JSON")

        # fixture 7（回归守卫）：落位必须**逐字节保真** —— 不注入 CRLF。
        # 背景：初版用 write_text 默认参数，Windows 上静默把 LF 转 CRLF（2026-10-16 实测踩中）；
        # 夹具源文件也须显式 newline=""，否则夹具自身变 CRLF ⇒ 恒等比较变成空转（假绿）。
        src7 = root / "src-lf.md"
        with open(src7, "w", encoding="utf-8", newline="") as f:
            f.write("# 交接｜行尾样例\n\n## 一、内容\nA\n\n## 二、结果\nB\n\n## 三、后续\nC\n")
        rc8 = cmd_place(argparse.Namespace(
            root=str(root), src=str(src7), kind="handoff", project="样例项目",
            task="行尾保真", date="2026-10-16", time="1202", dry_run=False))
        dest7 = root / "交接" / "样例项目" / "2026-10-16-1202-行尾保真.md"
        if rc8 != 0:
            fails.append("fixture7 行尾保真 place 应 rc=0")
        elif dest7.read_bytes() != src7.read_bytes():
            fails.append("fixture7 落位应逐字节保真（禁行尾转换：源 LF ⇒ 产物 LF）")

        # fixture 8：同日同任务、异时间 ⇒ 必须 WARN（防「想更新却静默新增」）；同名同时间走更新（.bak）
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            rc9 = cmd_place(argparse.Namespace(
                root=str(root), src=str(src7), kind="handoff", project="样例项目",
                task="行尾保真", date="2026-10-16", time="1203", dry_run=False))
        if rc9 != 0:
            fails.append("fixture8 异时间 place 应 rc=0")
        if "WARN" not in buf2.getvalue():
            fails.append("fixture8 同日同任务异时间应打印 WARN（防静默重复）")

    if fails:
        print("[FAIL] selftest：")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("[OK] selftest 全绿（8 组 fixture：合法×2 / 阴性对照×1 / 幂等×2 / JSON×1 / 行尾保真×1 / 防重复 WARN×1）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="memory_handoff —— ai-workflow 记忆写入与交接引擎")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("place", help="落位一篇交接/会话记忆")
    p.add_argument("--kind", required=True, choices=sorted(KIND_DIR))
    p.add_argument("--project", required=True, help="项目名=任务工作区名")
    p.add_argument("--task", required=True, help="任务短名（用作文件名主体）")
    p.add_argument("--src", required=True, help="内容 md 路径")
    p.add_argument("--root", default=str(DEFAULT_ROOT))
    p.add_argument("--date", default=None, help="YYYY-MM-DD（缺省今天）")
    p.add_argument("--time", default=None, help="HHMM（缺省当前）")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_place)

    for name, fn, helptext in (
        ("index", cmd_index, "重建 00-记忆索引.md"),
        ("stats", cmd_stats, "输出统计 JSON"),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--root", default=str(DEFAULT_ROOT))
        sp.add_argument("--dry-run", action="store_true")
        sp.set_defaults(func=fn)

    sb = sub.add_parser("backup", help="备份指定文件")
    sb.add_argument("--file", required=True)
    sb.set_defaults(func=cmd_backup)

    st = sub.add_parser("selftest", help="自测（含阴性对照）")
    st.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
