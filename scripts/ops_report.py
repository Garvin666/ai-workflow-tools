#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# [自研工具] ops_report.py
# 用途：每日运维日报与告警登记（量化平台）—— 汇总站点自愈/数据链路调度/调度自检/外设守护当日状态，产出七段日报与三级（P0/P1/P2）告警流水，并折入四支巡检结论
# 适用场景：本工作区（E:/ChatGPT/量化分析与数据分析）的每日运维收尾（建议 23:30）；也适用于任何「状态散落在多套机制的日志/状态文件里、需要每天翻一遍」的巡检场景
# 作者：ai-workflow 自研（量化平台运维沉淀，2026-10-11）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/ops_report.py
"""ops_report.py —— 每日运维日报与告警登记器（量化平台）

用途
----
把四套互不感知的自动化（站点自愈 / 数据链路调度 / 调度自检 / 外设守护）当日状态
汇总成**一份七段日报**与**一份机器可读告警流水**，并按三级（P0/P1/P2）判定。
另接线 `ops_backup.check_drift()`：检查备份的**硬链接漂移**
（S1 快照污染 / S3 链接能力失效 → P1；S2 链接已断属正常更替，只在日报展示）。

适用场景
--------
本工作区（E:/ChatGPT/量化分析与数据分析）的每日运维收尾（建议 23:30），
以及任何「状态散落在多套机制的日志/状态文件里、需要每天翻一遍」的巡检场景。

⚠️ 能力边界（刻意不实现）
------------------------
**不自动外发邮件/消息**。对外发送属高风险动作，须先确认收件人与内容清单。
本脚本只把 P0 通知正文写进 `daily_output/ops/outbox/`，由人或后续接线发送。

退出码
------
0 = 日报已生成（可能有 P1/P2）；1 = 存在 P0（需立即处置）；2 = 参数或环境错误。

用法
----
    .venv\\Scripts\\python.exe scripts\\ops_report.py                 # 生成当天日报
    .venv\\Scripts\\python.exe scripts\\ops_report.py --date 2026-10-10
    .venv\\Scripts\\python.exe scripts\\ops_report.py --no-probe      # 跳过站点探活（离线）
    .venv\\Scripts\\python.exe scripts\\ops_report.py --quiet         # 只出 JSON 摘要
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / ".work" / "daily" / "scheduler" / "state"
LOG_DIR = PROJECT_ROOT / ".work" / "daily" / "scheduler" / "logs"
BACKUP_ROOT = PROJECT_ROOT / "backup"
OPS_OUT = PROJECT_ROOT / "daily_output" / "ops"

SITE_PROBES = (
    ("后端 API", "http://127.0.0.1:8000/api/health"),
    ("前端站点", "http://127.0.0.1:4173/"),
    ("隧道 metrics", "http://127.0.0.1:20241/metrics"),
)
DISK_WARN_PCT = 15.0
DISK_CRIT_PCT = 8.0
LOG_VOLUME_WARN_MB = 2048.0
LOOKBACK_STATES = 10
#: 数据质量类 stage：名 → 中文标题
QUALITY_STAGES = {
    "sentinel": "估值漂移哨兵",
    "daily_continuity": "日线覆盖连续性",
    "market_sync_refill": "当日真值新鲜度",
    "forecast_verify_daily": "预测精度复核",
    "forecast_forward_daily": "前瞻预测留存",
}

#: 每日巡检支（`scripts/ops_probe.py` 的四支）—— 名 → 中文标题
PROBE_CHECKS = ("resource", "slot", "quality", "public")
PROBE_TITLES = {
    "resource": "资源巡检(T1)",
    "slot": "收盘档结果汇总(T7)",
    "quality": "数据质量复盘(T8)",
    "public": "公网可达性核验(T10)",
}


def _own_code(code: str) -> bool:
    """该 code 是**日报自己在 23:30 判定**的事件。

    ⚠️ 为什么需要这条分界线（而不是简单地把巡检 findings 全折进来）：
    巡检在 **07:00 / 21:15 / 22:00 / 22:45** 跑，用的磁盘读数与 `state/<date>.json` 都**早于** 23:30。
    日报在同一批事件上有更**新鲜**的读数 ⇒ 两者全折入会产生**同一故障两行**，
    且其中一行是过期读数。故：同类 code 以日报为准，巡检只贡献**日报判据覆盖不到**的部分
    （T10 的公网核验、T8 的跨日 streak、T7 的日历不可用）。
    这是「同一物理量必须同源」在**时序**上的延伸：同源还不够，还要取**同一时刻**的那一份。
    """
    if code in {"site_down", "disk_critical", "disk_low", "host_disk_low", "log_volume",
                "close_no_record", "slot_failed", "slot_degraded", "silent_days"}:
        return True
    if code.startswith("backup_") or code.startswith("drift"):
        return True
    # quality_<stage> 是日报的单日判据；quality_streak（跨日）是 T8 独占
    return code.startswith("quality_") and code != "quality_streak"


def load_probes(day: str) -> dict[str, dict[str, Any] | None]:
    """读当日四支巡检产物；缺失记 `None`（**不静默当成通过**，由 build_alerts 出声）。"""
    out: dict[str, dict[str, Any] | None] = {}
    stem = day.replace("-", "")
    for name in PROBE_CHECKS:
        p = OPS_OUT / f"{name}-{stem}.json"
        if not p.exists():
            out[name] = None
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            data["_path"] = str(p)
            out[name] = data if isinstance(data, dict) else None
        except (OSError, ValueError) as exc:
            out[name] = {"probe": name, "ok": False, "findings": [], "sections": {},
                         "unverified": [], "_path": str(p), "error": f"产物不可解析：{exc!r}"}
    return out


def _log(msg: str, level: str = "INFO") -> None:
    print(f"[{level}] {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #
def probe_site(timeout: float = 6.0) -> list[dict[str, Any]]:
    out = []
    for name, url in SITE_PROBES:
        ok = False
        err = ""
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            resp = opener.open(url, timeout=timeout)
            ok = resp.status == 200
            err = "" if ok else f"HTTP {resp.status}"
        except Exception as exc:  # noqa: BLE001 —— 探活失败即失败，不区分异常类型
            err = type(exc).__name__ + ": " + str(exc)[:120]
        out.append({"name": name, "url": url, "ok": ok, "error": err})
    return out


def disk_stats() -> list[dict[str, Any]]:
    out = []
    for label, path in (("工作区所在盘", PROJECT_ROOT), ("系统盘", Path(os.environ.get("SystemDrive", "C:") + "\\"))):
        try:
            u = shutil.disk_usage(str(path))
        except OSError as exc:
            out.append({"label": label, "error": repr(exc)})
            continue
        out.append({
            "label": label,
            "path": str(path),
            "total_gb": round(u.total / 1024**3, 1),
            "free_gb": round(u.free / 1024**3, 1),
            "free_pct": round(100.0 * u.free / u.total, 2) if u.total else 0.0,
        })
    return out


def dir_stats() -> list[dict[str, Any]]:
    out = []
    for label, path, is_log in (
        ("调度日志", LOG_DIR, True),
        ("脚本目录", PROJECT_ROOT / "scripts", False),
        ("日报目录", PROJECT_ROOT / "daily_output", False),
        # ⚠️ 名义值：硬链接项与源共享 inode，此处会被重复计入。
        # 真实新增量见第六段（按 manifest 的 method 分类求和）。
        # ⚠️ is_log=False：备份目录名义值可轻易过 2 GB，「日志目录 > 2 GB」不适用于它。
        ("备份目录(名义值)", BACKUP_ROOT, False),
    ):
        total = 0
        files = 0
        if path.exists():
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = [d for d in dirnames if d not in {"node_modules", ".venv"}]
                for fn in filenames:
                    files += 1
                    try:
                        total += os.path.getsize(os.path.join(dirpath, fn))
                    except OSError:
                        pass
        out.append({"label": label, "path": str(path.relative_to(PROJECT_ROOT)),
                    "mb": round(total / 1024 / 1024, 2), "files": files, "is_log": is_log})
    return out


def load_state(day: str) -> dict[str, Any] | None:
    p = STATE_DIR / f"{day}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def recent_state_days(n: int) -> list[str]:
    if not STATE_DIR.exists():
        return []
    days = []
    for p in STATE_DIR.glob("*.json"):
        try:
            dt.date.fromisoformat(p.stem)
        except ValueError:
            continue
        days.append(p.stem)
    return sorted(days, reverse=True)[:n]


def is_trading_day(day: str) -> bool | None:
    """尝试用本项目的交易日历判定；不可用则返回 None（不猜）。"""
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from daily_scheduler import calendar as cal  # noqa: PLC0415

        return bool(cal.is_trading_day(day, market="cn").is_trading)
    except Exception:  # noqa: BLE001 —— 日历不可用不该让日报崩掉
        return None


def load_backup(day: str) -> dict[str, Any] | None:
    for candidate in (day, *recent_state_days(3)):
        p = BACKUP_ROOT / candidate / "manifest.json"
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                data["_dir"] = candidate
                data["_is_today"] = candidate == day
                return data
            except (OSError, ValueError):
                return None
    return None


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #
def build_alerts(day: str, site_probes: list[dict], probes: dict[str, Any],
                 disks: list[dict], dirs: list[dict],
                 state: dict[str, Any] | None, backup: dict[str, Any] | None,
                 trading: bool | None,
                 drift: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    now = dt.datetime.now().isoformat(timespec="seconds")
    alerts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(level: str, code: str, target: str, detail: str, evidence: str,
            subject: str = "") -> None:
        # ⚠️ `subject` 是**稳定的机器键**（盘标签/档名/stage 名/探活对象），不是给人看的文案。
        #    为什么不用 detail 当键：detail 里嵌了**当次读数**（如「可用 11.48%」），07:00 与 23:30
        #    两次读数必然不同 ⇒ 用 detail 去重会失效，同一故障在日报里出现两行。
        key = (code, target, subject or detail)
        if key in seen:
            return
        seen.add(key)
        alerts.append({"ts": now, "level": level, "code": code, "target": target,
                       "subject": subject, "detail": detail, "evidence": evidence,
                       "first_seen": day, "notified": False})

    # 站点
    for p in site_probes:
        if not p["ok"]:
            add("P0", "site_down", "site", f"{p['name']} 探活失败", f"{p['url']} → {p['error']}",
                subject=p["name"])

    # 磁盘
    for d in disks:
        pct = d.get("free_pct")
        if pct is None:
            continue
        if pct < DISK_CRIT_PCT:
            add("P0", "disk_critical", "host", f"{d['label']} 可用仅 {pct}%（< {DISK_CRIT_PCT}%）",
                f"free {d['free_gb']} GB / total {d['total_gb']} GB", subject=d["label"])
        elif pct < DISK_WARN_PCT:
            # ⚠️ 告警级别是「可处置性」的函数，不是「远离阈值的程度」的函数。
            #    设计案 §2.2 的 15%/8% 原文写给**系统盘**；同一组阈值套到**工作区所在盘**上，
            #    会出现一条本方无法处置的**永久 P1**（删光整个工作区也只从 11.5% 拉到 13.5%），
            #    而 `verdict_of` 是集合判定 ⇒ 任一 P1 即「注意」，一条永久 P1 把三态压成两态。
            #    ⇒ 只对**系统盘**保持 P1（原样）；工作区所在盘降 P2（知情项，不进 verdict）。
            #    ⚠️ 两个阈值常量与 `if pct < DISK_CRIT_PCT` 分支**均原样不动** —— P0 红线两盘共用。
            #    ⚠️ `scripts/ops_probe.py::check_resource` 是同一判据的**第二份实现**，必须同步改。
            if d["label"] == "系统盘":
                add("P1", "disk_low", "host", f"{d['label']} 可用 {pct}%（< {DISK_WARN_PCT}%）",
                    f"free {d['free_gb']} GB / total {d['total_gb']} GB", subject=d["label"])
            else:
                add("P2", "host_disk_low", "host",
                    f"{d['label']} 可用 {pct}%（< {DISK_WARN_PCT}%，非系统盘 ⇒ 知情项）",
                    f"free {d['free_gb']} GB / total {d['total_gb']} GB", subject=d["label"])

    # 日志体量：⚠️ 只对**日志目录**生效（`is_log`）—— 把「日志目录 > 2 GB」套到源码/备份目录上
    #    是口径外扩；备份目录名义值可轻松过 2 GB，那会变成一个与判据本意无关的告警。
    for d in dirs:
        if d.get("is_log") and d["mb"] > LOG_VOLUME_WARN_MB:
            add("P1", "log_volume", "host", f"{d['label']} 体积 {d['mb']} MB（> {LOG_VOLUME_WARN_MB} MB）",
                f"{d['path']}（{d['files']} 文件）", subject=d["label"])

    # 数据链路
    if trading and state is None:
        add("P0", "close_no_record", "daily_scheduler", "交易日无任何档位记录（任务可能未被触发）",
            f"state/{day}.json 不存在", subject="state")
    if state:
        for slot_name, payload in (state.get("slots") or {}).items():
            status = str((payload or {}).get("status") or "unknown")
            if status == "failed":
                add("P0", "slot_failed", "daily_scheduler",
                    f"{slot_name} 档状态 failed",
                    f"failed_stages={payload.get('failed_stages')}", subject=str(slot_name))
            elif status in ("partial", "skipped_locked"):
                add("P1", "slot_degraded", "daily_scheduler",
                    f"{slot_name} 档状态 {status}",
                    f"failed={payload.get('failed_stages')} skipped={payload.get('skipped_stages')}",
                    subject=str(slot_name))

    # 数据质量
    if state:
        for slot_name, payload in (state.get("slots") or {}).items():
            for st in (payload or {}).get("stages") or []:
                name = st.get("name")
                if name not in QUALITY_STAGES:
                    continue
                status = str(st.get("status") or "")
                if status in ("failed",):
                    add("P1", f"quality_{name}", "data_quality",
                        f"{QUALITY_STAGES[name]} 未通过",
                        f"{slot_name}/{name}: {st.get('reason') or st.get('summary')}",
                        subject=str(name))

    # 静默停摆（连续 2 个交易日无记录）
    days = recent_state_days(LOOKBACK_STATES)
    silent = []
    for d in days[:5]:
        if is_trading_day(d) and load_state(d) is None:
            silent.append(d)
    if len(silent) >= 2:
        add("P0", "silent_days", "daily_scheduler",
            f"最近交易日有 {len(silent)} 天完全无记录", ", ".join(silent), subject="silent")

    # 备份
    if backup is None:
        add("P0", "backup_missing", "backup", "未找到任何备份产物", f"{BACKUP_ROOT}", subject="backup")
    else:
        summary = backup.get("summary") or {}
        if summary.get("failed"):
            add("P0", "backup_failed", "backup", f"备份有 {summary['failed']} 个失败项",
                f"{backup['_dir']}/manifest.json", subject="backup")
        if not backup.get("_is_today"):
            add("P1", "backup_stale", "backup", f"当日无备份，最近一份为 {backup['_dir']}",
                f"{backup['_dir']}/manifest.json", subject="backup")

    # 硬链接漂移（接线 ops_backup.check_drift；口径见本次任务 方案评审.md §3）
    # S1 快照污染 / S3 链接能力失效 ⇒ P1（由 check_drift 自带 level）；
    # S2 链接已断属正常更替，**不产生告警**，只在日报第六段展示。
    if drift is None:
        add("P2", "drift_not_checked", "backup", "硬链接漂移检测未执行", "drift=None", subject="drift")
    elif drift.get("error"):
        # ⚠️ 「检查没跑」不得与「检查通过」同形 —— 出错必出声
        add("P2", "drift_check_error", "backup",
            f"硬链接漂移检测未完成：{drift['error']}",
            f"backup={drift.get('backup')}", subject="drift")
    else:
        for f in drift.get("findings") or []:
            # subject 留空 ⇒ 去重键回落到 detail（逐文件唯一），避免两条同 code 漂移被判为重复
            add(str(f.get("level") or "P1"), str(f.get("code") or "backup_drift"), "backup",
                str(f.get("detail") or "硬链接漂移"), str(f.get("evidence") or ""))

    # ---- 巡检产物折入（T1/T7/T8/T10）----
    # 只折入**日报判据覆盖不到**的 code（见 `_own_code`）；同类事件以日报 23:30 的更新鲜读数为准。
    for name in PROBE_CHECKS:
        art = probes.get(name)
        if art is None:
            # ⚠️ 「巡检没跑」必须出声 —— 否则「没检查」会与「检查通过」在日报上长得一模一样
            add("P2", "probe_not_run", "ops_probe",
                f"{PROBE_TITLES[name]} 当日未跑（无产物）",
                f"daily_output/ops/{name}-{day.replace('-', '')}.json 不存在")
            continue
        if art.get("ok") is False:
            add("P1", "probe_error", "ops_probe", f"{PROBE_TITLES[name]} 未跑成",
                f"{art.get('error')}｜{art.get('_path')}")
        if art.get("unverified"):
            uv = art["unverified"]
            # 采到了但未设阈值/未校验 —— 单列一条，**不与 findings 合并**：
            # 「未校验」不是「通过」，但也不是「告警」。
            add("P2", "probe_unverified", "ops_probe",
                f"{PROBE_TITLES[name]} 有 {len(uv)} 项已采集但未设阈值/未校验",
                "; ".join(str(u.get("item")) for u in uv[:6]))
        for f in art.get("findings") or []:
            code = str(f.get("code") or "probe")
            if _own_code(code):
                continue
            add(str(f.get("level") or "P1"), code, str(f.get("target") or "ops_probe"),
                str(f.get("detail") or ""), str(f.get("evidence") or ""),
                subject=str(f.get("subject") or ""))
    return alerts


def verdict_of(alerts: list[dict[str, Any]]) -> str:
    levels = {a["level"] for a in alerts}
    if "P0" in levels:
        return "异常"
    if "P1" in levels:
        return "注意"
    return "OK"


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def render_report(day: str, alerts: list[dict], site_probes: list[dict],
                  probes: dict[str, Any], disks: list[dict],
                  dirs: list[dict], state: dict[str, Any] | None,
                  backup: dict[str, Any] | None, trading: bool | None,
                  drift: dict[str, Any] | None = None) -> str:
    v = verdict_of(alerts)
    p0 = [a for a in alerts if a["level"] == "P0"]
    p1 = [a for a in alerts if a["level"] == "P1"]
    p2 = [a for a in alerts if a["level"] == "P2"]
    weekday = "一二三四五六日"[dt.date.fromisoformat(day).weekday()]
    td = "交易日" if trading else ("非交易日" if trading is False else "交易日未知")

    L: list[str] = []
    L.append(f"# 运维日报 {day}（星期{weekday}，{td}）\n")
    L.append("\n## 一、结论\n")
    L.append(f"**[{v}]** 未闭环问题 {len(alerts)} 项（P0 {len(p0)} / P1 {len(p1)} / P2 {len(p2)}）\n")
    if v == "OK":
        L.append("\n各项检查均未触发告警阈值。\n")
    else:
        L.append("\n本节结论由下方各板块汇总得出，逐条证据见对应板块。\n")

    L.append("\n## 二、站点可用性\n")
    L.append("| 对象 | 结果 | 证据 |\n|---|---|---|\n")
    for p in site_probes:
        L.append(f"| {p['name']} | {'OK' if p['ok'] else '**失败**'} | `{p['url']}`"
                 + (f" → {p['error']}" if p["error"] else "") + " |\n")
    _pub = (probes.get("public") or {}).get("sections") or {}
    if _pub:
        reach = "**不可达**" if not _pub.get("reachable") else "OK"
        _a = _pub.get("assets") or {}
        L.append(f"| 公网根路径(T10) | {reach} | `{_pub.get('base')}/`"
                 + (f"；归因：{_pub.get('attribution')}" if _pub.get("attribution") else "")
                 + (f"；连续失败 {_pub.get('fail_streak')} 天" if _pub.get("fail_streak") else "") + " |\n")
        if _pub.get("reachable"):
            L.append(f"| 公网资产 sha256(T10) | {_a.get('match', 0)}/{_a.get('total', 0)} 一致 | "
                     f"不一致 {len(_a.get('mismatch') or [])} ｜ SPA 回退 {len(_a.get('spa_fallback') or [])} ｜ "
                     f"取不到 {len(_a.get('unfetchable') or [])} |\n")
            L.append(f"| index 引用集不变量(T10) | "
                     f"{'一致' if _pub.get('index_refs_match') else '**不一致**'} | "
                     f"本地 {len((_pub.get('index_refs') or {}).get('local') or [])} 个 vs "
                     f"公网 {len((_pub.get('index_refs') or {}).get('public') or [])} 个 |\n")
        ctl = _pub.get("control") or {}
        L.append(f"| 隧道侧对照(T10) | tunnel metrics {'OK' if ctl.get('tunnel_metrics_ok') else '**不可达**'} ｜ "
                 f"本机 :4173 {'OK' if ctl.get('local_origin_ok') else '**不可达**'} | "
                 f"用于区分「本机服务挂」与「隧道/DNS 挂」 |\n")
    L.append("\n> 本表为**即时探活**（生成日报那一刻）；常驻自愈看的是 `scripts/watchdog.log` 的连续失败计数。\n"
             "> 公网一行来自 T10 巡检产物（22:45 跑的，非日报生成时刻）；资产比对**不用返回码**"
             "—— 本站 SPA 回退会把不存在的 `/assets/*` 也返回 200 + index.html。\n")

    L.append("\n## 三、数据链路\n")
    _slot_art = probes.get("slot") or {}
    _slot = _slot_art.get("sections") or {}
    if _slot.get("slots"):
        # T7 的产物：档位×stage 表 + partial/failed/skipped_locked 的 reason 与 stderr_tail
        # ⚠️ 时点必须取**产物自己的 ts**，不得硬编码「21:15」—— 计划时刻与实际执行时刻是两件事，
        #    写死会在手工补跑时产出一句假话（本任务实测踩中）。
        L.append(f"> 数据来自 T7 巡检产物（实际生成于 {_slot_art.get('ts') or '未知时刻'}）。\n\n")
        L.append("| 档位 | 状态 | stage 数 | 失败 | 跳过 | 开始 | 结束 |\n|---|---|---|---|---|---|---|\n")
        for s in _slot["slots"]:
            L.append(f"| {s.get('slot')} | {s.get('status')} | {len(s.get('stages') or [])} | "
                     f"{len(s.get('failed') or [])} | {len(s.get('skipped') or [])} | "
                     f"{s.get('started_at') or ''} | {s.get('finished_at') or ''} |\n")
        probs = _slot.get("problem_stages") or []
        L.append("\n异常 stage 明细（`reason` / `stderr_tail`）：\n")
        if probs:
            for pr in probs:
                L.append(f"- `{pr.get('slot')}/{pr.get('name')}` [{pr.get('status')}]"
                         f" exit={pr.get('exit_code')} attempts={pr.get('attempts')}："
                         f"{str(pr.get('reason') or '（无 reason）')[:200]}\n")
                if pr.get("stderr_tail"):
                    L.append(f"  - stderr_tail：`{str(pr['stderr_tail'])[-300:]}`\n")
        else:
            L.append("（无）\n")
    elif state is None:
        L.append(f"当日无档位记录（`state/{day}.json` 不存在）。"
                 + ("" if _slot else "（T7 巡检产物亦缺失）") + "\n")
    else:
        L.append("> T7 巡检产物缺失，退化为日报内置简版表（**无 reason/stderr_tail 明细**）。\n\n")
        L.append("| 档位 | 状态 | stage 数 | 失败 | 跳过 | 开始 | 结束 |\n|---|---|---|---|---|---|---|\n")
        for slot_name, payload in (state.get("slots") or {}).items():
            p = payload or {}
            stages = p.get("stages") or []
            L.append(f"| {slot_name} | {p.get('status')} | {len(stages)} | "
                     f"{len(p.get('failed_stages') or [])} | {len(p.get('skipped_stages') or [])} | "
                     f"{p.get('started_at', '')} | {p.get('finished_at', '')} |\n")
        detail = []
        for slot_name, payload in (state.get("slots") or {}).items():
            for st in (payload or {}).get("stages") or []:
                if st.get("status") == "failed":
                    detail.append(f"- `{slot_name}/{st.get('name')}` 失败：{st.get('reason')}")
        L.append("\n失败 stage 明细：\n")
        L.append("\n".join(detail) + "\n" if detail else "（无）\n")

    L.append("\n## 四、数据质量\n")
    L.append("| 哨兵 | 状态 | 备注 |\n|---|---|---|\n")
    found = False
    if state:
        for slot_name, payload in (state.get("slots") or {}).items():
            for st in (payload or {}).get("stages") or []:
                if st.get("name") in QUALITY_STAGES:
                    found = True
                    note = st.get("reason") or st.get("summary") or ""
                    L.append(f"| {QUALITY_STAGES[st['name']]} | {st.get('status')} | {str(note)[:160]} |\n")
    if not found:
        L.append("| （当日无质量类 stage 记录） | — | — |\n")
    _q = (probes.get("quality") or {}).get("sections") or {}
    if _q.get("streak_hits"):
        L.append("\n**连续 2 个交易日同口径报缺口（T8）**：\n")
        for h in _q["streak_hits"]:
            L.append(f"- {QUALITY_STAGES.get(h.get('stage'), h.get('stage'))}："
                     f"{' 与 '.join(h.get('days') or [])}\n")
    elif _q:
        L.append(f"\n> T8 跨日判据已跑：回看 {_q.get('lookback_days')}，"
                 f"无「连续 2 个交易日同口径」命中。\n")

    L.append("\n## 五、资源\n")
    L.append("| 盘 | 可用 | 空闲/总量 |\n|---|---|---|\n")
    for d in disks:
        if "error" in d:
            L.append(f"| {d['label']} | 未知 | {d['error']} |\n")
        else:
            L.append(f"| {d['label']} | {d['free_pct']}% | {d['free_gb']} GB / {d['total_gb']} GB |\n")
    _r_art = probes.get("resource") or {}
    _r = _r_art.get("sections") or {}
    _dirs = _r.get("dirs") or dirs
    L.append("\n| 目录 | 体量(MB) | 文件数 | 已排除 |\n|---|---|---|---|\n")
    for d in _dirs:
        exc = d.get("excluded") or []
        exc_txt = "；".join(f"{e['name']} {e['mb']}MB/{e['files']}文件" for e in exc) if exc else "—"
        L.append(f"| {d['label']} | {d['mb']} | {d['files']} | {exc_txt} |\n")
    # 工作区级预算（2026-10-11 追加）—— **只渲染，不判定**：判定在 T1 巡检里（唯一实现），
    # 日报靠产物折入。⚠️ 刻意不在日报重算：全盘遍历实测 100+ 秒，而体积是慢变量，
    # 07:00 的读数在 23:30 依然有效。
    _ws = _r.get("workspace") or {}
    if _ws.get("error"):
        L.append(f"\n> ⚠️ 工作区体量扫描失败：`{_ws['error']}` —— **不是「未超标」**。\n")
    elif _ws:
        L.append(f"\n**工作区合计 {_ws.get('mb')} MB / {_ws.get('files')} 文件**"
                 f"（硬链接已去重；另 {_ws.get('dup_inode_files')} 个重复 inode、"
                 f"{_ws.get('dup_mb')} MB 未重复计入）\n")
        rows = _ws.get("budget_rows") or []
        if rows:
            L.append("\n| 目录 | 体量(MB) | 预算(MB) | 占比 | 状态 |\n|---|---|---|---|---|\n")
            for r in rows:
                if not r.get("exists"):
                    L.append(f"| {r['label']} | — | {r['budget_mb']:.0f} | — | 目录不存在（**不判**）|\n")
                    continue
                mark = "⚠️ 超预算" if r.get("over") else "ok"
                L.append(f"| {r['label']} | {r['mb']} | {r['budget_mb']:.0f} | "
                         f"{r['pct']}% | {mark} |\n")
            L.append("\n> 预算为**初值**（留 25%+ 余量），定位是抓**失控增长**（翻倍级），"
                     "不是抓「比昨天多一点点」——若初值就设成「今天已超标」，只会再造一条永久 P1。\n")

    if not _r:
        L.append("\n> ⚠️ T1 巡检产物缺失：上表为日报内置口径（目录集与 T1 不同，且**无内存/进程/句柄采集**）。\n")
    else:
        L.append("\n### T1 采集（**未设阈值**，只作展示与趋势）\n")
        L.append("| 指标 | 值 | 为什么没有阈值 |\n|---|---|---|\n")
        # ⚠️ `unverified` 在产物**顶层**，不在 `sections` —— 读错层级会让整张表只剩 0 行，
        #    而「未设阈值」的行恰恰是这张表存在的理由（本任务实测踩中）。
        for u in (_r_art.get("unverified") or []):
            L.append(f"| {u.get('item')} | `{str(u.get('value'))[:160]}` | {u.get('reason')} |\n")
        _sp = _r.get("site_processes")
        if isinstance(_sp, list):
            for s in _sp:
                L.append(f"| 进程 {s.get('label')}(:{s.get('port')}) | "
                         f"pid {s.get('pid')} ｜ 句柄 {s.get('handles')} ｜ 工作集 {s.get('ws_mb')} MB | "
                         f"站点进程，只作展示 |\n")
        L.append("\n> 采集项与阈值项**分开列**：采到但未设阈值的项不进告警，"
                 "但也**不得读成「正常」**——「未设阈值」不是「通过」。\n")

    L.append("\n## 六、备份\n")
    if backup is None:
        L.append("**未找到任何备份产物。**\n")
    else:
        s = backup.get("summary") or {}
        L.append(f"- 最近备份：`backup/{backup['_dir']}/`（当日备份：{'是' if backup.get('_is_today') else '**否**'}）\n")
        L.append(f"- 生成时间：{backup.get('created_at', '')}\n")
        L.append(f"- 项数：{s.get('total', '?')}（复制 {s.get('copied', '?')} / 硬链接 {s.get('hardlinked', '?')} / "
                 f"回退 {s.get('copy_fallback', '?')}），失败 {s.get('failed', '?')}\n")
        L.append(f"- SQLite 完整性校验通过：{s.get('sqlite_verified', '?')} 个\n")
        L.append(f"- 备份时磁盘可用：{backup.get('disk_free_pct', '?')}%\n")
        # 硬链接漂移：S1/S3 走告警（第七段），S2 只在此展示（正常更替，非故障）
        if drift is None:
            L.append("- ⚠️ 硬链接漂移检测**未执行**\n")
        elif drift.get("error"):
            L.append(f"- ⚠️ 硬链接漂移检测**未完成**：{drift['error']}\n")
        else:
            nb = len(drift.get("link_broken") or [])
            _uv = drift.get("unverified", 0) or 0
            # ⚠️ 「P1 漂移」必须只数 P1：findings 现在还含 P2 的 unverifiable，
            #    直接 len(findings) 会把「无法校验」记成「P1 漂移」（2026-10-11 同族缺陷）。
            _p1 = [f for f in (drift.get("findings") or []) if str(f.get("level")) == "P1"]
            L.append(f"- 硬链接漂移检测（`{drift.get('backup')}`）：已校验 {drift.get('checked', 0)} 个硬链接项"
                     + (f"（另有 {_uv} 项因缺 sha256 **无法校验**）" if _uv else "")
                     + f"，P1 漂移 {len(_p1)} 条\n")
            if nb:
                L.append(f"- 链接已断 {nb} 项：源被整文件替换，备份已冻结为独立副本"
                         "（**正常更替，非故障**；下次备份会重新链接）\n")
                for lb in (drift.get("link_broken") or [])[:5]:
                    L.append(f"  - `{lb.get('src')}`\n")

    L.append("\n## 七、待办\n")
    if alerts:
        L.append("| 级别 | 问题 | 建议动作 | 首次出现 |\n|---|---|---|---|\n")
        for a in alerts:
            L.append(f"| {a['level']} | {a['detail']} | 证据：{a['evidence']} | {a['first_seen']} |\n")
    else:
        L.append("（无）\n")
    L.append(f"\n---\n\n> 生成时间：{dt.datetime.now():%Y-%m-%d %H:%M:%S} ｜ "
             "生成器：`scripts/ops_report.py` ｜ 判定口径见 `报告-每日自动运维方案-20261011.md` §3\n")
    return "".join(L)


def render_notice(day: str, alerts: list[dict]) -> str:
    p0 = [a for a in alerts if a["level"] == "P0"]
    lines = [f"[P0] 量化平台运维告警（{day}）", f"生成时间：{dt.datetime.now():%Y-%m-%d %H:%M:%S}", ""]
    for a in p0:
        lines += [
            f"事件：{a['detail']}",
            f"对象：{a['target']} ｜ 代码：{a['code']}",
            f"证据：{a['evidence']}",
            "建议：先跑 `.venv\\Scripts\\python.exe -m daily_scheduler.cli doctor --days 5`，"
            "再按 `报告-每日自动运维方案-20261011.md` §3.3 的动作矩阵处置",
            "",
        ]
    lines.append("（本内容由 ops_report.py 写入 outbox，**尚未发送** —— 外发属高风险动作，需先确认收件人）")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 备份漂移（接线 ops_backup.check_drift）
# --------------------------------------------------------------------------- #
def check_backup_drift() -> dict[str, Any]:
    """调用同目录 `ops_backup.check_drift()`。

    ⚠️ **失败不静默**：导入失败或检测抛错时返回带 `error` 的结果，由 `build_alerts`
    转成 **P2 知会项** —— 否则「检查没跑」会与「检查通过」在日报上长得一模一样。
    """
    try:
        import ops_backup  # 同目录：直接运行本脚本时 sys.path[0] == scripts/
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"无法导入 ops_backup：{exc!r}",
                "checked": 0, "findings": [], "link_broken": [], "backup": None}
    try:
        return ops_backup.check_drift()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"check_drift 抛错：{exc!r}",
                "checked": 0, "findings": [], "link_broken": [], "backup": None}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="每日运维日报与告警登记器（不自动外发）")
    ap.add_argument("--date", default=None, help="目标日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--no-probe", action="store_true", help="跳过站点探活（离线/排障用）")
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

    trading = is_trading_day(day)
    site_probes = [] if args.no_probe else probe_site()
    probes = load_probes(day)
    disks = disk_stats()
    dirs = dir_stats()
    state = load_state(day)
    backup = load_backup(day)
    drift = check_backup_drift()
    alerts = build_alerts(day, site_probes, probes, disks, dirs, state, backup, trading, drift)

    OPS_OUT.mkdir(parents=True, exist_ok=True)
    report_path = OPS_OUT / f"日报-{day.replace('-', '')}.md"
    alerts_path = OPS_OUT / f"alerts-{day.replace('-', '')}.jsonl"
    report_path.write_text(
        render_report(day, alerts, site_probes, probes, disks, dirs, state, backup,
                      trading, drift),
        encoding="utf-8")
    with alerts_path.open("w", encoding="utf-8") as fh:
        for a in alerts:
            fh.write(json.dumps(a, ensure_ascii=False) + "\n")

    outbox = None
    if any(a["level"] == "P0" for a in alerts):
        ob_dir = OPS_OUT / "outbox"
        ob_dir.mkdir(parents=True, exist_ok=True)
        outbox = ob_dir / f"{day}-P0.txt"
        outbox.write_text(render_notice(day, alerts), encoding="utf-8")
        _log(f"存在 P0，通知正文已写入 {outbox}（未发送）", "WARN")

    verdict = verdict_of(alerts)
    summary = {
        "date": day,
        "trading_day": trading,
        "verdict": verdict,
        "alert_counts": {lv: sum(1 for a in alerts if a["level"] == lv) for lv in ("P0", "P1", "P2")},
        "drift": {"backup": drift.get("backup"), "checked": drift.get("checked"),
                  "unverified": drift.get("unverified", 0),
                  "p1": len([f for f in (drift.get("findings") or [])
                             if str(f.get("level")) == "P1"]),
                  "findings": len(drift.get("findings") or []),
                  "link_broken": len(drift.get("link_broken") or []),
                  "error": drift.get("error")},
        "probes": {name: (None if art is None else
                          {"level": art.get("level"), "findings": len(art.get("findings") or []),
                           "unverified": len(art.get("unverified") or []),
                           "ran": art.get("ok") is not False})
                   for name, art in probes.items()},
        "report": str(report_path),
        "alerts": str(alerts_path),
        "outbox": str(outbox) if outbox else None,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    _log(f"日报已生成：{report_path}（结论 {verdict}）")
    return 1 if verdict == "异常" else 0


if __name__ == "__main__":
    raise SystemExit(main())
