#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[自研工具] ops_rotate.py —— 每日运维产物轮转器（量化平台）

用途
----
按**保留期**清理「本系统自己生成、且名字里带完整日期」的每日产物，把 `daily_output/`
这类逐日增长的目录压在可控体量与文件数内。

适用场景
--------
本工作区（E:/ChatGPT/量化分析与数据分析）的每日运维收尾（建议 23:45，排在日报之后），
以及任何「按日期命名的每日产物需要控制留存」的运维目录。

仓库链接
--------
https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/ops_rotate.py

⚠️ 能力边界（刻意不实现）
------------------------
- **只删白名单形状**：`<YYYY-MM-DD>/` 目录、`日报-YYYYMMDD.md`、`alerts-YYYYMMDD.jsonl`、
  `<check>-YYYYMMDD.json`（巡检产物，check ∈ resource/slot/quality/public）。
  开发残留（`_pytest_*`）、用户资产（`recommend/`、`README.md`）、告警正文存档（`ops/outbox/`）
  一律**不动**，只把它们列进 `protected` 备核。
- **默认干跑**：不带 `--apply` 不删任何东西（与工作区既有的 archive_tasks / cleanup_task 同口径）。
- **保留语义 = 保留最新 N 个日期**（按现有产物排序取前 N），**不是**「今天往前推 N 个自然日」。
  与 `ops_backup.py --keep` 同源口径。故产物只有 9 个日期时，keep=14 不删任何东西。
- **不删工作区外的任何内容**；不碰 `backup/`（那是 `ops_backup.prune` 的职责）。

退出码
------
0 = 成功（含干跑）；1 = 有删除失败项；2 = 参数或环境错误。

用法
----
    .venv\\Scripts\\python.exe scripts\\ops_rotate.py                  # 干跑，只报告
    .venv\\Scripts\\python.exe scripts\\ops_rotate.py --apply          # 真删
    .venv\\Scripts\\python.exe scripts\\ops_rotate.py --keep 30
    .venv\\Scripts\\python.exe scripts\\ops_rotate.py --dir <测试夹具> --apply
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DAILY_OUT = PROJECT_ROOT / "daily_output"
DEFAULT_KEEP = 14

#: 白名单①：日期目录名
DATE_DIR_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
#: 白名单②：ops 产物文件名 —— **收紧为逐种字面形状**。
#: ⚠️ 2026-10-11 独立审核发现的缺陷（已修）：原写法
#: `(?:日报|alerts)-…\.(?:md|jsonl)` 会把交叉组合（`日报-20260101.jsonl` / `alerts-20260101.md`）
#: 也当白名单纳入 —— 属「只删白名单形状」的越界。故拆成多条各自锚死的正则。
#: 2026-10-11（运维加固 B/C/D）：追加**巡检产物**形状 `<check>-<YYYYMMDD>.json`。
#:   ⚠️ 不追加的后果：巡检产物会落入 `protected` **永不轮转** —— 等于用一个新巡检
#:   制造一个新的体积增长源（`ops_probe.py` 每日 4 份）。
REPORT_RES = (
    re.compile(r"^日报-(\d{4})(\d{2})(\d{2})\.md$"),
    re.compile(r"^alerts-(\d{4})(\d{2})(\d{2})\.jsonl$"),
    re.compile(r"^(?:resource|slot|quality|public)-(\d{4})(\d{2})(\d{2})\.json$"),
)
#: 永不触碰的名字（阴性对照清单；改了这里就等于改了「什么算受保护」）
PROTECTED_NAMES = {"README.md", ".gitkeep", "outbox"}


def _log(msg: str, level: str = "INFO") -> None:
    print(f"[{level}] {msg}", file=sys.stderr)


def _valid_date(y: str, m: str, d: str) -> str | None:
    try:
        return dt.date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def date_of_dir(name: str) -> str | None:
    m = DATE_DIR_RE.match(name)
    return _valid_date(*m.groups()) if m else None


def date_of_file(name: str) -> str | None:
    for rx in REPORT_RES:
        m = rx.match(name)
        if m:
            return _valid_date(*m.groups())
    return None


def tree_stats(p: Path) -> tuple[int, int]:
    """返回 (文件数, 字节数)。"""
    if p.is_file():
        try:
            return 1, p.stat().st_size
        except OSError:
            return 1, 0
    files = 0
    total = 0
    for dp, _dns, fns in os.walk(p):
        for fn in fns:
            files += 1
            try:
                total += os.path.getsize(os.path.join(dp, fn))
            except OSError:
                pass
    return files, total


def collect(root: Path) -> dict[str, Any]:
    """收集候选与受保护项，并按日期归并。**不改动任何东西。**"""
    candidates: list[dict[str, Any]] = []   # {date, path, kind, files, bytes}
    protected: list[dict[str, Any]] = []

    if not root.exists():
        return {"root": str(root), "exists": False, "candidates": [], "protected": []}

    for entry in sorted(root.iterdir()):
        if entry.is_dir():
            d = date_of_dir(entry.name)
            if d:
                files, size = tree_stats(entry)
                candidates.append({"date": d, "path": entry, "kind": "date_dir",
                                   "files": files, "bytes": size})
            else:
                files, size = tree_stats(entry)
                protected.append({"name": entry.name, "kind": "dir", "files": files, "bytes": size,
                                  "why": "目录名非 YYYY-MM-DD"})
        else:
            d = date_of_file(entry.name)
            if d:
                files, size = tree_stats(entry)
                candidates.append({"date": d, "path": entry, "kind": "ops_file",
                                   "files": files, "bytes": size})
            elif entry.name not in PROTECTED_NAMES:
                protected.append({"name": entry.name, "kind": "file", "files": 1,
                                  "bytes": entry.stat().st_size,
                                  "why": "文件名不含完整日期（开发残留/说明文件）"})
            else:
                protected.append({"name": entry.name, "kind": "file", "files": 1,
                                  "bytes": entry.stat().st_size, "why": "显式受保护名单"})

    # ops/ 子目录：只认日报/告警流水，outbox 等一律受保护
    ops = root / "ops"
    if ops.is_dir():
        for entry in sorted(ops.iterdir()):
            if entry.is_dir():
                files, size = tree_stats(entry)
                protected.append({"name": f"ops/{entry.name}", "kind": "dir", "files": files,
                                  "bytes": size, "why": "子目录（如 outbox 告警存档）不在白名单"})
                continue
            d = date_of_file(entry.name)
            if d:
                candidates.append({"date": d, "path": entry, "kind": "ops_file",
                                   "files": 1, "bytes": entry.stat().st_size})
            else:
                protected.append({"name": f"ops/{entry.name}", "kind": "file", "files": 1,
                                  "bytes": entry.stat().st_size, "why": "文件名不含完整日期"})
    return {"root": str(root), "exists": True, "candidates": candidates, "protected": protected}


def plan_rotation(coll: dict[str, Any], keep: int, today: str) -> dict[str, Any]:
    """按「保留最新 N 个日期」切分为 keep / drop 两组。"""
    dates = sorted({c["date"] for c in coll["candidates"]}, reverse=True)
    kept_dates = set(dates[:keep]) | {today}
    keep_items = [c for c in coll["candidates"] if c["date"] in kept_dates]
    drop_items = [c for c in coll["candidates"] if c["date"] not in kept_dates]
    return {"dates": dates, "kept_dates": sorted(kept_dates, reverse=True),
            "keep_items": keep_items, "drop_items": drop_items}


def rotate(root: Path, keep: int, apply_: bool, today: str) -> dict[str, Any]:
    coll = collect(root)
    if not coll["exists"]:
        return {"root": str(root), "exists": False, "mode": "apply" if apply_ else "dry-run",
                "candidates": 0, "removed": [], "failed": [], "protected": [],
                "freed_files": 0, "freed_bytes": 0, "note": "目标目录不存在"}

    pl = plan_rotation(coll, keep, today)
    removed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for item in pl["drop_items"]:
        p: Path = item["path"]
        if not apply_:
            removed.append({"path": str(p), "kind": item["kind"], "date": item["date"],
                            "files": item["files"], "bytes": item["bytes"], "dry_run": True})
            continue
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            removed.append({"path": str(p), "kind": item["kind"], "date": item["date"],
                            "files": item["files"], "bytes": item["bytes"], "dry_run": False})
        except OSError as exc:
            # ⚠️ 判据按**结果**而非按异常：本环境实测 rmtree 可能在「内容已删完、收尾 rmdir 阶段」
            # 抛错（沙箱内的 safe-delete 钩子），此时目标已消失、轮转目的已达成 ——
            # 按异常记失败会造成**假失败**。仅当目标仍在时才计入 failed（真失败：占用/权限）。
            if p.exists():
                failed.append({"path": str(p), "error": repr(exc)})
            else:
                removed.append({"path": str(p), "kind": item["kind"], "date": item["date"],
                                "files": item["files"], "bytes": item["bytes"], "dry_run": False,
                                "note": f"删除已达成；rmtree 在收尾阶段抛错：{exc!r}"})

    return {
        "root": str(root),
        "exists": True,
        "mode": "apply" if apply_ else "dry-run",
        "keep": keep,
        "candidate_dates": pl["dates"],
        "kept_dates": pl["kept_dates"],
        "keep_count": len(pl["keep_items"]),
        "drop_count": len(pl["drop_items"]),
        "removed": removed,
        "failed": failed,
        "protected": coll["protected"],
        "freed_files": sum(i["files"] for i in removed),
        "freed_bytes": sum(i["bytes"] for i in removed),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="每日运维产物轮转器（默认干跑，--apply 才删）")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                    help=f"保留最新 N 个日期（默认 {DEFAULT_KEEP}；与 ops_backup --keep 同源口径）")
    ap.add_argument("--apply", action="store_true", help="真正执行删除（缺省只干跑）")
    ap.add_argument("--dir", default=None, help="覆盖目标目录（测试用；缺省 daily_output）")
    ap.add_argument("--quiet", action="store_true", help="不向 stderr 打日志")
    args = ap.parse_args(argv)

    global _log
    if args.quiet:
        _log = lambda msg, level="INFO": None  # type: ignore[assignment]

    if args.keep < 1:
        _log(f"--keep 必须 >= 1（收到 {args.keep}）", "ERROR")
        return 2

    root = Path(args.dir).resolve() if args.dir else DAILY_OUT
    today = dt.date.today().isoformat()
    result = rotate(root, args.keep, args.apply, today)

    summary = {
        "root": result["root"],
        "mode": result["mode"],
        "keep": args.keep,
        "candidate_dates": result.get("candidate_dates", []),
        "kept": result.get("keep_count", 0),
        "dropped": result.get("drop_count", 0),
        "removed": len(result["removed"]),
        "failed": len(result["failed"]),
        "freed_files": result["freed_files"],
        "freed_bytes": result["freed_bytes"],
        "protected": len(result["protected"]),
        "protected_items": [f"{p['name']}（{p['why']}）" for p in result["protected"]],
        "removed_items": [r["path"] for r in result["removed"]],
        "failed_items": result["failed"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if result["failed"]:
        _log(f"轮转有 {len(result['failed'])} 个失败项", "ERROR")
        return 1
    if result["mode"] == "dry-run":
        _log(f"干跑：将删除 {summary['dropped']} 项 / {summary['freed_files']} 文件 / "
             f"{summary['freed_bytes'] / 1024 / 1024:.2f} MB（未落盘）")
    else:
        _log(f"轮转完成：删除 {summary['removed']} 项 / {summary['freed_files']} 文件 / "
             f"{summary['freed_bytes'] / 1024 / 1024:.2f} MB；受保护 {summary['protected']} 项未动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
