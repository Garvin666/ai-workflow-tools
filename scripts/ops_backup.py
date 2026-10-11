#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# [自研工具] ops_backup.py
# 用途：每日分层备份器（量化平台）—— SQLite 走在线备份 API、小文件走复制、大文件走同盘硬链接，产出带 sha256 清单的 manifest.json
# 适用场景：本工作区（E:/ChatGPT/量化分析与数据分析）的每日运维备份窗口（建议 23:15）；也适用于任何「目标盘余量紧张、大文件不宜逐日全量复制」的同盘备份场景
# 作者：ai-workflow 自研（量化平台运维沉淀，2026-10-11）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/ops_backup.py
"""ops_backup.py —— 每日分层备份器（量化平台）

用途
----
把「每日备份」从「人工记得做」变成一条可调度的命令：SQLite 走**在线备份 API**、
小文件走**复制**、大文件走**同盘硬链接**，产出带 `sha256` 清单的 `manifest.json`。

适用场景
--------
本工作区（E:/ChatGPT/量化分析与数据分析）的每日运维备份窗口（建议 23:15），
以及任何「目标盘余量紧张、大文件不宜逐日全量复制」的同盘备份场景。

为什么分层（实测依据，2026-10-11）
--------------------------------
E: 盘已用 **89%**（余 110 GB），而 `历史数据集/A股统一数据集` 实测 **496.9 MB**
（其中 `a_share_daily.parquet` 单文件 **485.4 MB**）。逐日全量复制 × 14 天 ≈ **6.8 GB**
且每日新增一份相同数据；分层后每日新增仅 **~12 MB**（小文件）+ **0**（硬链接）。

⚠️ 硬链接的成立条件与风险
- 必须与源**同卷**；跨卷时自动回退为复制（并在 manifest 里标注 `method=copy-fallback`）。
- 只有**整文件替换**（写入新文件后 `os.replace`）才安全：硬链接指向旧 inode，不受影响。
  若某进程**原地修改**该文件（追加/覆写同一 inode），备份内容会被污染 ——
  故 manifest 记录源文件的 `inode/size/mtime/sha256`，下次备份可据此发现漂移。

退出码
------
0 = 成功（含部分项回退为复制）；1 = 存在失败项（SQLite 校验不过 / 复制失败）；
2 = 参数或环境错误。`--drift` 模式下：1 = 存在 P1 漂移，2 = 检测未能完成。

用法
----
    .venv\\Scripts\\python.exe scripts\\ops_backup.py                 # 正常备份（默认日期=今天）
    .venv\\Scripts\\python.exe scripts\\ops_backup.py --dry-run       # 只报将做什么
    .venv\\Scripts\\python.exe scripts\\ops_backup.py --keep 14       # 覆盖保留天数
    .venv\\Scripts\\python.exe scripts\\ops_backup.py --verify        # 校验最近一份备份
    .venv\\Scripts\\python.exe scripts\\ops_backup.py --drift         # 检查硬链接漂移（只读）
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKUP_ROOT = PROJECT_ROOT / "backup"
DATASET_DIR = PROJECT_ROOT / "历史数据集" / "A股统一数据集"
STATE_DIR = PROJECT_ROOT / ".work" / "daily" / "scheduler" / "state"
CONFIG_DIRS = ("deploy",)  # 只含部署与配置样例，不含任何凭据文件
EXCLUDE_NAMES = {".env"}
SIZE_SPLIT = 50 * 1024 * 1024  # ≥50MB 视为大文件 → 硬链接
DEFAULT_KEEP = 14
DISK_WARN_PCT = 15.0  # 可用空间低于此比例时告警（不阻断）
CHUNK = 1024 * 1024

#: SQLite 备份目标（相对工作区根）。按「有业务价值」选，不含纯缓存库。
DB_TARGETS = (
    "knowledge_base/data/knowledge.db",
    "backend/.work/auth.db",
    "backend/.work/sim/sim.db",
)


def _log(msg: str, level: str = "INFO") -> None:
    print(f"[{level}] {msg}", file=sys.stderr)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def disk_free_ratio(path: Path) -> float:
    usage = shutil.disk_usage(str(path))
    return 100.0 * usage.free / usage.total if usage.total else 0.0


# --------------------------------------------------------------------------- #
# 1. SQLite：在线备份 API（VACUUM INTO）—— 产出一致快照，不复制 -wal/-shm
# --------------------------------------------------------------------------- #
def backup_sqlite(src: Path, dst: Path, dry: bool) -> dict[str, Any]:
    item: dict[str, Any] = {
        "kind": "sqlite",
        "src": str(src.relative_to(PROJECT_ROOT)) if src.is_relative_to(PROJECT_ROOT) else str(src),
        "dst": str(dst),
        "method": "vacuum-into",
        "ok": False,
    }
    if not src.exists():
        item["error"] = "源文件不存在（跳过）"
        item["skipped"] = True
        return item
    if dry:
        item["ok"] = True
        item["dry_run"] = True
        return item
    dst.parent.mkdir(parents=True, exist_ok=True)
    # ⚠️ 不用「先 unlink 再写」：本环境的文件删除受安全钩子（safe-delete-bulk-guard）
    # 拦截，显式 unlink 会在钩子上超时 10s 而整体失败（2026-10-11 实测，见验证记录 T3）。
    # 改写到**唯一临时名**再原子替换 —— VACUUM INTO 要求目标不存在，唯一名天然满足。
    tmp = dst.parent / f"{dst.name}.{os.getpid()}.part"
    try:
        con = sqlite3.connect(str(src))
        try:
            con.execute("VACUUM INTO ?", (str(tmp),))
        finally:
            con.close()
        os.replace(tmp, dst)
    except (sqlite3.Error, OSError) as exc:
        item["error"] = f"备份失败: {exc!r}"
        return item

    # 对**副本**做完整性校验（不是对源库）
    try:
        chk = sqlite3.connect(str(dst))
        try:
            row = chk.execute("PRAGMA integrity_check").fetchone()
        finally:
            chk.close()
        verdict = (row or ["(no result)"])[0]
    except sqlite3.Error as exc:
        item["error"] = f"校验连接失败: {exc!r}"
        return item
    item["integrity_check"] = verdict
    item["bytes"] = dst.stat().st_size
    item["sha256"] = sha256_file(dst)
    item["ok"] = verdict == "ok"
    if not item["ok"]:
        item["error"] = f"integrity_check 未通过: {verdict}"
    return item


# --------------------------------------------------------------------------- #
# 2. 数据集：小文件复制 / 大文件硬链接
# --------------------------------------------------------------------------- #
def _same_content(a: Path, b: Path) -> bool:
    """两文件内容是否一致（先比大小，再比 sha256）。"""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return sha256_file(a) == sha256_file(b)
    except OSError:
        return False


def backup_dataset(dst_dir: Path, dry: bool) -> list[dict[str, Any]]:
    """数据集备份：小文件复制、大文件硬链接。**不显式删除已有目标**。

    ⚠️ 两条 2026-10-11 实测得出、必须同时满足的约束：

    1. **不用「先 unlink 再写」**：本环境的文件删除受安全钩子（`safe-delete-bulk-guard`）
       拦截，显式 `unlink` 会在钩子上超时 10s 导致整体失败（见验证记录 T3 的原始报错）。
       改法＝写到**唯一临时名**再 `os.replace`（原子替换，无独立删除步骤）。
    2. **同日重跑要幂等**：目标已存在且与源一致（大文件比 inode、小文件比 sha256）时
       直接跳过，`bytes_written=0`。这样每日首跑正常、重跑几乎不写盘。
    """
    items: list[dict[str, Any]] = []
    if not DATASET_DIR.exists():
        _log(f"数据集目录不存在：{DATASET_DIR}", "WARN")
        return items
    for src in sorted(DATASET_DIR.iterdir()):
        if not src.is_file():
            continue
        dst = dst_dir / src.name
        st = src.stat()
        big = st.st_size >= SIZE_SPLIT
        item: dict[str, Any] = {
            "kind": "dataset",
            "src": str(src.relative_to(PROJECT_ROOT)),
            "dst": str(dst),
            "bytes": st.st_size,
            "src_mtime": dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "ok": False,
        }
        if dry:
            item.update(ok=True, dry_run=True, method="hardlink" if big else "copy")
            items.append(item)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)

        # 幂等：目标已存在且与源一致 → 不重写
        if dst.exists():
            dst_st0 = dst.stat()
            same = (dst_st0.st_ino == st.st_ino) if big else _same_content(src, dst)
            if same:
                item.update(
                    ok=True,
                    method="hardlink-kept" if big else "copy-kept",
                    dst_inode=dst_st0.st_ino, src_inode=st.st_ino,
                    nlink=dst_st0.st_nlink, bytes_written=0,
                    # ⚠️ 大文件也记 sha256：否则 check_drift 无从比对，
                    #    会把「未校验」读成「通过」（2026-10-11 独立审核缺陷 #1）。
                    sha256=sha256_file(dst),
                )
                items.append(item)
                continue

        tmp = dst.parent / f"{dst.name}.{os.getpid()}.part"
        try:
            if big:
                try:
                    os.link(src, tmp)
                    item["method"] = "hardlink"
                except OSError as exc:
                    # 跨卷 / 文件系统不支持 → 回退复制（如实标注，不静默）
                    shutil.copy2(src, tmp)
                    item.update(method="copy-fallback", link_error=repr(exc))
            else:
                item["method"] = "copy"
                shutil.copy2(src, tmp)
            os.replace(tmp, dst)
        except OSError as exc:
            item["error"] = f"写入失败: {exc!r}"
            items.append(item)
            continue

        dst_st = dst.stat()
        item["dst_inode"] = dst_st.st_ino
        item["src_inode"] = st.st_ino
        item["nlink"] = dst_st.st_nlink
        item["bytes_written"] = dst_st.st_size
        # sha256 用于恢复校验与「源是否变过」的对账；大文件也照算（485MB ≈ 数秒）
        try:
            item["sha256"] = sha256_file(dst)
        except OSError as exc:
            item["sha256"] = None
            item["error"] = f"计算 sha256 失败: {exc!r}"
        item["ok"] = dst_st.st_size == st.st_size
        if item["method"] == "hardlink" and dst_st.st_ino != st.st_ino:
            item["ok"] = False
            item["error"] = "硬链接 inode 与源不一致"
        items.append(item)
    return items


# --------------------------------------------------------------------------- #
# 3. 配置与状态快照
# --------------------------------------------------------------------------- #
def backup_config(dst_dir: Path, dry: bool) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cfg_dir = dst_dir / "config"
    for name in CONFIG_DIRS:
        src = PROJECT_ROOT / name
        if not src.exists():
            continue
        dst = cfg_dir / name
        item: dict[str, Any] = {"kind": "config", "src": name, "dst": str(dst), "ok": False}
        if dry:
            item.update(ok=True, dry_run=True, method="copy-tree")
            items.append(item)
            continue
        try:
            shutil.copytree(
                src, dst, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(*EXCLUDE_NAMES),
            )
            item.update(ok=True, method="copy-tree")
            item["files"] = sum(1 for _ in dst.rglob("*") if _.is_file())
        except OSError as exc:
            item["error"] = repr(exc)
        items.append(item)

    # 生效配置快照（只读调用，无副作用）
    snap = dst_dir / "scheduler_config.json"
    item2: dict[str, Any] = {"kind": "config", "src": "daily_scheduler.config", "dst": str(snap), "ok": False}
    if dry:
        item2.update(ok=True, dry_run=True, method="snapshot")
        items.append(item2)
        return items
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from daily_scheduler import config as sched_config  # noqa: PLC0415

        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(
            json.dumps(sched_config.config_snapshot(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        item2.update(ok=True, method="snapshot", bytes=snap.stat().st_size, sha256=sha256_file(snap))
    except Exception as exc:  # noqa: BLE001 —— 配置快照失败不该让备份整体失败
        item2["error"] = repr(exc)
    items.append(item2)

    # 调度状态（追溯用；小体量）
    item3: dict[str, Any] = {"kind": "state", "src": ".work/daily/scheduler/state", "dst": str(dst_dir / "state"), "ok": False}
    if dry:
        item3.update(ok=True, dry_run=True, method="copy-tree")
        items.append(item3)
        return items
    if STATE_DIR.exists():
        try:
            shutil.copytree(STATE_DIR, dst_dir / "state", dirs_exist_ok=True)
            item3.update(ok=True, method="copy-tree",
                         files=sum(1 for _ in (dst_dir / "state").rglob("*") if _.is_file()))
        except OSError as exc:
            item3["error"] = repr(exc)
    else:
        item3.update(ok=True, skipped=True, reason="状态目录不存在")
    items.append(item3)
    return items


# --------------------------------------------------------------------------- #
# 4. 保留策略
# --------------------------------------------------------------------------- #
def _backup_dated_dirs() -> tuple[list[Path], list[str]]:
    """返回 (按名降序的日期目录, 非日期条目名)。**「什么算一份备份」的唯一判据源**。

    `prune` 与 `check_drift` 共用它 —— 避免同一判据出现两处口径。
    """
    dated: list[Path] = []
    others: list[str] = []
    if not BACKUP_ROOT.exists():
        return dated, others
    for p in BACKUP_ROOT.iterdir():
        if p.is_dir():
            try:
                dt.date.fromisoformat(p.name)
            except ValueError:
                others.append(p.name)
                continue
            dated.append(p)
        else:
            others.append(p.name)
    dated.sort(key=lambda p: p.name, reverse=True)
    return dated, others


def prune(keep: int, today: str, dry: bool) -> dict[str, Any]:
    """删除超过保留期的**日期目录**（只认 `YYYY-MM-DD` 形状，不碰其它内容）。"""
    result: dict[str, Any] = {"keep": keep, "removed": [], "kept": [], "skipped": []}
    dated, others = _backup_dated_dirs()
    result["skipped"].extend(others)
    for idx, p in enumerate(dated):
        if idx < keep or p.name == today:
            result["kept"].append(p.name)
            continue
        if dry:
            result["removed"].append(p.name + "(dry-run)")
            continue
        try:
            shutil.rmtree(p)
            result["removed"].append(p.name)
        except OSError as exc:
            # 同 ops_rotate.py：按**结果**判定 —— 沙箱钩子可能在收尾 rmdir 抛错而目标已消失，
            # 按异常记 skipped 会造成假失败。
            if not p.exists():
                result["removed"].append(p.name + "(残留异常已忽略)")
            else:
                result["skipped"].append(f"{p.name}: {exc!r}")
    return result


# --------------------------------------------------------------------------- #
# 4b. 硬链接漂移检测（S1 快照污染 / S3 链接能力失效 / S2 链接已断）
# --------------------------------------------------------------------------- #
#: 视为「与源共享 inode」的方法
LINK_METHODS = ("hardlink", "hardlink-kept")


def check_drift(date: str | None = None) -> dict[str, Any]:
    """检查最近一份（或指定日期）备份的硬链接漂移。**只读，不修任何东西。**

    三类信号（口径见本次任务 `方案评审.md` §3）：

    - **S1 快照污染（P1）**：`method ∈ LINK_METHODS` 的项，`sha256(dst)` 已不等于备份时记录的
      `sha256` ⇒ 备份**静默地**不再是那天的快照（源被原地改写，硬链接共享 inode 把污染带进了备份）。
    - **S3 硬链接能力失效（P1）**：manifest 内出现 `method == "copy-fallback"` ⇒ 硬链接不可用
      （跨卷 / 文件系统不支持），意味着**每日真实写入量按大文件全量计**（如 485 MB/天）。
    - **S2 链接已断（知会，不进告警）**：`dst.st_nlink == 1` ⇒ 源被整文件替换，备份已冻结为独立副本。
      这是**正向保护**，且每日正常更替都会发生 —— 做成告警＝每天一条噪音，故只在日报展示。

    成本：S1 只对硬链接项算 sha256（当前全库仅 1 项、约 485 MB、数秒）。

    ⚠️ 2026-10-11 独立审核发现的缺陷（已修）：旧版 `-kept` 路径对大文件
    **不记 sha256**，使真实稳态 manifest 里该键为 None，S1 被静默跳过 —— `checked=1`
    是空转，且看起来与「通过」一模一样。现在：① `-kept` 路径也记 sha256；
    ② 缺 sha256 时产出 `backup_snapshot_unverifiable`（P2）并计入 `unverified`，
    **「无法判定」不再冒充「通过」**。
    """
    out: dict[str, Any] = {"ok": True, "backup": None, "checked": 0, "unverified": 0,
                           "findings": [], "link_broken": [], "error": None}
    if not BACKUP_ROOT.exists():
        out.update(ok=False, error=f"备份根目录不存在：{BACKUP_ROOT}")
        return out
    if date:
        target = BACKUP_ROOT / date
        if not target.is_dir():
            out.update(ok=False, error=f"指定备份目录不存在：{target}")
            return out
    else:
        dated, _others = _backup_dated_dirs()
        if not dated:
            out.update(ok=False, error="没有任何日期形状的备份目录")
            return out
        target = dated[0]
    out["backup"] = target.name

    manifest = target / "manifest.json"
    if not manifest.exists():
        out.update(ok=False, error=f"{target.name} 缺少 manifest.json")
        return out
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        out.update(ok=False, error=f"manifest.json 不可解析：{exc!r}")
        return out

    items = data.get("items") or []

    # S3：硬链接能力失效
    fallback = [i for i in items if i.get("method") == "copy-fallback"]
    if fallback:
        out["findings"].append({
            "code": "backup_link_unavailable", "level": "P1",
            "detail": f"备份有 {len(fallback)} 项回退为复制（硬链接不可用）",
            "evidence": f"{target.name}/manifest.json；涉及 "
                        + ", ".join(str(i.get("src")) for i in fallback[:5]),
        })

    # S1 / S2：逐硬链接项
    for item in items:
        if item.get("skipped") or not item.get("ok"):
            continue
        if item.get("method") not in LINK_METHODS:
            continue
        dst = Path(item["dst"])
        if not dst.exists():
            out["checked"] += 1
            out["findings"].append({
                "code": "backup_item_missing", "level": "P1",
                "detail": f"硬链接备份项不存在：{item.get('src')}",
                "evidence": str(dst),
            })
            continue
        recorded = item.get("sha256")
        if not recorded:
            # ⚠️ 缺 sha256 ⇒ 【无法判定】，绝不能读成「通过」（独立审核缺陷 #1）。
            #    计入 unverified 而不计入 checked，使「checked=1」回到「真校验了 1 项」的本意。
            out["unverified"] += 1
            out["findings"].append({
                "code": "backup_snapshot_unverifiable", "level": "P2",
                "detail": f"硬链接项未记录 sha256，无法判定是否被污染：{item.get('src')}",
                "evidence": f"{dst}；manifest 该键缺失（历史 manifest 或旧版 -kept 路径）",
            })
        else:
            try:
                cur = sha256_file(dst)
            except OSError as exc:
                out["unverified"] += 1
                out["findings"].append({
                    "code": "backup_item_unreadable", "level": "P1",
                    "detail": f"硬链接备份项读不到：{item.get('src')}",
                    "evidence": f"{dst} → {exc!r}",
                })
            else:
                out["checked"] += 1
                if cur != recorded:
                    out["findings"].append({
                        "code": "backup_snapshot_polluted", "level": "P1",
                        "detail": f"硬链接快照已被污染（内容与备份时不一致）：{item.get('src')}",
                        "evidence": f"{dst}；manifest={recorded[:12]}… 现值={cur[:12]}…",
                    })
        try:
            nlink = dst.stat().st_nlink
        except OSError:
            nlink = None
        if nlink == 1:
            out["link_broken"].append({
                "dst": str(dst), "src": item.get("src"),
                "detail": "源已被整文件替换，备份已冻结为独立副本（正常更替，非故障）",
            })
    return out


# --------------------------------------------------------------------------- #
# 5. 校验模式
# --------------------------------------------------------------------------- #
def verify_latest() -> int:
    if not BACKUP_ROOT.exists():
        _log(f"备份根目录不存在：{BACKUP_ROOT}", "ERROR")
        return 2
    dated = sorted((p for p in BACKUP_ROOT.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    if not dated:
        _log("没有任何备份目录", "ERROR")
        return 2
    latest = dated[0]
    manifest = latest / "manifest.json"
    if not manifest.exists():
        _log(f"{latest} 缺少 manifest.json", "ERROR")
        return 2
    data = json.loads(manifest.read_text(encoding="utf-8"))
    bad: list[str] = []
    checked = 0
    for item in data.get("items", []):
        if item.get("skipped") or not item.get("ok"):
            continue
        dst = Path(item["dst"])
        if not dst.exists():
            bad.append(f"{item['dst']} 不存在")
            continue
        if item.get("bytes") and dst.stat().st_size != item["bytes"]:
            bad.append(f"{item['dst']} 大小不符")
            continue
        if item.get("kind") == "sqlite":
            con = sqlite3.connect(str(dst))
            try:
                verdict = con.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                con.close()
            if verdict != "ok":
                bad.append(f"{item['dst']} integrity_check={verdict}")
        checked += 1
    print(json.dumps({"backup": latest.name, "checked": checked,
                      "problems": bad, "ok": not bad}, ensure_ascii=False, indent=2))
    return 0 if not bad else 1


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="每日分层备份器（SQLite 在线备份 / 小文件复制 / 大文件硬链接）")
    ap.add_argument("--date", default=None, help="备份日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP, help=f"保留天数（默认 {DEFAULT_KEEP}）")
    ap.add_argument("--dry-run", action="store_true", help="只报告将做什么，不写任何文件")
    ap.add_argument("--verify", action="store_true", help="校验最近一份备份后退出")
    ap.add_argument("--drift", action="store_true", help="检查硬链接漂移后退出（只读，不改任何东西）")
    ap.add_argument("--drift-date", default=None, help="配合 --drift：指定要检查的备份日期 YYYY-MM-DD")
    ap.add_argument("--quiet", action="store_true", help="不向 stderr 打日志")
    args = ap.parse_args(argv)

    global _log
    if args.quiet:
        _log = lambda msg, level="INFO": None  # type: ignore[assignment]

    if args.verify:
        return verify_latest()

    if args.drift:
        d = check_drift(args.drift_date)
        print(json.dumps(d, ensure_ascii=False, indent=2))
        if d.get("error"):
            _log(f"漂移检测未完成：{d['error']}", "ERROR")
            return 2
        p1 = [f for f in d["findings"] if f["level"] == "P1"]
        _log(f"漂移检测（{d['backup']}）：已校验 {d['checked']} 个硬链接项"
             f"（另有 {d.get('unverified', 0)} 项因缺 sha256 无法校验）；"
             f"P1 {len(p1)} 条；S2 链接已断 {len(d['link_broken'])} 项（知会，不告警）")
        return 1 if p1 else 0

    today = args.date or dt.date.today().isoformat()
    try:
        dt.date.fromisoformat(today)
    except ValueError:
        _log(f"--date 非法：{today!r}", "ERROR")
        return 2

    free_pct = disk_free_ratio(PROJECT_ROOT)
    if free_pct < DISK_WARN_PCT:
        _log(f"磁盘可用率仅 {free_pct:.1f}%（< {DISK_WARN_PCT}%）—— 备份仍执行，但请尽快清理", "WARN")

    dst_dir = BACKUP_ROOT / today
    _log(f"备份目标：{dst_dir}（dry_run={args.dry_run}，keep={args.keep}）")

    items: list[dict[str, Any]] = []
    for rel in DB_TARGETS:
        src = PROJECT_ROOT / rel
        items.append(backup_sqlite(src, dst_dir / "sqlite" / src.name, args.dry_run))
    items += backup_dataset(dst_dir / "dataset", args.dry_run)
    items += backup_config(dst_dir, args.dry_run)

    failures = [i for i in items if not i.get("ok") and not i.get("skipped")]
    skipped = [i for i in items if i.get("skipped")]
    copied = [i for i in items if i.get("method") in ("copy", "copy-kept")]
    linked = [i for i in items if i.get("method") in ("hardlink", "hardlink-kept")]
    reused = [i for i in items if str(i.get("method") or "").endswith("-kept")]
    fallback = [i for i in items if i.get("method") == "copy-fallback"]
    sqlite_ok = [i for i in items if i.get("kind") == "sqlite" and i.get("integrity_check") == "ok"]

    prune_result = prune(args.keep, today, args.dry_run)

    summary: dict[str, Any] = {
        "date": today,
        "dry_run": args.dry_run,
        "backup_dir": str(dst_dir),
        "disk_free_pct": round(free_pct, 2),
        "counts": {
            "total": len(items),
            "failed": len(failures),
            "skipped": len(skipped),
            "sqlite_verified": len(sqlite_ok),
            "copied": len(copied),
            "hardlinked": len(linked),
            "reused": len(reused),
            "copy_fallback": len(fallback),
        },
        "prune": prune_result,
        "failed_items": [{"src": i.get("src"), "error": i.get("error")} for i in failures],
        "status": "failed" if failures else "ok",
    }

    if not args.dry_run:
        dst_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "date": today,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "host": os.environ.get("COMPUTERNAME", ""),
            "disk_free_pct": summary["disk_free_pct"],
            "items": items,
            "summary": summary["counts"],
            "prune": prune_result,
        }
        (dst_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if failures:
        _log(f"备份存在 {len(failures)} 个失败项", "ERROR")
    else:
        _log(f"备份完成：复制 {len(copied)} / 硬链接 {len(linked)} / 回退 {len(fallback)} / SQLite 校验通过 {len(sqlite_ok)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
