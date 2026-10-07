#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# [自研工具] falsifiable-file-migration（可证伪的文件搬运）
# 用途：把「把一个目录/一批文件从一个位置迁到另一个位置」做成可证伪的三步（复制 → 逐文件 sha256 对账 → 原件送回收站 → 独立核实可还原）
# 适用场景：换盘 / 换 home / 把工作区外目录收进工作区 / 整理归档根；源目录只有一份、或源位置本身要被清空时
# 作者：ai-workflow 自研（沉淀-迁移与出站与判据契约技能-2026-10-07）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/skills/falsifiable-file-migration/scripts/migrate_falsifiable.py
"""可证伪的文件搬运：复制 → 逐文件 sha256 对账 → 原件送回收站 → 独立核实。

子命令
------
  plan    SRC DST [--baseline OUT]   清点源、落基线、打印清单（只读，不动任何东西）
  run     SRC DST [--baseline OUT]   复制源到目标 + 逐文件对账（**不删源**）
  trash   SRC                        把源（其下每个文件）送**回收站**（非硬删）
  verify  SRC --baseline IN          独立核实：回收站里能按「原路径 + 字节数」匹配到应被回收的那批
  selftest [--live]                  自测；--live 额外做一次真删/真还原的端到端往返

设计要点（为什么不是 shutil.move）
----------------------------------
* `move` 跨盘 = 复制 + 删除；中途失败留「半搬状态」，事后说不清哪半是新的。
  本工具把它拆成「复制 → 对账 → 送回收站」三段，**每段之间可证伪**。
* 对账口径是**按相对路径逐文件比对 sha256 + 字节数**，不是比文件数、也不是比两个集合。
* 原件送回收站而非硬删，保住退路；`verify` 独立去回收站里核对，而不是信 `trash` 的返回值。

⚠️ 抓 fail-open：有些卷（尤其是移动盘/网络盘）被策略配置为「删除时不使用回收站」，
   此时 SHFileOperationW 带 FOF_ALLOWUNDO 也会**静默硬删**、不报错。
   `verify` 就是用来抓它的 —— 硬删之后回收站里查不到记录，verify 必 FAIL。
   因此「trash 成功」绝不能当作「已进回收站」，**只有 verify 通过才算**。
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

__version__ = "1.0.0"


# --------------------------------------------------------------------------
# 基础：哈希 / 扫描 / 对账
# --------------------------------------------------------------------------
def digest(path, _buf=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_buf), b""):
            h.update(chunk)
    return h.hexdigest()


def scan(root):
    """返回 {相对路径(posix): {'sha':..., 'size':...}}。"""
    root = os.path.abspath(root)
    out = {}
    for dp, _dns, fns in os.walk(root):
        for fn in sorted(fns):
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            out[rel] = {"sha": digest(full), "size": os.path.getsize(full)}
    return out


def reconcile(src, dst):
    """按相对路径逐文件比对。返回 (ok, diffs)。"""
    a, b = scan(src), scan(dst)
    diffs = []
    for k in sorted(set(a) - set(b)):
        diffs.append("目标缺文件: %s" % k)
    for k in sorted(set(b) - set(a)):
        diffs.append("目标多文件: %s" % k)
    for k in sorted(set(a) & set(b)):
        if a[k]["sha"] != b[k]["sha"]:
            diffs.append(
                "内容不一致: %s (size %d -> %d, sha %s / %s)"
                % (k, a[k]["size"], b[k]["size"], a[k]["sha"][:12], b[k]["sha"][:12])
            )
    return (not diffs), diffs


# --------------------------------------------------------------------------
# 回收站：$I 记录解析 / 索引 / 匹配
# --------------------------------------------------------------------------
def parse_I(blob):
    """解析 Windows 回收站 $I 索引记录（**布局已对真实记录校准，非推测**）。

    实测布局（version >= 2，Vista 起）：
      [0:4)   版本（=2）
      [4:8)   保留（=0）
      [8:16)  文件字节数（u64 小端）
      [16:24) 删除时间（FILETIME）
      [24:28) 路径长度（u32 小端，单位 UTF-16 码元，**含结尾 NUL**）
      [28:)   原路径（UTF-16LE，以 NUL 结尾）

    ⚠️ 路径起于 **28** 而非 24 —— 24 处那 4 字节是长度字段。把它当路径起点会解出
    一个乱码首字符（实测 'r' / '+'），而记录仍「像是解出来了」⇒ **静默错配**，
    比直接抛错更坏。校准证据：真实记录 [24:28)=43，恰等于其 42 字符路径 + 1（NUL）。
    """
    if len(blob) < 28:
        raise ValueError("$I 记录过短：%d 字节" % len(blob))
    ver = int.from_bytes(blob[0:4], "little")
    size = int.from_bytes(blob[8:16], "little")
    if ver >= 2:
        n = int.from_bytes(blob[24:28], "little")
        raw = blob[28:]
        if n >= 1 and (n - 1) * 2 <= len(raw):
            raw = raw[: (n - 1) * 2]
        path = raw.decode("utf-16-le", "replace").split("\x00")[0]
    else:
        # version 1：路径是 ANSI，且长度字段位置不同 —— 只做尽力解析。
        enc = "mbcs" if os.name == "nt" else "latin-1"
        path = blob[24:].decode(enc, "replace").split("\x00")[0]
    return {"version": ver, "size": size, "path": path}


def make_I_bytes(path, size, version=2):
    """合成一条 $I 记录字节（**按上面校准过的真实布局**；供自测，不写盘）。"""
    if version < 2:
        raise ValueError("仅支持合成 v2 记录")
    head = version.to_bytes(4, "little") + (0).to_bytes(4, "little")
    head += int(size).to_bytes(8, "little") + (0).to_bytes(8, "little")
    head += (len(path) + 1).to_bytes(4, "little")  # 长度字段：字符数 + NUL
    return head + path.encode("utf-16-le") + b"\x00\x00"


def _norm(p):
    return os.path.normcase(os.path.normpath(p))


def _bin_root(drive):
    return os.path.join(drive + os.sep, "$Recycle.Bin")


def bin_records(drive, want_norm=None, since=None):
    """枚举某盘回收站条目 -> [{'path','size','i','r'}]。

    * 跳过无权读取的 SID 目录 —— 同盘回收站里常有 SYSTEM（S-1-5-18）或别的账户的
      目录，当前用户读不了，这是常态，不是错误。
    * 按 $I 的 mtime **降序**（最新在前）：刚被送进回收站的文件排在最前；配合
      `want_norm` 可在全部命中后**立即提前退出**，不必为两个文件读完十几万条记录
      （本机 C: 实测 10 万+ 条）。
    * `want_norm`：只要这些（已 normcase）路径对应的记录；None = 全要。
    """
    root = _bin_root(drive)
    if not os.path.isdir(root):
        return []
    try:
        sids = os.listdir(root)
    except OSError:
        return []
    ents = []
    for sid in sids:
        d = os.path.join(root, sid)
        if not os.path.isdir(d):
            continue
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                if not e.name.startswith("$I"):
                    continue
                try:
                    ents.append((e.stat().st_mtime, e.path, d, e.name))
                except OSError:
                    continue
    ents.sort(key=lambda t: t[0], reverse=True)
    out = []
    for mt, ipath, d, name in ents:
        if since is not None and mt < since:
            break
        try:
            with open(ipath, "rb") as f:
                rec = parse_I(f.read())
        except Exception:
            continue
        rec["i"] = ipath
        rec["r"] = os.path.join(d, "$R" + name[2:])
        if want_norm is None:
            out.append(rec)
        elif _norm(rec["path"]) in want_norm:
            out.append(rec)
            if len(out) >= len(want_norm):
                break
    return out


def index_bin(drive, want_norm=None, since=None):
    """-> {normcase(原路径): {'size':.., 'i':.., 'r':..}}"""
    idx = {}
    for rec in bin_records(drive, want_norm=want_norm, since=since):
        if rec.get("path"):
            idx[_norm(rec["path"])] = rec
    return idx


def check_bin(expected, idx):
    """expected: {绝对路径: 字节数}。返回 (missing, bad)。"""
    missing, bad = [], []
    for path, size in expected.items():
        rec = idx.get(_norm(path))
        if rec is None:
            missing.append(path)
        elif int(rec["size"]) != int(size):
            bad.append((path, int(size), int(rec["size"])))
    return missing, bad


# --------------------------------------------------------------------------
# 送回收站（绝不静默降级成 rm）
# --------------------------------------------------------------------------
def _win_trash(path):
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 3
    FOF_SILENT, FOF_NOCONFIRMATION = 0x0004, 0x0010
    FOF_ALLOWUNDO, FOF_NOERRORUI, FOF_WANTNUKEWARNING = 0x0040, 0x0400, 0x4000

    p = os.path.abspath(path)
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.fFlags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI | FOF_WANTNUKEWARNING
    buf = ctypes.create_unicode_buffer(p, len(p) + 2)  # 双 NUL 结尾
    op.pFrom = ctypes.cast(buf, wintypes.LPCWSTR)
    op.pTo = None
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0:
        raise OSError("SHFileOperationW 返回 %d（未进回收站）" % rc)
    if op.fAnyOperationsAborted:
        raise OSError("SHFileOperationW 被中止")


def _posix_trash(path):
    try:
        import send2trash  # type: ignore
        send2trash.send2trash(path)
        return
    except ImportError:
        pass
    for cmd in (["gio", "trash", "--", path], ["trash-put", "--", path]):
        try:
            r = subprocess.run(cmd, capture_output=True)
            if r.returncode == 0:
                return
        except FileNotFoundError:
            continue
    raise OSError(
        "本机没有可用的回收站机制（试过 send2trash / gio trash / trash-put）。"
        "**不降级硬删** —— 请安装其一，或明确授权改用硬删。"
    )


def send_to_trash(path):
    if os.name == "nt":
        return _win_trash(path)
    return _posix_trash(path)


def trash_tree(root):
    """把 root 下**每个文件**分别送回收站，再清掉空目录骨架。

    逐文件（而不是整目录一次性）的理由：每条 $I 记录带**原路径 + 字节数**，
    于是 verify 能做「逐文件配对核对」，证据强度远高于「整目录在不在」。
    代价：用户在回收站里看到的是 N 个条目而非 1 个文件夹（可随时整体还原）。
    """
    root = os.path.abspath(root)
    files = []
    for dp, _dns, fns in os.walk(root):
        for fn in fns:
            files.append(os.path.join(dp, fn))
    for f in files:
        send_to_trash(f)
    for dp, dns, _fns in sorted(os.walk(root, topdown=False)):
        for d in dns:
            p = os.path.join(dp, d)
            try:
                os.rmdir(p)
            except OSError:
                pass
    try:
        os.rmdir(root)
    except OSError:
        pass
    return len(files)


def restore_paths(paths, drive):
    """按原路径从回收站把文件搬回去（用于 --live 自测的清理）。返回 (ok, failed)。"""
    idx = index_bin(drive, want_norm={_norm(p) for p in paths})
    ok, failed = [], []
    for p in paths:
        rec = idx.get(_norm(p))
        if rec is None or not os.path.exists(rec["r"]):
            failed.append(p)
            continue
        try:
            os.makedirs(os.path.dirname(os.path.abspath(p)) or ".", exist_ok=True)
            shutil.move(rec["r"], p)
            try:
                os.remove(rec["i"])
            except OSError:
                pass
            ok.append(p)
        except Exception as e:  # noqa: BLE001
            failed.append("%s (%s)" % (p, e))
    return ok, failed


# --------------------------------------------------------------------------
# 基线 IO
# --------------------------------------------------------------------------
def write_baseline(path, src, files):
    payload = {
        "src": os.path.abspath(src),
        "count": len(files),
        "total_bytes": sum(v["size"] for v in files.values()),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": files,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


def read_baseline(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------
def cmd_plan(a):
    print("[plan] 清点源：%s" % a.src)
    files = scan(a.src)
    print("  文件数 %d ｜ 总字节 %d" % (len(files), sum(v["size"] for v in files.values())))
    for k in sorted(files):
        print("    %10d  %s" % (files[k]["size"], k))
    if a.baseline:
        payload = write_baseline(a.baseline, a.src, files)
        print("[plan] 基线已落盘：%s（count=%d）" % (a.baseline, payload["count"]))
    print("[plan] 下一步：run 复制并对账 → trash 送回收站 → verify 独立核实")
    return 0


def cmd_run(a):
    print("[run] 复制 %s -> %s（源保持不动）" % (a.src, a.dst))
    files = scan(a.src)
    if a.baseline:
        write_baseline(a.baseline, a.src, files)
        print("[run] 基线已落盘：%s" % a.baseline)
    shutil.copytree(a.src, a.dst, dirs_exist_ok=True)
    ok, diffs = reconcile(a.src, a.dst)
    if not ok:
        print("[run][FAIL] 对账不通过（**不要进入 trash**）：")
        for d in diffs[:20]:
            print("   - %s" % d)
        return 2
    print("[run][ OK ] 逐文件对账通过：%d 文件，sha256 与字节数逐一相等" % len(files))
    return 0


def cmd_trash(a):
    n = trash_tree(a.src)
    print("[trash] 已把 %d 个文件送回收站：%s" % (n, a.src))
    print("[trash] ⚠️ 这**不算完成** —— 必须再跑 verify 独立核实（抓「卷未启用回收站=静默硬删」）")
    return 0


def cmd_verify(a):
    src_abs = os.path.abspath(a.src)
    base = read_baseline(a.baseline)
    if os.path.exists(src_abs):
        print("[verify][FAIL] 源仍存在，搬运未完成：%s" % src_abs)
        return 2
    expected = {
        os.path.join(base["src"], rel.replace("/", os.sep)): meta["size"]
        for rel, meta in base["files"].items()
    }
    drive = os.path.splitdrive(os.path.abspath(base["src"]))[0] + os.sep
    want = {_norm(p) for p in expected}
    t0 = time.time()
    idx = index_bin(drive, want_norm=want)
    missing, bad = check_bin(expected, idx)
    print("[verify] 盘 %s：按期望路径定向扫描回收站，命中 %d / %d 条（耗时 %.1fs）"
          % (drive, len(idx), len(want), time.time() - t0))
    if missing or bad:
        print("[verify][FAIL] 未能证明可还原：缺 %d 项、字节数不符 %d 项" % (len(missing), len(bad)))
        for m in missing[:10]:
            print("   - 回收站里找不到: %s" % m)
        for p, want, got in bad[:10]:
            print("   - 字节数不符: %s（期望 %d / 实际 %d）" % (p, want, got))
        print("[verify] 若「缺」为全部项 ⇒ 极可能是该卷被配置为不使用回收站（fail-open 静默硬删）")
        return 2
    print("[verify][ OK ] %d/%d 项按「原路径 + 字节数」在回收站中配对成功 ⇒ 可还原" % (len(expected), len(expected)))
    return 0


# --------------------------------------------------------------------------
# 自测（判据必须非恒真）
# --------------------------------------------------------------------------
def _check(name, cond):
    print("   [%s] %s" % (" OK " if cond else "FAIL", name))
    return bool(cond)


def cmd_selftest(a):
    allok = True
    print("[selftest] ① 对账判据（reconcile）")
    with tempfile.TemporaryDirectory() as t:
        src, dst = os.path.join(t, "src"), os.path.join(t, "dst")
        os.makedirs(os.path.join(src, "sub"))
        with open(os.path.join(src, "a.txt"), "w", encoding="utf-8") as f:
            f.write("hello")
        with open(os.path.join(src, "sub", "b.txt"), "w", encoding="utf-8") as f:
            f.write("world")
        shutil.copytree(src, dst)
        ok, _ = reconcile(src, dst)
        allok &= _check("阴性对照：完全一致 ⇒ 判「一致」（非恒 FAIL）", ok)
        with open(os.path.join(dst, "sub", "b.txt"), "w", encoding="utf-8") as f:
            f.write("worlD")  # 改 1 字节
        ok, diffs = reconcile(src, dst)
        allok &= _check("阳性对照：改 1 字节 ⇒ 必须抓到「内容不一致」", (not ok) and any("内容不一致" in d for d in diffs))
        shutil.copytree(src, dst, dirs_exist_ok=True)
        os.remove(os.path.join(dst, "a.txt"))
        ok, diffs = reconcile(src, dst)
        allok &= _check("阳性对照：少一个文件 ⇒ 必须抓到「目标缺文件」", (not ok) and any("目标缺文件" in d for d in diffs))

    print("[selftest] ② $I 解析判据（parse_I；夹具按**真实布局**构造，见 make_I_bytes）")
    want_path = r"E:\some\path\file.txt"
    blob = make_I_bytes(want_path, 1234)
    rec = parse_I(blob)
    allok &= _check("版本解析 = 2", rec["version"] == 2)
    allok &= _check("字节数解析 = 1234", rec["size"] == 1234)
    allok &= _check("路径**精确**还原（含盘符与分隔符）", rec["path"] == want_path)
    allok &= _check("长度字段在 [24:28) 且 = 字符数 + 1（NUL）",
                    int.from_bytes(blob[24:28], "little") == len(want_path) + 1)
    # 防回归守卫：路径**不在** offset 24 —— 24 处是长度字段，写成 24 会解出乱码首字符
    at24 = blob[24:].decode("utf-16-le", "replace").split("\x00")[0]
    allok &= _check("防回归守卫：offset 24 解出的不是正确路径", at24 != want_path)
    try:
        parse_I(b"\x00" * 10)
        allok &= _check("过短记录必须抛错", False)
    except ValueError:
        allok &= _check("过短记录必须抛错", True)

    print("[selftest] ③ 回收站匹配判据（check_bin）")
    idx = {_norm(r"E:\a\one.bin"): {"size": 10, "i": "x", "r": "y"},
           _norm(r"E:\a\two.bin"): {"size": 20, "i": "x", "r": "y"}}
    miss, bad = check_bin({r"E:\a\one.bin": 10, r"E:\a\two.bin": 20}, idx)
    allok &= _check("阴性对照：全匹配 ⇒ 零缺零错（非恒 FAIL）", not miss and not bad)
    miss, bad = check_bin({r"E:\a\one.bin": 10, r"E:\a\three.bin": 30}, idx)
    allok &= _check("阳性对照：路径不在回收站 ⇒ 必进 missing", miss == [r"E:\a\three.bin"])
    miss, bad = check_bin({r"E:\a\one.bin": 999}, idx)
    allok &= _check("阳性对照：字节数不符 ⇒ 必进 bad", (not miss) and len(bad) == 1 and bad[0][1] == 999)
    allok &= _check("大小写/分隔符归一（normcase+normpath）", _norm(r"E:\A\ONE.BIN") == _norm(r"E:\a\one.bin"))

    if a.live:
        print("[selftest] ④ 端到端往返（真送回收站 → 真核实 → 真还原）")
        if os.name != "nt":
            allok &= _check("--live 仅实现于 Windows（本机 %s）" % os.name, False)
        else:
            with tempfile.TemporaryDirectory() as t:
                d = os.path.join(t, "live")
                os.makedirs(os.path.join(d, "n"))
                for rel in ("p.txt", "n/q.txt"):
                    with open(os.path.join(d, *rel.split("/")), "w", encoding="utf-8") as f:
                        f.write("live-" + rel)
                base = scan(d)
                paths = [os.path.join(d, r.replace("/", os.sep)) for r in base]
                drive = os.path.splitdrive(os.path.abspath(d))[0] + os.sep
                trash_tree(d)
                allok &= _check("源已清空", not os.path.exists(d))
                exp = dict(zip(paths, [m["size"] for m in base.values()]))
                idx = index_bin(drive, want_norm={_norm(p) for p in paths})
                miss, bad = check_bin(exp, idx)
                allok &= _check("独立核实：全部项在回收站配对成功（读的是**真实**系统记录）",
                                not miss and not bad)
                ok, failed = restore_paths(paths, drive)
                allok &= _check("还原回原路径", not failed)
                allok &= _check("还原后内容与基线逐文件相等", scan(d) == base)
    print("[selftest] %s" % ("全部通过" if allok else "存在 FAIL"))
    return 0 if allok else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # 兼容旗标写法：`--selftest [--live]` ≡ 子命令 `selftest [--live]`
    #   （SKILL.md 与本仓其它工具用 `--selftest`，故两种写法都接受）
    if "--selftest" in argv:
        argv = ["selftest" if _a == "--selftest" else _a for _a in argv]
    p = argparse.ArgumentParser(description="可证伪的文件搬运（复制→对账→送回收站→独立核实）")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="清点源、落基线、打印清单（只读）")
    sp.add_argument("src"); sp.add_argument("dst", nargs="?")
    sp.add_argument("--baseline"); sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("run", help="复制 + 逐文件对账（不删源）")
    sp.add_argument("src"); sp.add_argument("dst")
    sp.add_argument("--baseline"); sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("trash", help="把源下每个文件送回收站")
    sp.add_argument("src"); sp.set_defaults(func=cmd_trash)

    sp = sub.add_parser("verify", help="独立核实回收站可还原性")
    sp.add_argument("src"); sp.add_argument("--baseline", required=True)
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("selftest", help="自测（判据非恒真）")
    sp.add_argument("--live", action="store_true", help="额外做真删/真还原的端到端往返")
    sp.set_defaults(func=cmd_selftest)

    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
