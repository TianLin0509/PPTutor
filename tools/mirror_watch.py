# -*- coding: utf-8 -*-
r"""定时任务：GitHub 上构建出新版本后，自动转到公司内源（PPT Doctor 项目 8214）。

AI HUB-H 铁律（2026-11-25 田哥定）：只在公司内源对用户发布。GitHub 只负责构建（发成「预发布、
不设为最新」），阿里云备用服务器只作中转（放增量块和安装包，不再切换三份清单）。

为什么由家里电脑转：服务器和公司电脑直连 GitHub 都很慢（实测十几到几十 KB/s），家里电脑下 GitHub 快、
传阿里云也稳；公司电脑再从阿里云就近取文件上传内源。

每次运行：
    ① 按版本号找 GitHub 上最新的非草稿发布（含预发布）；
    ② 本版增量块还没放到备用服务器 → 调 mirror_release.py <版本> --blocks-only（清单不动）；
    ③ 块已就位、内源还没这一版 → openx_stage.py 放安装包，再调 openx_sync.py 上传内源。
失败不重试轰炸：同一版本连续失败 3 次后停手，等人看日志（状态文件里写着原因）。

    uv run --frozen --with paramiko python tools/mirror_watch.py   # 计划任务每 15 分钟跑一次（--frozen：别让本机镜像源配置改写 uv.lock）
    uv run --with paramiko python tools/mirror_watch.py --status   # 看上次结果
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "TianLin0509/PPTutor"
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


_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def newest_release(releases) -> str:
    """按版本号取最新的非草稿发布（含预发布）。GitHub 的「最新」标记不再使用（铁律：不设为最新）。"""
    versions = [r["tag_name"][1:] for r in releases
                if not r.get("draft") and _TAG.match(str(r.get("tag_name") or ""))]
    if not versions:
        raise RuntimeError("GitHub 上没有可用的版本发布")
    return max(versions, key=vkey)


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


# 2026-11-23 田哥定：公司内源是第一更新源。AI-Hub-Huawei（7938）那份由 AI HUB 的发车任务每 15 分钟同步；
# PPT Doctor 自己的内源项目（8214）由这里同步。只在本机有内源同步工具时做；同一版本成功一次就不再跑。
OPENX_SYNC = Path(os.environ.get("PPTD_OPENX_SYNC", r"D:\DevTools\aihub-collab\openx_sync.py"))
OPENX_PROJECT = 8214


def sync_own_openx(st, latest):
    if st.get("openx_own") == latest or not OPENX_SYNC.is_file():
        return
    env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    env["PYTHONIOENCODING"] = "utf-8"
    log(f"同步 v{latest} 到 PPT Doctor 内源项目 {OPENX_PROJECT}")
    # 安装包先放到备用服务器 openx-stage/，公司电脑从那里取（直连 GitHub 太慢、200 MB 常超时）；放不上也照样同步，退回 GitHub
    with LOG.open("a", encoding="utf-8") as f:
        subprocess.run(["uv", "run", "--frozen", "--with", "paramiko", "python", "tools/openx_stage.py", latest],
                       cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, creationflags=FLAGS,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=1800)
    r = subprocess.run([sys.executable, "-X", "utf8", str(OPENX_SYNC), "--components", "--only", "ppt-doctor-update",
                        "--project-id", str(OPENX_PROJECT), "--doctor-version", latest, "--timeout", "3000"],
                       cwd=OPENX_SYNC.parent, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=FLAGS,
                       env=env, timeout=3600, text=True, encoding="utf-8", errors="replace")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(r.stdout or "")
    # 同步工具在「6 小时内失败过、先不重试」时也返回 0，但输出里带 pending：这种不算同步成功（2026-11-23 实测误记过一次）
    if r.returncode == 0 and '"pending"' not in (r.stdout or ""):
        st["openx_own"] = latest
        log(f"v{latest} 内源项目 {OPENX_PROJECT} 已同步")
    else:
        log(f"v{latest} 内源项目 {OPENX_PROJECT} 未完成，退出码 {r.returncode}（下一轮自动再试）")


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
        latest = newest_release(get_json(f"https://api.github.com/repos/{REPO}/releases?per_page=30"))
    except Exception as e:  # noqa: BLE001
        st.update(last_result=f"查询失败：{e}")
        save_state(st)
        log(f"查询失败：{e}")
        return 1
    st["github"] = latest
    if st.get("staged") != latest:
        fails = st.get("fails") or {}
        if fails.get(latest, 0) >= MAX_FAILS:
            st.update(last_result=f"v{latest} 已连续失败 {MAX_FAILS} 次，停手等人看日志：{LOG}")
            save_state(st)
            return 1
        log(f"发现新版 v{latest}，先把增量块放到备用服务器作中转（清单不动）")
        st.update(last_result=f"正在中转 v{latest}", started_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        save_state(st)
        start = time.monotonic()
        with LOG.open("a", encoding="utf-8") as f:
            r = subprocess.run(["uv", "run", "--frozen", "--with", "paramiko", "python", "tools/mirror_release.py",
                                latest, "--blocks-only"],
                               cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, creationflags=FLAGS,
                               env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        minutes = (time.monotonic() - start) / 60
        if r.returncode:
            fails[latest] = fails.get(latest, 0) + 1
            log(f"v{latest} 增量块中转失败（第 {fails[latest]} 次），退出码 {r.returncode}")
            st.update(last_result=f"v{latest} 中转失败（第 {fails[latest]} 次）", fails=fails)
            save_state(st)
            return r.returncode
        log(f"v{latest} 增量块已就位并通过公网核对，用时 {minutes:.1f} 分钟")
        st.update(staged=latest, fails={})
    st["last_result"] = f"v{latest} 增量块已就位"
    try:
        sync_own_openx(st, latest)
    except Exception as e:  # noqa: BLE001
        log(f"内源同步出错：{e}")
    if st.get("openx_own") == latest:
        st["last_result"] = "已同步内源"
    save_state(st)
    return 0

if __name__ == "__main__":
    sys.exit(main())
