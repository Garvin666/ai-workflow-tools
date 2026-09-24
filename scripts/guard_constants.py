#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [自研工具] guard_constants.py
# 用途：跨模块同名常量/判据的**同源守卫** —— 机器校验 scripts/ 下同名顶层常量的关系是否与登记一致，漂移即 FAIL
# 适用场景：改动任何 scripts/*.py 的常量后跑一次；也用于新增
#           跨文件同名常量时，先把关系登记到本文件的 GROUPS 里再落代码。
#           日常单文件改动若未触及 GROUPS 登记的常量，则无需运行。
# 作者：ai-workflow 自研（重构优化-ai-workflow-2026-09-21，2026-09-21）
# 仓库：https://github.com/Garvin666/ai-workflow-tools
"""guard_constants.py - 跨模块判据同源守卫（ai-workflow v4.2.0 / 技能库卫生第 4 条）。

为什么要有这个文件
------------------
2026-09-21 的重复与冗余静态分析表明：`scripts/` 下存在多组**跨文件同名**的顶层常量，
它们的关系分两类：

  ① **必须同源**（今天取值完全相同）—— 一旦某处被改动而另一处没跟上，就会出现
     "两个模块对同一物理量用两套判据"，这正是技能库卫生第 4 条要防的事。
     例如 `RETRY_DELAYS`（ai_call / http_fetch）、`CODE_EXT`（checks_core / push_router）、
     `REF_PATTERN`（archive_tasks / checks_core）、`SELFTOOL_KEYS`（checks_core / verify_push）。

     ⚠️ 登记 `members` 时**必须写常量的定义处**，不能写转出它的入口模块。
     v4.4.2 把 `checks.py` 拆成包后，`checks.py` 改用 `from checks_core import *` 转出常量 ——
     星号导入在 AST 里**不产生 `Assign` 节点**，而本守卫正是按 AST 取数（`top_level_const`），
     于是三组常量恒报 MISSING、守卫对它们**静默失能**（2026-09-22 修复，见 CHANGELOG v4.6.0）。

  ② **刻意不同**（语义不同，合并会改变行为）—— 例如 `TEXT_EXT` 中 verify_push 的是
     publish_tools 的真子集（少 `.ts`/`.tsv`/`.bat`/`.css`/`.gitignore`/`.js`/`.sh`）；
     `DEFAULT_REPO` / `DEFAULT_BRANCH` 指向两个不同的远端仓库、读不同的环境变量。
     这类**不允许被"顺手统一"**，但关系被悄悄打破同样要能发现。

技能库卫生第 4 条给的处置是「**合并为单一事实源，或显式声明二者关系并配守卫测试**」。
本文件走的是后一条：不改动任何既有实现的取值与结构（零行为变更），
但把"谁和谁必须同源 / 谁必须是谁的子集 / 谁必须不等"登记成机器可判定的清单。

三类关系
--------
    eq      全部取值必须完全相同
    subset  第 2 个及之后成员的取值必须是第 1 个成员的**子集**（真子集亦可）
    ne      成员之间两两必须不相等（防止被"顺手统一"）

用法
----
    python guard_constants.py                 # 校验真实 scripts/，漂移返回 1
    python guard_constants.py --json          # 机器可读
    python guard_constants.py --selftest      # 阴性对照：注入漂移，确认守卫会红

诚实边界
--------
本守卫只覆盖 **GROUPS 显式登记过的**常量组。它**不会**自动发现新出现的跨文件同名常量 ——
新增同名常量时若不把它登记进来，守卫不会报。这是能力边界，不是漏检。
"""

import argparse
import ast
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)

# ---------------------------------------------------------------------------
# 关系登记表 —— 新增跨模块同名常量时，**先改这里**再写代码
# ---------------------------------------------------------------------------
GROUPS = [
    {
        "name": "RETRY_DELAYS",
        "policy": "eq",
        "members": ["scripts/ai_call.py", "scripts/http_fetch.py"],
        "why": "重试退避序列在两条调用链上必须一致，否则同一故障在不同通道表现不同",
    },
    {
        "name": "CODE_EXT",
        "policy": "eq",
        "members": ["scripts/checks_core.py", "scripts/push_router.py"],
        "why": "「什么算代码文件」在自检与分流推送的两侧必须是同一套判据。"
               "登记处取 checks_core.py（**定义处**）而非 checks.py —— 后者自 v4.4.2 起"
               "只以 `from checks_core import *` 转出，星号导入在 AST 里不产生 Assign 节点，"
               "本守卫按 AST 取数会恒报『找不到模块级常量』（2026-09-22 修复）",
    },
    {
        "name": "REF_PATTERN",
        "policy": "eq",
        "members": ["scripts/archive_tasks.py", "scripts/checks_core.py"],
        "why": "「文档引用完整性」是扁平检查（反引号写裸文件名即命中），归档门禁与自检必须同形。"
               "登记处取 checks_core.py（定义处），理由同 CODE_EXT",
    },
    {
        "name": "SELFTOOL_KEYS",
        "policy": "eq",
        "members": ["scripts/checks_core.py", "scripts/verify_push.py"],
        "why": "自研标注四项必填在自检与独立验收两侧须同源；此处只校验取值一致，"
               "**不引入共享依赖**，verify_push.py 的独立性不受影响。"
               "登记处取 checks_core.py（定义处），理由同 CODE_EXT",
    },
    {
        "name": "TEXT_EXT",
        "policy": "subset",
        "members": ["scripts/publish_tools.py", "scripts/verify_push.py"],
        "why": "verify_push 的文本面是 publish_tools 的**真子集**（少 .ts/.tsv/.bat/.css/"
               ".gitignore/.js/.sh）。这是刻意收窄，顺手统一会改变独立验收的行为",
    },
    {
        "name": "DEFAULT_REPO",
        "policy": "ne",
        "members": ["scripts/publish_tools.py", "scripts/push_ontology.py"],
        "why": "两个通道指向**不同的远端仓库**（ai-workflow-tools / ai-workflow-skill）、"
               "读不同的环境变量，被合并成同一个值会造成推送到错误仓库",
    },
    {
        "name": "DEFAULT_BRANCH",
        "policy": "ne",
        "members": ["scripts/publish_tools.py", "scripts/push_ontology.py"],
        "why": "同上：两个通道读不同的环境变量（SELFTOOL_BRANCH / ONTOLOGY_BRANCH）",
    },
]


def _read(path):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def top_level_const(src, name):
    """返回模块级 `NAME = ...` 的值。可取值则给 set/字面量，否则给规范化后的源码串。"""
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    try:
                        v = ast.literal_eval(node.value)
                        if isinstance(v, frozenset):
                            return frozenset(v), repr(sorted(v))
                        return set(v), repr(sorted(v))
                    except Exception:
                        pass
                    try:
                        return None, " ".join(ast.unparse(node.value).split())
                    except Exception:
                        return None, "<?>"
    return None, None


def _as_set(text):
    """把 indented repr 串还原成 set；失败返回 None（退回字符串集合比较）。"""
    try:
        v = ast.literal_eval(text)
        return set(v) if not isinstance(v, str) else {v}
    except Exception:
        return None


def check_group(group, root=SKILL_ROOT, resolver=None):
    """校验一组。root 为技能根；resolver 给 selftest 注入漂移用。"""
    name = group["name"]
    policy = group["policy"]
    texts = []
    for rel in group["members"]:
        if resolver is not None:
            t = resolver((rel, name))
        else:
            abspath = os.path.join(root, rel.replace("/", os.sep))
            # 登记表有**两种**失效形态，都要报成 MISSING 而不是崩：
            #   ① 文件不存在（路径写错 / 文件被移动或改名）
            #   ② 文件在但常量不在（拆分后常量搬走、或改用星号导入转出）
            # 实测（2026-09-22）：未处理 ① 时直接抛 FileNotFoundError + traceback，
            # 退出码虽是 1（fail-closed），但输出不是可解析的判据结论，排查方向也被带偏。
            if not os.path.exists(abspath):
                return {"group": name, "policy": policy, "status": "MISSING",
                        "detail": "登记的文件不存在：%s（登记表路径已失效）" % rel}
            src = _read(abspath)
            _, t = top_level_const(src, name)
        if t is None:
            return {"group": name, "policy": policy, "status": "MISSING",
                    "detail": "在 %s 中找不到模块级常量 %s" % (rel, name)}
        texts.append((rel, t))
    vals = [t for _, t in texts]

    if policy == "eq":
        ok = len(set(vals)) == 1
        detail = "取值一致" if ok else "取值不一致：%s" % (texts,)

    elif policy == "subset":
        base = _as_set(vals[0])
        rests = [_as_set(v) for _, v in texts[1:]]
        if base is None or any(r is None for r in rests):
            ok = all(set(v) <= set(base) for _, v in texts[1:])
            detail = "回退到字符串比较：%s" % ("子集关系成立" if ok else "子集关系被打破：%s" % (texts[1:],))
        else:
            ok = all(r <= base for r in rests)
            extra = [sorted(r - base) for r in rests if not r <= base]
            detail = "子集关系成立" if ok else "子集关系被打破，越界项：%s" % (extra,)

    elif policy == "ne":
        ok = len(set(vals)) == len(vals)
        detail = "两值不同（符合预期）" if ok else "值被统一了，可能推送到错误目标：%s" % (texts,)

    else:
        return {"group": name, "policy": policy, "status": "ERROR",
                "detail": "未知 policy: %s" % policy}

    return {"group": name, "policy": policy,
            "status": "OK" if ok else "DRIFT", "detail": detail}


# ---------------------------------------------------------------------------
# 阴性对照：注入漂移，确认守卫真的会红
# ---------------------------------------------------------------------------
NEGATIVE = [
    # (描述, group_name, 漂移后的 resolver)
    ("把 ai_call 的 RETRY_DELAYS 改成 (1, 2, 3)，http_fetch 不动",
     "RETRY_DELAYS", {("scripts/ai_call.py", "RETRY_DELAYS"): repr([1, 2, 3]),
                      ("scripts/http_fetch.py", "RETRY_DELAYS"): repr([2, 4, 8])}),
    ("把 verify_push 的 SELFTOOL_KEYS 少一项",
     "SELFTOOL_KEYS", {("scripts/checks_core.py", "SELFTOOL_KEYS"): repr(["名称", "用途", "适用场景", "仓库链接"]),
                       ("scripts/verify_push.py", "SELFTOOL_KEYS"): repr(["名称", "用途"])}),
    ("把 verify_push 的 TEXT_EXT 扩到超出 publish_tools（打破子集）",
     "TEXT_EXT", {("scripts/publish_tools.py", "TEXT_EXT"): repr([".py", ".md"]),
                  ("scripts/verify_push.py", "TEXT_EXT"): repr([".py", ".md", ".tsv"])}),
    ("把 push_ontology 的 DEFAULT_REPO 改成和 publish_tools 一样",
     "DEFAULT_REPO", {("scripts/publish_tools.py", "DEFAULT_REPO"): "R",
                      ("scripts/push_ontology.py", "DEFAULT_REPO"): "R"}),
]


def selftest():
    print("=== 阴性对照：注入漂移后守卫必须判 DRIFT ===")
    bad = 0
    for desc, gname, resolver in NEGATIVE:
        group = next(g for g in GROUPS if g["name"] == gname)
        # 先自证夹具与登记表同步：resolver 必须覆盖该组全部 members。
        # 否则 check_group 取不到值 → MISSING → 会被读成"对照未命中"，
        # 而真实原因是**夹具过期**、不是判据失灵。
        # 实测（2026-09-22）：改完 members 忘改本清单，--selftest 从 4/4 掉到 3/4，
        # 报错文本是"在 X 中找不到模块级常量"—— 指向的是常量，真因却是夹具。
        missing = [m for m in group["members"] if (m, gname) not in resolver]
        if missing:
            bad += 1
            print("[FAIL] %s\n       ⚠️ 夹具与 GROUPS.members **不同步**：resolver 未覆盖 %s"
                  "\n       （这是夹具过期，不是判据失灵 —— 改 members 后必须同步改 NEGATIVE）"
                  % (desc, missing))
            continue
        r = check_group(group, resolver=lambda k: resolver.get(k))
        hit = r["status"] == "DRIFT"
        bad += 0 if hit else 1
        print("[%s] %s\n       %s" % ("OK " if hit else "FAIL", desc, r["detail"][:150]))
    print("\n阴性对照 %d/%d 命中" % (len(NEGATIVE) - bad, len(NEGATIVE)))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description="跨模块判据同源守卫")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--selftest", action="store_true", help="阴性对照自测")
    ap.add_argument("--root", default=SKILL_ROOT, help="技能根路径（默认本脚本的上级目录）")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    results = [check_group(g, root=a.root) for g in GROUPS]
    bad = [r for r in results if r["status"] != "OK"]
    # 三态分离：DRIFT（取值不一致）/ MISSING（登记表失效，**无法判定**）/ ERROR（未知 policy）
    # 语义不同，不得合并成一个词上报。实测教训：原汇总语把三者统称"漂移 N 组"，
    # 于是 3 组 MISSING 被读成"有 3 处取值不一致"，先去查取值差异 —— 方向完全错了。
    counts = {s: sum(1 for r in results if r["status"] == s)
              for s in ("OK", "DRIFT", "MISSING", "ERROR")}

    if a.json:
        # "drift" 键保留原义 = 非 OK 总数（向后兼容既有调用方）；细分见 by_status
        print(json.dumps({"drift": len(bad), "by_status": counts, "results": results},
                         ensure_ascii=False, indent=1))
        return 1 if bad else 0

    print("=== 跨模块判据同源守卫（技能库卫生第 4 条）===")
    print("技能根：%s\n" % a.root)
    for r in results:
        mark = {"OK": "[ OK ]", "DRIFT": "[FAIL]", "MISSING": "[FAIL]", "ERROR": "[FAIL]"}[r["status"]]
        print("%s %-16s policy=%-7s %s" % (mark, r["group"], r["policy"], r["detail"][:160]))
    print("\n=== 结果：%d 组 —— OK %d / 漂移(DRIFT) %d / 登记失效(MISSING) %d / 错误(ERROR) %d ==="
          % (len(results), counts["OK"], counts["DRIFT"], counts["MISSING"], counts["ERROR"]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
