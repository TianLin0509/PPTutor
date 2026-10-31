# -*- coding: utf-8 -*-
r"""定时任务：发现 GitHub 上有新版本，就自动转传到阿里云备用服务器。

为什么由一台家里电脑来转，而不是服务器自己去 GitHub 拉：2026-10-31 实测服务器从 GitHub
下载只有约 13 KB/s（6.6 MB 用了 508 秒），一个版本要好几个小时；家里电脑下 GitHub 快、
传阿里云也走过多次（tools/mirror_release.py）。这样谁发版都不需要服务器密码。

每次运行：
    ① 查 GitHub 最新正式版（不含草稿 / 预发布）；
    ② 读备用服务器上的 manifest-Base.json 版本；
    ③ GitHub 更新 → 调 mirror_release.py <版本> 转传并公网核对；同版本 → 什么都不做。
失败不重试轰炸：同一版本连续失败 3 次后停手，等人看日志（状态文件里写着原因）。

    uv run --with paramiko python tools/mirror_watch.py            # 计划任务每 15 分钟跑一次
    uv run --with paramiko python tools/mirror_watch.py --status   # 看上次结果
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "TianLin0509/PPTutor"
MIRROR_MANIFEST = "https://me.lt-stockpartner.tech/pptutor/manifest-Base.json"
STATE_DIR = Path(os.environ.get("PPTD_MIRROR_STATE", r"D:\VibeData\pptdoctor-mirror-watch"))
STATE = STATE_DIR / "state.json"
LOG = STATE_DIR / "mirror-watch.log"
MAX_FAILS = 3
FLAGS = (subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS) if os.name == "nt" else 0

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


def log(msg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def get_json(url, timeout=60):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "pptdoctor-mirror-watch"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def vkey(v):
    return tuple(int(x) for x in v.split("."))


def load_state():
    try:
        return json.loads(STATE.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    st["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), "utf-8")


def single_instance():
    """上一轮还在传（大版本要传半小时以上）时，这一轮直接让路。进程退出时锁自动释放。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(STATE_DIR / "run.lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return handle


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.status:
        print(json.dumps(load_state(), ensure_ascii=False, indent=2))
        return 0
    lock = single_instance()
    if lock is None:
        return 0
    st = load_state()
    try:
        latest = get_json(f"https://api.github.com/repos/{REPO}/releases/latest")["tag_name"].lstrip("v")
        mirror = get_json(MIRROR_MANIFEST)["version"]
    except Exception as e:  # noqa: BLE001
        st.update(last_result=f"查询失败：{e}")
        save_state(st)
        log(f"查询失败：{e}")
        return 1
    st.update(github=latest, mirror=mirror)
    if vkey(mirror) >= vkey(latest):
        st.update(last_result="已同步", fails={})
        save_state(st)
        return 0
    fails = st.get("fails") or {}
    if fails.get(latest, 0) >= MAX_FAILS:
        st.update(last_result=f"v{latest} 已连续失败 {MAX_FAILS} 次，停手等人看日志：{LOG}")
        save_state(st)
        return 1
    log(f"发现新版 v{latest}（备用服务器还是 v{mirror}），开始转传")
    st.update(last_result=f"正在转传 v{latest}", started_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    save_state(st)
    start = time.monotonic()
    with LOG.open("a", encoding="utf-8") as f:
        r = subprocess.run(["uv", "run", "--with", "paramiko", "python", "tools/mirror_release.py", latest],
                           cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, creationflags=FLAGS,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    minutes = (time.monotonic() - start) / 60
    if r.returncode == 0:
        log(f"v{latest} 转传完成并通过公网核对，用时 {minutes:.1f} 分钟")
        st.update(mirror=latest, last_result=f"v{latest} 已转传（{minutes:.1f} 分钟）", fails={})
    else:
        fails[latest] = fails.get(latest, 0) + 1
        log(f"v{latest} 转传失败（第 {fails[latest]} 次），退出码 {r.returncode}")
        st.update(last_result=f"v{latest} 转传失败（第 {fails[latest]} 次）", fails=fails)
    save_state(st)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
