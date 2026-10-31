#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""发版的唯一入口：给主干当前版本打 vX.Y.Z 标签并推送，GitHub 上的 Release 流水线随即构建、自检、发布。

谁都可以发版（2026-10-31 起同事也直接维护主干）。本脚本只负责把「发哪一版」钉死：
    ① 主干干净、且与远端完全一致 —— 发出去的就是大家都看得到的那个提交；
    ② 版本号还没发过 —— 本地、远端标签、GitHub Release 三处都查；两人同时发同一版时，
       git 只接受先推上去的那个标签，后到的会被拒，不会出现两个同号的版本；
    ③ 发布说明写进带注释的标签正文，流水线从标签里读，第一行是标题。

用法（在主工作目录、主干上）：
    python scripts/release.py --title "一句话说清这版变化" --notes-file 说明.md
    python scripts/release.py --title "..." --notes "- 修复 xxx" --watch     # 推完等流水线跑完
    python scripts/release.py --check                                          # 只检查能不能发，不打标签

阿里云备用服务器由项目所有者电脑上的同步任务自动转传（tools/mirror_watch.py），发版人不用管。
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_SLUG = "TianLin0509/PPTutor"
FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


def run(*args, check=True):
    r = subprocess.run(list(args), cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", creationflags=FLAGS)
    if check and r.returncode:
        raise SystemExit(f"✗ {' '.join(args[:3])} 失败：{(r.stderr or r.stdout).strip()[:400]}")
    return r


def git(*args, check=True):
    return run("git", *args, check=check).stdout.strip()


def current_version():
    text = (ROOT / "src" / "pptx_finder" / "__init__.py").read_text("utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not m:
        raise SystemExit("✗ 读不到 src/pptx_finder/__init__.py 里的 __version__")
    return m.group(1)


def preflight():
    cfg = json.loads((ROOT / ".agents" / "project.json").read_text("utf-8"))
    trunk = cfg.get("trunk") or "master"
    if git("branch", "--show-current") != trunk:
        raise SystemExit(f"✗ 要在主干 {trunk} 上发版（先切过去，或在主工作目录跑）")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("✗ 主干有未提交改动；发版必须对应一个确切提交")
    git("fetch", "--tags", "origin", trunk)
    head, remote = git("rev-parse", "HEAD"), git("rev-parse", f"origin/{trunk}")
    if head != remote:
        ahead = git("rev-list", "--count", f"origin/{trunk}..HEAD")
        behind = git("rev-list", "--count", f"HEAD..origin/{trunk}")
        raise SystemExit(f"✗ 本地主干与远端不一致（本地多 {ahead}、少 {behind} 个提交）。"
                         "先 git pull --ff-only 或让合并脚本推上去，再发版")
    ver = current_version()
    tag = "v" + ver
    if git("tag", "-l", tag) or git("ls-remote", "--tags", "origin", f"refs/tags/{tag}"):
        raise SystemExit(f"✗ {tag} 已经发过（标签已存在）。先合并一次改动让版本号自动抬一位，再发版")
    if run("gh", "release", "view", tag, "--repo", REPO_SLUG, check=False).returncode == 0:
        raise SystemExit(f"✗ GitHub 上已有 {tag} 的 Release")
    return trunk, head, tag


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--title", help="发布标题（一句话，用户看得懂）")
    ap.add_argument("--notes", default="", help="发布说明正文（markdown）")
    ap.add_argument("--notes-file", type=Path, help="从文件读发布说明正文")
    ap.add_argument("--check", action="store_true", help="只检查，不打标签")
    ap.add_argument("--watch", action="store_true", help="推完等流水线跑完再退出")
    a = ap.parse_args()

    trunk, head, tag = preflight()
    print(f"可以发版：{tag} = {trunk} @ {head[:12]}")
    if a.check:
        return 0
    if not a.title:
        raise SystemExit("✗ 要给 --title（发布标题）")
    notes = a.notes_file.read_text("utf-8").strip() if a.notes_file else a.notes.strip()
    message = f"PPT Doctor {tag} · {a.title.strip()}\n\n{notes}".strip() + "\n"
    msg_file = ROOT / ".git-release-msg.tmp"
    msg_file.write_text(message, "utf-8")
    try:
        git("tag", "-a", tag, head, "-F", str(msg_file), "--cleanup=verbatim")
    finally:
        msg_file.unlink(missing_ok=True)
    r = run("git", "push", "origin", f"refs/tags/{tag}", check=False)
    if r.returncode:
        git("tag", "-d", tag, check=False)
        raise SystemExit(f"✗ 推标签被拒（多半是别人刚发了同一版）：{r.stderr.strip()[:300]}")
    print(f"✓ 已推送 {tag}。GitHub 正在构建发布（约 30–60 分钟）：")
    print(f"  https://github.com/{REPO_SLUG}/actions/workflows/release.yml")
    if a.watch:
        rid = ""
        for _ in range(30):
            out = run("gh", "run", "list", "--repo", REPO_SLUG, "--workflow", "release.yml", "--branch", tag,
                      "--limit", "1", "--json", "databaseId", "-q", ".[0].databaseId", check=False).stdout.strip()
            if out:
                rid = out
                break
            time.sleep(10)
        if not rid:
            print("  没等到流水线启动，去上面的网址看")
            return 1
        return run("gh", "run", "watch", rid, "--repo", REPO_SLUG, "--exit-status", check=False).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
