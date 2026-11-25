#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [自研工具] sync_copies.py
# 名称：sync_copies.py（技能母本 → 各独立副本的镜像分发器）
# 用途：把技能母本按**白名单**镜像到每一处独立副本，并把「这处到底是副本还是母本的联接」
#       变成可判定的前置条件；覆盖前自动备份，覆盖后自证零差异。
# 适用场景：技能本体改动需要让**所有副本**同步（多宿主／多实例家共用一份技能时）。
#           **不适用**：历史备份快照（更新它们等于篡改存档）、目录联接（分发=改母本自己）、
#           副本的 tasks/ 与 tmp/（各副本有自己的任务档案，覆盖即毁数据）。
# 作者：ai-workflow 自研（实施-效率改进建议-2026-11-25，2026-11-25）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/sync_copies.py
"""技能母本 → 各独立副本的白名单镜像分发（默认 dry-run）。

为什么需要它：技能被多宿主共用时，各副本会各自漂移成「母本已改、副本还是旧实现」。
手工复制既判不出「这处是副本还是母本自己的联接」，也没有幂等保证 —— 而**对联接做复制
等于往母本里写**，会虚报「已分发 N 处」。本脚本把这几件事变成可判定的前置条件。

四条判据：
1. **只碰独立实体**：`realpath` 相同或 `st_ino` 相同 ⇒ 判为母本联接，跳过。判定不用
   `Path.islink()` —— Windows 目录联接的 `islink()` 恒为 `False`（实测），靠它判会把
   联接当副本。
2. **历史快照一律跳过**：路径段命中 `backup`/`_skill-backups`/`skills-backup-*` ⇒ 视为存档。
3. **白名单镜像含删除**：只同步 `SKILL.md`/`README.md`/`Ledger.md`/`.gitignore` 与
   `references/`/`scripts/`/`assets/` 三棵树；白名单内母本已无的文件会被删（否则旧脚本残留）。
   `tasks/`/`tmp/`/`_archive/`/`__pycache__/`/`*.bak-*` 一律不碰。
4. **覆盖前备份、覆盖后自证**：写盘前把将被改写/删除的文件复制进带时间戳的备份目录；
   落盘后重新比对一次，仍有差异即 FAIL（防「部分写入却报成功」）。

退出码：0 成功（含 dry-run）／1 自证失败或备份失败／2 用法或定位错误。
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ── 白名单：相对技能根的顶层条目 ────────────────────────────────────────────
INCLUDE_FILES = ("SKILL.md", "README.md", "Ledger.md", ".gitignore")
INCLUDE_DIRS = ("references", "scripts", "assets")
# 即使落在白名单目录内也不碰（副本自己的任务档案/缓存/历史备份）
SKIP_PARTS = frozenset({"__pycache__", ".git", "tmp", "_archive", "tasks", "output", "node_modules"})
SKIP_NAME_SUBSTR = ".bak-"   # 语义同 .gitignore 的 `*.bak-*`：出现在**中间**也算
# 路径段命中这些即判为历史快照存档
BACKUP_HINTS = ("_skill-backups", "skills-backup", "skills-backup-")

# 用户目录下可能存在的宿主技能根（逐个子目录找 skills/ai-workflow）
HOST_SUBDIRS = (".agents", ".codex", ".claude", ".qoder", ".workbuddy-ai", ".workbuddy", ".dsh")


def real(p: Path) -> Path:
    return Path(os.path.realpath(str(p)))


def locate_source(explicit: str | None) -> tuple[Path | None, str]:
    """定位母本。默认由本脚本位置反推（scripts/ → 技能根），并要求它是 git 工作区。

    ⚠️ 不用 `Path.home()`：本机宿主把 HOME 设成数据家目录，`~/.workbuddy/skills/...`
    会落到一条**目录联接**上。也不用 `USERPROFILE`：多实例家环境下两者不等价。
    由 `__file__` 反推是唯一与「正在运行的是哪份拷贝」绑定的定位方式。
    """
    cand = real(Path(explicit)) if explicit else real(Path(__file__)).parent.parent
    if not (cand / "SKILL.md").is_file():
        return None, f"{cand} 下没有 SKILL.md，不是技能根"
    return cand, "ok"


def is_git_repo(src: Path) -> bool:
    return (src / ".git").exists()


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect(root: Path) -> dict[str, Path]:
    """白名单内的文件集合（相对路径 posix → 绝对路径）。"""
    out: dict[str, Path] = {}
    for name in INCLUDE_FILES:
        p = root / name
        if p.is_file():
            out[name] = p
    for d in INCLUDE_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(root)
            if any(part in SKIP_PARTS for part in rel.parts):
                continue
            if SKIP_NAME_SUBSTR in p.name:
                continue
            out[rel.as_posix()] = p
    return out


def classify(target: Path, src: Path) -> tuple[str, str]:
    """返回 (类别, 理由)。类别 ∈ independent / junction / backup / absent。"""
    segs = [s.lower() for s in target.parts]
    if any(h in s for s in segs for h in BACKUP_HINTS):
        return "backup", "路径段命中历史快照特征"
    if not target.exists():
        return "absent", "目标不存在"
    tr, sr = real(target), real(src)
    if str(tr).lower() == str(sr).lower():
        return "junction", "realpath 与母本相同（是母本的联接）"
    try:
        # 不同卷上 st_ino 可能无意义（为 0），仅在两侧都非 0 时才作为判据
        ti, si = target.stat().st_ino, src.stat().st_ino
        if ti and si and ti == si:
            return "junction", "inode 与母本相同（同一实体）"
    except OSError as e:
        return "absent", f"stat 失败：{e}"
    return "independent", "独立实体"


def discover(src: Path, extra_roots: list[str]) -> list[Path]:
    """枚举候选副本：用户目录下各宿主技能根 ＋ 显式追加的根。"""
    home = src.parents[2]                     # skills → .workbuddy → 用户目录
    cands: list[Path] = []
    for sub in HOST_SUBDIRS:
        cands.append(home / sub / "skills" / src.name)
    for root in extra_roots:
        rp = Path(root)
        # 追加根三种常见形态都试：<root>/skills/<name>、<root>/<name>、
        # <root>/<宿主子目录>/skills/<name>（工作区根就属第三种，只写工作区根即可）
        cands.append(rp / "skills" / src.name)
        cands.append(rp / src.name)
        for sub in HOST_SUBDIRS:
            cands.append(rp / sub / "skills" / src.name)
    seen, out = set(), []
    for c in cands:
        key = str(c).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def load_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[WARN] 配置文件不可解析，已忽略：{path}（{e}）")
        return {}


def plan_one(target: Path, src_map: dict[str, Path]) -> dict:
    tgt_map = collect(target)
    create = sorted(k for k in src_map if k not in tgt_map)
    update = sorted(k for k in src_map if k in tgt_map and sha(src_map[k]) != sha(tgt_map[k]))
    delete = sorted(k for k in tgt_map if k not in src_map)
    same = len(src_map) - len(create) - len(update)
    return {"target": target, "create": create, "update": update,
            "delete": delete, "same": same}


def backup_one(p: dict, target: Path, bak_root: Path) -> tuple[Path | None, str]:
    """把将被改写/删除的文件复制进带时间戳的备份目录。返回 (备份目录, 说明)。"""
    touched = p["update"] + p["delete"]
    if not touched:
        return None, "无文件被改写，未备份"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # ⚠️ 备份目录名必须**唯一到每一处副本**。首版用「父目录名-目标名」，而六处副本的父目录
    # 全叫 `skills`、目标名全叫 `ai-workflow` ⇒ 六次备份挤进同一个目录，后写的覆盖先写的
    # （实测 6 处只生成 2 个备份目录，4 份备份静默丢失）。补祖父目录名 ＋ 绝对路径短哈希。
    parts = [target.parent.parent.name, target.parent.name, target.name]
    slug = "-".join("".join(c if c.isalnum() or c in "-._" else "_" for c in p)
                    for p in parts if p)
    tag = hashlib.sha256(str(target).lower().encode("utf-8")).hexdigest()[:8]
    dst_root = bak_root / f"{stamp}-{slug}-{tag}"
    n = 0
    for k in touched:
        s = target / k
        if not s.is_file():
            continue
        d = dst_root / k
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
        n += 1
    if not n:
        return None, "无可备份文件（可能已被并发删除）"
    return dst_root, f"已备份 {n} 个文件 → {dst_root}"


def apply_one(p: dict, src_map: dict[str, Path]) -> tuple[int, int, int, list[str]]:
    t = p["target"]
    n_c = n_u = n_d = 0
    errs: list[str] = []
    for k in p["create"] + p["update"]:
        dst = t / k
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_map[k], dst)
        except OSError as e:
            errs.append(f"写入失败 {k}: {e}")
            continue
        if k in p["create"]:
            n_c += 1
        else:
            n_u += 1
    for k in p["delete"]:
        try:
            (t / k).unlink()
            n_d += 1
        except OSError as e:
            errs.append(f"删除失败 {k}: {e}")
    return n_c, n_u, n_d, errs


# ── 自测：造沙盒，逐条验四条判据 ────────────────────────────────────────────
def selftest() -> int:
    cases: list[tuple[str, bool, str]] = []

    def chk(name: str, cond: bool, detail: str = "") -> None:
        cases.append((name, bool(cond), detail))

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        src = tmp / "home" / ".workbuddy" / "skills" / "ai-workflow"
        for d in ("references", "scripts", "assets", "tasks", "tmp", "_archive"):
            (src / d).mkdir(parents=True, exist_ok=True)
        (src / ".git").mkdir()
        (src / "SKILL.md").write_text("---\nname: ai-workflow\nversion: 9.9.9\n---\n", encoding="utf-8")
        (src / "README.md").write_text("readme\n", encoding="utf-8")
        (src / "Ledger.md").write_text("ledger\n", encoding="utf-8")
        (src / "references" / "a.md").write_text("A\n", encoding="utf-8")
        (src / "scripts" / "s.py").write_text("print(1)\n", encoding="utf-8")
        (src / "scripts" / "x.py.bak-20260101-0000").write_text("old\n", encoding="utf-8")
        (src / "tasks" / "own.yaml").write_text("task\n", encoding="utf-8")
        (src / "tmp" / "scratch.txt").write_text("tmp\n", encoding="utf-8")
        # ⚠️ 顶层 tasks/tmp 不在 INCLUDE_DIRS 射程内 ⇒ 只造它们的差异验不到 SKIP_PARTS。
        # 必须再在**树内部**造嵌套样本，SKIP_PARTS 才是被真正执行的判据。
        (src / "scripts" / "__pycache__").mkdir(exist_ok=True)
        (src / "scripts" / "__pycache__" / "z.pyc").write_bytes(b"\x00\x01")
        (src / "references" / "tmp").mkdir(exist_ok=True)
        (src / "references" / "tmp" / "old.md").write_text("old\n", encoding="utf-8")

        # S1 collect 排除 tasks/tmp/__pycache__/*.bak-*
        m = collect(src)
        chk("S1 白名单只收 5 个（SKILL/README/Ledger + references/a + scripts/s）",
            set(m) == {"SKILL.md", "README.md", "Ledger.md", "references/a.md", "scripts/s.py"},
            f"实收 {sorted(m)}")

        # S2 空副本 → 全 create
        tgt = tmp / "copy1"
        tgt.mkdir()
        p = plan_one(tgt, m)
        chk("S2 空副本判 5 项 create", len(p["create"]) == 5 and not p["update"] and not p["delete"],
            f"create={len(p['create'])} update={len(p['update'])} delete={len(p['delete'])}")

        # S3 apply 后自证零差异 + 幂等
        apply_one(p, m)
        p2 = plan_one(tgt, m)
        chk("S3 落盘后重比为零差异", not (p2["create"] or p2["update"] or p2["delete"]),
            f"{p2['create']} {p2['update']} {p2['delete']}")

        # S4 副本多出的白名单内文件 → 判 delete；备份先落地
        extra = tgt / "scripts" / "stale.py"
        extra.write_text("stale\n", encoding="utf-8")
        p3 = plan_one(tgt, m)
        bak, why = backup_one(p3, tgt, tmp / "bak")
        chk("S4a 副本独有文件判 delete", p3["delete"] == ["scripts/stale.py"], f"{p3['delete']}")
        chk("S4b 备份目录含该文件", bak is not None and (bak / "scripts" / "stale.py").is_file(), why)

        # S5 内容不同 → 判 update（改一个字节）
        (tgt / "references" / "a.md").write_text("A2\n", encoding="utf-8")
        p4 = plan_one(tgt, m)
        chk("S5 内容差异判 update", p4["update"] == ["references/a.md"], f"{p4['update']}")

        # S6 母本自己作为目标 → junction 拒绝
        kind, why2 = classify(src, src)
        chk("S6 母本自身判 junction（拒发）", kind == "junction", f"{kind}:{why2}")

        # S7 历史快照路径 → backup 跳过
        snap = tmp / "home" / "_skill-backups" / "codex-20260101" / "ai-workflow"
        snap.mkdir(parents=True)
        kind2, why3 = classify(snap, src)
        chk("S7 历史快照判 backup（跳过）", kind2 == "backup", f"{kind2}:{why3}")

        # S8 普通副本判 independent
        kind3, why4 = classify(tgt, src)
        chk("S8 副本判 independent", kind3 == "independent", f"{kind3}:{why4}")

        # S9 追加根按宿主子目录展开：只给工作区根也能找到 <root>/.agents/skills/<name>
        ws = tmp / "ws"
        (ws / ".agents" / "skills" / "ai-workflow").mkdir(parents=True)
        found = {str(x).replace("\\", "/").lower() for x in discover(src, [str(ws)])}
        want = str(ws / ".agents" / "skills" / "ai-workflow").replace("\\", "/").lower()
        chk("S9 追加根展开到 <root>/.agents/skills/<name>", want in found,
            f"未命中：{want}")

        # S10 备份目录名对每处副本唯一 —— 父目录（skills）与目标名（ai-workflow）都相同的
        # 两处副本，在同一秒内备份不得落进同一个目录（首版实测 6 处只生成 2 个备份目录）
        a1 = tmp / "h1" / "skills" / "ai-workflow"
        a2 = tmp / "h2" / "skills" / "ai-workflow"
        for pa in (a1, a2):
            (pa / "scripts").mkdir(parents=True)
            (pa / "SKILL.md").write_text("old\n", encoding="utf-8")
        fk = {"SKILL.md": src / "SKILL.md"}
        b1, _ = backup_one(plan_one(a1, fk), a1, tmp / "bak2")
        b2, _ = backup_one(plan_one(a2, fk), a2, tmp / "bak2")
        chk("S10 两处同名副本的备份目录不撞",
            b1 is not None and b2 is not None and b1 != b2,
            f"{b1} vs {b2}")

    bad = [c for c in cases if not c[1]]
    for name, okk, detail in cases:
        print(f"  [{'OK' if okk else 'FAIL'}] {name}" + (f" — {detail}" if not okk else ""))
    print(f"\n自测：{len(cases) - len(bad)}/{len(cases)} 通过")
    return 0 if not bad else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="技能母本 → 独立副本的白名单镜像分发（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真正落盘（缺省只打印计划）")
    ap.add_argument("--source", help="母本技能根（缺省由本脚本位置反推）")
    ap.add_argument("--root", action="append", default=[], help="追加扫描根（可重复）")
    ap.add_argument("--config", help="JSON 配置：{\"extra_roots\": [...]}（缺省 ~/.workbuddy/cache/ai-workflow/copies.json）")
    ap.add_argument("--backup-dir", help="备份根（缺省 ~/.workbuddy/cache/ai-workflow/copies-backup）")
    ap.add_argument("--no-backup", action="store_true", help="覆盖前不备份（不推荐：副本本地改动将不可恢复）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    src, why = locate_source(a.source)
    if src is None:
        print(f"[FAIL] 母本定位失败：{why}")
        return 2
    if not is_git_repo(src) and not a.source:
        print(f"[FAIL] {src} 不是 git 工作区，判为「副本」而非母本。"
              f"若确实要从这里分发，请显式给 --source。")
        return 2

    home = src.parents[2]
    cfg_path = Path(a.config) if a.config else home / ".workbuddy/cache/ai-workflow/copies.json"
    cfg = load_config(cfg_path)
    extra = list(a.root) + list(cfg.get("extra_roots") or [])
    bak_root = Path(a.backup_dir) if a.backup_dir else home / ".workbuddy/cache/ai-workflow/copies-backup"

    src_map = collect(src)
    print(f"母本：{src}")
    print(f"白名单文件数：{len(src_map)}")
    print(f"追加根：{extra or '（无）'}{'' if cfg else ' —— 未找到配置文件，仅扫用户目录'}\n")

    plans, skipped = [], []
    for t in discover(src, extra):
        kind, reason = classify(t, src)
        if kind != "independent":
            skipped.append((t, kind, reason))
            continue
        plans.append(plan_one(t, src_map))

    if skipped:
        print("── 跳过 ──")
        for t, kind, reason in skipped:
            if kind == "absent":
                continue
            print(f"  [{kind}] {t} — {reason}")
        absent = sum(1 for _, k, _ in skipped if k == "absent")
        if absent:
            print(f"  （另有 {absent} 处候选不存在，未列出）")
        print()

    print("── 分发计划 ──")
    print("| 副本 | 新增 | 更新 | 删除 | 未变 |")
    print("| --- | --- | --- | --- | --- |")
    for p in plans:
        print(f"| {p['target']} | {len(p['create'])} | {len(p['update'])} "
              f"| {len(p['delete'])} | {p['same']} |")
    print()

    for p in plans:
        if not (p["create"] or p["update"] or p["delete"]):
            continue
        print(f"── {p['target']} ──")
        for k in p["create"]:
            print(f"  + {k}")
        for k in p["update"][:40]:
            print(f"  ~ {k}")
        if len(p["update"]) > 40:
            print(f"  ~ …另有 {len(p['update']) - 40} 个")
        for k in p["delete"]:
            print(f"  - {k}")
        print()

    if not a.apply:
        print("[DRY-RUN] 未落盘。加 --apply 生效。")
        return 0

    if not plans:
        print("无可分发目标。")
        return 0

    print("── 落地 ──")
    tot = [0, 0, 0]
    errs: list[str] = []
    for p in plans:
        if not a.no_backup:
            _, why_b = backup_one(p, p["target"], bak_root)
            if p["update"] or p["delete"]:
                print(f"    备份：{why_b}")
        c, u, d, e = apply_one(p, src_map)
        tot[0] += c
        tot[1] += u
        tot[2] += d
        errs += e
        print(f"  {p['target']}: 新增 {c} / 更新 {u} / 删除 {d}")
    print(f"\n合计：新增 {tot[0]} / 更新 {tot[1]} / 删除 {tot[2]}；"
          f"副本 {len(plans)} 处，跳过 {len(skipped)} 处")

    # 覆盖后自证：仍有差异即失败（防「部分写入却报成功」）
    residual = []
    for p in plans:
        q = plan_one(p["target"], src_map)
        if q["create"] or q["update"] or q["delete"]:
            residual.append((p["target"], q))
    if residual or errs:
        print("\n[FAIL] 落盘后自证未通过：")
        for e in errs[:10]:
            print(f"  {e}")
        for t, q in residual:
            print(f"  {t}: 残留 新增{len(q['create'])} 更新{len(q['update'])} 删除{len(q['delete'])}")
        return 1
    print("[OK] 落盘后自证：全部副本与母本零差异")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
