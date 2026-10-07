#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# [自研工具] outbound-publish-guard（出站内容守卫）
# 用途：推 public 仓 / 发布站点 / 导出交付包之前，分级扫描本机绝对路径与本机用户名路径与凭据字面量
# 适用场景：任何把内容发到本机之外的动作之前；尤其内容里可能残留本机路径或凭据时
# 作者：ai-workflow 自研（沉淀-迁移与出站与判据契约技能-2026-10-07）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/skills/outbound-publish-guard/scripts/scan_outbound_paths.py
"""出站内容守卫：分级扫描文件/目录里的**本机绝对路径**与**凭据字面量**。

为什么需要它
------------
把内容推到公开仓 / 发布站点 / 导出交付包之前，最容易漏的不是逻辑错误，而是**机器痕迹**：
本机主目录路径（连用户名一起暴露）、盘符路径、以及不小心贴进去的 Key。
这些一旦出站，就**收不回来**（公开仓的历史抹不干净）。

分级（口径写死，否则 WARN 会淹没 FAIL）
--------------------------------------
  FAIL（阻断，rc=2）  本机主目录路径 / POSIX 家目录路径 / 凭据字面量
  WARN（默认放行）    本机盘符路径（不含用户名）
在判定「本机主目录路径」时，**已掩码形态不算命中** —— 用户名段里含 `< > … % *`
（如 `C:\\Users\\<用户名>\\…`）或就是 username/user/name 这类占位词，一律判「通过」。
这条很重要：否则**自己的脱敏结果会被自己再报一次**，人就会开始忽略告警。

用法
----
  python scan_outbound_paths.py FILE_OR_DIR [...]        # 扫描（只读）
  python scan_outbound_paths.py … --strict               # WARN 也算失败
  python scan_outbound_paths.py … --json out.json        # 另存机器可读报告
  python scan_outbound_paths.py selftest                 # 自测（四类夹具，判据不漏不误）

⚠️ 扫描器自身不含任何真实凭据；自测夹具用的是**假用户名/假 Key**（someuser / sk-TESTONLY…）。
"""
import argparse
import io
import json
import os
import re
import sys
import tempfile

__version__ = "1.0.0"

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".idea"}
MAX_BYTES = 5 * 1024 * 1024  # 超过就跳过（出站内容通常是文本/源码）

# ---- FAIL 级 -------------------------------------------------------------
RE_WIN_HOME = re.compile(r"[A-Za-z]:[\\/]Users[\\/]([^\\/\s\"']+)[\\/]", re.I)
RE_POSIX_HOME = re.compile(r"(?<![:A-Za-z0-9])/(?:home|Users)/([^/\s\"']+)/")
RE_KEYS = [
    ("OpenAI 风格密钥", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("GitHub Token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack Token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]
# ---- WARN 级 -------------------------------------------------------------
RE_DRIVE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z]):[\\/](?![\\/])")

MASK_CHARS = "<>\u2026%*"
MASK_WORDS = {"username", "user", "name", "<name>", "<user>", "<用户名>"}


def _looks_masked(username):
    """用户名段是不是占位符（已掩码）—— 是则不算命中。"""
    if not username:
        return True
    if any(c in username for c in MASK_CHARS):
        return True
    return username.strip().lower() in MASK_WORDS


def _mask_home(text):
    """把命中片段里的用户名替换成 <用户名>，避免报告本身又漏一次。"""
    m = RE_WIN_HOME.search(text)
    if m:
        return text.replace(m.group(1), "<用户名>")
    m = RE_POSIX_HOME.search(text)
    if m:
        return text.replace(m.group(1), "<用户名>")
    return text


def _mask_secret(text):
    if len(text) <= 10:
        return "…"
    return "%s…(len=%d)" % (text[:6], len(text))


def _spans(regex, line):
    return [m.span() for m in regex.finditer(line)]


def _inside(pos, spans):
    return any(a <= pos < b for a, b in spans)


def scan_text(text, path="<text>"):
    """扫描一段文本 -> [{'file','line','level','kind','masked'}]"""
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        fail_spans = []
        # FAIL：Windows 家目录（先取 span，供 WARN 去重用）
        for m in RE_WIN_HOME.finditer(line):
            fail_spans.append(m.span())
            if _looks_masked(m.group(1)):
                continue  # 已掩码 —— 判「通过」，不误报
            hits.append({"file": path, "line": i, "level": "FAIL",
                         "kind": "本机主目录路径", "masked": _mask_home(m.group(0))})
        for m in RE_POSIX_HOME.finditer(line):
            fail_spans.append(m.span())
            if _looks_masked(m.group(1)):
                continue
            hits.append({"file": path, "line": i, "level": "FAIL",
                         "kind": "POSIX 家目录路径", "masked": _mask_home(m.group(0))})
        # FAIL：凭据
        for kind, rx in RE_KEYS:
            for m in rx.finditer(line):
                fail_spans.append(m.span())
                hits.append({"file": path, "line": i, "level": "FAIL",
                             "kind": kind, "masked": _mask_secret(m.group(0))})
        # WARN：盘符路径（跳过已被 FAIL 覆盖的位置，避免同一处报两次）
        for m in RE_DRIVE.finditer(line):
            if _inside(m.start(), fail_spans):
                continue
            hits.append({"file": path, "line": i, "level": "WARN",
                         "kind": "本机盘符路径", "masked": m.group(0)[:12]})
    return hits


def _read_text(p):
    try:
        if os.path.getsize(p) > MAX_BYTES:
            return None
        raw = open(p, "rb").read()
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None  # 二进制
    return raw.decode("utf-8", "replace")


def iter_files(targets):
    for t in targets:
        if os.path.isfile(t):
            yield t
        elif os.path.isdir(t):
            for dp, dns, fns in os.walk(t):
                dns[:] = [d for d in dns if d not in SKIP_DIRS]
                for fn in sorted(fns):
                    yield os.path.join(dp, fn)
        else:
            print("[skip] 不存在：%s" % t, file=sys.stderr)


def run_scan(targets):
    all_hits, scanned = [], 0
    for p in iter_files(targets):
        text = _read_text(p)
        if text is None:
            continue
        scanned += 1
        all_hits.extend(scan_text(text, p))
    return all_hits, scanned


# --------------------------------------------------------------------------
# 自测：四类夹具，判据必须**不漏也不误**
# --------------------------------------------------------------------------
def _check(name, cond):
    print("   [%s] %s" % (" OK " if cond else "FAIL", name))
    return bool(cond)


def cmd_selftest(a):
    allok = True
    with tempfile.TemporaryDirectory() as t:
        def w(fn, body):
            p = os.path.join(t, fn)
            with io.open(p, "w", encoding="utf-8") as f:
                f.write(body)
            return p

        # ⚠️ 夹具字面量**拆开拼接**：否则本文件会被自己的规则命中（扫描器自摆乌龙）。
        HOME_PREFIX = "C" + ":" + "\\" + "Users"      # 源码里不存在连续的 C:\Users
        FAKE_KEY = "s" + "k-" + "TESTONLYNOTAREALKEY" + "0123456789"
        DRIVE_E = "E" + ":" + "\\"
        U = "<" + "用户" + "名>"

        p_fail = w("fail.txt", "请看 " + HOME_PREFIX + "\\someuser\\Desktop\\thing\\x.md\n"
                               "key=" + FAKE_KEY + "\n")
        p_warn = w("warn.txt", "归档在 " + DRIVE_E + "ChatGPT\\somewhere\\data 下\n")
        p_mask = w("masked.txt", "归档在 " + HOME_PREFIX + "\\" + U + "\\Desktop\\thing 下\n"
                                 "也写过 " + HOME_PREFIX + "\\username\\x 与 " + HOME_PREFIX + "\\%USERNAME%\\y\n")
        p_rel = w("rel.txt", "路径写成 恶补/工作流/2026-10/x.pdf 与 ./tmp/out.txt 与 /usr/local/bin\n"
                             "还有网址 https://example.com/Users/foo/bar 与 s3://bucket/key\n")
        p_clean = w("clean.txt", "这里什么都没有，只有普通中文与 code 片段。\n")

        h_fail = scan_text(_read_text(p_fail), "fail.txt")
        h_warn = scan_text(_read_text(p_warn), "warn.txt")
        h_mask = scan_text(_read_text(p_mask), "masked.txt")
        h_rel = scan_text(_read_text(p_rel), "rel.txt")
        h_clean = scan_text(_read_text(p_clean), "clean.txt")

        print("[selftest] ① 阳性：真 FAIL 样本")
        allok &= _check("本机主目录路径被判 FAIL",
                        any(h["level"] == "FAIL" and h["kind"] == "本机主目录路径" for h in h_fail))
        allok &= _check("凭据字面量被判 FAIL",
                        any(h["level"] == "FAIL" and h["kind"].endswith("密钥") for h in h_fail))
        allok &= _check("报告里的片段已掩码（不含完整用户名）",
                        all("someuser" not in h["masked"] for h in h_fail))
        allok &= _check("报告里的凭据已截断（不回显完整值）",
                        all(h["masked"] != FAKE_KEY for h in h_fail))

        print("[selftest] ② 分级：盘符路径只算 WARN")
        allok &= _check("盘符路径出 WARN", any(h["level"] == "WARN" for h in h_warn))
        allok &= _check("盘符路径**不**出 FAIL", not any(h["level"] == "FAIL" for h in h_warn))

        print("[selftest] ③ 阴性：掩码形态与相对路径/URL 不得误报")
        allok &= _check("已掩码形态（<用户名> / username / %USERNAME%）零 FAIL",
                        not any(h["level"] == "FAIL" for h in h_mask))
        allok &= _check("相对路径与 URL 零命中", not h_rel)
        allok &= _check("干净文本零命中", not h_clean)

        print("[selftest] ④ 端到端：跑目录扫描")
        hits, scanned = run_scan([t])
        allok &= _check("目录扫描确实读了文件（非空跑）", scanned >= 5)
        allok &= _check("目录扫描能同时报出 FAIL 与 WARN",
                        any(h["level"] == "FAIL" for h in hits) and any(h["level"] == "WARN" for h in hits))
    print("[selftest] %s" % ("全部通过" if allok else "存在 FAIL"))
    return 0 if allok else 1


def main(argv=None):
    p = argparse.ArgumentParser(description="出站内容守卫：分级扫描本机绝对路径与凭据字面量")
    p.add_argument("targets", nargs="*", help="要扫描的文件或目录")
    p.add_argument("--strict", action="store_true", help="WARN 也算失败（rc=2）")
    p.add_argument("--selftest", action="store_true",
                   help="自测（四类夹具；等价于位置参数 selftest）")
    p.add_argument("--json", dest="json_out", help="把机器可读报告写到该路径")
    p.add_argument("--version", action="version", version=__version__)
    a = p.parse_args(argv)

    if a.selftest or (a.targets and a.targets[0] == "selftest"):
        return cmd_selftest(a)
    if not a.targets:
        p.error("需要至少一个目标，或用 selftest")

    hits, scanned = run_scan(a.targets)
    fails = [h for h in hits if h["level"] == "FAIL"]
    warns = [h for h in hits if h["level"] == "WARN"]
    for h in hits:
        print("%s:%d: [%s] %s  ->  %s" % (h["file"], h["line"], h["level"], h["kind"], h["masked"]))
    print("— 扫描 %d 文件 ｜ FAIL=%d  WARN=%d ｜ 命中文件 %d"
          % (scanned, len(fails), len(warns), len({h["file"] for h in hits})))
    if a.json_out:
        with io.open(a.json_out, "w", encoding="utf-8") as f:
            json.dump({"scanned": scanned, "fail": len(fails), "warn": len(warns), "hits": hits},
                      f, ensure_ascii=False, indent=2)
    if fails:
        print("⇒ 有 FAIL 级命中：**不得出站**，先脱敏（已提交见 outbound-publish-guard 的 amend 流程）")
        return 2
    if warns and a.strict:
        print("⇒ --strict：WARN 视为失败")
        return 2
    if warns:
        print("⇒ 仅有 WARN 级命中：若已登记可放行；建议改相对路径")
    else:
        print("⇒ 干净")
    return 0


if __name__ == "__main__":
    sys.exit(main())
