#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [自研工具] verify_copies.py
# 名称：verify_copies.py（技能各副本与母本的一致性的全量验收器）
# 用途：逐文件比对母本与每一处副本，报出「仅母本有／仅副本有／内容不同」三类差异；
#       与分发器**零代码共享**，自己遍历、自己算哈希，覆盖白名单内**全部**文件而非抽样探针。
# 适用场景：分发后验收、日常巡检「有没有副本漂移成旧实现」。
#           **不适用**：替代分发（本脚本只读不写）、跨机器比较（不联网，只读本地路径）。
# 作者：ai-workflow 自研（实施-效率改进建议-2026-11-25，2026-11-25）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/verify_copies.py
"""副本一致性全量验收（只读）。

为什么不能只探几个文件：旧版探针只比 `SKILL.md`/`README.md`/三个脚本共 5 个文件，
于是**只改 `Ledger.md` 时它三项全 OK、完全无感** —— 那 5 个文件之外的任何漂移都测不到。
抽样哨兵能发现「大部分漂移」，但会把「恰好只改了未抽样文件」读成一致。本脚本覆盖
白名单内**每一个文件**，因此结论是「全等」而不是「抽样未见差异」。

与 `sync_copies.py` 的关系：**刻意不共享任何代码**（同一套假设的检查器验不出那套假设
本身的错）。两者对「白名单」与「不碰哪些目录」的口径必须一致，这靠本文件 selftest 的
S5 用例钉住（在 `tasks/` 下造差异，必须不报），而不是靠 import 同一个常量。

退出码：0 全部一致（或全部为联接）／1 存在差异／2 用法或定位错误。
"""
import argparse
import hashlib
import os
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 与分发器口径一致，但**独立书写**（不 import）：口径漂移由 selftest 兜，不靠共享常量
TOP_FILES = ("SKILL.md", "README.md", "Ledger.md", ".gitignore")
TREES = ("references", "scripts", "assets")
NEVER = frozenset({"__pycache__", ".git", "tmp", "_archive", "tasks", "output", "node_modules"})
BAK_SUBSTR = ".bak-"   # 语义同 .gitignore 的 `*.bak-*`：出现在文件名**中间**也算
SNAPSHOT_HINTS = ("_skill-backups", "skills-backup")


def sha256f(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def walk(root: Path) -> dict[str, str]:
    """白名单内相对路径 → sha256。自己实现遍历，不复用分发器代码。"""
    got: dict[str, str] = {}
    for nm in TOP_FILES:
        f = root / nm
        if f.is_file():
            got[nm] = sha256f(f)
    for t in TREES:
        d = root / t
        if not d.is_dir():
            continue
        stack = [d]
        while stack:
            cur = stack.pop()
            try:
                entries = list(cur.iterdir())
            except OSError:
                continue
            for e in entries:
                if e.is_dir():
                    if e.name not in NEVER:
                        stack.append(e)
                elif e.is_file():
                    if BAK_SUBSTR in e.name:
                        continue
                    rel = e.relative_to(root).as_posix()
                    got[rel] = sha256f(e)
    return got


def version_of(skill_md: Path) -> str:
    """读 frontmatter 的 version。逐行前缀匹配，不用正则——`\\s` 在多层转写里会被吃掉。"""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "缺"
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("version:"):
            return s.split(":", 1)[1].strip()
    return "未找到"


def classify(target: Path, src: Path) -> str:
    segs = [s.lower() for s in target.parts]
    if any(h in s for s in segs for h in SNAPSHOT_HINTS):
        return "snapshot"
    if not target.exists():
        return "absent"
    try:
        if str(Path(os.path.realpath(str(target)))).lower() == \
           str(Path(os.path.realpath(str(src)))).lower():
            return "junction"
    except OSError:
        pass
    return "copy"


HOST_SUBDIRS = (".agents", ".codex", ".claude", ".qoder", ".workbuddy-ai", ".workbuddy", ".dsh")


def candidates(home: Path, name: str, extra: list[str]) -> list[Path]:
    """候选位置枚举。与分发器同口径，但独立实现（不 import）。"""
    out: list[Path] = [home / s / "skills" / name for s in HOST_SUBDIRS]
    for r in extra:
        rp = Path(r)
        out.append(rp / "skills" / name)
        out.append(rp / name)
        out.extend(rp / s / "skills" / name for s in HOST_SUBDIRS)
    return out


def diff_one(tgt: Path, src_map: dict[str, str]) -> dict:
    tgt_map = walk(tgt)
    only_src = sorted(set(src_map) - set(tgt_map))
    only_tgt = sorted(set(tgt_map) - set(src_map))
    differ = sorted(k for k in src_map if k in tgt_map and src_map[k] != tgt_map[k])
    return {"only_src": only_src, "only_tgt": only_tgt, "differ": differ,
            "same": len(src_map) - len(only_src) - len(differ), "total": len(tgt_map)}


def selftest() -> int:
    cases: list[tuple[str, bool, str]] = []

    def chk(name: str, cond: bool, detail: str = "") -> None:
        cases.append((name, bool(cond), detail))

    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        src = t / "src"
        for d in TREES + ("tasks",):
            (src / d).mkdir(parents=True, exist_ok=True)
        (src / "SKILL.md").write_text("---\nversion: 1.2.3\n---\n", encoding="utf-8")
        (src / "README.md").write_text("r\n", encoding="utf-8")
        (src / "Ledger.md").write_text("l\n", encoding="utf-8")
        (src / "references" / "x.md").write_text("x\n", encoding="utf-8")
        (src / "scripts" / "a.py").write_text("a\n", encoding="utf-8")
        (src / "tasks" / "own.yaml").write_text("own\n", encoding="utf-8")
        sm = walk(src)
        chk("S1 只收白名单（3 顶层 + references/x + scripts/a）",
            set(sm) == {"SKILL.md", "README.md", "Ledger.md", "references/x.md", "scripts/a.py"},
            f"{sorted(sm)}")

        def mk(name: str) -> Path:
            c = t / name
            (c / "references").mkdir(parents=True)
            (c / "scripts").mkdir(parents=True)
            for rel in ("SKILL.md", "README.md", "Ledger.md", "references/x.md", "scripts/a.py"):
                (c / rel).write_bytes((src / rel).read_bytes())
            return c

        c_ok = mk("ok")
        chk("S2 完全一致判零差异", not any(diff_one(c_ok, sm)[k]
                                    for k in ("only_src", "only_tgt", "differ")),
            str(diff_one(c_ok, sm)))
        chk("S3 一致性下版本读得到", version_of(c_ok / "SKILL.md") == "1.2.3",
            version_of(c_ok / "SKILL.md"))

        c_mod = mk("mod")
        (c_mod / "Ledger.md").write_text("l2\n", encoding="utf-8")
        d = diff_one(c_mod, sm)
        chk("S4a 内容不同被抓（仅改未抽样文件 Ledger.md 也抓得到）",
            d["differ"] == ["Ledger.md"], str(d["differ"]))

        c_extra = mk("extra")
        (c_extra / "scripts" / "stale.py").write_text("s\n", encoding="utf-8")
        chk("S4b 仅副本有被抓", diff_one(c_extra, sm)["only_tgt"] == ["scripts/stale.py"],
            str(diff_one(c_extra, sm)["only_tgt"]))

        c_miss = mk("miss")
        (c_miss / "references" / "x.md").unlink()
        chk("S4c 仅母本有被抓", diff_one(c_miss, sm)["only_src"] == ["references/x.md"],
            str(diff_one(c_miss, sm)["only_src"]))

        c_task = mk("taskdiff")
        (c_task / "tasks").mkdir(exist_ok=True)
        (c_task / "tasks" / "own.yaml").write_text("different\n", encoding="utf-8")
        (c_task / "tmp").mkdir(exist_ok=True)
        (c_task / "tmp" / "junk.txt").write_text("j\n", encoding="utf-8")
        # ⚠️ 仅造顶层 tasks/tmp 差异是**弱用例**：walk() 只遍历 references/scripts/assets
        # 三棵树，顶层 tasks/ 根本不在射程内 —— 于是这些用例在 NEVER 集合被删掉后仍然绿。
        # 想过 NEVER 这条判据，差异必须造在**树内部**的嵌套目录里（实测变异才红）。
        (c_task / "scripts" / "__pycache__").mkdir(exist_ok=True)
        (c_task / "scripts" / "__pycache__" / "a.cpython-313.pyc").write_bytes(b"\x00\x01")
        (c_task / "references" / "tmp").mkdir(exist_ok=True)
        (c_task / "references" / "tmp" / "scratch.md").write_text("old\n", encoding="utf-8")
        d5 = diff_one(c_task, sm)
        chk("S5 tasks/tmp/__pycache__ 的差异不报（含树内嵌套，能真正验到 NEVER）",
            not (d5["only_src"] or d5["only_tgt"] or d5["differ"]), str(d5))

        c_bak = mk("bakdiff")
        (c_bak / "scripts" / "a.py.bak-20260101-0000").write_text("old\n", encoding="utf-8")
        (c_bak / "references" / "x.md.bak-20260101-0000").write_text("old\n", encoding="utf-8")
        d6 = diff_one(c_bak, sm)
        chk("S6 `*.bak-*` 出现在文件名中间也不计入",
            not (d6["only_src"] or d6["only_tgt"] or d6["differ"]), str(d6))

        chk("S7 母本自身判 junction", classify(src, src) == "junction", classify(src, src))
        chk("S8 副本判 copy", classify(c_ok, src) == "copy", classify(c_ok, src))
        snap = t / "_skill-backups" / "s1" / "ai-workflow"
        snap.mkdir(parents=True)
        chk("S9 历史快照判 snapshot", classify(snap, src) == "snapshot", classify(snap, src))

        # S10 追加根按宿主子目录展开（只给工作区根即可覆盖 <root>/.claude/skills/<name>）
        ws = t / "ws"
        got = {str(x).replace("\\", "/").lower()
               for x in candidates(t / "home", "ai-workflow", [str(ws)])}
        want = str(ws / ".claude" / "skills" / "ai-workflow").replace("\\", "/").lower()
        chk("S10 追加根展开到 <root>/.claude/skills/<name>", want in got, f"未命中：{want}")

    bad = [c for c in cases if not c[1]]
    for name, okk, detail in cases:
        print(f"  [{'OK' if okk else 'FAIL'}] {name}" + (f" — {detail}" if not okk else ""))
    print(f"\n自测：{len(cases) - len(bad)}/{len(cases)} 通过")
    return 0 if not bad else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="副本一致性全量验收（只读）")
    ap.add_argument("--source", help="母本技能根（缺省由本脚本位置反推）")
    ap.add_argument("--root", action="append", default=[], help="追加扫描根（可重复）")
    ap.add_argument("--config", help="JSON 配置：{\"extra_roots\": [...]}")
    ap.add_argument("--show", type=int, default=20, help="每类差异最多列出多少条（缺省 20）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    src = Path(os.path.realpath(a.source)) if a.source else \
        Path(os.path.realpath(str(Path(__file__)))).parent.parent
    if not (src / "SKILL.md").is_file():
        print(f"[FAIL] {src} 下没有 SKILL.md，不是技能根")
        return 2

    home = src.parents[2]
    cfg_path = Path(a.config) if a.config else home / ".workbuddy/cache/ai-workflow/copies.json"
    extra: list[str] = list(a.root)
    if cfg_path.is_file():
        try:
            import json
            extra += list((json.loads(cfg_path.read_text(encoding="utf-8"))
                           .get("extra_roots") or []))
        except (OSError, ValueError) as e:
            print(f"[WARN] 配置不可解析，已忽略：{cfg_path}（{e}）")

    cands: list[Path] = candidates(home, src.name, extra)

    src_map = walk(src)
    src_ver = version_of(src / "SKILL.md")
    print(f"母本：{src}　版本 {src_ver}　白名单文件 {len(src_map)}\n")

    rows, fails = [], 0
    seen: set[str] = set()
    for c in cands:
        key = str(c).lower()
        if key in seen:
            continue
        seen.add(key)
        kind = classify(c, src)
        if kind in ("absent", "snapshot"):
            continue
        if kind == "junction":
            rows.append((c, "联接（跟随母本）", "-", "-", "-", "-"))
            continue
        d = diff_one(c, src_map)
        ver = version_of(c / "SKILL.md")
        bad = bool(d["only_src"] or d["only_tgt"] or d["differ"]) or ver != src_ver
        if bad:
            fails += 1
        rows.append((c, "一致" if not bad else "**不一致**", ver,
                     str(len(d["only_src"])), str(len(d["only_tgt"])), str(len(d["differ"]))))

    if not rows:
        print("[FAIL] 未发现任何副本（母本以外的技能根）—— 结论不可得，不等于一致")
        return 2

    print("| 位置 | 结论 | 版本 | 仅母本有 | 仅副本有 | 内容不同 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for c, verdict, ver, a1, a2, a3 in rows:
        print(f"| {c} | {verdict} | {ver} | {a1} | {a2} | {a3} |")
    print()

    for c, verdict, ver, *_ in rows:
        if "不一致" not in verdict:
            continue
        d = diff_one(c, src_map)
        print(f"── {c} ──")
        for label, key in (("仅母本有", "only_src"), ("仅副本有", "only_tgt"), ("内容不同", "differ")):
            items = d[key]
            if not items:
                continue
            print(f"  {label}（{len(items)}）：")
            for k in items[:a.show]:
                print(f"    {k}")
            if len(items) > a.show:
                print(f"    …另有 {len(items) - a.show} 个")
        if ver != src_ver:
            print(f"  版本：母本 {src_ver} ／ 此处 {ver}")
        print()

    copies = [r for r in rows if "联接" not in r[1]]
    print(f"结论：副本 {len(copies)} 处（另有联接 {len(rows) - len(copies)} 处不参与比对），"
          f"不一致 {fails} 处")
    if fails:
        print("[FAIL] 存在差异 —— 用 sync_copies.py 对齐后再复验")
        return 1
    print("[OK] 全部副本与母本逐文件一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
