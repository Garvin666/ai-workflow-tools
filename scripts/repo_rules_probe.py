#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# [自研工具] repo_rules_probe.py
# 用途：探测外部 GitHub 仓库的协作规则面（协作文件／治理规则／agent 指令／配额），产出三态结论
# 适用场景：给上游提 PR、在别人仓开 issue、改 fork，或任何跨仓写操作之前
# 作者：ai-workflow
# 仓库：https://github.com/Garvin666/ai-workflow-tools
"""[自研工具] repo_rules_probe.py —— 外部仓库规则前置探测

名称：repo_rules_probe.py
用途：在读写/推送任何非本机仓库之前，一次性摸清对方的协作规则面
      （协作文件 / 治理规则 / agent 指令 / 配额），产出**三态**结论。
适用场景：要给上游提 PR、改 fork、在别人仓开 issue，或任何跨仓写操作之前。
仓库：https://github.com/Garvin666/ai-workflow-tools

三条设计约束均来自 2026-10-20 的 GitHub 端点实测（见
tasks/技能演进-外部仓库规则前置调研-v4.46.0-2026-10-20/调研报告.md）：

  ① `community/profile` 一次拿全 6 类协作文件，且认得非根目录位置
     （openai/codex 的 CONTRIBUTING 在 docs/contributing.md）—— 不必自己拼路径。
  ② `rulesets` 的**列表不含规则内容**，必须二次取详情才知道「要过哪些检查、要几个 approval」。
  ③ `branch protection` 的 404 有**两种含义**：
       - message = "Branch not protected" → 确实没有保护（可判「无」）
       - message = "Not Found"            → 仓库/分支不存在或无权查看（只能判「未检测」）
     只看状态码必然把「看不到」误判成「没有」。这是本脚本存在的核心理由。

退出码（遵循 references/interface-contract.md 四码语义）：
  0 = 至少一个仓库取到了结论（有/无），且无致命错误
  1 = 保留（本脚本不产出「不通过」类结论）
  2 = 用法错误（参数非法、--repo 缺失）
  3 = 全部仓库都「未检测」——既不能说过，也不能说不过
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

API = "https://api.github.com"
UA = "ai-workflow-repo-rules-probe/1.0"

# 三分态。值本身即判据，不允许出现第四种写法（如「疑似」「部分」）。
STATE_YES = "有"
STATE_NO = "无"
STATE_UNKNOWN = "未检测"
STATES = (STATE_YES, STATE_NO, STATE_UNKNOWN)

# agent 指令文件候选：顺序即优先级（AGENTS.md 跨工具覆盖面最大，实测 20+ 工具支持）。
# 只探根层与两个常见目录，不递归整树——递归会退化成 token 与配额黑洞。
AGENT_CANDIDATES = ("AGENTS.md", "AGENT.md", "CLAUDE.md", "CONVENTIONS.md")

# community/profile 里参与判定的 6 类文件
COMMUNITY_KEYS = (
    "contributing",
    "code_of_conduct_file",
    "issue_template",
    "pull_request_template",
    "license",
    "readme",
)


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _resolve_token(cli_token: str | None) -> str | None:
    """凭据解析：复用 http_fetch 的单一实现，避免两套优先级口径。

    优先级（唯一事实源在 scripts/http_fetch.py::_resolve_token）：
    --token > GITHUB_TOKEN > GH_TOKEN > gh CLI。
    导入失败才回退本地精简实现，并在 stderr 说明——不静默降级。
    """
    if cli_token:
        return cli_token
    try:
        from http_fetch import _resolve_token as _hf  # 同目录模块

        return _hf(None)
    except Exception as exc:  # pragma: no cover - 仅在 http_fetch 不可导入时触发
        print(f"[WARN] 未能复用 http_fetch 凭据解析（{type(exc).__name__}），回退本地实现", file=sys.stderr)
        import os

        for env_name in ("GITHUB_TOKEN", "GH_TOKEN"):
            val = os.environ.get(env_name)
            if val:
                return val
        return None


def _http_get(url: str, token: str | None, timeout: float = 15.0):
    """返回 (status, body)。status=0 表示网络失败（不是 HTTP 语义）。

    显式禁用代理：本机回环/直连曾被 HTTP_PROXY 劫持成 502，
    把网络问题伪装成接口问题（见 references/quality-gates.md 反模式清单）。
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": UA,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, {"raw": raw[:300]}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"message": raw[:300]}
    except Exception as exc:
        return 0, {"message": f"网络失败：{type(exc).__name__}: {exc}"}


def _classify_status(status: int, message: str | None) -> tuple[str, str]:
    """把 (状态码, message) 映射成三分态。**这是本脚本的核心判据。**

    404 必须读 message 才能分开「没有保护」与「看不到」；
    403/401/网络失败一律「未检测」——它们都不能证明「没有」。
    """
    msg = (message or "").strip()
    low = msg.lower()
    if status == 200:
        return STATE_YES, ""
    if status == 404:
        if "not protected" in low:
            return STATE_NO, "分支未设保护（message=Branch not protected）"
        if "not found" in low:
            return STATE_UNKNOWN, "404/Not Found：仓库或分支不存在，或当前凭据无权查看"
        return STATE_UNKNOWN, f"404 且 message 语义不明：{msg[:80]}"
    if status == 403:
        return STATE_UNKNOWN, f"403 权限不足：{msg[:80]}"
    if status == 401:
        return STATE_UNKNOWN, f"401 未认证：{msg[:80]}"
    if status == 0:
        return STATE_UNKNOWN, msg or "网络失败"
    return STATE_UNKNOWN, f"HTTP {status}：{msg[:80]}"


def _file_state(node) -> str:
    """community/profile 的单个文件节点 → 两态（这一层没有「未检测」）。"""
    if not node:
        return STATE_NO
    if isinstance(node, dict):
        if node.get("url") or node.get("html_url"):
            return STATE_YES
    return STATE_NO


def _extract_rule_facts(detail: dict) -> dict:
    """从 ruleset 详情里抽可执行约束。只抽三类，其余原样忽略。"""
    checks: list[str] = []
    approvals = None
    code_owner = None
    pr_gate = False
    for rule in detail.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        rtype = rule.get("type")
        params = rule.get("parameters") or {}
        if rtype == "required_status_checks":
            checks = [c.get("context") for c in params.get("required_status_checks") or [] if c.get("context")]
        elif rtype == "pull_request":
            pr_gate = True
            approvals = params.get("required_approving_review_count")
            code_owner = params.get("require_code_owner_review")
    return {
        "pull_request_gate": pr_gate,
        "required_checks": checks,
        "required_approvals": approvals,
        "code_owner_review": code_owner,
    }


def probe_community(slug: str, token: str | None, timeout: float) -> dict:
    status, body = _http_get(f"{API}/repos/{slug}/community/profile", token, timeout)
    state, reason = _classify_status(status, body.get("message") if isinstance(body, dict) else "")
    if state != STATE_YES:
        return {"state": state, "reason": reason, "files": {}, "health_percentage": None}
    files = body.get("files") or {}
    out: dict[str, dict] = {}
    for key in COMMUNITY_KEYS:
        node = files.get(key)
        if key == "code_of_conduct_file" and not node:
            node = files.get("code_of_conduct")
        out[key] = {
            "state": _file_state(node),
            "url": (node or {}).get("html_url") if isinstance(node, dict) else None,
        }
    return {
        "state": STATE_YES,
        "reason": "",
        "files": out,
        "health_percentage": body.get("health_percentage"),
    }


def probe_rulesets(slug: str, token: str | None, timeout: float, detail_limit: int = 3) -> dict:
    status, body = _http_get(f"{API}/repos/{slug}/rulesets", token, timeout)
    state, reason = _classify_status(status, body.get("message") if isinstance(body, dict) else "")
    if state != STATE_YES:
        return {"state": state, "reason": reason, "items": []}
    if not isinstance(body, list) or not body:
        return {"state": STATE_NO, "reason": "无 ruleset（列表为空）", "items": []}
    items = []
    details_fetched = 0
    for rs in body:
        if not isinstance(rs, dict):
            continue
        item = {
            "id": rs.get("id"),
            "name": rs.get("name"),
            "target": rs.get("target"),
            "enforcement": rs.get("enforcement"),
        }
        if rs.get("enforcement") == "active" and details_fetched < detail_limit:
            d_status, detail = _http_get(f"{API}/repos/{slug}/rulesets/{rs.get('id')}", token, timeout)
            details_fetched += 1
            if d_status == 200 and isinstance(detail, dict):
                item.update(_extract_rule_facts(detail))
            else:
                item["detail_state"] = STATE_UNKNOWN
                item["detail_reason"] = _classify_status(d_status, detail.get("message"))[1]
        items.append(item)
    return {"state": STATE_YES, "reason": "", "items": items, "details_fetched": details_fetched}


def probe_protection(slug: str, branch: str, token: str | None, timeout: float) -> dict:
    status, body = _http_get(f"{API}/repos/{slug}/branches/{branch}/protection", token, timeout)
    state, reason = _classify_status(status, body.get("message") if isinstance(body, dict) else "")
    out = {"state": state, "reason": reason, "required_checks": [], "required_approvals": None}
    if state == STATE_YES and isinstance(body, dict):
        rsc = body.get("required_status_checks") or {}
        out["required_checks"] = rsc.get("contexts") or []
        pr = body.get("required_pull_request_reviews") or {}
        out["required_approvals"] = pr.get("required_approving_review_count")
    return out


def probe_agent_files(slug: str, branch: str, token: str | None, timeout: float) -> dict:
    """探 agent 指令文件。只取存在性与大小，**不拉正文**（见调研报告 §3C）。"""
    status, body = _http_get(f"{API}/repos/{slug}/contents/?ref={branch}", token, timeout)
    state, reason = _classify_status(status, body.get("message") if isinstance(body, dict) else "")
    if state != STATE_YES:
        return {"state": state, "reason": reason, "found": []}
    root = {it.get("name"): it for it in body if isinstance(it, dict)}
    found = []
    for idx, name in enumerate(AGENT_CANDIDATES):
        node = root.get(name)
        if node:
            found.append({
                "name": name,
                "location": "/",
                "priority": idx + 1,
                "size": node.get("size"),
                "url": node.get("html_url"),
            })
    gh_status, gh_body = _http_get(f"{API}/repos/{slug}/contents/.github?ref={branch}", token, timeout)
    if gh_status == 200 and isinstance(gh_body, list):
        for it in gh_body:
            if isinstance(it, dict) and it.get("name") == "copilot-instructions.md":
                found.append({"name": "copilot-instructions.md", "location": "/.github", "priority": None,
                              "size": it.get("size"), "url": it.get("html_url")})
    cur_status, cur_body = _http_get(f"{API}/repos/{slug}/contents/.cursor/rules?ref={branch}", token, timeout)
    if cur_status == 200:
        count = len(cur_body) if isinstance(cur_body, list) else 1
        found.append({"name": ".cursor/rules", "location": "/.cursor",
                      "priority": None, "size": None, "count": count, "url": None})
    if found:
        return {"state": STATE_YES, "reason": "", "found": found}
    return {
        "state": STATE_NO,
        "reason": "根目录、.github、.cursor/rules 均未发现 agent 指令文件",
        "found": [],
    }


def probe_rate_limit(token: str | None, timeout: float) -> dict:
    status, body = _http_get(f"{API}/rate_limit", token, timeout)
    if status != 200 or not isinstance(body, dict):
        return {"state": STATE_UNKNOWN, "reason": f"HTTP {status}"}
    core = (body.get("resources") or {}).get("core") or {}
    search = (body.get("resources") or {}).get("search") or {}
    return {
        "state": STATE_YES,
        "core_remaining": core.get("remaining"),
        "core_limit": core.get("limit"),
        "search_remaining": search.get("remaining"),
        "reset_epoch": core.get("reset"),
    }


def probe_basic(slug: str, token: str | None, timeout: float) -> dict:
    status, body = _http_get(f"{API}/repos/{slug}", token, timeout)
    state, reason = _classify_status(status, body.get("message") if isinstance(body, dict) else "")
    if state != STATE_YES:
        return {"state": state, "reason": reason}
    lic = body.get("license") or {}
    return {
        "state": STATE_YES,
        "reason": "",
        "default_branch": body.get("default_branch") or "main",
        "archived": body.get("archived"),
        "pushed_at": body.get("pushed_at"),
        "license": lic.get("spdx_id") or lic.get("key"),
        "private": body.get("private"),
        "allow_squash_merge": body.get("allow_squash_merge"),
        "allow_merge_commit": body.get("allow_merge_commit"),
        "allow_rebase_merge": body.get("allow_rebase_merge"),
    }


def probe_repo(slug: str, token: str | None, timeout: float, detail_limit: int) -> dict:
    basic = probe_basic(slug, token, timeout)
    branch = basic.get("default_branch") or "main"
    payload = {
        "repo": slug,
        "fetched_at": _now(),
        "basic": basic,
        "community": probe_community(slug, token, timeout),
        "governance": {
            "rulesets": probe_rulesets(slug, token, timeout, detail_limit),
            "branch_protection": probe_protection(slug, branch, token, timeout),
        },
        "agent_files": probe_agent_files(slug, branch, token, timeout),
        "rate_limit": probe_rate_limit(token, timeout),
    }
    payload["summary"] = summarize(payload)
    return payload


def _mark(state: str) -> str:
    return {STATE_YES: "有", STATE_NO: "无", STATE_UNKNOWN: "未检测"}.get(state, state)


def summarize(payload: dict) -> list[str]:
    """人读摘要：3–6 行。**未检测必须显式出现**，不得被写成「无」。"""
    repo = payload.get("repo", "?")
    basic = payload.get("basic") or {}
    if basic.get("state") != STATE_YES:
        return [f"- {repo}：无法访问（{basic.get('reason', '未知')}）——结论为「未检测」，不是「无规则」"]

    lines = [f"- {repo}（默认分支 {basic.get('default_branch')}，最近推送 {str(basic.get('pushed_at'))[:10]}，"
             f"许可证 {basic.get('license') or '无'}）"]

    community = payload.get("community") or {}
    if community.get("state") == STATE_YES:
        files = community.get("files") or {}
        parts = []
        for key in ("contributing", "pull_request_template", "issue_template", "code_of_conduct_file"):
            label = {
                "contributing": "CONTRIBUTING",
                "pull_request_template": "PR 模板",
                "issue_template": "issue 模板",
                "code_of_conduct_file": "行为准则",
            }[key]
            parts.append(f"{label} {_mark((files.get(key) or {}).get('state'))}")
        lines.append(f"  协作：{' · '.join(parts)}（健康度 {community.get('health_percentage')}%）")
    else:
        lines.append(f"  协作：未检测（{community.get('reason', '')}）")

    gov = payload.get("governance") or {}
    rs = gov.get("rulesets") or {}
    if rs.get("state") == STATE_YES:
        active = [i for i in rs.get("items") or [] if i.get("enforcement") == "active"]
        checks: list[str] = []
        approvals = None
        code_owner = None
        for item in active:
            checks.extend(item.get("required_checks") or [])
            if item.get("required_approvals") is not None:
                approvals = item.get("required_approvals")
            if item.get("code_owner_review") is not None:
                code_owner = item.get("code_owner_review")
        bits = [f"{len(active)} 条生效规则（共 {len(rs.get('items') or [])} 条）"]
        if checks:
            bits.append(f"必过检查 {', '.join(dict.fromkeys(checks))}")
        if approvals is not None:
            bits.append(f"审批 {approvals}")
        if code_owner:
            bits.append("需 code owner 审")
        lines.append(f"  治理：{'｜'.join(bits)}")
    elif rs.get("state") == STATE_NO:
        lines.append("  治理：无 ruleset")
    else:
        lines.append(f"  治理：ruleset 未检测（{rs.get('reason', '')}）")

    bp = gov.get("branch_protection") or {}
    if bp.get("state") == STATE_YES:
        extra = []
        if bp.get("required_checks"):
            extra.append(f"必过 {', '.join(bp['required_checks'])}")
        if bp.get("required_approvals") is not None:
            extra.append(f"审批 {bp['required_approvals']}")
        lines.append(f"  分支保护：有{'（' + '，'.join(extra) + '）' if extra else ''}")
    elif bp.get("state") == STATE_NO:
        lines.append("  分支保护：无（message 明确为 Branch not protected）")
    else:
        lines.append(f"  分支保护：未检测（{bp.get('reason', '')}）——不要读成「无保护」")

    agent = payload.get("agent_files") or {}
    if agent.get("state") == STATE_YES:
        names = [f"{f.get('name')}@{f.get('location')}" for f in agent.get("found") or []]
        lines.append(f"  指令文件：{', '.join(names)}（只报存在性，未拉正文）")
    elif agent.get("state") == STATE_NO:
        lines.append("  指令文件：未发现 AGENTS/AGENT/CLAUDE/CONVENTIONS/.cursor/.github")
    else:
        lines.append(f"  指令文件：未检测（{agent.get('reason', '')}）")

    rl = payload.get("rate_limit") or {}
    if rl.get("state") == STATE_YES:
        lines.append(f"  配额：core {rl.get('core_remaining')}/{rl.get('core_limit')}")
    return lines


def selftest() -> int:
    """离线自检。**阴性对照必须过**：把「看不到」判成「没有」时，本函数必须报错。"""
    failures: list[str] = []
    counter = {"n": 0}  # 断言计数不写死：增删用例时不得手工同步数字

    def check(name: str, cond: bool, detail: str = "") -> None:
        counter["n"] += 1
        if not cond:
            failures.append(f"{name}: {detail}")

    # 1. 三态分类：404 的两种 message 必须落到不同态
    check("200→有", _classify_status(200, "")[0] == STATE_YES)
    check("404/not protected→无", _classify_status(404, "Branch not protected")[0] == STATE_NO)
    check("404/Not Found→未检测", _classify_status(404, "Not Found")[0] == STATE_UNKNOWN)
    check("403→未检测", _classify_status(403, "Must have permissions")[0] == STATE_UNKNOWN)
    check("401→未检测", _classify_status(401, "Bad credentials")[0] == STATE_UNKNOWN)
    check("网络失败→未检测", _classify_status(0, "网络失败：timeout")[0] == STATE_UNKNOWN)

    # 2. 反向断言：三分支都要被覆盖到，防「某态恒不出现」的死代码
    observed = {
        _classify_status(200, "")[0],
        _classify_status(404, "Branch not protected")[0],
        _classify_status(404, "Not Found")[0],
    }
    check("三态全覆盖", observed == set(STATES), f"实测覆盖 {observed}")

    # 3. 文件节点两态
    check("null→无", _file_state(None) == STATE_NO)
    check("空 dict→无", _file_state({}) == STATE_NO)
    check("含 url→有", _file_state({"url": "u"}) == STATE_YES)

    # 4. ruleset 事实抽取
    facts = _extract_rule_facts({
        "rules": [
            {"type": "deletion"},
            {"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": "cla"}, {"context": "CI required"}]}},
            {"type": "pull_request", "parameters": {
                "required_approving_review_count": 1, "require_code_owner_review": True}},
        ]
    })
    check("必过检查抽取", facts["required_checks"] == ["cla", "CI required"], str(facts))
    check("审批数抽取", facts["required_approvals"] == 1, str(facts))
    check("code owner 抽取", facts["code_owner_review"] is True, str(facts))

    # 5. 摘要语义：未检测不得被写成「无保护」/「无」
    fake = {
        "repo": "x/y",
        "basic": {"state": STATE_YES, "default_branch": "main", "pushed_at": "2026-10-20T00:00:00Z", "license": "MIT"},
        "community": {"state": STATE_YES, "health_percentage": 50,
                      "files": {"contributing": {"state": STATE_NO}, "pull_request_template": {"state": STATE_NO},
                                "issue_template": {"state": STATE_NO}, "code_of_conduct_file": {"state": STATE_NO}}},
        "governance": {"rulesets": {"state": STATE_UNKNOWN, "reason": "403 权限不足"},
                       "branch_protection": {"state": STATE_UNKNOWN, "reason": "404/Not Found"}},
        "agent_files": {"state": STATE_NO, "found": []},
        "rate_limit": {"state": STATE_UNKNOWN},
    }
    text = "\n".join(summarize(fake))
    check("摘要含『未检测』", "未检测" in text, text)
    check("摘要不把未检测写成『无保护』", "分支保护：无" not in text, text)
    check("摘要行数在 3–8 行", 3 <= len(summarize(fake)) <= 8, str(len(summarize(fake))))

    if failures:
        print("[FAIL] selftest 未通过：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"[OK] selftest 全绿：{counter['n']} 项断言通过（含 404 两义、三态全覆盖、摘要不误写）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="探测外部 GitHub 仓库的协作规则面（三态：有/无/未检测）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python scripts/repo_rules_probe.py --repo openai/codex\n"
               "  python scripts/repo_rules_probe.py --repo Garvin666/ai-workflow-skill,Garvin666/ai-workflow-tools --json\n",
    )
    parser.add_argument("--repo", help="owner/name，多个用逗号分隔")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON（缺省为人读摘要）")
    parser.add_argument("--token", help="GitHub PAT（缺省按 GITHUB_TOKEN → GH_TOKEN → gh CLI 解析）")
    parser.add_argument("--detail-limit", type=int, default=3, help="最多取多少条生效 ruleset 的详情（默认 3）")
    parser.add_argument("--timeout", type=float, default=15.0, help="单请求超时秒数（默认 15）")
    parser.add_argument("--selftest", action="store_true", help="离线自检（不联网）")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    if not args.repo:
        parser.print_usage(sys.stderr)
        print("[FAIL] 缺少 --repo owner/name", file=sys.stderr)
        return 2

    slugs = [s.strip() for s in args.repo.split(",") if s.strip()]
    bad = [s for s in slugs if s.count("/") != 1]
    if bad:
        print(f"[FAIL] 仓库标识非法（应为 owner/name）：{', '.join(bad)}", file=sys.stderr)
        return 2

    token = _resolve_token(args.token)
    results = [probe_repo(s, token, args.timeout, max(1, args.detail_limit)) for s in slugs]

    if args.json:
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    else:
        for item in results:
            for line in item["summary"]:
                print(line)

    concluded = [r for r in results if (r.get("basic") or {}).get("state") == STATE_YES]
    if not concluded:
        print("[WARN] 全部仓库均无法判定（未检测），本结果不构成任何「无规则」结论", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
