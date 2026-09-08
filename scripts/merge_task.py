#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""主干的唯一入口。

为什么要有这个脚本：
    防覆盖靠两件事 —— ① 各干各的（worktree 隔离，pre-commit 管）
    ② 一次只落一个（单一入口，pre-push 管 + 本脚本执行）。
    合并位 Agent 说 PASS 不算数，**测试由本脚本亲自跑**，跑出来的结果才作数。

为什么必须在主工作目录合：
    SuperRAN 的 editable 安装把 import 路径硬指向主仓库，
    在 worktree 里跑测试拿到的是主仓库的代码，证据是假的。
    所以「合进去 → 跑测试 → 不过就回滚」这一套必须在主目录做。
    AI HUB 没这个问题，但统一走同一条路，少一种情况要记。

用法：
    python scripts/merge_task.py <分支名>
    python scripts/merge_task.py <分支名> --dry-run    # 只验不合

项目差异全部读 .agents/project.json，本脚本对项目一无所知。
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / ".agents" / "project.json"
_MERGE_LOCK_HANDLE = None


def say(msg=""):
    print(msg, flush=True)


def run(cmd, cwd=None, env=None, check=True, capture=True):
    """跑一条命令。capture=False 时把输出直接透给终端（跑测试用）。"""
    e = dict(os.environ)
    if env:
        e.update(env)
    r = subprocess.run(
        cmd, cwd=str(cwd or REPO), env=e, shell=isinstance(cmd, str),
        capture_output=capture, text=True, encoding="utf-8", errors="replace",
    )
    if check and r.returncode != 0:
        out = (r.stdout or "") + (r.stderr or "") if capture else "(输出见上)"
        raise RuntimeError(f"命令失败（退出码 {r.returncode}）：{cmd}\n{out}")
    return r


def git(*args, **kw):
    return run(["git", *args], **kw).stdout.strip()


def acquire_merge_lock():
    """同一仓库一次只允许一个合并进程触碰主工作区。进程退出时 OS 自动释放锁。"""
    global _MERGE_LOCK_HANDLE
    common = Path(git("rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = (REPO / common).resolve()
    lock_path = common / "hub-merge-task.lock"
    handle = open(lock_path, "a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _MERGE_LOCK_HANDLE = handle
    return True


def load_config():
    if not CONFIG.exists():
        say(f"✗ 找不到项目配置：{CONFIG}")
        say("  这个项目还没整理过。先让一个普通 agent 跑 project-prep skill。")
        sys.exit(2)
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError as ex:
        say(f"✗ 项目配置不是合法 JSON：{ex}")
        sys.exit(2)


def main_worktree():
    line = git("worktree", "list", "--porcelain").splitlines()[0]
    return Path(line.replace("worktree ", "", 1).strip())


# ── 版本号：由合并那一刻抬，不由分支抬 ──────────────────────────────────────
#
# 版本号那几行是**所有并行分支都要改的同几行**。两个群聊同时基于同一个主干开工，
# 第二个合进来的必然遇到「数值和主干撞了」或「文本冲突」二选一 —— 而分支自己
# 无从知道它会是第几个合进去的，那个信息只有合并那一刻才存在。
# 合并是串行的（上面那把锁保证），所以挪到这里，这个冲突就不可能再发生。
#
# 项目没配 versionBump 就整段不生效，行为和以前一模一样。
VERSION_LINE = re.compile(r'^[+-]\s*"version"\s*:\s*"[^"]*"\s*,?\s*$')


def conflicted_paths():
    out = run(["git", "diff", "--name-only", "--diff-filter=U"], check=False).stdout
    return [p.strip() for p in out.splitlines() if p.strip()]


def only_version_lines(base, branch, path):
    """分支对这个文件的改动是不是只有版本号那几行。

    不看合并结果，看分支相对分叉点改了什么 —— 只碰版本号的分支，
    它那份改动本来就要被这里的自动抬升覆盖掉，丢掉它不损失任何信息。
    """
    diff = run(["git", "diff", base, branch, "--", path], check=False)
    if diff.returncode != 0:
        return False
    changed = False
    for line in (diff.stdout or "").splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            if not VERSION_LINE.match(line):
                return False
            changed = True
    return changed


def resolve_version_conflicts(trunk, branch, version_files):
    """只自动化解「双方都只动了版本号」这一种冲突，别的一律照旧报冲突。

    取主干那一份（--ours），因为紧接着就会基于主干的值重新抬一次。
    """
    if not version_files:
        return False, []
    stuck = conflicted_paths()
    if not stuck:
        return False, []
    base = run(["git", "merge-base", trunk, branch], check=False).stdout.strip()
    if not base:
        return False, stuck
    unresolved = [p for p in stuck if p not in set(version_files) or not only_version_lines(base, branch, p)]
    if unresolved:
        return False, stuck
    for p in stuck:
        run(["git", "checkout", "--ours", "--", p], check=False)
        run(["git", "add", "--", p], check=False)
    return not conflicted_paths(), stuck


def bump_version(commands, version_files, bypass):
    for cmd in commands:
        r = run(cmd, check=False, capture=False)
        if r.returncode != 0:
            raise RuntimeError(f"抬版本号失败（退出码 {r.returncode}）：{cmd}")
    if version_files:
        run(["git", "add", "--", *version_files], env=bypass, check=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("branch", help="要合并的任务分支")
    ap.add_argument("--dry-run", action="store_true", help="只 rebase 和跑测试，不真合")
    args = ap.parse_args()

    cfg = load_config()
    trunk = cfg.get("trunk") or "master"
    name = cfg.get("name") or REPO.name
    tests = cfg.get("test") or []
    after = cfg.get("afterMerge") or []
    version_bump = [c for c in (cfg.get("versionBump") or []) if isinstance(c, str) and c.strip()]
    version_files = [p for p in (cfg.get("versionFiles") or []) if isinstance(p, str) and p.strip()]
    branch = args.branch

    say(f"── {name} · 合并 {branch} → {trunk} ──")
    say()

    if not acquire_merge_lock():
        say("✗ 另一个合并任务正在运行，本次没有触碰主工作区。")
        say("  等前一个结束后再重试；不要并行启动两个合并位。")
        return 2

    # ① 必须在主工作目录 —— 否则 SuperRAN 的测试证据是假的
    here, main_wt = REPO.resolve(), main_worktree().resolve()
    if str(here).lower() != str(main_wt).lower():
        say("✗ 必须在主工作目录跑这个脚本。")
        say(f"  当前：{here}")
        say(f"  主目录：{main_wt}")
        say("  原因：worktree 里的测试可能解析到主仓库的代码，结果不可信。")
        sys.exit(2)

    # ③ 分支必须存在（要先确认，下面算改动集要用它）
    if run(["git", "rev-parse", "--verify", branch], check=False).returncode != 0:
        say(f"✗ 分支不存在：{branch}")
        sys.exit(2)

    # ② 别人的未提交改动：只拦真会被踩到的，不搞一刀切
    #
    #    老版本是「工作区一脏就拒绝」。听起来安全，实际后果是：
    #    任何人在主目录留一份忘了提交的半成品，整个合并闸门就**永久瘫痪**
    #    （实测真发生了：两个文件躺了一天多，所有合并都跑不了）。
    #
    #    但也不能简单放宽 —— 失败回滚原本用 `git reset --hard`，
    #    那会连别人的在途改动一起抹掉。放宽判据必须同时把回滚改成按路径回滚。
    # 注意：这里不能用 git()，它会对整段输出 strip，把第一行开头的空格吃掉
    #（未暂存修改是 " M path"，被 strip 成 "M path" 后按列取路径会错位成 "ath"）。
    # 这个 bug 让重叠检测整个失效过一次，测试才抓到。
    dirty_raw = run(["git", "status", "--porcelain"]).stdout
    dirty_paths = set()
    for ln in dirty_raw.splitlines():
        if len(ln) < 4:
            continue
        p = ln[3:].strip().strip('"')
        if " -> " in p:              # 重命名取目标路径
            p = p.split(" -> ", 1)[1].strip().strip('"')
        if p:
            dirty_paths.add(p)

    merge_paths = [p for p in git("diff", "--name-only", f"{trunk}...{branch}").splitlines() if p]
    # 版本号文件即使不在本次改动集里也会被自动抬升写到，所以它们一样要参与
    # 「会不会踩到别人未提交的改动」和「失败要还原哪些文件」这两件事。
    touched_paths = merge_paths + [p for p in version_files if version_bump and p not in merge_paths]
    overlap = sorted(dirty_paths & set(touched_paths))

    if overlap:
        say("✗ 本次合并会碰到你未提交的这些文件，先处理掉再合：")
        for p in overlap[:10]:
            say(f"    {p}")
        say("  （合下去会覆盖你还没保存的工作，本脚本不替你决定怎么处理。）")
        sys.exit(2)

    unrelated_dirty = sorted(dirty_paths - set(touched_paths))
    if unrelated_dirty:
        say(f"⚠ 主目录还有 {len(unrelated_dirty)} 个与本次合并无关的未提交改动：")
        for p in unrelated_dirty[:5]:
            say(f"    {p}")
        say("  它们不在本次改动范围内，会原样保留（回滚也只回滚本次涉及的文件）。")
        say()

    original = git("rev-parse", trunk)
    say(f"① 记下回滚点：{trunk} = {original[:12]}")

    bypass = {"HUB_ALLOW_MAIN_COMMIT": "1", "HUB_ALLOW_TRUNK_PUSH": "1"}
    merged_sha = None

    def rollback():
        """撤销本次合并，回到 original。

        因为合并用的是 --no-commit，绝大多数情况下工作区处于 MERGING 状态，
        `git merge --abort` 就能干净收场 —— 而且它本身就会尽量保留与合并无关的本地改动，
        比按路径手动还原更稳。上面的重叠检查保证了无关改动确实能被重建。

        只有极少数情况 HEAD 已经前进（比如中断发生在 commit 之后），才需要退 HEAD。
        那时也**不能用 `git reset --hard`** —— 它会连别人的在途改动一起抹掉。
        """
        run(["git", "merge", "--abort"], check=False)
        head = run(["git", "rev-parse", "HEAD"], check=False).stdout.strip()
        if head and head != original:
            if unrelated_dirty:
                run(["git", "reset", "--soft", original], env=bypass, check=False)
                if touched_paths:
                    run(["git", "checkout", original, "--", *touched_paths], env=bypass, check=False)
                run(["git", "reset"], env=bypass, check=False)   # 清索引，别把还原留成暂存
            else:
                run(["git", "reset", "--hard", original], env=bypass, check=False)

    try:
        git("checkout", trunk, env=bypass)

        # ④ 主干可能已经被别的任务推进过 —— 这是并行开发唯一新增的复杂度
        if run(["git", "remote", "get-url", "origin"], check=False).returncode == 0:
            if run(["git", "fetch", "origin", trunk], check=False).returncode == 0:
                behind = git("rev-list", "--count", f"{trunk}..origin/{trunk}")
                if behind != "0":
                    say(f"   主干落后远端 {behind} 个提交，先对齐")
                    git("merge", "--ff-only", f"origin/{trunk}", env=bypass)
                    original = git("rev-parse", trunk)

        # ② 先合进工作区但**不提交** —— 崩溃安全的关键。
        #
        #    老写法是先 `git merge` 生成提交、再跑测试、失败才回滚。
        #    问题是这中间有个几十秒的窗口：脚本一旦被杀（Ctrl+C、SIGPIPE、
        #    终端关掉、机器 OOM），主干就停在「已合但没验」的状态，
        #    而且没人知道。实测踩到过 —— 把输出管道给 head 就触发了 SIGPIPE。
        #
        #    改成 --no-commit 之后：测试期间主干的提交历史根本没动过，
        #    工作区处于 git 自己认得的 MERGING 状态，被杀了也一眼看得出、
        #    一条 `git merge --abort` 就能清干净。
        # 已经合过的分支：--no-commit 下 git 只会说 "Already up to date"，
        # 不产生待提交内容，后面的 commit 就会报个莫名其妙的错。
        # 用户重跑一次合并是很正常的事，得给句人话。
        if run(["git", "merge-base", "--is-ancestor", branch, trunk], check=False).returncode == 0:
            say(f"② {branch} 已经在 {trunk} 里了，无需重复合并")
            say()
            say(f"✓ 已是最新：{trunk} @ {original[:12]}")
            return 0

        say(f"② 合并 {branch}（先不提交，测过了再落）")
        merged = run(["git", "merge", "--no-ff", "--no-commit", branch], env=bypass, check=False)
        if merged.returncode != 0:
            healed, stuck = resolve_version_conflicts(trunk, branch, version_files if version_bump else [])
            if not healed:
                detail = "、".join(stuck) if stuck else ((merged.stdout or "") + (merged.stderr or "")).strip()[:400]
                raise RuntimeError(f"合并有冲突，需要工作位基于最新主干 rebase 后处理：{detail}")
            say(f"   只有版本号撞了（{'、'.join(stuck)}），已按主干化解 —— 下一步会重新抬")
        say("   已合进工作区，主干提交历史暂未改变")

        # ③ 抬版本号 —— 放在测试之前，让版本一致性单测也守着这一步的结果
        if version_bump:
            say("③ 抬版本号（合并是串行的，所以这件事由脚本做，分支不用碰）")
            bump_version(version_bump, version_files, bypass)

        # ⑤ 亲自跑测试 —— 不采信任何 Agent 的说法
        if not tests:
            say("④ 项目没配测试命令，跳过（建议补上）")
        else:
            say(f"④ 跑测试（{len(tests)} 条）")
            for i, t in enumerate(tests, 1):
                say(f"   [{i}/{len(tests)}] {t}")
                r = run(t, check=False, capture=False)
                if r.returncode != 0:
                    raise RuntimeError(f"测试没过：{t}")
            say("   全部通过")

        if args.dry_run:
            say()
            say("⑤ --dry-run，撤销不真合")
            rollback()
            say(f"   已回到 {original[:12]}")
            say()
            say("结论：验证通过，可以合。")
            return 0

        # ⑥ 测试过了才真正落成提交
        git("commit", "--no-edit", env=bypass)
        merged_sha = git("rev-parse", "HEAD")
        say(f"   已提交 {merged_sha[:12]}")

    except Exception as ex:
        say()
        say(f"✗ {ex}")
        say()
        say(f"回滚 {trunk} → {original[:12]}")
        rollback()
        if unrelated_dirty:
            say(f"已回滚（按路径，保留了 {len(unrelated_dirty)} 个无关的未提交改动）。")
        else:
            say("已回滚，主干没有被污染。")
        say()
        say("这个分支还在，改完再跑一次本脚本即可。")
        return 1

    # ⑥ 推远端（有就推，没有也不算失败 —— 远端只是备份，不是关卡）
    say("⑤ 推远端")
    if run(["git", "remote", "get-url", "origin"], check=False).returncode == 0:
        r = run(["git", "push", "origin", trunk], env=bypass, check=False)
        say("   已推送" if r.returncode == 0 else f"   推送失败（本地已合，不影响）：{(r.stderr or '').strip()[:200]}")
    else:
        say("   没配 origin，跳过")

    # ⑦ 合并后动作 —— 项目专属的东西全在这里，脚本本身不知道是什么
    if after:
        say(f"⑥ 合并后动作（{len(after)} 条）")
        for a in after:
            cmd = a.replace("{branch}", branch).replace("{sha}", merged_sha or "")
            say(f"   {cmd}")
            r = run(cmd, check=False, capture=False)
            if r.returncode != 0:
                say("   ⚠ 这条失败了，但代码已经合进去了，不回滚")

    say()
    say(f"✓ 已合并：{branch} → {trunk} @ {merged_sha[:12]}")
    say(f"  撤回：git revert -m 1 {merged_sha[:12]}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\n中断")
        sys.exit(130)
