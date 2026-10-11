#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# [自研工具] ops_probe.py
# 用途：每日运维四支巡检（量化平台）—— 按设计案 `报告-每日自动运维方案-20261011.md` §2.2 实现四支互相独立、各自定时的巡检，并把结论落成结构化产物，供 23:30 的 `ops_report.py` 折入当日日报与告警流水
# 适用场景：本工作区（E:/ChatGPT/量化分析与数据分析）的每日运维；也适用于任何「需要按不同时刻分批巡检、但结论最终汇总到一份日报」的本地运维场景
# 作者：ai-workflow 自研（量化平台运维沉淀，2026-10-11）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/ops_probe.py
"""ops_probe.py —— 每日运维四支巡检（量化平台）

四支巡检
--------
| 子命令 | 设计案 | 建议时刻 | 做什么 |
|---|---|---|---|
| `--check resource` | **T1** 磁盘与日志体积巡检 | 07:00 | 磁盘可用率 + 目录体量/文件数 + 内存 + 进程 + 站点进程句柄/工作集 |
| `--check slot`     | **T7** 收盘档结果汇总   | 21:15 | 档位×stage 表；对 partial/failed/skipped_locked 提取 `reason`/`stderr_tail`；无记录 → P0 |
| `--check quality`  | **T8** 数据质量复盘     | 22:00 | 哨兵缺口汇总；**同口径连续 2 个交易日** → P0 |
| `--check public`   | **T10** 公网可达性核验  | 22:45 | 公网 index 引用集不变量 + 资产逐文件 sha256 + 隧道侧对照 |

产出
----
`daily_output/ops/<check>-<YYYYMMDD>.json`，结构见 方案评审.md §七。

⚠️ 退出码语义（刻意如此，见 方案评审.md 四·4）
--------------------------------------------
0 = **本次巡检跑成了**（无论有没有发现问题）；2 = 参数或环境错误。
**「发现了问题」不改退出码** —— 否则 watchdog 的「当日失败 3 次止损」会被「发现了问题」
误触发：每发现一次问题就白跑 3 轮、刷爆日志，并掩盖真正的执行失败。
**判决（发现问题）与执行成败（跑成没跑成）必须是两个物理量。**

⚠️ 与 `ops_report.py` 的关系
---------------------------
本脚本**只采集与判定，不写** `alerts-<date>.jsonl`（那是 `ops_report.py` 独占的产物）。
两支的 `findings` 用**同 code / 同 target / 同 subject** 表达同一事件 ⇒ 日报合并时按
`(code,target,subject)` 去重，不会出现同一故障两行。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# ⚠️ 同源硬约束：`QUALITY_STAGES`（哪些 stage 算质量哨兵）与交易日历口径的
#    **唯一事实源在 `ops_report.py`**。这里刻意**不再抄一份** —— 抄一份就会出现
#    「日报认 5 个哨兵、巡检认 4 个」这类静默口径漂移。
#    fail-loud：导入不了就直接退出（退出码 1，watchdog 按失败处理），**不静默降级成空集**
#    —— 空集会让「所有哨兵都通过」与「一个哨兵都没检查」长得一模一样。
try:
    import ops_report  # 同目录；直接运行本脚本时 sys.path[0] == scripts/
except Exception as _exc:  # noqa: BLE001
    raise SystemExit(f"[FATAL] 无法导入同目录 ops_report（QUALITY_STAGES/日历口径的唯一事实源）：{_exc!r}")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / ".work" / "daily" / "scheduler" / "state"
OPS_OUT = PROJECT_ROOT / "daily_output" / "ops"
DIST_DIR = PROJECT_ROOT / "frontend" / "dist"

#: 复用日报的阈值常量与采集口径 —— **同一物理量必须同源**（本工作区硬规则）
DISK_WARN_PCT = 15.0
DISK_CRIT_PCT = 8.0
LOG_VOLUME_WARN_MB = 2048.0

PUBLIC_BASE = "https://quantity.website"
LOCAL_ORIGIN = "http://127.0.0.1:4173"
TUNNEL_METRICS = "http://127.0.0.1:20241/metrics"
PUBLIC_TIMEOUT = 15.0
PUBLIC_ATTEMPTS = 2
PUBLIC_WORKERS = 6
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ops-probe/1.0"

#: T1 的目录集：标签 → (路径, 是否日志目录, 需排除的子目录名)
#: ⚠️ `backend/.work` 有万余运行时产物（占 backend 约 99%），统计规模**必须排除**
#:    —— 本工作区硬规则；排除量另行记入 `excluded` 以示透明，不与正文数字混淆。
RESOURCE_DIRS = (
    ("调度日志", STATE_DIR.parent / "logs", True, frozenset()),
    ("后端目录", PROJECT_ROOT / "backend", False,
     frozenset({".work", "__pycache__", ".venv", "node_modules", "dist", "output"})),
    ("日志目录", PROJECT_ROOT / "logs", True, frozenset()),
    ("脚本目录", PROJECT_ROOT / "scripts", False, frozenset({"__pycache__"})),
)

NON_TRADING_NOTE = "非交易日，无档位记录属正常"


def _log(msg: str, level: str = "INFO") -> None:
    print(f"[{level}] {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# 通用：结构 / 落盘
# --------------------------------------------------------------------------- #
def new_result(probe: str, day: str) -> dict[str, Any]:
    return {"probe": probe, "date": day,
            "ts": dt.datetime.now().isoformat(timespec="seconds"),
            "ok": True, "level": None, "findings": [], "sections": {}, "unverified": []}


def finding(level: str, code: str, target: str, subject: str,
            detail: str, evidence: str) -> dict[str, Any]:
    return {"level": level, "code": code, "target": target, "subject": subject,
            "detail": detail, "evidence": evidence}


def _bump(result: dict[str, Any], level: str) -> None:
    order = {"P0": 3, "P1": 2, "P2": 1}
    cur = result.get("level")
    if cur is None or order.get(level, 0) > order.get(cur, 0):
        result["level"] = level


def add_findings(result: dict[str, Any], items: list[dict[str, Any]]) -> None:
    for it in items:
        result["findings"].append(it)
        _bump(result, it["level"])


def write_artifact(result: dict[str, Any], out_dir: Path | None = None,
                   date: str | None = None) -> Path:
    d = out_dir or OPS_OUT
    d.mkdir(parents=True, exist_ok=True)
    day = date or result["date"]
    p = d / f"{result['probe']}-{day.replace('-', '')}.json"
    p.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# T1 —— 资源巡检
# --------------------------------------------------------------------------- #
def _disk_stats() -> list[dict[str, Any]]:
    """与 `ops_report.disk_stats()` 同源同口径（标签也一致，便于告警去重）。"""
    out = []
    for label, path in (("工作区所在盘", PROJECT_ROOT),
                        ("系统盘", Path(os.environ.get("SystemDrive", "C:") + "\\"))):
        try:
            u = shutil.disk_usage(str(path))
        except OSError as exc:
            out.append({"label": label, "error": repr(exc)})
            continue
        out.append({"label": label, "path": str(path),
                    "total_gb": round(u.total / 1024 ** 3, 1),
                    "free_gb": round(u.free / 1024 ** 3, 1),
                    "free_pct": round(100.0 * u.free / u.total, 2) if u.total else 0.0})
    return out


def _tree(p: Path, excludes: frozenset[str]) -> tuple[int, int]:
    """返回 (文件数, 字节数)；不进入 excludes 命名的**任意层级**子目录。"""
    files = 0
    total = 0
    for dp, dns, fns in os.walk(p):
        dns[:] = [d for d in dns if d not in excludes]
        for fn in fns:
            files += 1
            try:
                total += os.path.getsize(os.path.join(dp, fn))
            except OSError:
                pass
    return files, total


def _dir_stats() -> list[dict[str, Any]]:
    out = []
    for label, path, is_log, excludes in RESOURCE_DIRS:
        files, total = _tree(path, excludes) if path.exists() else (0, 0)
        excluded = []
        if excludes and path.exists():
            for child in sorted(path.iterdir()):
                if child.is_dir() and child.name in excludes:
                    ef, eb = _tree(child, frozenset())
                    excluded.append({"name": child.name, "files": ef,
                                     "mb": round(eb / 1024 / 1024, 2)})
        out.append({"label": label,
                    "path": str(path.relative_to(PROJECT_ROOT)) if path.exists() else str(path),
                    "mb": round(total / 1024 / 1024, 2), "files": files,
                    "is_log": is_log, "excluded": excluded})
    return out


# --------------------------------------------------------------------------- #
# 工作区级预算（2026-10-11 追加）
# --------------------------------------------------------------------------- #
#: 为什么需要独立于「宿主盘判据」：
#:   `_disk_stats()` 只能回答「盘要满了」，回答不了「是不是**我们自己**在膨胀」。
#:   实测 E: 上工作区仅占该盘已用的 1.8% ⇒ 宿主盘告警对本方的动作**不可处置**；
#:   真正可处置的信号是**工作区/目录自身的绝对体量**（删中间产物、轮转、清临时区）。
#:
#: ── 阈值规则（2026-10-11 复校；规则本身必须**可复算**）─────────────────────
#:   旧写法是「实测 × 1.2~2，刻意留 25%+ 余量」。这是一句**伪装成规则的描述**：
#:   实际取值散布在 1.22–4.26（逐行倍数见下表行尾注释），复核时**无法判定对错**
#:   —— 落在区间里就算对，那这个区间就永远对。故 2026-10-11 换成单值规则：
#:
#:      预算 = 2 × 该行的「可持续上限」，再向上取整到 整百 MB / 整 GB。
#:      其中「可持续上限」：无机制行 = 当前实测体量；
#:                        有机制行 = 机制上限（见下条，本表只有 daily_output 属此类）。
#:
#:   为什么是 ×2 而不是 ×1.x：本条判据的定位是抓**失控增长（翻倍级）**，不是抓
#:   「比昨天多一点点」。紧贴实测的阈值会把**正常增长**报成越线 ⇒ 又造一条永久 P1
#:   （正是本任务要治的病）；而在没有可信增速模型的前提下（见下方「已知局限」），
#:   任何「按天数推」的阈值都只是把「不可复算」换一种写法。
#:
#:   ⚠️ 余量必须给**不可处置**的那一行，不能给好清理的那一行：旧表里最紧的两行恰恰是
#:      `历史数据集`(82.0%) 与 `工作区总量`(80.3%) —— 这两行**一行都不能删**（前者是
#:      源数据，后者是全量）；而真正可清理的 `daily_output` 却宽松到 4.26×。等于把
#:      「越线」永远留给最没法处置的对象。新表把余量拉平到 ~2.0–2.1×。
#:
#:   ⚠️ 旧表还**不自洽**：Σ(其余各行) = 20,630 MB > 总量 18,432 MB —— 分量之和超过
#:      整体，等于说「子目录可以比工作区还大」，量纲上就不可能。新表强制
#:      `总量 ≥ Σ(其余各行)`（守卫见 tests/test_ops_probe.py）。
#:
#: 2026-10-11 定阈实测（`ops_probe.py --check resource` 产物，单位 MB）：
#:   工作区总量 14809.01 / 历史数据集 10919.87 / tasks 1375.60 / `.scratch` 1198.46 /
#:   `.work` 512.77 / backup 516.02 / daily_output 35.19
#:   `daily_output` 单日量（`YYYY-MM-DD/` 满跑日）实测 4.5–4.8 MB ⇒ 取 5.0 MB 作上限。
#:
#: ⚠️ 复校纪律：**每 30 天**按最新实测重算一次。
#:    设计案 §7 第 4 条写的是「两周后按实际增长速率校准」—— 那指的是**初值校准**，
#:    本次（2026-10-11）做的就是它；此后转为 30 天周期（×2 的余量撑得住，而每次复校
#:    ＝一次 ~40 s 全盘遍历 ＋ 改 7 个常量，再频繁并无收益）。
#:    ⚠️ 但本次真正的发现是：让阈值烂掉的**不是「复校日没到」，而是「规则不可复算」**
#:      —— 只要写成区间描述，复校那天同样判不出对错。故**规则先于周期**。
#: ⚠️ 已知局限：本条**不引入增速模型**。理由＝增速只有 1 天样本，且 `历史数据集`
#:    近 1 日 2,091 MB vs 近 30 日 1,272 MB ⇒ 均值被单日尖峰污染，据此外推的
#:    「到线天数」不可信。故用「实测 × 2 ＋ 每 30 天定期复校」替代增速外推。
#: 口径：`(展示名, 相对工作区根的路径, 预算 MB)`；路径为 `""` 表示**工作区总量**。
WS_BUDGET_MB = (
    ("工作区总量", "", 30 * 1024),             # 2 × 14809.01 = 29618 → 上取整到整 GB
    ("历史数据集", "历史数据集", 22 * 1024),    # 2 × 10919.87 = 21840 → 上取整到整 GB
    ("tasks", "tasks", 2800),                # 2 × 1375.60 = 2751  → 上取整到整百
    (".scratch", ".scratch", 2400),          # 2 × 1198.46 = 2397  → 上取整到整百
    (".work", ".work", 1100),                # 2 ×  512.77 = 1026  → 上取整到整百
    ("backup", "backup", 1100),              # 2 ×  516.02 = 1032  → 上取整到整百
    ("daily_output", "daily_output", 140),   # 2 × (keep 14 × 单日 5.0 MB) = 140（机制上限）
)

#: 遍历时**在层里**剪掉的目录名 —— 依赖树/缓存不剪会把真实体量淹没
WS_SKIP_DIRNAMES = frozenset({".git", "node_modules", "__pycache__", ".venv", ".mypy_cache"})

WS_BUDGET_CODE = "ws_budget_exceeded"


def ws_scan(root: Path | None = None) -> dict[str, Any]:
    """工作区体量：**单次遍历 + 全局 inode 去重**，按顶层目录归因。

    ⚠️ 三处必须做对（本任务实测踩中，写错都**不抛异常、只算错数**）：
    ① **硬链接去重必须在整个工作区范围内做一次** —— `backup/` 与 `历史数据集/` 共享 1 个
       inode（实测 0.474 GB）；逐目录各开一个 `seen` 集会把**总量**多算 3.2%。
    ② 用 `os.scandir` 而不是 `os.walk` + `os.stat`：Windows 上 `DirEntry.stat()` 直接复用
       目录枚举缓存，省掉每文件一次 syscall。**但它只可信 `st_size`** —— `st_nlink`/`st_ino`
       在 Windows 上恒为 0（实测 Py3.12），必须回落 `os.stat` 才拿得到 inode 身份。
    ③ `.venv`/`node_modules`/`__pycache__` 必须在**遍历层**剪掉（不是算完再减）。
    ④ **遍历顺序必须确定（按名排序）**：跨目录共享的 inode 只能归给「先遇到的那个目录」，
       而 `os.scandir` 的枚举顺序**不保证**稳定 ⇒ 不排序会让同一次体量在两次运行里
       落到不同的目录上（`backup` 与 `历史数据集` 共享 0.474 GB，归属会来回跳）。
       注意这是**口径本身的固有歧义**（一个 inode 只有一个体积，却有两个目录名），
       排序只是让它**可复现**；总量不受影响。
    """
    base = Path(root) if root is not None else PROJECT_ROOT
    seen: set[tuple[int, int]] = set()
    per_bytes: dict[str, int] = {}
    per_files: dict[str, int] = {}
    total = files = dup_files = dup_bytes = unreadable = 0
    stack: list[tuple[Path, str]] = [(base, "")]
    while stack:
        cur, top = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            unreadable += 1
            continue
        # 子目录**逆序入栈** ⇒ 出栈顺序 = 名字升序（栈是 LIFO），保证 DFS 顺序确定
        subdirs: list[tuple[Path, str]] = []
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name in WS_SKIP_DIRNAMES:
                        continue
                    subdirs.append((Path(e.path), top or e.name))
                    continue
                st = e.stat(follow_symlinks=False)
                # 🔴 Windows（实测 Py3.12）上 `DirEntry.stat()` 的 `st_nlink`/`st_ino` **恒为 0**，
                #    只有 `st_size` 可信。直接拿它去重 ⇒ 去重**变成空操作**：不抛异常、
                #    总量静默多算 3.2%（本仓实测 0.474 GB）。
                #    故缺 inode 身份时回落 `os.stat`（多一次 syscall，是正确性的代价）。
                if st.st_nlink == 0 or st.st_ino == 0:
                    st = os.stat(e.path, follow_symlinks=False)
            except OSError:
                unreadable += 1
                continue
            files += 1
            per_files[top] = per_files.get(top, 0) + 1
            if st.st_nlink > 1:
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    # 同一 inode 的**第二个**链接：体积已归首次遇到的那一份 ⇒ 只记重复数
                    dup_files += 1
                    dup_bytes += st.st_size
                    continue
                seen.add(key)
            total += st.st_size
            per_bytes[top] = per_bytes.get(top, 0) + st.st_size
        stack.extend(reversed(subdirs))
    return {
        "mb": round(total / 1024 / 1024, 2), "files": files,
        "dup_inode_files": dup_files, "dup_mb": round(dup_bytes / 1024 / 1024, 2),
        "unreadable": unreadable,
        # ⚠️ 键集取 `per_bytes` ∪ `per_files`：若某目录的字节**全部**来自重复 inode，
        #    它的体积为 0 ⇒ 只按 per_bytes 建键会让该目录**整体消失**，
        #    而下游把「查不到」读成「目录不存在」⇒ **静默漏判**。
        "dirs": {k: {"mb": round(per_bytes.get(k, 0) / 1024 / 1024, 2),
                     "files": per_files.get(k, 0)}
                 for k in sorted(set(per_bytes) | set(per_files),
                                 key=lambda k: -per_bytes.get(k, 0))},
    }


def ws_budget_rows(scan: dict[str, Any]) -> list[dict[str, Any]]:
    """预算表 —— **唯一实现**：日报里展示的「占比」与触发告警用的那个数**必然同源**。

    ⚠️ 若把「展示」与「判定」写成两处，就又造出一对「同一物理量两处实现」
       （本仓已经吃过一次：磁盘判据日报/巡检各写一份，漏改一份就各说一套）。
    ⚠️ 目录不存在 ⇒ `exists=False`、`mb=None`，**不参与判定**：这不是「通过」。
    """
    dirs = scan.get("dirs") or {}
    rows = []
    for label, key, budget_mb in WS_BUDGET_MB:
        node = scan if key == "" else (dirs.get(key) or None)
        cur = node.get("mb") if node else None
        rows.append({
            "label": label, "key": key, "budget_mb": budget_mb,
            "mb": cur, "files": (node or {}).get("files"),
            "pct": round(cur / budget_mb * 100, 1) if cur is not None else None,
            "over": bool(cur is not None and cur > budget_mb),
            "exists": cur is not None,
        })
    return rows


def ws_budget_findings(scan: dict[str, Any]) -> list[dict[str, Any]]:
    """工作区级预算判定 —— 由 `ws_budget_rows` 派生（**不另算一遍**）。

    ⚠️ 与 `ops_report._own_code` 的约定：`ws_budget_exceeded` **不得**登记为「日报自有 code」——
       本判据只在巡检里实现，日报通过**折入**拿到（否则日报要重算一遍 100+ 秒的全盘遍历）。
       体积是**慢变量**：07:00 的读数在 23:30 依然有效，故不适用「取同一时刻那一份」的顾虑。
    """
    out = []
    for r in ws_budget_rows(scan):
        if not r["over"]:
            continue
        path = PROJECT_ROOT if r["key"] == "" else PROJECT_ROOT / r["key"]
        out.append(finding(
            "P1", WS_BUDGET_CODE, "host", r["label"],
            f"{r['label']} 体积 {r['mb']:.1f} MB（> 预算 {r['budget_mb']:.0f} MB，{r['pct']:.0f}%）",
            f"{path}｜预算为初值，见 tasks/T1磁盘判据按盘分流-2026-10-11/方案评审.md"))
    return out


def _memory() -> dict[str, Any]:
    """零依赖取物理内存。⚠️ 64 位陷阱：`GetCurrentProcess` 若用默认 restype=c_int，
    伪句柄会被截断（本任务实测句柄采集返回 -1 即此因）；本函数只用 Structure，不受影响。"""
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        raise OSError("GlobalMemoryStatusEx 返回失败")
    gib = 1024 ** 3
    return {"load_pct": int(st.dwMemoryLoad),
            "total_gb": round(st.ullTotalPhys / gib, 2),
            "avail_gb": round(st.ullAvailPhys / gib, 2)}


def _process_count() -> int:
    r = subprocess.run(["tasklist", "/NH", "/FO", "CSV"], capture_output=True, timeout=60,
                       creationflags=0x08000000)
    txt = (r.stdout or b"").decode("utf-8", errors="replace")
    return sum(1 for ln in txt.splitlines() if ln.strip().startswith('"'))


def _pid_on_port(port: int) -> int | None:
    r = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=30,
                       creationflags=0x08000000)
    txt = (r.stdout or b"").decode("gbk", errors="replace")
    for ln in txt.splitlines():
        if f":{port} " in ln and "LISTENING" in ln:
            parts = ln.split()
            if len(parts) >= 5 and parts[-1].isdigit():
                return int(parts[-1])
    return None


def _proc_detail(pid: int) -> dict[str, Any]:
    """取任意 PID 的句柄数与工作集。

    ⚠️ 实测：`OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` 对 svchost/System 这类
    受保护进程会失败（本任务实测 5/5 失败），但对**本项目自身**进程（同用户、非提权）成功。
    故本函数只用于站点三件套，失败即如实记 null，不做任何猜测。
    """
    import ctypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    k = ctypes.windll.kernel32
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    ph = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not ph:
        return {"pid": pid, "open": False, "handles": None, "ws_mb": None}
    try:
        cnt = ctypes.c_ulong()
        ok_h = k.GetProcessHandleCount(ctypes.c_void_p(ph), ctypes.byref(cnt))
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        psapi = ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_ulong]
        ok_m = psapi.GetProcessMemoryInfo(ctypes.c_void_p(ph), ctypes.byref(pmc), pmc.cb)
        return {"pid": pid, "open": True,
                "handles": int(cnt.value) if ok_h else None,
                "ws_mb": round(pmc.WorkingSetSize / 1024 / 1024, 1) if ok_m else None}
    finally:
        k.CloseHandle(ctypes.c_void_p(ph))


def _site_processes() -> list[dict[str, Any]]:
    out = []
    for label, port in (("后端", 8000), ("前端", 4173), ("隧道", 20241)):
        pid = _pid_on_port(port)
        item: dict[str, Any] = {"label": label, "port": port, "pid": pid}
        item.update(_proc_detail(pid) if pid else {"open": False, "handles": None, "ws_mb": None})
        out.append(item)
    return out


def check_resource(day: str) -> dict[str, Any]:
    r = new_result("resource", day)
    disks = _disk_stats()
    dirs = _dir_stats()
    r["sections"] = {"disks": disks, "dirs": dirs}

    # —— 判定：只判设计案给了阈值的两类（磁盘 / 日志目录），与 ops_report 同 code+target+subject ——
    for d in disks:
        pct = d.get("free_pct")
        if pct is None:
            r["unverified"].append({"item": f"disk:{d['label']}", "value": None,
                                    "reason": f"磁盘用量取不到：{d.get('error')}"})
            continue
        if pct < DISK_CRIT_PCT:
            add_findings(r, [finding("P0", "disk_critical", "host", d["label"],
                                     f"{d['label']} 可用仅 {pct}%（< {DISK_CRIT_PCT}%）",
                                     f"free {d['free_gb']} GB / total {d['total_gb']} GB")])
        elif pct < DISK_WARN_PCT:
            # ⚠️ 与 `scripts/ops_report.py::build_alerts` 的磁盘段是**同一判据的第二份实现**，
            #    必须**逐字同源改**（本仓暂无跨文件同源守卫）——漏改一处会让巡检产物仍写 P1，
            #    日报折入后 verdict 又被拉回「注意」，等于白改。
            #    分流依据：设计案 §2.2 的 15%/8% 是写给**系统盘**的；工作区所在盘降 P2（不进 verdict）。
            #    两个阈值常量与 `if pct < DISK_CRIT_PCT` 分支**均原样不动** —— P0 红线两盘共用。
            if d["label"] == "系统盘":
                add_findings(r, [finding("P1", "disk_low", "host", d["label"],
                                         f"{d['label']} 可用 {pct}%（< {DISK_WARN_PCT}%）",
                                         f"free {d['free_gb']} GB / total {d['total_gb']} GB")])
            else:
                add_findings(r, [finding("P2", "host_disk_low", "host", d["label"],
                                         f"{d['label']} 可用 {pct}%（< {DISK_WARN_PCT}%，非系统盘 ⇒ 知情项）",
                                         f"free {d['free_gb']} GB / total {d['total_gb']} GB")])
    for d in dirs:
        if d["is_log"] and d["mb"] > LOG_VOLUME_WARN_MB:
            add_findings(r, [finding("P1", "log_volume", "host", d["label"],
                                     f"{d['label']} 体积 {d['mb']} MB（> {LOG_VOLUME_WARN_MB} MB）",
                                     f"{d['path']}（{d['files']} 文件）")])

    # —— 工作区级预算（2026-10-11 追加）：回答「是不是**我们自己**在膨胀」——
    #    ⚠️ 「扫不到」不得与「没超标」同形 ⇒ 扫描失败时走 `unverified`（日报折入成 P2），
    #       而不是静默不判。与上面磁盘取不到数的处理同口径。
    try:
        ws = ws_scan()
        ws["budget_rows"] = ws_budget_rows(ws)
        r["sections"]["workspace"] = ws
        add_findings(r, ws_budget_findings(ws))
    except Exception as exc:  # noqa: BLE001 —— 扫描失败不得让整支巡检崩掉
        r["sections"]["workspace"] = {"error": repr(exc)}
        r["unverified"].append({"item": "workspace_budget", "value": None,
                                "reason": f"工作区体量扫描失败：{exc!r}"})

    # —— 采集但**不判**：设计案 §7.4 只承认磁盘与日志目录三个初值 ——
    try:
        mem = _memory()
        r["sections"]["memory"] = mem
        r["unverified"].append({"item": "memory_load_pct", "value": mem["load_pct"],
                                "reason": "设计案未给内存阈值（§7.4 只列磁盘与日志目录）"})
    except Exception as exc:  # noqa: BLE001
        r["sections"]["memory"] = {"error": repr(exc)}
        r["unverified"].append({"item": "memory_load_pct", "value": None,
                                "reason": f"内存采集失败：{exc!r}"})
    try:
        procs = _process_count()
        r["sections"]["processes"] = {"count": procs}
        r["unverified"].append({"item": "process_count", "value": procs,
                                "reason": "设计案未给进程数阈值（且本机无历史基线）"})
    except Exception as exc:  # noqa: BLE001
        r["sections"]["processes"] = {"error": repr(exc)}
        r["unverified"].append({"item": "process_count", "value": None,
                                "reason": f"进程数采集失败：{exc!r}"})
    try:
        sp = _site_processes()
        r["sections"]["site_processes"] = sp
        # value 取**紧凑文本**（机器可读的完整结构在 sections.site_processes）：
        # 日报的「未设阈值」表是给人看的，塞一个 dict repr 会把表格撑成一坨。
        r["unverified"].append({"item": "site_process_handles_ws",
                                "value": "；".join(
                                    f"{s['label']} 句柄{s.get('handles')}/{s.get('ws_mb')}MB"
                                    for s in sp),
                                "reason": "设计案未给句柄/工作集阈值（采集面 > 判定面，只作展示与趋势）"})
    except Exception as exc:  # noqa: BLE001
        r["sections"]["site_processes"] = {"error": repr(exc)}
        r["unverified"].append({"item": "site_process_handles_ws", "value": None,
                                "reason": f"站点进程采集失败：{exc!r}"})
    return r


# --------------------------------------------------------------------------- #
# T7 —— 收盘档结果汇总
# --------------------------------------------------------------------------- #
def _load_state(day: str, state_dir: Path | None = None) -> dict[str, Any] | None:
    """读档位状态。默认路径复用 `ops_report.load_state`（同源）；仅测试覆盖时才自行解析。"""
    if state_dir is None:
        return ops_report.load_state(day)
    p = state_dir / f"{day}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_trading_day(day: str) -> bool | None:
    """复用 `ops_report.is_trading_day` 的本项目日历口径；不可用返回 None（不猜）。"""
    try:
        return ops_report.is_trading_day(day)
    except Exception:  # noqa: BLE001
        return None


def _slot_rows(state: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把 state 翻成「档位 × stage」表 + 异常 stage 明细（含 reason / stderr_tail）。"""
    slots: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    for slot_name, payload in (state.get("slots") or {}).items():
        p = payload or {}
        stages = []
        for st in p.get("stages") or []:
            row = {"name": st.get("name"), "status": st.get("status"),
                   "exit_code": st.get("exit_code"), "attempts": st.get("attempts"),
                   "duration_seconds": st.get("duration_seconds")}
            stages.append(row)
            if str(st.get("status")) in ("failed", "skipped", "skipped_locked") or st.get("reason"):
                problems.append({
                    "slot": slot_name, "name": st.get("name"), "status": st.get("status"),
                    "exit_code": st.get("exit_code"), "attempts": st.get("attempts"),
                    "reason": st.get("reason"),
                    "stderr_tail": (str(st.get("stderr_tail"))[-600:]
                                    if st.get("stderr_tail") else None),
                    "summary": st.get("summary"),
                })
        slots.append({"slot": slot_name, "status": p.get("status"),
                      "stages": stages, "failed": p.get("failed_stages") or [],
                      "skipped": p.get("skipped_stages") or [],
                      "reason": p.get("reason"),
                      "started_at": p.get("started_at"), "finished_at": p.get("finished_at")})
    return slots, problems


def check_slot(day: str, state_dir: Path | None = None) -> dict[str, Any]:
    r = new_result("slot", day)
    trading = _is_trading_day(day)
    state = _load_state(day, state_dir)
    r["sections"] = {"trading": trading, "state_file": f"state/{day}.json",
                     "exists": state is not None, "slots": [], "problem_stages": []}

    if state is None:
        if trading is True:
            # 与 ops_report 同 code/target/subject ⇒ 日报合并时去重
            add_findings(r, [finding("P0", "close_no_record", "daily_scheduler", "state",
                                     "交易日无任何档位记录（任务可能未被触发）",
                                     f"state/{day}.json 不存在（T7 于 21:15 判定）")])
        elif trading is False:
            r["sections"]["note"] = NON_TRADING_NOTE
        else:
            add_findings(r, [finding("P1", "slot_calendar_unavailable", "daily_scheduler", "calendar",
                                     "交易日历不可用，无法判定今日是否应有档位记录",
                                     "ops_report.is_trading_day 返回 None")])
            r["unverified"].append({"item": "close_record_expected", "value": None,
                                    "reason": "日历不可用 ⇒ 不判 P0（宁可缺失不给错值）"})
        return r

    slots, problems = _slot_rows(state)
    r["sections"]["slots"] = slots
    r["sections"]["problem_stages"] = problems

    for s in slots:
        status = str(s.get("status") or "unknown")
        if status == "failed":
            add_findings(r, [finding("P0", "slot_failed", "daily_scheduler", str(s["slot"]),
                                     f"{s['slot']} 档状态 failed",
                                     f"failed_stages={s['failed']}")])
        elif status in ("partial", "skipped_locked"):
            add_findings(r, [finding("P1", "slot_degraded", "daily_scheduler", str(s["slot"]),
                                     f"{s['slot']} 档状态 {status}",
                                     f"failed={s['failed']} skipped={s['skipped']}")])
    return r


# --------------------------------------------------------------------------- #
# T8 —— 数据质量复盘
# --------------------------------------------------------------------------- #
#: ⚠️ 与 `ops_report.QUALITY_STAGES` **同源**（同一对象，不是同值副本）
QUALITY_STAGES = ops_report.QUALITY_STAGES
QUALITY_LOOKBACK = 10


def quality_gaps(state: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """当天有缺口的质量类 stage：名 → {status, note}。"""
    gaps: dict[str, dict[str, Any]] = {}
    if not state:
        return gaps
    for slot_name, payload in (state.get("slots") or {}).items():
        for st in (payload or {}).get("stages") or []:
            name = st.get("name")
            if name not in QUALITY_STAGES:
                continue
            if str(st.get("status")) == "failed":
                gaps[name] = {"slot": slot_name, "status": st.get("status"),
                              "note": st.get("reason") or st.get("summary") or ""}
    return gaps


def check_quality(day: str, state_dir: Path | None = None) -> dict[str, Any]:
    r = new_result("quality", day)
    sd = state_dir or STATE_DIR
    state = _load_state(day, sd)
    gaps = quality_gaps(state)
    r["sections"] = {"trading": _is_trading_day(day), "gaps": gaps, "streak_hits": [],
                     "lookback_days": []}

    for name, info in gaps.items():
        add_findings(r, [finding("P1", f"quality_{name}", "data_quality", name,
                                 f"{QUALITY_STAGES[name]} 未通过",
                                 f"{info['slot']}/{name}: {str(info['note'])[:200]}")])

    # —— 连续 2 个交易日同口径 → P0（ops_report 做不到的跨日判据，是 T8 的增量）——
    if sd.exists():
        days = []
        for p in sd.glob("*.json"):
            try:
                days.append(dt.date.fromisoformat(p.stem).isoformat())
            except ValueError:
                continue
        days = sorted(days, reverse=True)[:QUALITY_LOOKBACK]
        # ⚠️ 只认「今天确是最新交易日」的情形：否则拿两个历史日凑出的 streak 与今日无关
        trading_days = [d for d in days if _is_trading_day(d) is True]
        r["sections"]["lookback_days"] = trading_days[:3]
        if trading_days and trading_days[0] == day and len(trading_days) >= 2:
            prev = quality_gaps(_load_state(trading_days[1], sd))
            for name in sorted(set(gaps) & set(prev)):
                r["sections"]["streak_hits"].append({"stage": name, "days": trading_days[:2]})
                add_findings(r, [finding("P0", "quality_streak", "data_quality", name,
                                         f"{QUALITY_STAGES[name]} 连续 2 个交易日同口径报缺口",
                                         f"{trading_days[1]} 与 {day} 均未通过")])
        elif not trading_days:
            r["unverified"].append({"item": "quality_streak", "value": None,
                                    "reason": "无可判定的交易日 ⇒ 连续 2 日判据本次未生效"})
    else:
        r["unverified"].append({"item": "quality_streak", "value": None,
                                "reason": f"state 目录不存在：{sd}"})
    return r


# --------------------------------------------------------------------------- #
# T10 —— 公网可达性核验
# --------------------------------------------------------------------------- #
REF_RE = re.compile(r'(?:src|href)="(/[^"]*)"')


def _http_get(url: str, timeout: float = PUBLIC_TIMEOUT) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*",
        "Cache-Control": "no-cache", "Pragma": "no-cache",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read()
            return {"ok": True, "status": resp.status, "body": body,
                    "ctype": resp.headers.get("Content-Type", ""),
                    "cf_cache": resp.headers.get("cf-cache-status", ""),
                    "error": ""}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "body": b"",
                "ctype": exc.headers.get("Content-Type", "") if exc.headers else "",
                "cf_cache": "", "error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001 —— 探针要把所有异常如实带出来
        return {"ok": False, "status": None, "body": b"", "ctype": "", "cf_cache": "",
                "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def _refs(html: bytes) -> set[str]:
    try:
        txt = html.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return set()
    return {m for m in REF_RE.findall(txt) if not m.startswith("//")}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _local_files(root: Path | None = None) -> dict[str, str]:
    """本地构建产物：相对路径 → 本地 sha256。

    ⚠️ 必须吃 `root` 参数，**不得**在函数内直接读模块全局 `DIST_DIR` ——
    否则任何「换目录」的调用（测试夹具、将来的多站点）都会**静默比对本机的真 dist**，
    而表面上「比过了」。本任务实测踩中：夹具里 2 个资产被拿去和真 dist 的 38 个比，
    结论直接是 36 条 sha256 不符。
    """
    base = root if root is not None else DIST_DIR
    out: dict[str, str] = {}
    if not base.exists():
        return out
    for dp, _dns, fns in os.walk(base):
        for fn in fns:
            p = Path(dp) / fn
            rel = p.relative_to(base).as_posix()
            try:
                out[rel] = _sha(p.read_bytes())
            except OSError:
                pass
    return out


def check_public(day: str, base: str = PUBLIC_BASE, dist_dir: Path | None = None,
                 prior_streak: int = 0, local_origin: str = LOCAL_ORIGIN) -> dict[str, Any]:
    r = new_result("public", day)
    dist = dist_dir or DIST_DIR
    sec: dict[str, Any] = {"base": base, "dist_dir": str(dist), "reachable": False,
                           "attempts": [], "index_refs_match": None,
                           "assets": {"total": 0, "match": 0, "mismatch": [], "spa_fallback": [],
                                      "unfetchable": []},
                           "control": {}, "attribution": ""}
    r["sections"] = sec

    # —— ① 公网 index（含 2 次尝试）——
    idx: dict[str, Any] = {}
    for att in range(1, PUBLIC_ATTEMPTS + 1):
        idx = _http_get(base + "/")
        sec["attempts"].append({"attempt": att, "ok": idx["ok"], "status": idx.get("status"),
                                "error": idx.get("error", "")})
        if idx["ok"] and idx.get("status") == 200:
            break
    reachable = bool(idx.get("ok") and idx.get("status") == 200)
    sec["reachable"] = reachable
    sec["cf_cache_status"] = idx.get("cf_cache", "")

    # —— ② 隧道侧 / 本机侧对照（用于区分「本机服务挂」与「隧道/DNS 挂」）——
    tun = _http_get(TUNNEL_METRICS, timeout=6.0)
    loc = _http_get(local_origin + "/", timeout=8.0)
    sec["control"] = {
        "tunnel_metrics_ok": bool(tun["ok"]),
        "local_origin_ok": bool(loc["ok"] and loc.get("status") == 200),
        "local_origin_note": loc.get("error", "") or f"HTTP {loc.get('status')}",
    }

    if not reachable:
        if sec["control"]["tunnel_metrics_ok"] and sec["control"]["local_origin_ok"]:
            sec["attribution"] = "本机服务在跑、隧道进程在跑 ⇒ 问题在隧道出口/DNS/CF 侧"
        elif not sec["control"]["tunnel_metrics_ok"]:
            sec["attribution"] = "隧道进程（:20241 metrics）也不可达 ⇒ 问题在隧道或更上游"
        elif not sec["control"]["local_origin_ok"]:
            sec["attribution"] = "本机 :4173 也不可达 ⇒ 问题在本机服务"
        else:
            sec["attribution"] = "两侧对照不可用，无法归因"
        streak = prior_streak + 1
        sec["fail_streak"] = streak
        level = "P1" if streak >= 2 else "P2"
        add_findings(r, [finding(level, "public_unreachable", "site", "public-index",
                                 f"公网 {base}/ 连续 {streak} 天不可达（本次 {PUBLIC_ATTEMPTS} 次尝试均失败）",
                                 f"attempts={sec['attempts']}；归因：{sec['attribution']}")])
        return r
    sec["fail_streak"] = 0

    # —— ③ 不变量：公网 index 的引用集 ≡ 本地 index 的引用集 ——
    local_idx = dist / "index.html"
    local_refs = _refs(local_idx.read_bytes()) if local_idx.exists() else set()
    pub_refs = _refs(idx["body"])
    sec["index_refs"] = {"local": sorted(local_refs), "public": sorted(pub_refs)}
    match = local_refs == pub_refs
    sec["index_refs_match"] = match
    if not local_refs:
        r["unverified"].append({"item": "index_refs", "value": None,
                                "reason": f"本地 {local_idx} 不存在或无引用 ⇒ 不变量判据本次未生效"})
    elif not match:
        only_local = sorted(local_refs - pub_refs)
        only_pub = sorted(pub_refs - local_refs)
        add_findings(r, [finding("P1", "public_index_ref_mismatch", "site", "index-refs",
                                 "公网 index 引用的资源集合与本地构建不一致",
                                 f"仅本地有={only_local[:8]}；仅公网有={only_pub[:8]}")])

    # —— ④ 资产逐文件 sha256（index.html 自身除外：CF 必然注入 beacon / 归一化空白）——
    files = _local_files(dist)
    targets = {k: v for k, v in files.items() if k != "index.html"}

    def _one(item: tuple[str, str]) -> dict[str, Any]:
        rel, local_sha = item
        got = _http_get(f"{base}/{rel}")
        rec = {"path": "/" + rel, "status": got.get("status"),
               "ctype": got.get("ctype", ""), "error": got.get("error", "")}
        if not got["ok"]:
            rec["verdict"] = "unfetchable"
            return rec
        ctype = (got.get("ctype") or "").lower()
        # ⚠️ SPA 回退：不存在的 /assets/* 会返回 200 + index.html（本仓实测）⇒ 必须单独识别，
        #    否则「资产已丢失」会被读成「站点健康」。这是本判据存在的唯一理由。
        if ctype.startswith("text/html") and not rel.endswith(".html"):
            rec["verdict"] = "spa_fallback"
            return rec
        rec["verdict"] = "match" if _sha(got["body"]) == local_sha else "mismatch"
        if got.get("cf_cache"):
            rec["cf_cache"] = got["cf_cache"]
        return rec

    with ThreadPoolExecutor(max_workers=PUBLIC_WORKERS) as ex:
        results = list(ex.map(_one, sorted(targets.items())))

    sec["assets"]["total"] = len(results)
    for rec in results:
        key = rec["verdict"]
        if key == "match":
            sec["assets"]["match"] += 1
        elif key == "mismatch":
            sec["assets"]["mismatch"].append(rec)
        elif key == "spa_fallback":
            sec["assets"]["spa_fallback"].append(rec)
        else:
            sec["assets"]["unfetchable"].append(rec)
    if not results:
        r["unverified"].append({"item": "public_assets", "value": None,
                                "reason": f"本地无构建产物可比对：{dist}"})
    if sec["assets"]["mismatch"]:
        add_findings(r, [finding("P1", "public_asset_mismatch", "site", "assets",
                                 f"{len(sec['assets']['mismatch'])} 个资产与公网 sha256 不一致",
                                 "; ".join(x["path"] for x in sec["assets"]["mismatch"][:8]))])
    if sec["assets"]["spa_fallback"]:
        add_findings(r, [finding("P1", "public_asset_spa_fallback", "site", "assets",
                                 f"{len(sec['assets']['spa_fallback'])} 个本地资产在公网取到 HTML（SPA 回退）",
                                 "; ".join(x["path"] + f"(status={x['status']})"
                                           for x in sec["assets"]["spa_fallback"][:8]))])
    if sec["assets"]["unfetchable"]:
        add_findings(r, [finding("P1", "public_asset_unfetchable", "site", "assets",
                                 f"{len(sec['assets']['unfetchable'])} 个资产取不到",
                                 "; ".join(f"{x['path']}({x['error']})"
                                           for x in sec["assets"]["unfetchable"][:8]))])
    return r


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
CHECKS = ("resource", "slot", "quality", "public")


def prior_public_streak(day: str, out_dir: Path | None = None) -> int:
    """读最近一份**早于 today** 的 public 产物，取其 fail_streak（跨运行「连续失败」的依据）。"""
    d = out_dir or OPS_OUT
    if not d.exists():
        return 0
    cands = []
    for p in d.glob("public-*.json"):
        stem = p.stem.split("-", 1)[1]
        try:
            cands.append((dt.datetime.strptime(stem, "%Y%m%d").date().isoformat(), p))
        except ValueError:
            continue
    prev = [c for c in sorted(cands) if c[0] < day]
    if not prev:
        return 0
    try:
        return int(json.loads(prev[-1][1].read_text(encoding="utf-8")).get("sections", {})
                   .get("fail_streak") or 0)
    except (OSError, ValueError, TypeError):
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="每日运维四支巡检（只采集与判定，不写 alerts）")
    ap.add_argument("--check", required=True, choices=CHECKS, help="巡检支：resource/slot/quality/public")
    ap.add_argument("--date", default=None, help="目标日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--out-dir", default=None, help="产物目录覆盖（测试用）")
    ap.add_argument("--state-dir", default=None, help="state 目录覆盖（测试用；缺省用生产路径）")
    ap.add_argument("--quiet", action="store_true", help="不向 stderr 打日志")
    args = ap.parse_args(argv)

    global _log
    if args.quiet:
        _log = lambda msg, level="INFO": None  # type: ignore[assignment]

    day = args.date or dt.date.today().isoformat()
    try:
        dt.date.fromisoformat(day)
    except ValueError:
        _log(f"--date 非法：{day!r}", "ERROR")
        return 2

    out_dir = Path(args.out_dir).resolve() if args.out_dir else None
    state_dir = Path(args.state_dir).resolve() if args.state_dir else None
    try:
        if args.check == "resource":
            res = check_resource(day)
        elif args.check == "slot":
            res = check_slot(day, state_dir)
        elif args.check == "quality":
            res = check_quality(day, state_dir)
        else:
            res = check_public(day, prior_streak=prior_public_streak(day, out_dir))
    except Exception as exc:  # noqa: BLE001 —— 巡检脚本自身崩了必须显式出声，不得静默
        _log(f"巡检 {args.check} 自身异常：{exc!r}", "ERROR")
        return 2

    path = write_artifact(res, out_dir=out_dir, date=day)
    summary = {"probe": res["probe"], "date": day, "level": res["level"],
               "findings": len(res["findings"]), "unverified": len(res["unverified"]),
               "artifact": str(path)}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    _log(f"巡检 {args.check} 完成：level={res['level']} findings={len(res['findings'])} → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
