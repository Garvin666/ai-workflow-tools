#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [自研工具] gate.py
# 用途：**运行时闸门** —— 在"删改既有文件"这类动作**执行之前**判定目标是否在授权范围内，越界即拒绝并留痕。
# 适用场景：准备删除/覆盖/移动工作区外的既有文件、或批量递归操作之前先跑一次；
#           工作区内的日常读写不需要它（那是常态，不需要每次过闸）。
# 作者：ai-workflow 自研（技能增强-ai-workflow-v4.3.0-2026-09-21，2026-09-21）
# 仓库：https://github.com/Garvin666/ai-workflow-tools
"""gate.py - 运行时闸门：删改既有文件前的范围拦截 + 留痕（ai-workflow v4.3.0 / 批次 C）。

它在补什么洞
------------
`checks.py plan` 的「交付物越界检查」是**事后**判据 —— 它检查**已登记的交付物**是否越界，
跑在对账时（阶段 5）。而红线③真正要防的动作（删改工作区外的既有文件）**发生在阶段 3**，
事后判据拦不住已经发生的事。本工具把那道口径**前移**到动作执行之前。

⚠️ **诚实边界（必须与能力一起声明）**
------------------------------------------------------------------
* 它拦得住「**未登记／未授权的越界动作**」；
* 它**无法判断**"该授权的动作有没有被漏掉" —— 你**不说**要删什么，它一无所知。
* 因此它是**拦截 + 留痕**，**不是许可**。把它当成"跑过了就等于安全"是误用。

范围口径与 `checks.py` 同源（技能库卫生第 4 条）
---------------------------------------------------
授权范围判定的三个来源（工作区根 / 基础设施例外 / 越界授权）在 `checks.py` 里有唯一定义。
本工具**按文件路径加载 `checks.py` 复用其常量**，而不是抄一份 —— 抄一份就是新增一处
"同一物理量的判据出现两处"。**加载失败 → 拒绝（rc=3），不退回自带副本**：退回等于
默默改用第二套口径，而这种"看起来在工作、实则用错规则"的失败最难发现（fail-closed）。

用法
----
    # 判定（默认 dry，不写台账；越界/授权事件才写）
    python scripts/gate.py check "<待改文件>" --base "<工作区根>" --intent delete

    # 用户当次授权后的放行（**必须写明理由**，理由进台账）
    python scripts/gate.py check "<待改文件>" --base "<工作区根>" --intent write \
        --allow-outside "用户当次授权：本次任务需修改该文件（原话已记入 Ledger）"

    # 全量留痕（排查用）
    python scripts/gate.py check <path> --base <root> --intent read --log-all

    # 阴性对照（证明判据不是恒真）
    python scripts/gate.py --selftest

退出码：0 = 放行（含基础设施例外）；1 = 拒绝（越界／基础设施例外下的写删而无授权）；
        2 = 用法错误；3 = fail-closed（无法判定：规则加载失败／路径解析失败）
"""
import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

INTENTS = ("read", "write", "delete", "move")
# ⚠️ **必须区分「范围豁免」与「风险确认」** —— 这是本工具第一版栽过的坑：
# 技能根命中 `INFRA_EXCEPTIONS` 时，只豁免**读取与工具用途**；对**写/删既有文件**，
# USER.md 第 2 条明确「豁免的只是越界申请这一步，**不豁免**高风险前置确认」。
# 第一版把 INFRA 一律判 ALLOW，实测 `--intent delete <技能根既有文件>` 被直接放行 ——
# 闸门放行了本该被拦的动作，属**假绿**（比不装闸门更危险，因为它给人一种安全错觉）。
HIGH_RISK_INTENTS = ("write", "delete", "move")


def needs_authorization(verdict: str, intent: str) -> bool:
    """该 (范围判定, 意图) 组合是否**必须先取得当次授权**。"""
    if verdict == "OUTSIDE":
        return True
    if verdict == "INFRA" and intent in HIGH_RISK_INTENTS:
        return True
    return False
# 台账落点：基础设施例外路径（允许写），不进工作区、不外传。
AUDIT_PATH = Path(os.environ.get("AIWF_GATE_AUDIT")
                  or (Path.home() / ".workbuddy" / "cache" / "ai-workflow" / "gate_audit.jsonl"))


class FailClosed(Exception):
    """无法判定 → 拒绝。**绝不**退回"默认放行"。"""


def load_scope_rules(checks_path: Path | None = None):
    """按文件路径加载同目录 `checks.py`，取回范围口径常量。失败 → FailClosed。

    `checks_path` 仅供 `--selftest` 注入（验证"口径源不可用时确实拒绝"），生产路径传 None。
    """
    p = checks_path or (Path(__file__).resolve().parent / "checks.py")
    if not p.exists():
        raise FailClosed("找不到 checks.py（范围口径的唯一事实源）：%s" % p)
    try:
        spec = importlib.util.spec_from_file_location("aiwf_checks_for_gate", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001 —— 任何加载失败都必须拒绝，不放行
        raise FailClosed("加载 checks.py 失败：%s: %s" % (type(e).__name__, e))
    infra = getattr(mod, "INFRA_EXCEPTIONS", None)
    if not infra:
        raise FailClosed("checks.py 未提供 INFRA_EXCEPTIONS —— 口径缺失即拒绝（不猜）")
    return {"infra": tuple(infra), "auth_key": getattr(mod, "SCOPE_AUTH_KEY", "越界授权"),
            "source": str(p)}


def classify(target: str, base: Path, rules: dict) -> dict:
    """判定单个目标：INSIDE / INFRA / OUTSIDE。路径解析失败 → FailClosed。"""
    raw = (target or "").strip().strip('"').strip("'")
    if not raw:
        raise FailClosed("空路径")
    try:
        p = Path(raw)
        abspath = (base / p).resolve() if not p.is_absolute() else p.resolve()
        base_r = base.resolve()
    except (OSError, RuntimeError, ValueError) as e:
        raise FailClosed("路径解析失败 %r：%s" % (raw, e))

    posix = abspath.as_posix()
    inside = posix == base_r.as_posix() or posix.startswith(base_r.as_posix().rstrip("/") + "/")
    if inside:
        return {"target": raw, "resolved": posix, "verdict": "INSIDE",
                "reason": "落在工作区根内（--base=%s）" % base_r.as_posix()}
    for frag in rules["infra"]:
        if frag in posix:
            return {"target": raw, "resolved": posix, "verdict": "INFRA",
                    "reason": "命中基础设施例外路径（%s）——不算越界，但仅限工具用途" % frag}
    return {"target": raw, "resolved": posix, "verdict": "OUTSIDE",
            "reason": "在工作区根之外，且不属基础设施例外"}


def audit(rec: dict) -> str:
    """写台账。返回状态串；**写失败必须显式报出**（留痕失败不能被静默吞掉）。"""
    try:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return "written"
    except OSError as e:
        return "FAILED(%s)" % e


def cmd_check(args) -> int:
    try:
        rules = load_scope_rules()
    except FailClosed as e:
        print("[FAIL-CLOSED] %s" % e, file=sys.stderr)
        print("        → 拒绝执行（无法判定范围时**不放行**，这是设计而非故障）", file=sys.stderr)
        audit({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "verdict": "FAIL_CLOSED",
               "detail": str(e), "base": args.base, "intent": args.intent})
        return 3

    base = Path(args.base)
    if not base.exists():
        print("[FAIL-CLOSED] --base 不存在：%s" % base, file=sys.stderr)
        return 3

    results, denied = [], []
    for t in args.targets:
        try:
            r = classify(t, base, rules)
        except FailClosed as e:
            print("[FAIL-CLOSED] %s" % e, file=sys.stderr)
            audit({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "verdict": "FAIL_CLOSED",
                   "detail": str(e), "target": t, "base": args.base, "intent": args.intent})
            return 3
        r["intent"] = args.intent
        r["exists"] = Path(r["resolved"]).exists()
        if needs_authorization(r["verdict"], args.intent):
            if args.allow_outside:
                r["verdict_final"] = "ALLOW_AUTHORIZED"
            else:
                r["verdict_final"] = "DENY"
                denied.append(r)
        else:
            r["verdict_final"] = "ALLOW"
        results.append(r)

    print("=== gate 运行时闸门 ===")
    print("意图：%s    工作区根：%s    口径来源：%s"
          % (args.intent, base, Path(rules["source"]).name))
    for r in results:
        print("  [%s] %s" % (r["verdict_final"], r["resolved"]))
        print("      ∟ %s" % r["reason"])
        if r["verdict"] == "INFRA" and r["intent"] in HIGH_RISK_INTENTS:
            print("      ∟ ⚠️ 基础设施例外**只豁免读取与工具用途**，不豁免写/删既有文件"
                  "（USER.md 第 2 条）→ 须先取得当次确认")
        if r["intent"] in ("delete", "move") and r["exists"]:
            print("      ∟ ⚠️ 目标是**已存在**的文件，且意图为 %s —— 属高风险动作："
                  "先列受影响清单 + 备份 + 取得明确确认，再执行" % r["intent"])
    if args.allow_outside:
        print("  ⚠️ 放行授权理由：%s" % args.allow_outside)

    n_deny = len(denied)
    print("=== 结论：%s（%d 个目标，拒绝 %d）===" % ("拒绝" if n_deny else "放行", len(results), n_deny))
    if n_deny:
        kinds = sorted({"越界" if r["verdict"] == "OUTSIDE" else "基础设施例外下的写/删"
                        for r in denied})
        print("    被拒原因：%s —— 须先停下、说明要访问什么/为什么/需要什么授权，" % "、".join(kinds))
        print("    取得**当次**同意后用 --allow-outside \"<理由>\" 再次执行"
              "（上一任务的授权不延续到本任务）。")

    # 留痕策略：只记"需要被追溯"的事件（越界判定、授权放行），常态 INSIDE 默认不写 —— 零噪音。
    if n_deny or args.allow_outside or args.log_all:
        for r in results:
            if not (r["verdict_final"] == "ALLOW" and not args.log_all):
                r["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                r["base"] = str(base)
                r["allow_outside_reason"] = args.allow_outside
                r["audit"] = audit(r)
    return 1 if n_deny else 0


# ── 阴性对照：证明判据不是恒真 ────────────────────────────────────────────────
SELFTESTS = [
    ("base 内相对路径", "tasks/x/a.md", "INSIDE", "write"),
    ("base 内绝对路径", r"<BASE>\tasks\x\a.md", "INSIDE", "delete"),
    ("`..` 上跳越界", "../隔壁目录/a.md", "OUTSIDE", "delete"),
    ("工作区外绝对路径", r"E:\完全另一个目录\既有文件.md", "OUTSIDE", "delete"),
    ("基础设施例外", r"<HOME>\.workbuddy\skills\ai-workflow\SKILL.md", "INFRA", "write"),
    ("基础设施例外·cache", r"<HOME>\.workbuddy\cache\ai-workflow\x.jsonl", "INFRA", "write"),
]


def cmd_selftest() -> int:
    base = Path.cwd()
    home = str(Path.home())
    rules = None
    try:
        rules = load_scope_rules()
    except FailClosed as e:
        print("[FAIL-CLOSED] %s" % e)
        return 3
    print("=== gate 阴性对照（口径来源：%s）===" % rules["source"])
    print("base = %s" % base)
    bad = 0
    for name, tpl, want, intent in SELFTESTS:
        t = tpl.replace("<BASE>", str(base)).replace("<HOME>", home)
        try:
            got = classify(t, base, rules)["verdict"]
        except FailClosed as e:
            got = "FAIL_CLOSED(%s)" % e
        flag = "OK " if got == want else "XX "
        if got != want:
            bad += 1
        print("  [%s] %-22s → %-10s（期望 %s）" % (flag, name, got, want))

    # 放行/拒绝的**决策**阴性对照（同一目标，只改 --allow-outside）
    print("  -- 决策层对照：越界目标在有无授权下的相反结论 --")
    cases = [(None, "DENY"), ("用户当次授权：X", "ALLOW_AUTHORIZED")]
    for why, want in cases:
        class A:
            pass
        a = A()
        a.targets = [r"E:\完全另一个目录\既有文件.md"]
        a.base = str(base)
        a.intent = "delete"
        a.allow_outside = why
        a.log_all = False
        g = classify(a.targets[0], base, rules)
        got = "DENY" if (g["verdict"] == "OUTSIDE" and not why) else (
            "ALLOW_AUTHORIZED" if g["verdict"] == "OUTSIDE" else "ALLOW")
        flag = "OK " if got == want else "XX "
        if got != want:
            bad += 1
        print("  [%s] allow_outside=%-22s → %s（期望 %s）" % (flag, repr(why), got, want))

    # ⭐ **决策层 × 意图对照**：范围判定相同、意图不同 → 结论必须相反。
    #    第一版的假绿正出在这里 —— 只测了 `classify`（范围），没测**决策**（放行/拒绝），
    #    于是「技能根 + delete」被静默放行而 selftest 全绿。
    print("  -- 决策层 × 意图对照：同一目标(基础设施例外) + 不同意图 --")
    infra_path = str(Path.home() / ".workbuddy" / "skills" / "ai-workflow" / "SKILL.md")
    for intent, why, want in [("read", None, "ALLOW"),
                              ("delete", None, "DENY"),
                              ("delete", "用户当次授权：X", "ALLOW_AUTHORIZED")]:
        g = classify(infra_path, base, rules)
        got = ("ALLOW_AUTHORIZED" if why else "DENY") if needs_authorization(g["verdict"], intent) else "ALLOW"
        flag = "OK " if got == want else "XX "
        if got != want:
            bad += 1
        print("  [%s] %-8s allow_outside=%-20s → %s（期望 %s）"
              % (flag, intent, repr(why), got, want))

    # fail-closed 对照：**注入一个不存在口径源**，必须拒绝而不是放行。
    # 只测"正常情况下能加载"是没有意义的（那是恒真的）。
    print("  -- fail-closed 对照：口径源不可用时必须拒绝，不得放行 --")
    try:
        load_scope_rules(Path(__file__).resolve().parent / "_不存在的_checks.py")
        print("  [XX ] 口径源缺失时未拒绝 —— fail-closed 失效")
        bad += 1
    except FailClosed as e:
        print("  [OK ] 口径源缺失 → FailClosed（%s）" % str(e)[:70])
    # 反向确认：正常路径下确实能加载（否则上面那条"拒绝"可能只是因为永远加载不了）
    try:
        r2 = load_scope_rules()
        print("  [OK ] 正常口径源可加载（infra 例外 %d 条）—— 证明上一条是**条件性**拒绝，不是恒拒绝"
              % len(r2["infra"]))
    except FailClosed as e:
        print("  [XX ] 正常口径源也加载失败：%s" % e)
        bad += 1
    print("=== 阴性对照：%s ===" % ("全部符合预期" if not bad else "%d 项不符" % bad))
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="gate.py", description="运行时闸门：删改既有文件前的范围拦截（fail-closed）")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("check", help="判定目标是否在授权范围内")
    p.add_argument("targets", nargs="+", help="一个或多个目标路径")
    p.add_argument("--base", required=True, help="工作区根（授权范围的唯一基准）")
    p.add_argument("--intent", default="read", choices=INTENTS, help="意图（默认 read）")
    p.add_argument("--allow-outside", default="",
                   help="用户当次授权后的放行；**须写明理由**（进台账）。覆盖两种情形："
                        "① 工作区外越界 ② **基础设施例外下的写/删**（后者不因属例外而免确认）")
    p.add_argument("--log-all", action="store_true", help="全量写台账（默认只记越界/授权事件）")
    ap.add_argument("--selftest", action="store_true", help="跑阴性对照")
    a = ap.parse_args()

    if a.selftest:
        return cmd_selftest()
    if a.cmd == "check":
        return cmd_check(a)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
