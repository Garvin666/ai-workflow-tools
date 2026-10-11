#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[自研工具] ops_probe.py —— 每日运维四支巡检（量化平台）

名称：ops_probe.py
用途：按设计案 `报告-每日自动运维方案-20261011.md` §2.2 实现四支**互相独立、各自定时**的巡检，
      并把结论落成结构化产物，供 23:30 的 `ops_report.py` 折入当日日报与告警流水。
适用场景：本工作区（E:/ChatGPT/量化分析与数据分析）的每日运维；也适用于任何
      「需要按不同时刻分批巡检、但结论最终汇总到一份日报」的本地运维场景。
仓库链接：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/ops_probe.py

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
            add_findings(r, [finding("P1", "disk_low", "host", d["label"],
                                     f"{d['label']} 可用 {pct}%（< {DISK_WARN_PCT}%）",
                                     f"free {d['free_gb']} GB / total {d['total_gb']} GB")])
    for d in dirs:
        if d["is_log"] and d["mb"] > LOG_VOLUME_WARN_MB:
            add_findings(r, [finding("P1", "log_volume", "host", d["label"],
                                     f"{d['label']} 体积 {d['mb']} MB（> {LOG_VOLUME_WARN_MB} MB）",
                                     f"{d['path']}（{d['files']} 文件）")])

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
