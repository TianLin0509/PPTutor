"""frozen exe 端到端验证：包清单、单实例 IPC、实时索引和版本快照。

证明 root-scoped watcher、Qt 本地 IPC 和版本守护在 PyInstaller frozen 下完整工作。
DECKS 放 Desktop（显式配置为库根）——不能放 AppData/Temp（被排除）。
DATA(vault) 隔离在临时目录，不碰生产。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_EXE_ENV = os.environ.get("PPT_DOCTOR_EXE") or os.environ.get("PPTUTOR_EXE")
EXE = Path(_EXE_ENV) if _EXE_ENV else ROOT / "dist" / "PPT Doctor" / "PPT Doctor.exe"
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK
except Exception:  # noqa: BLE001
    pass

import fixtures_gen as fx  # noqa: E402

_data_tmp = Path(tempfile.mkdtemp(prefix="frozen_verify_"))
_run_id = f"{os.getpid()}-{int(time.time())}"
DATA = _data_tmp / "appdata"
DATA.mkdir(parents=True)
# Frozen smoke tests must not rewrite the user's real Windows Startup link to
# the temporary dist build before the release is installed.
(DATA / "ui.json").write_text(
    json.dumps({
        "autostart": False,
        # v1.0.12+ defaults new profiles to basic mode.  This probe is
        # specifically for the optional version watcher, so opt in explicitly.
        "version_management_enabled": True,
    }),
    encoding="utf-8",
)
# DECKS 放在 Desktop 并显式配置为唯一库根；不碰用户其它目录。
DECKS = Path(os.path.expanduser("~")) / "Desktop" / f"_pptxverify_decks_{_run_id}"
DECKS.mkdir(parents=True, exist_ok=True)
PPTX = DECKS / "测试方案.pptx"

ENV = dict(os.environ)
ENV["PPTX_FINDER_DATA_DIR"] = str(DATA)
ENV["PPTX_FINDER_ROOTS"] = str(DECKS)  # 扫描、watcher、live index、版本共享同一根
ENV["PPTX_FINDER_SINGLETON_NAME"] = f"ppt-doctor-frozen-verify-{_run_id}"


def _cleanup() -> None:
    shutil.rmtree(_data_tmp, ignore_errors=True)
    shutil.rmtree(DECKS, ignore_errors=True)


def count_versions() -> int:
    os.environ["PPTX_FINDER_DATA_DIR"] = str(DATA)
    from pptx_finder.versioning.manager import VersionManager
    m = VersionManager()
    n = len(m.list_versions(str(PPTX)))
    m.stop()
    return n


def package_runtime_files_ok() -> bool:
    names = {path.name.casefold() for path in EXE.parent.rglob("*") if path.is_file()}
    required = {
        "_ssl.pyd",
        "libssl-3.dll",
        "libcrypto-3.dll",
        "qt6network.dll",
        "qschannelbackend.dll",
    }
    forbidden = {"libssl-3-x64.dll", "libcrypto-3-x64.dll"}
    missing = sorted(required - names)
    unexpected = sorted(forbidden & names)
    ok = not missing and not unexpected
    print(f"层0 · TLS/QtNetwork 包清单 = {ok} missing={missing} unexpected={unexpected}")
    return ok


def main() -> int:
    if not EXE.exists():
        print("FAIL: exe 不存在", EXE)
        _cleanup()
        return 1
    package_ok = package_runtime_files_ok()

    # 预置：造 pptx + 源码建 v1
    fx.make_pptx(PPTX, [{"body": "封面 frozen 测试"}, {"body": "第二页 量子计算 章节"}])
    os.environ["PPTX_FINDER_DATA_DIR"] = str(DATA)
    from pptx_finder.versioning.manager import VersionManager
    pre = VersionManager()
    pre.catch_up_root(str(DECKS))
    n1 = len(pre.list_versions(str(PPTX)))
    pre.stop()
    print(f"预置：建 v1 后版本数 = {n1}（期望 1）")

    print(f"启动 frozen exe: {EXE.name} ...")
    proc = subprocess.Popen([str(EXE)], env=ENV)
    time.sleep(8)  # 等启动 + watcher observer 就绪
    alive = proc.poll() is None
    print(f"层1 · exe 启动 8s 后存活（不崩溃）= {alive}")
    if not alive:
        print(f"FAIL: exe 退出码 {proc.returncode} —— 启动即崩")
        _cleanup()
        return 1

    # 层2：同 singleton 名的第二实例应通过 QLocalSocket 激活首实例后快速退出。
    ipc_started = time.monotonic()
    try:
        second = subprocess.run([str(EXE)], env=ENV, timeout=5, check=False)
        ipc_elapsed = time.monotonic() - ipc_started
        ipc_ok = second.returncode == 0 and ipc_elapsed < 5 and proc.poll() is None
    except subprocess.TimeoutExpired:
        ipc_elapsed = time.monotonic() - ipc_started
        ipc_ok = False
    print(f"层2 · Qt 本地单实例 IPC = {ipc_ok} ({ipc_elapsed:.2f}s，首实例仍存活={proc.poll() is None})")

    # 层3：改存显式库根下 pptx → root-scoped watcher 应自动留版本
    time.sleep(1)
    fx.make_pptx(PPTX, [{"body": "封面 改稿"}, {"body": "第二页 经典计算"}, {"body": "新增第三页"}])
    print("已改存 pptx（显式库根内），等 watcher 防抖(1.5s)+快照 ...")
    time.sleep(6)

    proc.terminate()
    try:
        proc.wait(timeout=6)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    time.sleep(0.5)

    n2 = count_versions()
    print(f"层3 · 改存后版本数 = {n2}（期望 2 = root-scoped watcher 端到端工作）")

    ok = package_ok and alive and ipc_ok and n1 == 1 and n2 == 2
    print(f"\n=== frozen 发布候选验证：{'PASS' if ok else 'FAIL'} ===")
    _cleanup()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
