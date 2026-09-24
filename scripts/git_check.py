#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [自研工具] git_check.py
# 用途：把「项目 Git 开发规范」（分支模型 / commit 注释规范 / 禁止提交清单）里**可机器判定的部分**
#       做成只读校验器 —— 提交前查注释格式、配完 .gitignore 查五类覆盖率、写代码前查分支状态。
# 适用场景：① 生成 commit 信息之前（msg）② 新项目首次配置或改动 .gitignore 之后（ignore）
#           ③ 开工写代码前的预检（preflight）。日常只读查看 git 状态不需要它。
#           ⚠️ 它是**只读**校验器：不切分支、不提交、不推送 —— 所有 git 写动作仍由执行者按红线① 发起。
# 作者：ai-workflow 自研（技能增强-ai-workflow-v4.6.0-2026-09-22，2026-09-22）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/git_check.py
"""git_check.py - 项目 Git 开发规范校验（ai-workflow v4.6.0）。

为什么单独成文件、而不是把规则抄进 SKILL.md
--------------------------------------------
规范写在 `references/git-conventions.md` 里只是**描述**；"文档说一套、commit 又违反一套"正是本工作区
最怕的「描述与实现不符」。故把其中**确定性那部分**（类型枚举、格式、五类覆盖率、当前分支）
落成可执行判据 —— **判据能跑，才有资格说"严格遵守"**。

依赖边界（刻意不 import 技能本体）
----------------------------------
本脚本要能被推送到 `ai-workflow-tools` 独立分发，故**不 import** `checks.py` 家族 ——
输出格式（[ OK ]/[FAIL]/[SKIP]/[WARN]）刻意与其一致，但实现自包含。这是有意的取舍：
**独立可分发 > 少写 30 行**。代价是 `COMMIT_TYPES` 与 `checks_core.VALID_COMMIT_TYPES`
成为两处代码常量 —— 该漂移风险由 `checks_parity.py` 的「提交类型」守卫项兜住
（它把本文件、`references/git-conventions.md`、`references/data-model.md` 三处并集比对）。

绿有三种，分开报
----------------
本脚本沿用技能口径：**通过 / 未检测 / 无法判定** 必须分开报。故：
  · 非 git 仓库、.gitignore 不存在 → **[SKIP]**（未检测），不是通过；
  · `main` 上有未提交改动 → **[WARN]**（无法判定那是新功能还是文档修正），不是 FAIL。
把后两种报成第一种，比漏判更危险。
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

# ── 提交类型：唯一物理量，三处同源（本文件 / git-conventions.md §3.2 / data-model.md §2.2）──
COMMIT_TYPES = ("feat", "fix", "refactor", "docs")

# 提交信息格式：`<类型>[(<范围>)][!]: <描述>`
#   · 类型须为小写字母数字开头（是否合法由 COMMIT_TYPES 裁定）
#   · scope 与 `!` 为可选扩展（见规范 §3.1）
#   · 冒号须为 ASCII `: ` 且后接非空描述
_MSG_RE = re.compile(
    r"^(?P<type>[a-z][a-z0-9]*)(?:\((?P<scope>[^()\n]{1,40})\))?(?P<breaking>!)?: (?P<desc>\S.*)$"
)

# 无信息描述（只 WARN：格式合规但说了等于没说，见规范 §3.3）
_VAGUE_DESCS = {
    "update", "updates", "updated", "fix", "fixbug", "fix bug", "bugfix", "wip", "tmp",
    "temp", "test", "change", "changes", "修改", "更新", "改了", "改了下", "改一下",
    "提交", "保存", "临时", "一下", ".",
}
_MSG_TITLE_MAX = 72

# ── 五类禁止提交（规范 §4.1）：模式为**规范化后**形态（去前导 `/`、去 `**/`、去尾随 `/`）──
IGNORE_RULES = (
    ("模型权重", ("*.pt", "*.pth", "*.ckpt", "*.safetensors", "*.onnx", "*.h5", "*.bin",
                  "*.gguf", "models", "weights", "checkpoints")),
    ("数据集", ("data", "datasets", "raw", "*.parquet", "*.tfrecord", "*.arrow", "*.feather")),
    ("虚拟环境目录", (".venv", "venv", "env", "__pycache__", "node_modules", ".next", "dist", "build")),
    ("日志", ("*.log", "logs", "log", "*.out", "*.err")),
    ("密钥", (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "secrets.*", "credentials.json")),
)

PROTECTED_BRANCH = "main"
DEV_BRANCH = "dev"
EXP_PREFIX = "exp-"

# ── 结果收集（格式与 checks.py 家族一致，但实现自包含）──
results: list[tuple[str, str, str]] = []  # (状态, 检查项, 说明)
MARKS = {"OK": "[ OK ]", "FAIL": "[FAIL]", "SKIP": "[SKIP]", "WARN": "[WARN]"}


def ok(item: str, note: str = "") -> None:
    results.append(("OK", item, note))


def fail(item: str, note: str = "") -> None:
    results.append(("FAIL", item, note))


def skip(item: str, note: str = "") -> None:
    results.append(("SKIP", item, note))


def warn(item: str, note: str = "") -> None:
    results.append(("WARN", item, note))


def result_line() -> str:
    """四个计数分开写 —— 不写"X/Y 通过"，那种写法会把 SKIP/WARN 混进"通过"里。"""
    n = {k: sum(1 for r in results if r[0] == k) for k in MARKS}
    return (f"=== 结果：通过 {n['OK']}/{len(results)}，FAIL={n['FAIL']}，"
            f"SKIP={n['SKIP']}，WARN={n['WARN']} ===")


def print_results() -> None:
    for st, item, note in results:
        print(f"{MARKS[st]} {item}" + (f" — {note}" if note else ""))


def _git(args: list[str], repo: Path) -> tuple[int, str]:
    """跑一条只读 git 命令。返回 (退出码, 合并后的输出)。"""
    try:
        p = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                           encoding="utf-8", errors="replace", timeout=30)
    except FileNotFoundError:
        return 127, "找不到 git 可执行文件（未安装或不在 PATH）"
    except subprocess.TimeoutExpired:
        return 124, "git 命令超时（30s）"
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode, out.strip()


# ── 子命令 ①：校验 commit 注释 ────────────────────────────────────────────────

def cmd_msg(args) -> int:
    raw = args.message
    line = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    if not line:
        fail("commit 注释非空", "空提交信息")
        return 1

    m = _MSG_RE.match(line)
    if not m:
        # ⚠️ 补充说明写进 note，**不另起 print** —— handler 内的 print 会早于 main 的统一
        #    汇总输出，导致提示行跑到 [FAIL] 行**之前**（实测已复现），读起来像结论错位。
        fail("commit 注释格式",
             f"『{line}』不匹配「<类型>: <描述>」（规范 §3.1）；"
             f"正确示例：feat: 新增 DAG 工作流调度器 / fix: 修正汇率漏乘含税系数")
        return 1
    ok("commit 注释格式", "「类型 + 冒号 + 描述」三件套齐备")

    ctype = m.group("type")
    if ctype not in COMMIT_TYPES:
        fail("提交类型合法",
             f"『{ctype}』不在 {'/'.join(COMMIT_TYPES)}（规范 §3.2）；"
             f"需要新增类型（chore/perf/test 等）须先经用户确认，再同步手册 + 两处常量")
        return 1
    extra = []
    if m.group("scope"):
        extra.append(f"scope={m.group('scope')}")
    if m.group("breaking"):
        extra.append("breaking!")
    ok("提交类型合法", f"{ctype}（可选扩展：{'、'.join(extra) if extra else '未使用'}）")

    desc = m.group("desc").strip()
    if len(line) > _MSG_TITLE_MAX:
        warn("注释长度", f"首行 {len(line)} 字符 > {_MSG_TITLE_MAX}（规范未硬性要求，建议把细节移入正文）")
    else:
        ok("注释长度", f"{len(line)} 字符 ≤ {_MSG_TITLE_MAX}")

    key = desc.lower().replace(" ", "")
    if len(desc) < 3 or key in {v.replace(" ", "") for v in _VAGUE_DESCS}:
        warn("描述信息量", f"『{desc}』偏无信息 —— 机器只能判格式，判不了质量（规范 §3.3 靠人审）")
    else:
        ok("描述信息量", "非空且非无信息词（**仅粗筛**，是否说清仍靠人审）")
    return 0 if not any(r[0] == "FAIL" for r in results) else 1


# ── 子命令 ②：校验 .gitignore 覆盖率 ─────────────────────────────────────────

def _norm_ignore_lines(text: str) -> set:
    """规范化 .gitignore 的模式行：去注释/空行/白名单行，去前导 `/` 与 `**/`、去尾随 `/`。"""
    out: set = set()
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("!"):
            continue
        s = s.split(" #", 1)[0].strip()  # 行尾注释
        if not s:
            continue
        s = s.lstrip("/")
        if s.startswith("**/"):
            s = s[3:]
        s = s.rstrip("/")
        if s:
            out.add(s)
    return out


def _check_ignore(repo: Path) -> None:
    gi = repo / ".gitignore"
    if not gi.exists():
        skip(".gitignore 存在性", f"{repo} 下无 .gitignore —— **未检测**（不是通过）")
        return
    try:
        text = gi.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        fail(".gitignore 可读", f"读取失败：{e}")
        return
    have = _norm_ignore_lines(text)
    if not have:
        skip(".gitignore 内容", "文件存在但无有效模式行 —— **未检测**（不是通过）")
        return
    ok(".gitignore 可读", f"{len(have)} 条有效模式")
    for name, patterns in IGNORE_RULES:
        if have & set(patterns):
            ok(f"禁止提交覆盖：{name}", "已命中 " + "/".join(sorted(have & set(patterns))[:3]))
        else:
            hint = "、".join(patterns[:5])
            fail(f"禁止提交覆盖：{name}",
                 f"未匹配到该类模式（启发式）—— 建议补：{hint}（规范 §4.1）")


def cmd_ignore(args) -> int:
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        fail("项目目录存在性", f"{repo} 不是目录")
        return 1
    _check_ignore(repo)
    return 0 if not any(r[0] == "FAIL" for r in results) else 1


# ── 子命令 ③：校验分支状态 ───────────────────────────────────────────────────

def _check_branch(repo: Path) -> None:
    code, out = _git(["rev-parse", "--is-inside-work-tree"], repo)
    if code != 0 or out.strip() != "true":
        skip("git 仓库", f"{repo} 不是 git 仓库 —— **无法判定**（不是通过）")
        return
    ok("git 仓库", str(repo))

    code, branch = _git(["branch", "--show-current"], repo)
    if code != 0:
        fail("当前分支", f"读取失败：{branch}")
        return
    branch = branch.strip()
    if not branch:
        warn("当前分支", "处于游离 HEAD（detached）—— 提交会丢失分支归属，先 git switch <分支>")
    else:
        ok("当前分支", branch)

    code, porcelain = _git(["status", "--porcelain"], repo)
    dirty = [l for l in porcelain.splitlines() if l.strip()] if code == 0 else []
    if code != 0:
        skip("工作区状态", "读取失败 —— **未检测**")
    elif dirty:
        ok("工作区状态", f"{len(dirty)} 处未提交改动")
    else:
        ok("工作区状态", "干净")

    # 规则①：禁止直接在 main 开发
    if branch == PROTECTED_BRANCH:
        if dirty:
            warn("分支保护（规则①）",
                 f"当前在 {PROTECTED_BRANCH} 上有 {len(dirty)} 处未提交改动 —— "
                 f"机器**判不出**那是新功能还是文档修正（只 WARN）；若属开发新功能，"
                 f"先 git switch {DEV_BRANCH} 或 git switch -c {EXP_PREFIX}<主题>")
        else:
            ok("分支保护（规则①）", f"在 {PROTECTED_BRANCH} 上且工作区干净，符合「main 只放稳定版」")
    elif branch in (DEV_BRANCH,) or branch.startswith(EXP_PREFIX):
        ok("分支保护（规则①）", f"{branch} 属开发/实验线，符合规范 §2.1")
    elif branch:
        warn("分支命名", f"『{branch}』不在 main / {DEV_BRANCH} / {EXP_PREFIX}* 三线内（规范 §2.2）")


def cmd_branch(args) -> int:
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        fail("仓库目录存在性", f"{repo} 不是目录")
        return 1
    _check_branch(repo)
    return 0 if not any(r[0] == "FAIL" for r in results) else 1


# ── 子命令 ④：综合预检 ───────────────────────────────────────────────────────

def cmd_preflight(args) -> int:
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        fail("仓库目录存在性", f"{repo} 不是目录")
        return 1
    print(f"--- 分支状态（{repo}）---")
    _check_branch(repo)
    print("--- .gitignore 覆盖率 ---")
    _check_ignore(repo)
    return 0 if not any(r[0] == "FAIL" for r in results) else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="git_check.py",
        description="项目 Git 开发规范校验（只读）—— 规范见 references/git-conventions.md",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("msg", help="校验 commit 注释是否符合规范 §3")
    p1.add_argument("message", help='commit 信息，如 "feat: 新增 DAG 工作流调度器"')
    p1.set_defaults(handler=cmd_msg)

    p2 = sub.add_parser("ignore", help="校验 .gitignore 是否覆盖五类禁止提交（规范 §4）")
    p2.add_argument("--repo", default=".", help="项目目录（默认当前目录）")
    p2.set_defaults(handler=cmd_ignore)

    p3 = sub.add_parser("branch", help="校验当前分支状态（规范 §2）")
    p3.add_argument("--repo", default=".", help="仓库目录（默认当前目录）")
    p3.set_defaults(handler=cmd_branch)

    p4 = sub.add_parser("preflight", help="综合预检（branch + ignore）")
    p4.add_argument("--repo", default=".", help="仓库目录（默认当前目录）")
    p4.set_defaults(handler=cmd_preflight)

    args = ap.parse_args()
    code = args.handler(args)
    print_results()
    return code


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
