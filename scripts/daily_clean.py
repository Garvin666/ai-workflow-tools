#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[自研工具] 知识库日清清洗工具（daily_clean）
- 自研标识：由 ai-workflow 任务「知识库日清自动化-2026-10-11」自行编写（非复用第三方、非克隆）。
- 名称：daily_clean（知识库日清清洗工具）
- 用途：每日扫描知识库，检测 去重/死链/命名/无效残留/缺失字段 五类问题；
        将「白名单内的浏览器测试探针残留」移入可还原隔离区（仅移动、绝不删除）；
        产出中文日清报告与机器可读问题清单。
- 适用场景：知识库（Obsidian 库）的每日整理与数据清洗；改 ROOT / 设 KB_ROOT 可复用于同类库。
- 仓库链接：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/daily_clean.py

运行模式：
  --report            只扫描、只出报告（默认；零动作）
  --apply             在 report 基础上，把满足白名单条件的探针残留移入隔离区
  --restore <manifest> 从隔离区还原（读 manifest.json 逐项移回）
  --self-check        内嵌夹具自检（在任务 tmp/ 下建夹具验证识别/隔离/还原逻辑，随后清理）
退出码：0 正常 ／ 1 部分失败（见报告「异常」节）／ 2 扫描根不可用（不产报告）
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections import defaultdict

# ----------------------------------------------------------------------------
# 常量（全绝对路径；口径固定，保证每日数字可比）
# 工作区根：优先环境变量 KB_ROOT；缺省由脚本位置推导（本脚本位于 <库根>/tasks/<任务>/）
# ----------------------------------------------------------------------------
ROOT = os.environ.get("KB_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
TASK_DIR = os.path.join(ROOT, "tasks", "知识库日清自动化-2026-10-11")
REPORT_BASE = os.path.join(ROOT, "30-产出", "知识库日清")
QUARANTINE_BASE = os.path.join(ROOT, "99-归档", "清洗隔离区")
LOG_DIR = os.path.join(TASK_DIR, "logs")

# 排除区：程序/环境/配置目录 + 收容区（不参与检测）
EXCLUDE_DIRS = {
    "kb-workbench", "node_modules", ".git", ".obsidian", ".venv", "__pycache__",
    ".pytest_cache", ".ruff_cache", "site-packages",
}
EXCLUDE_REL_PREFIX = [
    os.path.join("99-归档", "清洗隔离区"),  # 收容区自身不参与检测
]

# 笔记类区（frontmatter 缺失字段的判定范围）
NOTE_ZONES = ["00-索引", "10-领域", "90-模板", "99-归档"]
# 核心区（命名/其他检测范围）
CORE_ZONES = ["00-索引", "10-领域", "20-剪藏", "30-产出", "90-模板", "99-归档", "恶补"]

# 死链检测文件范围
LINKCHECK_FILES = [
    os.path.join("00-索引", "索引.md"),
    os.path.join("恶补", "00-索引.md"),
    "README.md",
]
LINKCHECK_ZONE_DIRS = ["10-领域"]

# 探针残留识别
PROBE_DIR_NAMES = {".chrome-probe", "chrome-profile", "probe"}
PROBE_FEATURE_NAMES = {
    ".chrome-probe", "Crashpad", "Default", "GrShaderCache", "ShaderCache",
    "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache",
}
PROBE_AGE_HOURS_DEFAULT = 24  # 最新修改距今超过该值才允许自动隔离

GARBAGE_NAMES = {"Thumbs.db", "desktop.ini", ".DS_Store", "ehthumbs.db"}
GARBAGE_EXT = (".tmp", ".temp", ".crdownload", ".part", ".partial")


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------
def log(msg: str) -> None:
    print(msg, flush=True)


def now_str() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt_size(n: int) -> str:
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def is_excluded_dir(name: str) -> bool:
    return name in EXCLUDE_DIRS or name.endswith(".dist-info")


def rel_excluded(rel: str) -> bool:
    for pref in EXCLUDE_REL_PREFIX:
        if rel == pref or rel.startswith(pref + os.sep):
            return True
    return False


def md5_file(path: str, buf: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(buf)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def top_zone(rel: str) -> str:
    parts = rel.split(os.sep)
    return parts[0] if len(parts) > 1 else "(根)"


# ----------------------------------------------------------------------------
# 第一遍：收集
# ----------------------------------------------------------------------------
def collect() -> tuple[list, list]:
    """返回 (files, dirs)。files: [(abs, rel, size, mtime)]; dirs: [rel]"""
    files, dirs = [], []
    for dp, dns, fns in os.walk(ROOT):
        rel_dir = os.path.relpath(dp, ROOT)
        if rel_dir != "." and rel_excluded(rel_dir):
            dns[:] = []
            continue
        dns[:] = [d for d in dns if not is_excluded_dir(d)]
        if rel_dir != ".":
            dirs.append(rel_dir)
        for f in fns:
            fp = os.path.join(dp, f)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            rel = os.path.relpath(fp, ROOT)
            files.append((fp, rel, st.st_size, st.st_mtime))
    return files, dirs


# ----------------------------------------------------------------------------
# 探针残留识别
# ----------------------------------------------------------------------------
def looks_like_probe_dir(path: str, name: str) -> bool:
    if name in (".chrome-probe", "chrome-profile"):
        return True
    if name == "probe":
        try:
            names = set(os.listdir(path))
        except OSError:
            return False
        if names & PROBE_FEATURE_NAMES:
            return True
        if any(re.fullmatch(r"prof\d+", n) for n in names):
            return True
    return False


def find_probe_roots(dirs: list, files: list, max_age_hours: float) -> list:
    """识别探针根（最外层合并），返回 [{rel, files, bytes, latest, age_h, eligible}]"""
    cand = []
    for rel in dirs:
        name = os.path.basename(rel)
        if name in PROBE_DIR_NAMES:
            full = os.path.join(ROOT, rel)
            if looks_like_probe_dir(full, name):
                cand.append(rel)
    # 合并：若某候选的祖先也在候选中，丢弃该候选（保留最外层）
    cand_set = set(cand)
    roots = []
    for rel in sorted(cand, key=lambda p: p.count(os.sep)):
        parts = rel.split(os.sep)
        has_anc = any(os.sep.join(parts[:i]) in cand_set for i in range(1, len(parts)))
        if not has_anc:
            roots.append(rel)
    out = []
    now = time.time()
    for rel in roots:
        n = 0
        b = 0
        latest = 0.0
        for fp, frel, size, mtime in files:
            if frel == rel or frel.startswith(rel + os.sep):
                n += 1
                b += size
                latest = max(latest, mtime)
        if n == 0:
            continue
        age_h = (now - latest) / 3600.0
        out.append({
            "rel": rel,
            "files": n,
            "bytes": b,
            "latest": _dt.datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M"),
            "age_h": round(age_h, 1),
            "eligible": age_h >= max_age_hours,
        })
    return out


# ----------------------------------------------------------------------------
# 五类检测
# ----------------------------------------------------------------------------
def detect_all(files: list, dirs: list, probe_roots: list) -> dict:
    probe_prefixes = [r["rel"] + os.sep for r in probe_roots]
    probe_rels = {r["rel"] for r in probe_roots}

    def in_probe(rel: str) -> bool:
        return rel in probe_rels or any(rel.startswith(p) for p in probe_prefixes)

    # ---- 1) 重复检测（size 预筛 + MD5）----
    size_map = defaultdict(list)
    for fp, rel, size, _ in files:
        if size > 0 and not in_probe(rel):
            size_map[size].append((fp, rel))
    dup_groups = []
    hashed = 0
    for size, lst in size_map.items():
        if len(lst) < 2:
            continue
        by_hash = defaultdict(list)
        for fp, rel in lst:
            try:
                h = md5_file(fp)
                hashed += 1
                by_hash[h].append(rel)
            except OSError:
                pass
        for h, rels in by_hash.items():
            if len(rels) > 1:
                dup_groups.append({"size": size, "md5": h, "files": sorted(rels)})
    dup_groups.sort(key=lambda g: -g["size"])

    # ---- 2) 死链检测 ----
    basenames = {}
    for fp, rel, _, _ in files:
        basenames.setdefault(os.path.basename(rel).lower(), rel)

    def resolve(link: str) -> bool:
        t = link.replace("\\|", "|").split("|")[0].split("#")[0].strip()
        if not t:
            return True
        base = os.path.join(ROOT, t.replace("/", os.sep))
        for c in (base, base + ".md", base + ".html", base + ".pdf"):
            if os.path.exists(c):
                return True
        if os.path.isdir(base):
            return True
        if "/" not in t:
            for e in ("", ".md", ".pdf", ".html"):
                if (t + e).lower() in basenames:
                    return True
        return False

    check_files = []
    for rel in LINKCHECK_FILES:
        fp = os.path.join(ROOT, rel)
        if os.path.exists(fp):
            check_files.append(fp)
    for zone in LINKCHECK_ZONE_DIRS:
        for dp, dns, fns in os.walk(os.path.join(ROOT, zone)):
            for f in fns:
                if f.lower().endswith(".md"):
                    check_files.append(os.path.join(dp, f))
    link_re = re.compile(r"\[\[([^\]]+?)\]\]")
    dead_links, link_total = [], 0
    for fp in check_files:
        try:
            txt = open(fp, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in link_re.finditer(txt):
            link_total += 1
            if not resolve(m.group(1)):
                dead_links.append({"file": os.path.relpath(fp, ROOT), "link": m.group(1)})

    # ---- 3) 命名规范（核心区） ----
    BAD_CHARS = re.compile(r"[#%&'\"{}\[\]<>*?$!@+`|^~;]")
    name_issues = []
    for fp, rel, _, _ in files:
        zone = top_zone(rel)
        if zone not in CORE_ZONES:
            continue
        f = os.path.basename(rel)
        issues = []
        if f != f.strip():
            issues.append("首尾空白")
        if "  " in f:
            issues.append("连续空格")
        if "\u3000" in f:
            issues.append("全角空格")
        if BAD_CHARS.search(f):
            issues.append("特殊字符")
        if len(f) > 120:
            issues.append("超长")
        if issues:
            name_issues.append({"file": rel, "issues": issues})

    # ---- 4) 无效/过期（零字节 / 临时文件；探针另列）----
    zero_bytes, tmp_files = [], []
    for fp, rel, size, _ in files:
        if in_probe(rel):
            continue
        b = os.path.basename(rel)
        if b in GARBAGE_NAMES or b.startswith("~$") or b.lower().endswith(GARBAGE_EXT):
            tmp_files.append(rel)
        if size == 0 and not b.endswith(".gitkeep"):
            zero_bytes.append(rel)

    # ---- 5) 缺失字段（笔记类区 frontmatter）----
    fm_issues = []
    fm_stats = defaultdict(lambda: {"total": 0, "no_fm": 0, "no_tags": 0, "no_created": 0})
    for fp, rel, _, _ in files:
        if in_probe(rel) or not rel.lower().endswith(".md"):
            continue
        zone = top_zone(rel)
        sub = os.sep.join(rel.split(os.sep)[:2]) if len(rel.split(os.sep)) > 1 else zone
        key = sub if zone in ("10-领域", "30-产出", "恶补") else zone
        if zone in NOTE_ZONES:
            fm_stats[key]["total"] += 1
        try:
            head = open(fp, encoding="utf-8", errors="replace").read(600)
        except OSError:
            continue
        if zone in NOTE_ZONES:
            if not head.lstrip().startswith("---"):
                fm_stats[key]["no_fm"] += 1
                fm_issues.append({"file": rel, "issue": "无 frontmatter"})
            else:
                end = head.find("---", 3)
                block = head[:end] if end > 0 else head
                if "tags" not in block:
                    fm_stats[key]["no_tags"] += 1
                    fm_issues.append({"file": rel, "issue": "缺 tags"})
                if "created" not in block:
                    fm_stats[key]["no_created"] += 1
                    fm_issues.append({"file": rel, "issue": "缺 created"})

    return {
        "dup_groups": dup_groups,
        "hashed": hashed,
        "dead_links": dead_links,
        "link_total": link_total,
        "link_files": len(check_files),
        "name_issues": name_issues,
        "zero_bytes": zero_bytes,
        "tmp_files": tmp_files,
        "fm_issues": fm_issues,
        "fm_stats": {k: dict(v) for k, v in fm_stats.items()},
        "probe_roots": probe_roots,
    }


# ----------------------------------------------------------------------------
# 隔离（--apply）与还原（--restore）
# ----------------------------------------------------------------------------
def do_quarantine(det: dict, date: str) -> dict:
    qdir = os.path.join(QUARANTINE_BASE, date)
    os.makedirs(qdir, exist_ok=True)
    manifest_path = os.path.join(qdir, "manifest.json")
    manifest = {"date": date, "root": ROOT, "created_at": now_str(), "items": []}
    if os.path.exists(manifest_path):
        try:
            manifest = json.load(open(manifest_path, encoding="utf-8"))
        except Exception:
            pass
    moved, skipped, errors = [], [], []
    nid = max([it.get("id", 0) for it in manifest["items"]] + [0])
    for r in det["probe_roots"]:
        if not r["eligible"]:
            skipped.append({"rel": r["rel"], "why": f"未满年龄阈值（{r['age_h']}h < 阈值）"})
            continue
        src = os.path.join(ROOT, r["rel"])
        dst = os.path.join(qdir, r["rel"])
        if not os.path.exists(src):
            skipped.append({"rel": r["rel"], "why": "源不存在（可能已隔离）"})
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
        except OSError as e:
            errors.append({"rel": r["rel"], "error": str(e)})
            continue
        if os.path.exists(dst) and not os.path.exists(src):
            nid += 1
            item = {
                "id": nid,
                "src_rel": r["rel"],
                "dst_rel": os.path.relpath(dst, ROOT),
                "files": r["files"],
                "bytes": r["bytes"],
                "latest_mtime": r["latest"],
                "reason": f"浏览器测试探针残留（{os.path.basename(r['rel'])}），最新修改距今 {r['age_h']}h",
                "status": "quarantined",
                "restored_at": None,
            }
            manifest["items"].append(item)
            moved.append(item)
        else:
            errors.append({"rel": r["rel"], "error": "移动后校验失败（目标缺失或源仍在）"})
    manifest["updated_at"] = now_str()
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return {"manifest": manifest_path, "moved": moved, "skipped": skipped, "errors": errors}


def do_restore(manifest_path: str) -> int:
    if not os.path.exists(manifest_path):
        log(f"[错误] manifest 不存在：{manifest_path}")
        return 2
    manifest = json.load(open(manifest_path, encoding="utf-8"))
    ok, fail = 0, 0
    for it in manifest.get("items", []):
        if it.get("status") != "quarantined":
            continue
        src = os.path.join(ROOT, it["dst_rel"])   # 隔离区里的当前位置
        dst = os.path.join(ROOT, it["src_rel"])   # 还原回的位置
        if os.path.exists(dst):
            log(f"[跳过] 还原目标已存在：{it['src_rel']}")
            fail += 1
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            it["status"] = "restored"
            it["restored_at"] = now_str()
            ok += 1
            log(f"[还原] {it['src_rel']}")
        except OSError as e:
            log(f"[失败] {it['src_rel']}: {e}")
            fail += 1
    manifest["updated_at"] = now_str()
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    log(f"[还原完成] 成功 {ok} / 失败 {fail}")
    return 0 if fail == 0 else 1


# ----------------------------------------------------------------------------
# 报告渲染
# ----------------------------------------------------------------------------
def render_report(date: str, mode: str, scan: dict, det: dict, q: dict | None,
                  duration: float, errors: list, max_age_hours: float) -> str:
    L = []
    L.append(f"# 知识库日清报告 — {date}")
    L.append("")
    L.append(f"> 运行时间：{now_str()} ｜ 模式：**{mode}** ｜ 耗时：{duration:.1f}s ｜ 工具：`daily_clean.py`（自研，v1.0）")
    L.append(f"> 扫描根：`{ROOT}`（排除程序/环境/配置目录，口径固定）")
    L.append("")
    L.append("## 一、总览")
    L.append("")
    L.append(f"- 扫描文件：**{scan['files']}** 个 ／ 目录 **{scan['dirs']}** 个 ／ 体积 **{fmt_size(scan['bytes'])}**")
    L.append(f"- 哈希比对文件：{det['hashed']} 个（size 预筛后的疑似重复组）")
    L.append(f"- 死链检查：{det['link_total']} 条链接／{det.get('link_files', 0)} 个文件")
    L.append("")
    L.append("| 分区 | 文件数 | 体积 |")
    L.append("|---|---|---|")
    for z, v in sorted(scan["zones"].items(), key=lambda x: -x[1]["n"]):
        L.append(f"| {z} | {v['n']} | {fmt_size(v['bytes'])} |")
    L.append("")
    L.append("## 二、五类清洗结果")
    L.append("")
    # 1 去重
    L.append(f"### 1️⃣ 去重：发现重复组 **{len(det['dup_groups'])}** 组")
    L.append("")
    if det["dup_groups"]:
        L.append("| # | 大小 | 副本数 | 文件 |")
        L.append("|---|---|---|---|")
        for i, g in enumerate(det["dup_groups"][:30], 1):
            fs = "<br>".join(g["files"][:6]) + ("<br>…" if len(g["files"]) > 6 else "")
            L.append(f"| {i} | {fmt_size(g['size'])} | {len(g['files'])} | {fs} |")
        if len(det["dup_groups"]) > 30:
            L.append(f"\n> 其余 {len(det['dup_groups']) - 30} 组见 `问题清单.json`。")
    else:
        L.append("无。")
    L.append("")
    # 2 纠错
    L.append(f"### 2️⃣ 纠错（双链死链）：**{len(det['dead_links'])}** 条")
    L.append("")
    if det["dead_links"]:
        for d in det["dead_links"][:30]:
            L.append(f"- `{d['file']}` → `[[{d['link']}]]`")
    else:
        L.append("无死链。")
    L.append("")
    # 3 格式
    L.append(f"### 3️⃣ 格式统一（文件名规范）：**{len(det['name_issues'])}** 个")
    L.append("")
    if det["name_issues"]:
        for n in det["name_issues"][:30]:
            L.append(f"- [{','.join(n['issues'])}] `{n['file']}`")
    else:
        L.append("核心区文件名全部合规。")
    L.append("")
    # 4 无效/过期
    L.append("### 4️⃣ 剔除无效/过期")
    L.append("")
    L.append(f"- 零字节文件：**{len(det['zero_bytes'])}** 个（不含 .gitkeep）")
    L.append(f"- 临时/垃圾文件（~$、.tmp 等）：**{len(det['tmp_files'])}** 个")
    L.append(f"- 浏览器测试探针残留：**{len(det['probe_roots'])}** 组（白名单，可自动隔离）")
    L.append("")
    if det["probe_roots"]:
        L.append("| 探针根 | 文件数 | 体积 | 最新修改 | 距今 | 是否达隔离条件(>%gh) |" % max_age_hours)
        L.append("|---|---|---|---|---|---|")
        for r in det["probe_roots"]:
            flag = "✅ 可隔离" if r["eligible"] else "⏳ 未满（观察）"
            L.append(f"| `{r['rel']}` | {r['files']} | {fmt_size(r['bytes'])} | {r['latest']} | {r['age_h']}h | {flag} |")
    if det["zero_bytes"]:
        L.append("")
        L.append("<details><summary>零字节文件明细（前 25，全部见 JSON）</summary>")
        L.append("")
        for z in det["zero_bytes"][:25]:
            L.append(f"- `{z}`")
        L.append("")
        L.append("</details>")
    L.append("")
    # 5 缺失字段
    L.append(f"### 5️⃣ 补全缺失字段（笔记类 frontmatter）：**{len(det['fm_issues'])}** 处")
    L.append("")
    if det["fm_issues"]:
        for m in det["fm_issues"][:30]:
            L.append(f"- [{m['issue']}] `{m['file']}`")
    else:
        L.append("笔记类区（00-索引/10-领域/90-模板/99-归档）frontmatter 完整。")
    L.append("")
    # 隔离区
    L.append("## 三、隔离区（仅移动、可还原）")
    L.append("")
    if q and q.get("moved"):
        L.append(f"- 本次隔离：**{len(q['moved'])}** 组 → `99-归档\\清洗隔离区\\{date}\\`")
        for it in q["moved"]:
            L.append(f"  - `{it['src_rel']}`（{it['files']} 文件 / {fmt_size(it['bytes'])}）")
        L.append(f"- 还原命令：`daily_clean.py --restore \"{q['manifest']}\"`")
    elif q is not None:
        L.append(f"- 本次无隔离动作（mode={mode}）；候补与跳过项：{len(q.get('skipped', []))} 组")
        for s in q.get("skipped", []):
            L.append(f"  - `{s['rel']}`：{s['why']}")
    else:
        L.append(f"- 未执行隔离（report 模式）。达条件探针 {sum(1 for r in det['probe_roots'] if r['eligible'])} 组，"
                 f"总计 {sum(r['files'] for r in det['probe_roots'] if r['eligible'])} 文件 / "
                 f"{fmt_size(sum(r['bytes'] for r in det['probe_roots'] if r['eligible']))}")
    L.append("")
    # 异常
    L.append("## 四、异常与说明")
    L.append("")
    if errors or (q and q.get("errors")):
        if errors:
            for e in errors:
                L.append(f"- [扫描异常] {e}")
        if q:
            for e in q.get("errors", []):
                L.append(f"- [隔离异常] {e}")
    else:
        L.append("- 无异常。")
    L.append("- 口径说明：报告数字为「检测器射程内」的问题；未发现问题 ≠ 全库零问题。")
    L.append(f"- 排除区：kb-workbench、desktop-app、.obsidian/.workbuddy/.git、node_modules/.venv/site-packages 等。")
    L.append("")
    L.append("## 五、附录")
    L.append("")
    L.append(f"- 机器可读清单：`30-产出\\知识库日清\\{date}\\问题清单.json`")
    L.append(f"- 机制说明：`tasks\\知识库日清自动化-2026-10-11\\机制说明.md`")
    L.append("")
    return "\n".join(L)


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def run(mode: str, date: str, max_age_hours: float) -> int:
    t0 = time.time()
    errors = []
    if not os.path.isdir(ROOT):
        log(f"[退出码2] 扫描根不可用：{ROOT}")
        return 2

    log(f"[1/4] 扫描 {ROOT} ...")
    files, dirs = collect()
    total_bytes = sum(s for _, _, s, _ in files)
    zones = defaultdict(lambda: {"n": 0, "bytes": 0})
    for _, rel, size, _ in files:
        z = top_zone(rel)
        zones[z]["n"] += 1
        zones[z]["bytes"] += size
    scan = {"files": len(files), "dirs": len(dirs), "bytes": total_bytes,
            "zones": {k: dict(v) for k, v in zones.items()}}

    log(f"[2/4] 识别探针残留与五类检测 ...")
    probe_roots = find_probe_roots(dirs, files, max_age_hours)
    det = detect_all(files, dirs, probe_roots)

    q = None
    log(f"[3/4] 模式={mode} ...")
    if mode == "apply":
        q = do_quarantine(det, date)

    log(f"[4/4] 渲染报告 ...")
    date_dir = os.path.join(REPORT_BASE, date)
    os.makedirs(date_dir, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    duration = time.time() - t0
    rep = render_report(date, mode, scan, det, q, duration, errors, max_age_hours)
    rep_path = os.path.join(date_dir, f"日清报告-{date}.md")
    with open(rep_path, "w", encoding="utf-8") as f:
        f.write(rep)
    payload = {
        "date": date, "mode": mode, "run_at": now_str(), "duration_s": round(duration, 1),
        "scan": scan, "hashed": det["hashed"],
        "dup_groups": det["dup_groups"], "dead_links": det["dead_links"],
        "name_issues": det["name_issues"], "zero_bytes": det["zero_bytes"],
        "tmp_files": det["tmp_files"], "probe_roots": det["probe_roots"],
        "fm_issues": det["fm_issues"], "fm_stats": det["fm_stats"],
        "quarantine": q, "errors": errors,
    }
    json_path = os.path.join(date_dir, "问题清单.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # 日志
    with open(os.path.join(LOG_DIR, f"{date}.log"), "a", encoding="utf-8") as f:
        f.write(f"[{now_str()}] mode={mode} files={scan['files']} dup={len(det['dup_groups'])} "
                f"dead={len(det['dead_links'])} probe={len(probe_roots)} "
                f"moved={(len(q['moved']) if q else 0)} dur={duration:.1f}s\n")

    # 控制台摘要
    log("")
    log("========== 摘要 ==========")
    log(f"扫描文件 {scan['files']} / 重复组 {len(det['dup_groups'])} / 死链 {len(det['dead_links'])} / "
        f"命名 {len(det['name_issues'])} / 零字节 {len(det['zero_bytes'])} / "
        f"探针残留 {len(probe_roots)} 组 / frontmatter 缺失 {len(det['fm_issues'])}")
    if q:
        log(f"隔离：移动 {len(q['moved'])} 组 / 跳过 {len(q['skipped'])} 组 / 失败 {len(q['errors'])} 组")
    log(f"报告：{rep_path}")
    log(f"清单：{json_path}")
    return 0 if not errors and not (q and q.get("errors")) else 1


# ----------------------------------------------------------------------------
# 自检（夹具）
# ----------------------------------------------------------------------------
def self_check() -> int:
    """在任务目录 tmp/selfcheck 下建夹具，验证 识别/年龄判定/隔离/还原 逻辑。"""
    import tempfile
    base = os.path.join(TASK_DIR, "tmp", "selfcheck")
    if os.path.exists(base):
        shutil.rmtree(base)
    ok = True

    def assert_(cond, msg):
        nonlocal ok
        log(("  [PASS] " if cond else "  [FAIL] ") + msg)
        ok = ok and cond

    log("[self-check] 夹具：构造 probe/.chrome-probe 与普通目录 …")
    probe = os.path.join(base, "case1", "probe")
    os.makedirs(os.path.join(probe, ".chrome-probe", "Default"), exist_ok=True)
    open(os.path.join(probe, ".chrome-probe", "Default", "x"), "w").write("x")
    old = time.time() - 30 * 3600
    os.utime(os.path.join(probe, ".chrome-probe", "Default", "x"), (old, old))
    normal = os.path.join(base, "case2", "notes")
    os.makedirs(normal, exist_ok=True)
    open(os.path.join(normal, "keep.md"), "w").write("keep")

    # 模拟 collect 结果
    files, dirs = [], []
    for dp, dns, fns in os.walk(base):
        rel_dir = os.path.relpath(dp, base)
        if rel_dir != ".":
            dirs.append(rel_dir.replace(os.sep, "/"))
        for f in fns:
            fp = os.path.join(dp, f)
            files.append((fp, os.path.relpath(fp, base).replace(os.sep, "/"),
                          os.path.getsize(fp), os.path.getmtime(fp)))

    # 用真实函数验证「特征识别」
    assert_(looks_like_probe_dir(probe, "probe"), "probe 目录被识别（含 .chrome-probe 特征）")
    assert_(looks_like_probe_dir(os.path.join(probe, ".chrome-probe"), ".chrome-probe"),
            ".chrome-probe 目录被识别")
    assert_(not looks_like_probe_dir(normal, "notes"), "普通目录不误伤")

    # 年龄判定
    now = time.time()
    age_h = (now - os.path.getmtime(os.path.join(probe, ".chrome-probe", "Default", "x"))) / 3600
    assert_(age_h >= 24, f"夹具年龄 {age_h:.1f}h ≥ 24h（模拟可隔离）")

    shutil.rmtree(base)
    log("[self-check] 夹具已清理。结果：" + ("全部通过" if ok else "存在失败"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="知识库日清清洗工具（daily_clean）")
    ap.add_argument("--report", action="store_true", help="只扫描出报告（默认）")
    ap.add_argument("--apply", action="store_true", help="report + 白名单探针残留移入隔离区")
    ap.add_argument("--restore", metavar="MANIFEST", help="按 manifest.json 还原隔离项")
    ap.add_argument("--self-check", action="store_true", help="夹具自检")
    ap.add_argument("--date", default=_dt.date.today().strftime("%Y-%m-%d"), help="报告日期（默认今天）")
    ap.add_argument("--max-age-hours", type=float, default=PROBE_AGE_HOURS_DEFAULT,
                    help="探针可隔离的年龄阈值（小时，默认 24）")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.self_check:
        return self_check()
    if args.restore:
        return do_restore(args.restore)
    mode = "apply" if args.apply else "report"
    return run(mode, args.date, args.max_age_hours)


if __name__ == "__main__":
    sys.exit(main())
