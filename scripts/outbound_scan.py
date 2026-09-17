#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [自研工具] outbound_scan.py
# 用途：出站前扫描的**可机器化部分** —— 检测"将外发的文件"里是否含本机绝对路径（含用户名的主目录路径、本机盘符路径）
# 适用场景：推送远端 / 发布上线 / 发送给他人 / 写入共享目录**之前**，扫描待外发的文件集合；工作区内的日常读写不需要它。
#           ⚠️ 它只覆盖 references/security-guide.md 出站清单的**第 3/4 项**（内部绝对路径 / 本机主目录路径），
#           **不覆盖**凭据特征、个人隐私、元数据、隐藏内容 —— 那些仍需人工逐项比对。
# 作者：ai-workflow 自研（技能增强-ai-workflow-v3.5.0-2026-09-17，2026-09-17）
# 仓库：https://github.com/Garvin666/ai-workflow-tools
"""outbound_scan.py - 出站前扫描：本机绝对路径检测（ai-workflow v3.5.0 / P0-2）。

为什么单独成文件、而不是把正则抄进每个推送脚本
------------------------------------------------
推送通道有两条（`push_ontology.py` / `publish_tools.py`），分流器 `push_router.py` 又在上游。
把同一个正则抄三份，就是本技能《结构实测》点名的「零抽象层 + 复制粘贴」病灶再加一处；
而"两处判据不一致"正是技能自己最怕的事。故**单一实现 + 被导入**，而不是逐份复制。

两级严重度（不对齐清单第 4 项与第 3 项会失真）
----------------------------------------------
    FAIL  含用户名的主目录路径（`C:\\Users\\<具体用户名>\\…`）—— 出站清单**第 4 项的字面要求**
          "换成相对路径或占位符"。占位符形态（`<用户名>` / `%USERNAME%` / `$HOME` / 省略号）不算命中。
    WARN  其它具名盘符路径（`E:\\<中文目录>\\…`）—— 属清单第 3 项「内部目录结构」。
          本版**只提示不阻断**：把它一并升级为 FAIL 会让存量档案大面积变红，而它的处置需要人拍板
          （改占位符 vs 接受现状）。**这是一条如实登记的边界，不是漏检。**

**已经掩码好的形态两侧都不算命中**（`C:\\Users\\<用户名>\\…`、`C:\\Users\\…\\x`、`~/.workbuddy/`）——
报它们等于惩罚"已按规则脱敏的文件"，而那种误判会让执行者放弃使用占位符（本轮实测确实先踩了这个坑）。

行内豁免
--------
命中的行里若出现 `outbound-scan:allow` 字样，则计入**豁免**并单独列出。
唯一合法用途是"本检测器自身的正则字面量"以及"文档中示范该格式的行"——
与 `push_router.py` 对 `.md` 的收窄同理：**收窄判据，而不是把文档改得躲开门禁**。

用法
----
    python outbound_scan.py <文件或目录> [...]        # 目录会递归（跳过 .git/__pycache__/二进制）
    python outbound_scan.py --list files.txt          # 从文件读待扫列表（每行一个路径）
    python outbound_scan.py <路径> --json             # 机器可读输出

退出码: 0 = 无 FAIL 级命中（WARN 不算失败）；1 = 有 FAIL 级命中；2 = 用法错误
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

# 前一个字符不能是字母数字 —— 否则 `https://api…` 里的 `s:/` 会被当成 `s:` 盘符（假阳性）。
# `{1,2}` 是为了同时覆盖 `C:\Users`（源码原样）与 `C:\\Users`（Python 字面量转义后）两种写法。
# 先定位「盘符出现处」，再**就地判定级别**，而不是用两条各自完整的正则去抢匹配。
# 原因（本轮实测）：`WARN_RE` 那种写法会把 `C:\Users\<用户名>\项目` 与 `C:\Users\…\x`
# 这类**已经掩码好的**形态也报成命中 —— 那等于惩罚"已经按规则脱敏的文件"，
# 属于典型的误判（门禁出问题绝大多数是误判而非漏判）。
#
# 前一个字符不能是字母数字 —— 否则 `https://api…` 里的 `s:/` 会被当成 `s:` 盘符（假阳性）。
# `[\\/]{1,2}` 同时覆盖 `C:\Users`（源码原样）与 `C:\\Users`（Python 字面量转义后）两种写法。
DRIVE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]):[\\/]{1,2}(?![\\/])")
USERS_RE = re.compile(r"^Users[\\/]{1,2}")
NAME_CH = re.compile(r"[A-Za-z0-9_\u4e00-\u9fff]")
# 掩码/占位形态的首字符：`<用户名>`、`…`（省略号）、`%USERNAME%`、`$HOME`、`{name}`、`***`、`...`
MASK_CH = "<>…%$*{}."


def _classify(line: str, at: int) -> str | None:
    """在 `at`（盘符起始处）判定级别：FAIL / WARN / None（掩码形态或非本机路径）。

    FAIL = 含用户名的主目录路径（出站清单**第 4 项**字面要求）；
    WARN = 其它具名盘符路径（第 3 项「内部目录结构」）；
    None = 后随占位符（已掩码）、或盘符后不是具名目录（如 `D:\\ 下所有` 这类泛指）。
    """
    m = DRIVE_RE.match(line, at)
    if not m:
        return None
    tail = line[m.end():]
    u = USERS_RE.match(tail)
    if u:
        rest = tail[u.end():]
        if rest and not rest[0] in MASK_CH and NAME_CH.match(rest):
            return "FAIL"
        return None
    if tail and NAME_CH.match(tail):
        return "WARN"
    return None


ALLOW_TOKEN = "outbound-scan:allow"

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules"}
SKIP_EXT = (".pyc", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".7z",
            ".xlsx", ".xls", ".docx", ".pptx", ".woff", ".woff2", ".ttf", ".mp4")
MAX_BYTES = 4 * 1024 * 1024


def scan_text(text: str) -> list[dict]:
    """扫一段文本，返回命中列表：{行号, 级别, 片段, 豁免}。**每行最多记一条**（FAIL 优先）。"""
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        level, at = None, -1
        for m in DRIVE_RE.finditer(line):
            got = _classify(line, m.start())
            if got == "FAIL":
                level, at = "FAIL", m.start()
                break
            if got == "WARN" and level is None:
                level, at = "WARN", m.start()
        if level:
            hits.append({"行号": i, "级别": level, "片段": _snippet(line, at),
                         "豁免": ALLOW_TOKEN in line})
    return hits


def _snippet(line: str, at: int) -> str:
    s = line.strip()
    if len(s) > 110:
        s = ("…" if at > 40 else "") + s[max(0, at - 20): max(0, at - 20) + 100] + "…"
    return s


def scan_file(path: Path) -> list[dict]:
    """扫单个文件；二进制/超限/非文本按不可扫处理（返回空，不报错）。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    if len(raw) > MAX_BYTES or b"\0" in raw[:4096]:
        return []
    return scan_text(raw.decode("utf-8", "replace"))


def iter_files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for c in sorted(p.rglob("*")):
                if not c.is_file() or any(part in SKIP_DIRS for part in c.parts):
                    continue
                if c.suffix.lower() in SKIP_EXT:
                    continue
                out.append(c)
        elif p.is_file():
            out.append(p)
    return out


def scan_paths(paths: list[str]) -> dict:
    """返回 {fail: [...], warn: [...], allowed: [...]}，每条含 文件/行号/级别/片段/豁免。"""
    res = {"fail": [], "warn": [], "allowed": []}
    for f in iter_files(paths):
        for h in scan_file(f):
            rec = dict(h)
            rec["文件"] = str(f)
            if h["豁免"]:
                res["allowed"].append(rec)
            elif h["级别"] == "FAIL":
                res["fail"].append(rec)
            else:
                res["warn"].append(rec)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(prog="outbound_scan.py",
                                 description="出站前扫描：本机绝对路径检测（出站清单第 3/4 项）")
    ap.add_argument("paths", nargs="*", help="待扫文件或目录")
    ap.add_argument("--list", dest="listfile", help="从文件读取待扫路径列表（每行一个，# 开头忽略）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("-q", "--quiet", action="store_true", help="只输出汇总")
    a = ap.parse_args()

    targets = list(a.paths)
    if a.listfile:
        targets += [ln.strip() for ln in Path(a.listfile).read_text(encoding="utf-8-sig").splitlines()
                    if ln.strip() and not ln.strip().startswith("#")]
    if not targets:
        ap.error("至少给出一个路径，或用 --list 指定列表文件")

    res = scan_paths(targets)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        for rec in res["fail"] + res["warn"]:
            print("[%s] %s:%d  %s" % (rec["级别"], rec["文件"], rec["行号"], rec["片段"]))
        if not a.quiet:
            for rec in res["allowed"]:
                print("[SKIP] %s:%d  行内含 %s（豁免）" % (rec["文件"], rec["行号"], ALLOW_TOKEN))
        print("=== 出站扫描：FAIL=%d，WARN=%d，豁免行=%d（扫描 %d 个文件）==="
              % (len(res["fail"]), len(res["warn"]), len(res["allowed"]), len(iter_files(targets))))
        if res["warn"]:
            print("    ⚠️ WARN 级属出站清单第 3 项（内部目录结构），本版只提示不阻断 —— 边界已如实登记。")
    return 1 if res["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
