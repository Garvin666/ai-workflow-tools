# -*- coding: utf-8 -*-
# [自研工具] laya_client.py
# 用途：ai-workflow 四个 judge（self-judge / method-judge / retrieval-judge / homework-judge）调用本地
#       Laya 服务的客户端——契约映射 + 超时重试 + 降级，把 Laya 的 answers 翻译成 judge 契约字段。
# 适用场景：阶段 0 第 1 步（self-judge）、阶段 0 作业识别（homework-judge）、阶段 3 执行期六动作第②步
#           （method-judge）、阶段 3 第 5 条前（retrieval-judge）。
# 作者：ai-workflow 自研（作业模式-ai-workflow-v4.7.0-2026-09-23，2026-09-23）
# 仓库：https://github.com/Garvin666/ai-workflow-tools
"""
依赖：**纯标准库**（与 ai_call.py / kb.py 同风格）。服务不可用时抛 LayaUnavailable，由调用侧降级到 Realization A。

用法：
    python laya_client.py --judge self-judge      --state "帮我写个脚本合并这三份 CSV"
    python laya_client.py --judge homework-judge  --state "已知 a=1,b=2，求 a+b，要完整过程" [--clues "第三章课后题"]
    python laya_client.py --judge method-judge    --what "取 GitHub 指标" --input "repo a/b" --expect "star 数" \
                          --candidates "http_fetch,gh api,web_search"
    python laya_client.py --judge retrieval-judge --need "现行国标限值" --clues "实验报告" --workspace "E:/X"
    python laya_client.py --selfcheck             # 探测服务是否可用（exit 0/2）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# 与 ai_call.py 同口径：指数退避 (0,2,4,8)。
# ⚠️ 诚实边界：stdlib urllib 的 timeout 是**单个 socket 超时**，无法把"连接超时"与"读取超时"
#    分离（requests 才支持 (connect, read) 元组）。故：
#      - READ_TIMEOUT  用于 /v1/judge（覆盖连接 + 读取）
#      - CONNECT_TIMEOUT 仅用于 /healthz、/readyz 这类轻量探测（要求快失败）
#    需要严格分离时改用 http.client + socket.settimeout，本版不引入 requests（保持零新增依赖）。
RETRY_DELAYS = (0, 2, 4, 8)
CONNECT_TIMEOUT = float(os.environ.get("LAYA_CONNECT_TIMEOUT") or 3)
READ_TIMEOUT = float(os.environ.get("LAYA_READ_TIMEOUT") or 15)
DEFAULT_BASE = os.environ.get("LAYA_BASE_URL") or "http://127.0.0.1:%s" % (
    os.environ.get("LAYA_PORT") or 8731)

# 本地回环专用的「不带环境代理」opener（理由见 `_opener()` 的 docstring —— 这是本机实测缺陷）
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

TEMPLATES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "judges.json")

# ---------------------------------------------------------------------------
# 路由钉死（2026-09-23 实测，用户确认）
#   服务端 Router 按"字母中 Han 占比"判语言。judge payload 里必然含英文标识符
#   （http_fetch.py / gh api / Grep / web_search …），会把中文步骤的占比压下去 →
#   实测 method/retrieval 被判成 "English Latin text" 并走 **english** ckpt：
#     延迟 10621.9 / 14856.5 ms，而同一 payload 显式 multilingual 只要 1960.2 ms（5.4×–7.6×）
#     且 english ckpt 跑中文的官方评估 ECE = 0.376（读中文最差的 ckpt）
#   `--default multilingual` 只在**判不出**时兜底，这里是"判了、判错了"，兜不住。
# ⇒ 对这两类**显式指定** multilingual；--model 显式传参优先（覆盖本表）。
#   self_judge 的 state 是纯中文、实测路由正确（1073.3 ms），**暂不钉**，继续观察。
#   2026-09-23 追加：homework_judge 同样钉 multilingual —— 作业题干**必然**含公式、变量名、
#   代码片段或英文术语（如 x^2 / for i in range / Python），与 method/retrieval 属**同一风险源**。
#   ⚠️ 诚实标注：本项**未实测**（截至本轮 Laya 服务未运行）。钉死不受实测约束的理由：
#   中文作业题走 multilingual 是无害且更稳的选择（english ckpt 跑中文的官方评估 ECE=0.376，为最差档）。
#   服务可用后须补一次实测并回填实测值到 references/laya-backend.md。
PINNED_MODEL_BY_KIND = {"method_judge": "multilingual", "retrieval_judge": "multilingual",
                        "homework_judge": "multilingual"}


class LayaUnavailable(Exception):
    """服务不可用 / 契约不合法：调用侧应降级到 Realization A（脑内协议）。"""


class LayaBadResponse(Exception):
    """响应结构不合法。**不重试**（重试不会让它变合法）。"""


def _token():
    return os.environ.get("LAYA_JUDGE_TOKEN") or ""


def _opener(url):
    """取该 URL 应使用的 opener —— **本地回环显式绕过环境代理**。

    ⚠️ 为什么必须绕（2026-09-23 本机实测，不是理论担忧）：本机环境设了
    `HTTP_PROXY` / `HTTPS_PROXY`（指向本机某端口），而 `urllib` **默认读环境代理**，
    于是对 `http://127.0.0.1:8731` 的请求会被代理拦下并返回 **502 Bad Gateway**。
    后果极其隐蔽：服务其实正常，客户端却判"服务不可用" → **静默降级回 Realization A**，
    整个 Realization B 形同虚设且不留任何显式错误。
    判据：对不可达的本地端口，异常应是**连接类**（URLError/ConnectionRefused），
    而不是 `HTTPError 502`（那是"代理替我答了"）。自证脚本 `_offline_check.py` 有该阴性对照。
    """
    host = urllib.parse.urlsplit(url).hostname or ""
    return _NO_PROXY_OPENER if host in ("127.0.0.1", "localhost", "::1") else urllib.request.build_opener()


def _request(base, path, payload=None, timeout=None):
    url = base.rstrip("/") + path
    timeout = READ_TIMEOUT if timeout is None else timeout
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    tok = _token()
    if tok:
        headers["Authorization"] = "Bearer %s" % tok      # S1：凭据只经环境变量，不落盘、不打印
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with _opener(url).open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def healthz(base=DEFAULT_BASE):
    return _request(base, "/healthz", timeout=CONNECT_TIMEOUT)


def readyz(base=DEFAULT_BASE):
    return _request(base, "/readyz", timeout=CONNECT_TIMEOUT)


def judge(state, questions, model=None, base=DEFAULT_BASE, retries=RETRY_DELAYS):
    """调一次 /v1/judge。只重试"可恢复"的失败（连接/超时/5xx）；4xx 不重试。

    返回 (result, meta)；meta 含 degraded 标记与耗时，供调用侧留痕。
    """
    payload = {"state": state, "questions": questions}
    if model:
        payload["model"] = model
    last = None
    for attempt, delay in enumerate(retries):
        if delay:
            time.sleep(delay)
        t0 = time.time()
        try:
            res = _request(base, "/v1/judge", payload)
            answers = res.get("answers")
            if not isinstance(answers, dict):
                raise LayaBadResponse("answers 缺失或不是对象：%r" % res)
            return res, {"attempts": attempt + 1, "degraded": False,
                         "elapsed_ms": round((time.time() - t0) * 1000, 1),
                         "model_key": (res.get("routing") or {}).get("model")}
        except urllib.error.HTTPError as e:
            code = e.code
            if 400 <= code < 500 and code != 429:
                raise LayaBadResponse("HTTP %s: %s" % (code, e.read()[:400].decode("utf-8", "replace")))
            last = "HTTP %s" % code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = "%s: %s" % (type(e).__name__, e)
        except LayaBadResponse:
            raise
    raise LayaUnavailable("Laya 服务不可用（已重试 %d 次，最后错误：%s）" % (len(retries), last))


# ---------------------------------------------------------------------------
# 契约映射：Laya answers -> judge 契约字段
# ---------------------------------------------------------------------------
def _load_templates():
    with open(TEMPLATES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _normalise(probs):
    """softmax 已归一化；round 到 4 位会破坏 sum=1，故只在输出层做，并显式重归一化。"""
    total = sum(probs.values()) or 1.0
    return {k: v / total for k, v in probs.items()}


def _combine(probs, delta):
    """top2 差距小于 delta 时输出 'A+B'（契约允许组合类）。"""
    items = sorted(probs.items(), key=lambda kv: -kv[1])
    if len(items) >= 2 and (items[0][1] - items[1][1]) < delta:
        return "%s+%s" % (items[0][0], items[1][0])
    return items[0][0]


def map_self_judge(result, templates=None):
    """-> self-judge 契约：category/distribution/confidence/dimensions/ambiguity/route_hint[/secondary]"""
    t = (templates or _load_templates())["self_judge"]
    a = result["answers"]
    cat = a.get("category") or {}
    if cat.get("type") != "choice":
        raise LayaBadResponse("category 不是 choice：%r" % cat)
    dist = _normalise({k: float(v) for k, v in (cat.get("probabilities") or {}).items()})
    if not dist:
        raise LayaBadResponse("category 缺 probabilities")

    # 口径同源：契约 §4 定义 confidence = max(distribution)。
    # ⚠️ 不要用 Laya 自带的 confidence 字段 —— 它是 1 - H(p)/log(k) 的熵置信，与 max(p) 是两个量。
    ordered = sorted(dist.items(), key=lambda kv: -kv[1])
    top, second = ordered[0], (ordered[1] if len(ordered) > 1 else (None, 0.0))
    band = t["thresholds"]["ambiguity_band"]
    noul = float((a.get("ambiguity") or {}).get("noul", 1.0))

    out = {
        "category": top[0],
        "distribution": {k: round(v, 4) for k, v in dist.items()},
        "confidence": round(top[1], 4),
        "dimensions": {},
        "ambiguity": bool(band[0] <= noul <= band[1]),
        "route_hint": t["route_hint"][top[0]],
    }
    out["distribution"] = _renorm(out["distribution"])
    for d in ("D1", "D2", "D3", "D4", "D5"):
        q = a.get(d) or {}
        if q.get("type") == "score":
            out["dimensions"][d] = round(float(q["score"]), 4)
    # 次标签：与主类差距 <0.15 时显式拆层（契约 §5 混合请求）
    if second[0] and (top[1] - second[1]) < 0.15:
        out["secondary"] = second[0]
    out["_laya"] = {
        "model_key": (result.get("routing") or {}).get("model"),
        "entropy_confidence": cat.get("confidence"),
        "noul_ambiguity": round(noul, 4),
        "latency_ms": (result.get("meta") or {}).get("latency_ms"),
        "usage": result.get("usage"),
    }
    return out


def _renorm(d):
    total = sum(d.values()) or 1.0
    return {k: round(v / total, 4) for k, v in d.items()}


def map_method_judge(result, candidates, templates=None):
    """-> method-judge 契约：gap_class/candidates[{tool,fit_score}]/needs_tool/confidence/route_hint"""
    t = (templates or _load_templates())["method_judge"]
    a = result["answers"]
    gap = a.get("gap_class") or {}
    probs = {k: float(v) for k, v in (gap.get("probabilities") or {}).items()}
    if not probs:
        raise LayaBadResponse("gap_class 缺 probabilities")
    cand_q = a.get("candidates") or {}
    cand_probs = {k: float(v) for k, v in (cand_q.get("probabilities") or {}).items()}
    ranked = sorted(cand_probs.items(), key=lambda kv: -kv[1]) or [(c, 0.0) for c in candidates]
    return {
        "gap_class": _combine(probs, t["combine_gap_delta"]),
        "candidates": [{"tool": k, "fit_score": round(v, 4)} for k, v in ranked],
        "needs_tool": bool(float((a.get("needs_tool") or {}).get("noul", 0.0)) >= 0.5),
        "confidence": round(max(v for _, v in ranked), 4),
        "route_hint": "待主代理按 capability-routing.md 判据确认后引入",
        "_laya": {"model_key": (result.get("routing") or {}).get("model"),
                  "dimensions": {k: (a.get(k) or {}).get("score")
                                 for k in ("M1", "M2", "M3", "M4", "M5") if a.get(k)},
                  "latency_ms": (result.get("meta") or {}).get("latency_ms")},
    }


def map_retrieval_judge(result, candidates, templates=None):
    """-> retrieval-judge 契约：source_class/candidates[{source,fit_score}]/needs_retrieval/confidence/route_hint"""
    t = (templates or _load_templates())["retrieval_judge"]
    a = result["answers"]
    src = a.get("source_class") or {}
    probs = {k: float(v) for k, v in (src.get("probabilities") or {}).items()}
    if not probs:
        raise LayaBadResponse("source_class 缺 probabilities")
    cand_probs = {k: float(v) for k, v in ((a.get("candidates") or {}).get("probabilities") or {}).items()}
    ranked = sorted(cand_probs.items(), key=lambda kv: -kv[1]) or [(c, 0.0) for c in candidates]
    return {
        "source_class": _combine(probs, t["combine_src_delta"]),
        "candidates": [{"source": k, "fit_score": round(v, 4)} for k, v in ranked],
        "needs_retrieval": bool(float((a.get("needs_retrieval") or {}).get("noul", 0.0)) >= 0.5),
        "confidence": round(max(v for _, v in ranked), 4),
        "route_hint": "查不到时须区分『已查 A/B，0 命中』与『未查 C/D』，禁止直接断言不存在",
        "_laya": {"model_key": (result.get("routing") or {}).get("model"),
                  "dimensions": {k: (a.get(k) or {}).get("score")
                                 for k in ("R1", "R2", "R3", "R4", "R5") if a.get(k)},
                  "latency_ms": (result.get("meta") or {}).get("latency_ms")},
    }


def map_homework_judge(result, templates=None):
    """-> homework-judge 契约：mode/distribution/confidence/dimensions[W1–W5]/ambiguity/route_hint/needs_homework

    ⚠️ `needs_homework` 是**派生字段**（= 主类恰为「作业题」），**不是独立 question** ——
    这样从设计上消除「mode=讲解题 却 needs_homework=true」这类自相矛盾（该坑与 method-judge
    的 `needs_tool` × `gap_class` 自相矛盾同类：那边靠事后识别，这边直接不给出矛盾的机会）。
    """
    t = (templates or _load_templates())["homework_judge"]
    a = result["answers"]
    m = a.get("mode") or {}
    if m.get("type") != "choice":
        raise LayaBadResponse("mode 不是 choice：%r" % m)
    dist = _normalise({k: float(v) for k, v in (m.get("probabilities") or {}).items()})
    if not dist:
        raise LayaBadResponse("mode 缺 probabilities")

    ordered = sorted(dist.items(), key=lambda kv: -kv[1])
    top = ordered[0]                      # dist 非空 ⇒ ordered 非空
    band = t["thresholds"]["ambiguity_band"]
    noul = float((a.get("ambiguity") or {}).get("noul", 1.0))
    mode = _combine(dist, t["combine_mode_delta"])
    out = {
        "mode": mode,
        "distribution": _renorm({k: round(v, 4) for k, v in dist.items()}),
        "confidence": round(top[1], 4),
        "dimensions": {},
        "ambiguity": bool(band[0] <= noul <= band[1]),
        "route_hint": t["route_hint"][top[0]],
        "needs_homework": top[0] == "作业题",
    }
    for d in ("W1", "W2", "W3", "W4", "W5"):
        q = a.get(d) or {}
        if q.get("type") == "score":
            out["dimensions"][d] = round(float(q["score"]), 4)
    out["_laya"] = {
        "model_key": (result.get("routing") or {}).get("model"),
        "entropy_confidence": m.get("confidence"),
        "noul_ambiguity": round(noul, 4),
        "latency_ms": (result.get("meta") or {}).get("latency_ms"),
        "usage": result.get("usage"),
    }
    # ⚠️ 刻意**不产** `secondary`（与 self-judge 不同）：模式归属是**有组合语义**的枚举，
    #   top2 接近时由 `_combine` 直出「作业题+讲解题」——一件事只在一处表达，
    #   避免"组合串"与"secondary"两个字段描述同一状态（同「技能库卫生」第 4 条）。
    return out


# ---------------------------------------------------------------------------
# questions 构造
# ---------------------------------------------------------------------------
def build_questions(kind, candidates=None, templates=None):
    t = (templates or _load_templates())[kind]
    qs = {}

    def put(qid, q):
        qs[qid] = json.loads(json.dumps(q, ensure_ascii=False))   # 深拷贝，避免改到模板

    if kind == "self_judge":
        for qid, q in t["fixed"].items():
            put(qid, q)
        return qs

    if kind == "homework_judge":
        # 模式选用判定：主类（choice）+ 模糊判定（noul）+ W1–W5 维度；**无候选注入**
        #（候选是"工具/源"的概念，模式选用没有候选池 —— 硬塞一个会导致 Laya 在无选项时乱分配概率）
        put("mode", t["mode"])
        put("ambiguity", t["ambiguity"])
        for qid, q in t["dimensions"].items():
            put(qid, q)
        return qs

    put(kind == "method_judge" and "gap_class" or "source_class", t["gap_class" if kind == "method_judge" else "source_class"])
    put("needs_tool" if kind == "method_judge" else "needs_retrieval",
        t["needs_tool" if kind == "method_judge" else "needs_retrieval"])
    for qid, q in t["dimensions"].items():
        put(qid, q)
    cands = [c.strip() for c in (candidates or []) if c.strip()]
    if cands:
        label = "tool" if kind == "method_judge" else "source"
        qs["candidates"] = {
            "type": "choice",
            "instructions": "下列候选项中，哪一个最契合这一步的目标？",
            "criteria": {c: None for c in cands},
        }
        qs["candidates"]["_label"] = label
    return qs


def state_of(kind, fields):
    """把结构化入参序列化成 state 文本（laya 内部对 dict/list 走 json.dumps）。"""
    return json.dumps(fields, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 对外入口：一次调用完成一个 judge
# ---------------------------------------------------------------------------
def run_judge(kind, state, candidates=None, model=None, base=DEFAULT_BASE, templates=None,
              precheck=True):
    """一次调用完成一个 judge。

    precheck=True：先用 3s 探 /readyz，失败即**立即降级**，避免"服务没起"时走完 15s×4 的重试才失败。
    """
    if precheck:
        try:
            if not readyz(base).get("ready"):
                raise LayaUnavailable("服务未就绪（/readyz ready=false）")
        except LayaUnavailable:
            raise
        except Exception as e:
            raise LayaUnavailable("服务未就绪（/readyz 探测失败：%s: %s）" % (type(e).__name__, e))
    questions = build_questions(kind, candidates, templates)
    questions.pop("_label", None)
    for q in list(questions.values()):
        q.pop("_label", None)
    # 路由钉死：显式入参优先；否则按 PINNED_MODEL_BY_KIND 钉（理由见常量处注释）
    if not model:
        model = PINNED_MODEL_BY_KIND.get(kind)
    result, meta = judge(state, questions, model=model, base=base)
    if kind == "self_judge":
        out = map_self_judge(result, templates)
    elif kind == "method_judge":
        out = map_method_judge(result, candidates or [], templates)
    elif kind == "homework_judge":
        out = map_homework_judge(result, templates)
    else:
        out = map_retrieval_judge(result, candidates or [], templates)
    out["_laya"]["attempts"] = meta["attempts"]
    out["_laya"]["degraded"] = meta["degraded"]
    return out


def selfcheck(base=DEFAULT_BASE):
    try:
        h = healthz(base)
    except Exception as e:
        print(json.dumps({"ok": False, "stage": "healthz", "error": "%s: %s" % (type(e).__name__, e)},
                         ensure_ascii=False))
        return 2
    if h.get("status") != "ok":
        print(json.dumps({"ok": False, "stage": "healthz", "detail": h}, ensure_ascii=False))
        return 2
    try:
        r = readyz(base)
    except Exception as e:
        print(json.dumps({"ok": False, "stage": "readyz", "error": "%s: %s" % (type(e).__name__, e)},
                         ensure_ascii=False))
        return 2
    try:
        out = run_judge("self_judge", "帮我写个脚本，把这三份 CSV 合并成一份", base=base)
    except Exception as e:
        print(json.dumps({"ok": False, "stage": "judge", "error": "%s: %s" % (type(e).__name__, e)},
                         ensure_ascii=False))
        return 2
    ok = (out.get("category") in ("chat", "code", "content")
          and abs(sum(out["distribution"].values()) - 1.0) < 1e-3
          and len(out.get("dimensions", {})) == 5)
    print(json.dumps({"ok": ok, "ready": r, "sample": out}, ensure_ascii=False, indent=2))
    return 0 if ok else 2


def main(argv=None):
    ap = argparse.ArgumentParser(description="Laya judge 客户端（契约映射 + 重试 + 降级）")
    ap.add_argument("--judge", choices=["self-judge", "method-judge", "retrieval-judge", "homework-judge"])
    ap.add_argument("--state")
    ap.add_argument("--what"), ap.add_argument("--input"), ap.add_argument("--expect")
    ap.add_argument("--need"), ap.add_argument("--clues"), ap.add_argument("--workspace")
    ap.add_argument("--candidates", help="逗号分隔的候选工具/源")
    ap.add_argument("--model", help="指定 checkpoint 名（english / multilingual）")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args(argv)

    if args.selfcheck:
        return selfcheck(args.base)

    if not args.judge:
        ap.error("需要 --judge 或 --selfcheck")

    kind = args.judge.replace("-", "_")
    cands = [c for c in (args.candidates or "").split(",") if c.strip()]
    if kind == "self_judge":
        state = args.state or ""
        if not state:
            ap.error("self-judge 需要 --state")
    elif kind == "homework_judge":
        # 模式选用判定：state = 请求原文；--clues 可选（教材/章节/作业要求等语境线索）
        state = args.state or ""
        if not state:
            ap.error("homework-judge 需要 --state")
        if args.clues:
            state = state_of(kind, {"request_text": state, "context_clues": args.clues})
    elif kind == "method_judge":
        state = state_of(kind, {"step_what": args.what or "", "step_input": args.input or "",
                                "step_expect": args.expect or ""})
    else:
        state = state_of(kind, {"step_need": args.need or "", "step_clues": args.clues or "",
                                "workspace": args.workspace or ""})
    try:
        out = run_judge(kind, state, cands, model=args.model, base=args.base)
    except (LayaUnavailable, LayaBadResponse) as e:
        # 降级契约：调用侧据此回退 Realization A，流程不得中断
        print(json.dumps({"degraded": True, "reason": str(e)}, ensure_ascii=False))
        return 3
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
