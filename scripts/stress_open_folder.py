# -*- coding: utf-8 -*-
r"""「打开所在文件夹」的两层压测：命令行往返 + 真机落点。

用户反馈「经常打开成错误的文件夹或者打开失败」。这个功能的坑全在 Windows 的命令行
与 shell 语义上，普通单测覆盖不到，所以单独做一个可复跑的压测。

    A 层  不启动任何进程。用 Windows 自己的 CommandLineToArgvW 当裁判，对上万条
          对抗性路径（特殊字符 / 各语种 / emoji / 组合字符 / 长度扫描 / 随机 fuzz）
          验证「explorer 拿到的参数」必须逐字等于我们要传的路径。
    B 层  真起资源管理器，用 Shell.Application 读回它**实际停在哪个文件夹**，
          判据不是函数返回 True。只关闭本脚本自己开出来的窗口。

用法：
    uv run python scripts/stress_open_folder.py            # 两层都跑
    uv run python scripts/stress_open_folder.py --quick    # 只跑 A 层（不开窗口）

历史（2026-08-31，v1.5.2 之后）：A 层 62,177 条挖出 5 条尾部反斜杠往返失败，顺着
它真机实测发现盘符根 `C:\` 的三个反直觉事实——见 actions.open_drive_root 的注释。
"""
from __future__ import annotations

import ctypes
import itertools
import os
import random
import shutil
import subprocess
import sys
import time
import urllib.parse
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pptx_finder import actions  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

_shell32 = ctypes.windll.shell32
_kernel32 = ctypes.windll.kernel32
_shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
_shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]

BASE = ROOT / "artifacts" / "stress-open-folder"
SETTLE = 1.6


# ======================= A 层 =======================
def argv_of(cmdline: str) -> list[str]:
    n = ctypes.c_int(0)
    p = _shell32.CommandLineToArgvW(cmdline, ctypes.byref(n))
    if not p:
        raise OSError("CommandLineToArgvW failed")
    try:
        return [p[i] for i in range(n.value)]
    finally:
        _kernel32.LocalFree(p)


SPECIALS = list(" ,&^%!#$()[]{}+=~'`;@-_.’“”、，。（）《》…—")
WORDS = [
    "PPT Doctor", "季度 汇报", "a & b", "comma,dir", "100% 完成", "R&D",
    "v1.5.2 (final)", "!important", "#标签", "$成本", "报告^副本", "~$临时",
    "café", "naïve", "résumé", "北京 2026", "日本語 フォルダ", "한국어",
    "Ελληνικά", "Русский", "🎉庆功", "𝕏平台", "a" * 120, "很长的中文目录名" * 12,
    "dots...", "%TEMP%", "%USERPROFILE%", "$(whoami)", "`cmd`",
]
PATH_ROOTS = [
    r"C:\Users\me\Desktop", r"C:\Program Files (x86)", r"D:\某个 目录",
    "\\\\server\\share", "\\\\192.168.1.10\\公共 盘\\子目录", "C:\\", "D:\\",
]
LEAVES = ["deck.pptx", "无扩展名", "报告 v2.pptx", "a,b.docx", "x&y.pdf",
          "文件" + "名" * 100 + ".pptx", ".hidden"]

_ILLEGAL = set('<>:"/\\|?*') | {chr(i) for i in range(32)}
_ALPHABET = [c for c in (
    [chr(i) for i in range(32, 127)]
    + list("中文测试报告演示汇报方案季度年度项目")
    + list("あいうえおカタカナ한국어ЖДЛЭΩλβ")
    + list("éèêëàçñüößåøæ")
    + ["🎉", "📊", "𝕏", "👨‍👩‍👧", "🇨🇳"]
    + ["\u0301", "\u200b", "\u200e", "\u202e", "\ufeff"]
    + ["'", "’", "“", "”", "，", "。", "（", "）", "　"]
) if c not in _ILLEGAL]


def _fuzz(rounds: int, seed: int = 20260831):
    rnd = random.Random(seed)
    drives = ["C:\\", "D:\\", "E:\\", "\\\\srv\\sh\\", "\\\\10.0.0.1\\共享\\"]
    for _ in range(rounds):
        segs = []
        for _ in range(rnd.randint(1, 5)):
            seg = "".join(rnd.choice(_ALPHABET) for _ in range(rnd.randint(1, 24)))
            segs.append(seg.strip() or "x")
        yield rnd.choice(drives) + "\\".join(segs)


def _corpus(fuzz_rounds: int):
    for c in SPECIALS:
        for tpl in (r"C:\dir{0}name\deck.pptx", r"C:\a\b{0}c", r"C:\{0}\x.pptx"):
            yield tpl.format(c)
    for r, w, leaf in itertools.product(PATH_ROOTS, WORDS, LEAVES):
        yield f"{r}\\{w}\\{leaf}"
    for p in ["C:\\", "D:\\", r"C:\dir\\", "\\\\server\\share\\", "C:",
              "\\\\server", "\\\\server\\share"]:
        yield p
    for n in (200, 250, 255, 258, 259, 260, 261, 300, 400):
        pad = "x" * max(1, n - 20)
        yield f"C:\\stress\\{pad}\\deck.pptx"
    yield from _fuzz(fuzz_rounds)


def layer_a(fuzz_rounds: int = 60_000) -> int:
    total = bad = roots = 0
    failures = []
    for raw in _corpus(fuzz_rounds):
        total += 1
        # 复刻 open_folder 的真实流程：先归一化，再判断是不是根路径
        target = os.path.normpath(os.path.abspath(raw))
        if actions.is_path_root(target):
            roots += 1          # 根路径由 open_drive_root 接管，不进 /select
            continue
        if target.endswith("\\"):
            bad += 1
            failures.append((target, "-", "归一化后仍带尾部反斜杠却不是根路径"))
            continue
        cmd = actions.explorer_select_command(target)
        try:
            argv = argv_of(cmd)
        except OSError as e:
            bad += 1
            failures.append((target, cmd, f"解析失败 {e}"))
            continue
        # explorer 拿到的是两个引号之间的原文；对不含尾部反斜杠的路径，这与
        # CommandLineToArgvW 的结果一致，所以可以拿后者当裁判。
        verbatim = cmd.split("/select,", 1)[1]
        if len(argv) != 2 or argv[1] != "/select," + target or verbatim != f'"{target}"':
            bad += 1
            if len(failures) < 20:
                failures.append((target, cmd, f"argv={argv!r} 原文={verbatim!r}"))
    print(f"A 层：{total} 条（根路径 {roots} 条走分流），不一致 {bad} 条")
    for raw, cmd, why in failures[:20]:
        print(f"\n  路径 : {raw!r}\n  命令 : {cmd}\n  实得 : {why}")
    return bad


# ======================= B 层 =======================
def _windows() -> dict[int, str]:
    """{hwnd: 真实路径}。读 Folder.Self.Path 而不是解析 LocationURL——URL 形式对
    UNC 是 `file://localhost/C$/...`，按本地路径切片会把主机名切掉。"""
    import win32com.client

    out: dict[int, str] = {}
    try:
        for w in win32com.client.Dispatch("Shell.Application").Windows():
            try:
                hwnd = int(w.HWND)
            except Exception:  # noqa: BLE001
                continue
            try:
                path = str(w.Document.Folder.Self.Path or "")
            except Exception:  # noqa: BLE001
                try:
                    url = str(w.LocationURL or "")
                    path = (urllib.parse.unquote(url[8:]).replace("/", "\\")
                            if url.lower().startswith("file:") else "")
                except Exception:  # noqa: BLE001
                    path = ""
            if path:
                out[hwnd] = path
    except Exception as e:  # noqa: BLE001
        print("  !! 读窗口失败:", e)
    return out


def _close(hwnds) -> None:
    if not hwnds:
        return
    import win32com.client

    for w in list(win32com.client.Dispatch("Shell.Application").Windows()):
        try:
            if int(w.HWND) in hwnds:
                w.Quit()
        except Exception:  # noqa: BLE001
            pass


def _norm(p: str) -> str:
    return os.path.normcase(str(p).rstrip("\\")) or os.path.normcase(str(p))


B_NAMES = [
    "PPT Doctor", "comma,dir", "a & b", "100% 完成", "R&D 报告 (2026)",
    "!important #标签", "$成本 ^副本", "v1.5.2 [final] {v2}", "it's a 'test'",
    "back`tick;semi@at", "plus+equal=tilde~", "季度 汇报 2026", "日本語 フォルダ",
    "한국어 폴더", "Русская папка", "Ελληνικά", "café naïve résumé",
    "e\u0301 组合音标", "🎉庆功 📊数据", "%TEMP% 不该被展开", "~$看着像临时文件",
    "　全角空格", "很长的中文目录名" * 14,
]


def _build_tree():
    if BASE.exists():
        shutil.rmtree(BASE, ignore_errors=True)
    BASE.mkdir(parents=True, exist_ok=True)
    cases = []
    for i, name in enumerate(B_NAMES):
        try:
            d = BASE / name
            d.mkdir(parents=True, exist_ok=True)
            f = d / LEAVES[i % len(LEAVES)]
            f.write_bytes(b"x")
        except OSError as e:
            print(f"  (跳过 {name[:24]!r}: {e})")
            continue
        cases.append((f"名字: {name[:26]}", str(f), str(d), True))

    cases.append(("目标是目录本身", str(BASE / "PPT Doctor"), str(BASE), True))

    deep = BASE / "深" / "层" / "嵌 套" / "d4" / "d5" / "d6 & 7"
    deep.mkdir(parents=True, exist_ok=True)
    (deep / "deck.pptx").write_bytes(b"x")
    cases.append(("深层嵌套 6 级", str(deep / "deck.pptx"), str(deep), True))

    attrs = BASE / "attrs"
    attrs.mkdir(exist_ok=True)
    for label, flag, leaf in [("隐藏文件", "+h", "hidden.pptx"),
                              ("只读文件", "+r", "readonly.pptx")]:
        p = attrs / leaf
        p.write_bytes(b"x")
        subprocess.run(["attrib", flag, str(p)], capture_output=True)
        cases.append((label, str(p), str(attrs), True))

    tgt = BASE / "junction 目标"
    tgt.mkdir(exist_ok=True)
    (tgt / "inside.pptx").write_bytes(b"x")
    link = BASE / "junction 链接"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(tgt)],
                   capture_output=True, text=True)
    if link.exists():
        cases.append(("junction 里的文件", str(link / "inside.pptx"), str(link), True))

    cases.append(("盘符根 C:\\", "C:\\", "C:\\", True))
    unc = "\\\\localhost\\C$\\Windows"
    if os.path.isdir(unc):
        cases.append(("UNC 目录", unc, "\\\\localhost\\C$", True))
        cases.append(("UNC 共享根", "\\\\localhost\\C$\\", "\\\\localhost\\C$", True))
    else:
        print("  (跳过 UNC：\\\\localhost\\C$ 不可达)")

    gone = BASE / "父目录还在"
    gone.mkdir(exist_ok=True)
    cases.append(("文件已删，父目录在", str(gone / "没有这个.pptx"), str(gone), True))
    cases.append(("父目录也不存在", str(BASE / "根本没有" / "x.pptx"), "", False))

    # 超 MAX_PATH：期望**优雅失败**。系统 LongPathsEnabled 默认为 0，资源管理器
    # 自己也定位不了这种路径（/select 裸路径、直接打开目录、加 \\?\ 前缀三种
    # 写法实测全部回落到「桌面」/「文档」），所以返回 False 让 UI 提示「找不到」
    # 才是对的，比打开一个错目录强。
    long_dir = BASE / ("L" * 60) / ("O" * 60) / ("N" * 60) / ("G" * 60)
    try:
        os.makedirs("\\\\?\\" + str(long_dir), exist_ok=True)
        lf = str(long_dir / "deck.pptx")
        with open("\\\\?\\" + lf, "wb") as fh:
            fh.write(b"x")
        cases.append((f"超 MAX_PATH ({len(lf)} 字符，应优雅失败)", lf, "", False))
    except OSError as e:
        print("  (跳过超长路径:", e, ")")
    return cases


def layer_b() -> int:
    print("造语料…")
    cases = _build_tree()
    print(f"共 {len(cases)} 条用例\n")
    ok = fail = 0
    for label, path, want, want_ret in cases:
        before = _windows()
        t0 = time.perf_counter()
        try:
            ret, err = actions.open_folder(path), ""
        except Exception as e:  # noqa: BLE001 压测就是要抓异常
            ret, err = None, f"{type(e).__name__}: {e}"
        ms = (time.perf_counter() - t0) * 1000
        if err or ret != want_ret:
            print(f"  失败   {ms:7.1f} ms  {label:34s} "
                  f"{err or f'返回 {ret}，期望 {want_ret}'}")
            fail += 1
            continue
        if not want_ret:
            time.sleep(0.4)
            new = set(_windows()) - set(before)
            if new:
                print(f"  失败   {ms:7.1f} ms  {label:34s} 不该开窗口")
                fail += 1
                _close(new)
            else:
                print(f"  OK    {ms:7.1f} ms  {label}")
                ok += 1
            continue
        time.sleep(SETTLE)
        after = _windows()
        new = {h: p for h, p in after.items() if h not in before}
        changed = [p for h, p in after.items()
                   if h in before and _norm(before[h]) != _norm(p)]
        if any(_norm(p) == _norm(want) for p in list(new.values()) + changed):
            print(f"  OK    {ms:7.1f} ms  {label}")
            ok += 1
        else:
            print(f"  落点错 {ms:7.1f} ms  {label:34s} "
                  f"期望 {want} | 新窗口 {list(new.values())} | 变更 {changed}")
            fail += 1
        _close(set(new))
        time.sleep(0.25)

    # 连点爆发：用户双击 / 狂点时不能抛异常
    f = BASE / "PPT Doctor" / LEAVES[0]
    if f.exists():
        before = _windows()
        errs = 0
        t0 = time.perf_counter()
        for _ in range(40):
            try:
                actions.open_folder(str(f))
            except Exception:  # noqa: BLE001
                errs += 1
        ms = (time.perf_counter() - t0) * 1000
        time.sleep(2.5)
        new = set(_windows()) - set(before)
        _close(new)
        print(f"\n40 次连点：异常 {errs} 次，{ms:.0f} ms（{ms/40:.1f} ms/次），"
              f"开出 {len(new)} 个窗口（已关）")
        if errs:
            fail += errs

    _cleanup()
    print(f"\nB 层：通过 {ok}，失败 {fail}")
    return fail


def _cleanup() -> None:
    link = BASE / "junction 链接"
    if link.exists():
        subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)
    subprocess.run(["attrib", "-r", "-h", "-s", str(BASE / "attrs" / "*"), "/S"],
                   capture_output=True)
    shutil.rmtree(BASE, ignore_errors=True)
    if BASE.exists():
        subprocess.run(["cmd", "/c", "rmdir", "/S", "/Q", str(BASE)], capture_output=True)


def main() -> int:
    quick = "--quick" in sys.argv
    bad = layer_a(6_000 if quick else 60_000)
    if quick:
        print("\n(--quick：跳过 B 层真机验证)")
        return 1 if bad else 0
    print()
    bad += layer_b()
    print("\n" + ("全部通过" if bad == 0 else f"共 {bad} 处失败"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
