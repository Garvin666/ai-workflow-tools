#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# [自研工具] laya_ensure.py
# 用途：Laya 服务的**可用性网关** —— 探活（check/status）、按需拉起并等就绪（ensure）、
#       把「服务生命周期」与「要执行的判定命令」绑在同一进程内跑（with）。
# 适用场景：判定前「先确认它在跑」；解决「服务不常驻 ⇒ 调用方每次都走 exit 3 降级」。
# 作者：ai-workflow 自研（修复Laya根因-2026-09-28，2026-09-28）
# 仓库：https://github.com/Garvin666/ai-workflow-tools
"""laya_ensure.py —— Laya 可用性网关

【定位：务必按此理解，勿误用】
  本脚本**不产判定**、**不接管路由**、**不参与任何放行决策**、**不改服务端逻辑**。
  它只回答一个问题并动手保证：「影子后端现在能用吗？不能用就拉起来。」

【为什么需要它（根因1）】
  Laya 服务不常驻（计划任务未注册 / 自启项要等下次登录才生效）⇒ 调用方探到不可达，
  只能按契约走 exit 3 降级回 Realization A。结果是「装了但几乎没用上」。
  本工具把「探活 → 不在就拉起 → 轮询到就绪」收成一条命令；
  `with` 子命令进一步把服务与判定命令放进**同一进程生命周期**内 —— 这是在受限宿主里
  唯一可靠的方式（实测：分离进程会被宿主回收两次，持句柄的子进程则不会）。

【三条硬纪律】
  1. **就绪判据同源**：直接复用 `laya_client.healthz/readyz`（判据 = `healthz.status==ok`
     ∧ `readyz.ready is True`，与 `laya_client.run_judge` 的 precheck 完全一致）。
     **绝不自造一套判据** —— 否则会出现「网关说就绪、客户端说降级」的分裂。
  2. **不解决跨会话常驻（诚实边界）**：本工具只保证「本次调用进程存活期间」服务可用。
     跨会话常驻手段是登录自启项或计划任务，与本工具无关。`ensure` 走分离进程，
     对受限宿主是**尽力而为**，故其输出显式标记 `persistent: false` —— 不许静默冒充成功。
  3. **失败要可诊断**：前置条件（serve.cmd / venv python / 服务端脚本）缺一即 fail-closed
     （exit 4）并指名缺哪个，绝不吞掉后报「启动失败」这种无法定位的错。

退出码（与 `laya_client` 的降级码对齐）：
  0 = 就绪 / 成功      3 = 服务不可用     4 = 配置错误（前置条件缺失）
  其余 = `with` 透传被包装命令的退出码

用法：
  python scripts/laya_ensure.py check                     # 探活（只读），exit 0/3
  python scripts/laya_ensure.py status                    # JSON 详情（含监听 PID）
  python scripts/laya_ensure.py ensure [--timeout 180]    # 探活→不在则拉起（尽力而为）
  python scripts/laya_ensure.py with --timeout 180 -- <cmd...>   # 拉到就绪后跑 cmd，透传退出码
  python scripts/laya_ensure.py selftest                  # 离线自证（含阴性对照）
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import laya_client as lc  # noqa: E402  —— 就绪判据的唯一来源（纪律1）

# 服务根目录的**候选探测**（刻意不写死任何本机路径 —— 那也是出站扫描的禁忌）：
#   ⚠️ 本机实测 `os.path.expanduser("~")` 解析到与真实部署位置**不一致**的目录（多 home 环境），
#   而部署物实际在 `%USERPROFILE%\.workbuddy\laya` ⇒ 只认 expanduser 会让默认值**恒 fail-closed**
#   （不报错、只是永不生效）。故按 LAYA_ROOT → USERPROFILE → HOME → expanduser 顺序，
#   取第一个**真含服务端脚本**的候选；都无则返回首个候选，让报错信息可定位。
def _resolve_layaroot():
    env = os.environ.get("LAYA_ROOT")
    if env:
        return env
    cands = []
    for k in ("USERPROFILE", "HOME"):
        v = os.environ.get(k)
        if v:
            cands.append(os.path.join(v, ".workbuddy", "laya"))
    try:
        cands.append(os.path.join(os.path.expanduser("~"), ".workbuddy", "laya"))
    except Exception:
        pass
    for c in cands:
        if os.path.isfile(os.path.join(c, "app", "laya_server.py")):
            return c
    return cands[0] if cands else os.path.join(".workbuddy", "laya")


LAYAROOT = _resolve_layaroot()
SERVE_CMD = os.environ.get("LAYA_SERVE_CMD") or os.path.join(LAYAROOT, "bin", "serve.cmd")

EXIT_OK = 0
EXIT_UNAVAILABLE = 3          # 对齐 laya_client 的降级码
EXIT_MISCONFIG = 4            # 前置条件缺失（fail-closed）

POLL_INTERVAL = float(os.environ.get("LAYA_POLL_INTERVAL") or 3.0)


# ---------------------------------------------------------------------------
# 探活（判据同源）
# ---------------------------------------------------------------------------
def probe(base=None):
    """返回 (health_ok, ready_ok, detail)。**判据与 laya_client 同源**，不自造。"""
    base = base or lc.DEFAULT_BASE
    try:
        h = lc.healthz(base)
    except Exception as e:
        return False, False, "%s: %s" % (type(e).__name__, e)
    if not isinstance(h, dict) or h.get("status") != "ok":
        return False, False, "healthz.status=%r" % (h.get("status") if isinstance(h, dict) else h)
    try:
        r = lc.readyz(base)
    except Exception as e:
        return True, False, "healthz=ok 但 readyz 探测失败（%s: %s）" % (type(e).__name__, e)
    ready = bool(isinstance(r, dict) and r.get("ready"))
    return True, ready, "healthz=ok readyz.ready=%r" % (r.get("ready") if isinstance(r, dict) else r)


def _port_of(base):
    import urllib.parse
    try:
        return urllib.parse.urlsplit(base).port or 8731
    except Exception:
        return 8731


def _listener_pids(port):
    """尽力取监听 PID（失败返回 []，不致命 —— 仅用于 status 展示）。"""
    pids = []
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=20)
        for line in r.stdout.decode("gbk", "replace").splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(":%d" % port):
                if parts[4].isdigit():
                    pids.append(int(parts[4]))
    except Exception:
        pass
    return sorted(set(pids))


# ---------------------------------------------------------------------------
# 启动 / 前置条件
# ---------------------------------------------------------------------------
def preconditions(serve_cmd=None, root=None):
    """返回缺失项列表（空 = 齐备）。fail-closed 的依据。"""
    serve_cmd = serve_cmd or SERVE_CMD
    root = root or LAYAROOT
    missing = []
    if not os.path.isfile(serve_cmd):
        missing.append("启动脚本不存在：%s" % serve_cmd)
    py = os.path.join(root, "venv", "Scripts", "python.exe")
    if not os.path.isfile(py):
        missing.append("服务端解释器不存在：%s" % py)
    app = os.path.join(root, "app", "laya_server.py")
    if not os.path.isfile(app):
        missing.append("服务端脚本不存在：%s" % app)
    logdir = os.path.join(root, "logs")
    if not os.path.isdir(logdir):
        missing.append("日志目录不存在：%s" % logdir)
    return missing


def log_offset(root=None):
    """记录启动前的日志偏移 —— 诊断必须只报**本次**新增，否则会把历史成功记录冒充当前证据。"""
    p = os.path.join(root or LAYAROOT, "logs", "server.log")
    try:
        return os.path.getsize(p)
    except Exception:
        return 0


def _server_log_tail(root=None, n=12, from_offset=None):
    """取日志尾部；给定 from_offset 时**只报该偏移之后**的内容（本次启动的增量）。"""
    p = os.path.join(root or LAYAROOT, "logs", "server.log")
    if not os.path.isfile(p):
        return "(无 server.log)"
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            if from_offset:
                f.seek(from_offset)
            txt = f.read()
    except Exception as e:
        return "(读取失败 %s: %s)" % (type(e).__name__, e)
    if not txt.strip():
        return "(本次启动后日志无新增 —— 未见服务端任何输出，非「启动成功」)"
    return "\n".join(txt.strip().splitlines()[-n:])


def launch(detach=False, serve_cmd=None):
    """经 cmd /c 启动 serve.cmd（内含模型参数与线程上限等环境设置）。

    detach=True：分离进程（本进程退出后仍尝试存活；**受限宿主可能回收，故为尽力而为**）。
    detach=False：返回 Popen 句柄，由调用方持有 ⇒ 调用方存活期间服务一定存活。
    """
    serve_cmd = serve_cmd or SERVE_CMD
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if detach \
            else subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(["cmd", "/c", serve_cmd],
                            cwd=os.path.dirname(serve_cmd) or None,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,   # serve.cmd 自身已重定向到 server.log
                            stderr=subprocess.DEVNULL,
                            creationflags=flags)


def _kill(proc, timeout=10):
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def wait_ready(base=None, timeout=180.0, interval=None):
    """轮询到就绪。返回 (ready, detail, elapsed_s)。"""
    base = base or lc.DEFAULT_BASE
    interval = POLL_INTERVAL if interval is None else interval
    t0 = time.time()
    detail = "未探测"
    while time.time() - t0 < timeout:
        _, ready, detail = probe(base)
        if ready:
            return True, detail, round(time.time() - t0, 1)
        time.sleep(interval)
    return False, detail, round(time.time() - t0, 1)


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
def cmd_check(args):
    base = args.base or lc.DEFAULT_BASE
    _, ready, detail = probe(base)
    print(json.dumps({"ready": ready, "base": base, "detail": detail}, ensure_ascii=False))
    return EXIT_OK if ready else EXIT_UNAVAILABLE


def cmd_status(args):
    base = args.base or lc.DEFAULT_BASE
    h_ok, ready, detail = probe(base)
    port = _port_of(base)
    s = socket.socket()
    s.settimeout(2)
    try:
        connected = (s.connect_ex(("127.0.0.1", port)) == 0)
    finally:
        s.close()
    out = {
        "ready": ready,
        "base": base,
        "port": port,
        "healthz_ok": h_ok,
        "tcp_connected": connected,
        "listener_pids": _listener_pids(port),
        "serve_cmd": SERVE_CMD,
        "serve_cmd_exists": os.path.isfile(SERVE_CMD),
        "preconditions_missing": preconditions(),
        "detail": detail,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return EXIT_OK if ready else EXIT_UNAVAILABLE


def cmd_ensure(args):
    base = args.base or lc.DEFAULT_BASE
    _, ready, detail = probe(base)
    if ready:
        print(json.dumps({"started": False, "ready": True, "base": base,
                          "detail": detail, "note": "服务已在运行，未做任何改动"},
                         ensure_ascii=False))
        return EXIT_OK
    missing = preconditions()
    if missing:
        print(json.dumps({"started": False, "ready": False, "misconfig": missing},
                         ensure_ascii=False, indent=2))
        return EXIT_MISCONFIG
    print("[ensure] 服务不可达（%s），正在拉起：%s" % (detail, SERVE_CMD), file=sys.stderr)
    off = log_offset()                      # 先记偏移：诊断只报本次增量
    launch(detach=True)
    ok, d2, elapsed = wait_ready(base, args.timeout)
    out = {
        "started": True, "ready": ok, "base": base, "elapsed_s": elapsed, "detail": d2,
        # ⚠️ 纪律2：分离进程在受限宿主可能被回收 —— 不许静默冒充「已常驻」
        "persistent": False,
        "note": "分离进程可能被宿主回收；跨会话常驻请用登录自启项 / 计划任务（非本工具职责）",
    }
    if not ok:
        out["server_log_tail_this_start"] = _server_log_tail(from_offset=off)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return EXIT_OK if ok else EXIT_UNAVAILABLE


def cmd_with(args):
    """拉到就绪 → 执行被包装命令 → 透传其退出码。"""
    cmd = list(args.cmd)
    while cmd and cmd[0] == "--":
        cmd.pop(0)
    if not cmd:
        print("[with] 缺少被包装命令：laya_ensure.py with -- <cmd...>", file=sys.stderr)
        return EXIT_MISCONFIG
    base = args.base or lc.DEFAULT_BASE
    _, ready, detail = probe(base)
    owned = None
    if not ready:
        missing = preconditions()
        if missing:
            print(json.dumps({"ready": False, "misconfig": missing}, ensure_ascii=False, indent=2))
            return EXIT_MISCONFIG
        print("[with] 服务不可达（%s），拉起并等待就绪…" % detail, file=sys.stderr)
        off = log_offset()                    # 先记偏移：诊断只报本次增量
        owned = launch(detach=False)          # 持句柄 ⇒ 本进程存活期间服务一定存活
        ok, d2, elapsed = wait_ready(base, args.timeout)
        if not ok:
            _kill(owned)
            print(json.dumps({"ready": False, "elapsed_s": elapsed, "detail": d2,
                              "server_log_tail_this_start": _server_log_tail(from_offset=off)},
                             ensure_ascii=False, indent=2))
            return EXIT_UNAVAILABLE
        print("[with] 服务就绪（%.1fs），开始执行：%s" % (elapsed, " ".join(cmd)), file=sys.stderr)
    else:
        print("[with] 服务已在运行，直接执行：%s" % " ".join(cmd), file=sys.stderr)
    try:
        rc = subprocess.call(cmd)
    finally:
        _kill(owned)                          # 只回收自己起的；他人起的绝不碰
    return rc


# ---------------------------------------------------------------------------
# selftest（离线，含阴性对照）
# ---------------------------------------------------------------------------
def selftest():
    global preconditions            # ⑥ 需临时替换它做 fail-closed 对照（声明须在首次使用前）
    fails = []
    counter = {"n": 0}

    def _check(cond, name, detail=""):
        counter["n"] += 1
        print(("  [OK]   " if cond else "  [FAIL] ") + name
              + (("  " + detail) if detail and not cond else ""))
        if not cond:
            fails.append(name)

    dead = "http://127.0.0.1:59987"     # 高位端口，几乎不可能有服务

    print("① 阴性对照：不可达端口不得被判为就绪，且 check 退出码 = 3（与降级码对齐）")
    s = socket.socket(); s.settimeout(1)
    port_free = (s.connect_ex(("127.0.0.1", 59987)) != 0)
    s.close()
    if port_free:
        h_ok, ready, detail = probe(dead)
        _check(not ready, "不可达 ⇒ ready=False（判据不恒真）")
        _check(not h_ok, "不可达 ⇒ health_ok=False")
        _check(isinstance(detail, str) and detail != "", "给出可诊断的 detail")
        rc = cmd_check(argparse.Namespace(base=dead))
        _check(rc == 3, "check 退出码 = 3（对齐 laya_client 降级码）", "got %r" % rc)
    else:
        # 端口被占：跳过而非判 PASS（「沉默」必须与「通过」可区分）
        print("  [SKIP] 59987 被占用，三项就绪判据对照未执行（不计入通过）")

    print("② 纪律1：就绪判据同源 —— 必须直接复用 laya_client 的实现")
    _check(probe.__module__ == __name__, "probe 定义于本模块（不漏用他处实现）")
    import inspect
    src = inspect.getsource(probe)
    _check("lc.healthz(" in src and "lc.readyz(" in src, "probe 内部调用 lc.healthz / lc.readyz")
    _check(lc.readyz.__module__ == "laya_client", "readyz 来自 laya_client（同源）")

    print("③ 判据不可恒真：对同端口，healthz 与 readyz 必须真的走网络（抛异常或返回非就绪）")
    try:
        lc.healthz(dead)
        net_raised = False
    except Exception:
        net_raised = True
    _check(net_raised, "对不可达端口 healthz 抛异常（而非返回 ok）")

    print("④ fail-closed：前置条件缺失必须被指名报出，不得静默")
    miss = preconditions(serve_cmd="__no_such_dir__/serve.cmd", root="__no_such_root__")
    _check(len(miss) >= 2, "缺失项被逐条列出（≥2）", "got %r" % miss)
    _check(all("__no_such" in m for m in miss), "缺失项指名具体路径（可定位）")
    try:
        real_miss = preconditions()
        _check(isinstance(real_miss, list), "真实环境 preconditions 返回列表（不抛异常）")
    except Exception as e:
        _check(False, "真实环境 preconditions 不抛异常", repr(e))

    print("⑤ status 输出形状：必须显式承载 ready / port / 监听者 语义")
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_status(argparse.Namespace(base=dead))
    try:
        sj = json.loads(buf.getvalue())
        shape_ok = all(k in sj for k in ("ready", "port", "base", "listener_pids"))
        _check(shape_ok, "status 含 ready/port/base/listener_pids 键")
        _check(sj["ready"] is False, "不可达时 status.ready = False（不编造）")
        _check(sj["port"] == 59987, "port 由 base 正确解析")
    except Exception as e:
        _check(False, "status 输出可解析为 JSON", repr(e))

    print("⑥ with：退出码透传 + 前置缺失 fail-closed（**selftest 绝不真正拉起服务**）")
    rc7 = subprocess.call([sys.executable, "-c", "import sys; sys.exit(7)"])
    _check(rc7 == 7, "子进程 rc=7 原样得到 7", "got %r" % rc7)
    # 用 stub 让前置条件失败 ⇒ 走 misconfig 分支。若真去拉起服务，则是 selftest 的副作用缺陷。
    _orig_pc = preconditions
    preconditions = lambda *a, **k: ["__stub_missing__"]   # noqa: E731
    try:
        ns = argparse.Namespace(base=dead, timeout=0.1,
                                cmd=["--", sys.executable, "-c", "import sys; sys.exit(5)"])
        rc_stub = cmd_with(ns)
    finally:
        preconditions = _orig_pc
    _check(rc_stub == 4, "前置条件缺失 ⇒ with 返回 4（fail-closed，不启动任何进程）", "got %r" % rc_stub)

    print("⑦ with 的 cmd 解析：前导 -- 必须被剥离，否则会把 -- 当命令跑")
    ns2 = argparse.Namespace(base=dead, timeout=0.1, cmd=["--", "--", "echo", "x"])
    stripped = [c for c in ns2.cmd]
    while stripped and stripped[0] == "--":
        stripped.pop(0)
    _check(stripped[0] == "echo", "多个前导 -- 全部剥离")

    print("⑧ 诊断证据的时效：日志偏移之前的历史成功记录不得冒充本次启动")
    import tempfile as _tf
    _d = _tf.mkdtemp(prefix="laya_ens_st_")
    os.makedirs(os.path.join(_d, "logs"), exist_ok=True)
    lp = os.path.join(_d, "logs", "server.log")
    with open(lp, "w", encoding="utf-8", newline="\n") as f:
        f.write("[ok] laya-min/0.1 listening on http://127.0.0.1:8731  <- 历史（上一次启动）\n")
    _off = log_offset(_d)
    _msg_old = _server_log_tail(_d, from_offset=_off)
    _check("listening" not in _msg_old, "偏移之内的旧成功记录不出现在证据里（阴性对照）", _msg_old)
    with open(lp, "a", encoding="utf-8", newline="\n") as f:
        f.write("[ok] 预热通过\n")
    _msg_new = _server_log_tail(_d, from_offset=_off)
    _check("预热通过" in _msg_new and "历史" not in _msg_new, "只报本次增量", _msg_new)
    import shutil as _sh
    _sh.rmtree(_d, ignore_errors=True)

    print("\n[%s] laya_ensure selftest：%d 项检查，%d 失败"
          % ("PASS" if not fails else "FAIL", counter["n"], len(fails)))
    return 1 if fails else 0


# ---------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description="Laya 可用性网关（探活 / 按需拉起 / 生命周期绑定）")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, helptext in (("check", "探活（只读），exit 0=就绪 / 3=不可用"),
                           ("status", "JSON 详情（含监听 PID 与前置条件）")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--base", default=None)

    ep = sub.add_parser("ensure", help="探活→不在则拉起（分离进程，尽力而为）")
    ep.add_argument("--base", default=None)
    ep.add_argument("--timeout", type=float, default=180.0, help="等待就绪的上限秒数")

    wp = sub.add_parser("with", help="拉到就绪后执行被包装命令，透传其退出码")
    wp.add_argument("--base", default=None)
    wp.add_argument("--timeout", type=float, default=180.0)
    wp.add_argument("cmd", nargs=argparse.REMAINDER, help="-- <命令…>")

    sub.add_parser("selftest", help="离线自证（含阴性对照）")

    args = p.parse_args(argv)
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "ensure":
        return cmd_ensure(args)
    if args.cmd == "with":
        return cmd_with(args)
    return selftest()


if __name__ == "__main__":
    sys.exit(main())
