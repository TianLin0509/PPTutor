# 维护者上手指南（所有者与协作者通用）

2026-10-31 起，PPT Doctor 由项目所有者和几位协作者一起维护：**每个人都直接把改动合进主干 `master`，
也都能自己发新版本**，不需要别人审核。本文是给你（和你的 AI 编程助手）的完整上手说明。
让 AI 干活时，直接告诉它「先读 docs/MAINTAINERS.md 和 .agents/AUTHOR.md」。

## 一、一次性准备（约 10 分钟）

1. 接受 GitHub 邮件里的协作邀请（仓库 `TianLin0509/PPTutor`）。
2. 装好 Git、[uv](https://docs.astral.sh/uv/)、GitHub CLI（`gh`），并登录：`gh auth login`。
3. 克隆到一个固定目录，这个目录就是你的「主目录」（`.agents/` 文档里写的
   `C:\Users\lintian\pptx-finder` 是所有者电脑上的主目录，你那边换成自己的路径）：

   ```bash
   git clone https://github.com/TianLin0509/PPTutor.git pptx-finder
   cd pptx-finder
   git config core.hooksPath .githooks     # 打开防误操作钩子：主目录不能直接提交、主干不能直接 push
   uv sync
   ```

## 二、日常改动：一个任务一个 worktree

```bash
git worktree add ../pptd-<日期>-<任务> -b feat/<一句话短语> master
cd ../pptd-<日期>-<任务>
uv sync                                  # 每个 worktree 必须有自己的 .venv（原因见 .agents/AUTHOR.md）
# …改代码、在 worktree 里跑相关测试…
git commit -am "feat: ……"
```

细则（分支命名、哪些坑别踩、怎么写提交说明）全在 `.agents/AUTHOR.md` 和 `.agents/PITFALLS.md`。

## 三、合进主干：回到主目录跑合并脚本

```bash
cd <你的主目录>
python scripts/merge_task.py feat/<分支名>
```

脚本会：对齐远端最新主干 → 合进来 → 自动抬版本号 → **亲自跑全量测试（约 15 分钟）** →
通过才提交并推送。没通过会自动回滚，主干不会被弄脏。

- 别人在你测试期间推了新提交？脚本会自动撤回、基于新主干重合重测，最多 3 次，**绝不强推**。
- 不要用 `git push --force`，也不要绕过脚本直接推 `master`。

## 四、发新版本

在主目录、主干上（合并脚本推送成功之后）：

```bash
python scripts/release.py --check                              # 先看能不能发
python scripts/release.py --title "一句话说清这版变化" --notes-file 说明.md --watch
```

- 它会检查：主干干净且与远端一致、这个版本号没发过。两人同时发同一版时，只有先到的那个成功。
- 推上去的是一个带说明的 `vX.Y.Z` 标签。GitHub 的 Release 流水线（`.github/workflows/release.yml`）
  接手：干净机器上打包三个渠道、两个安装包，跑冻结自检（GIF 录制、打包自检、图片转文字、公式识别），
  全部通过才发布并设为最新，约 30–60 分钟。进度看 Actions 页面。
- 流水线中途失败：看 Actions 里的 `release-logs` 附件找原因，修好后再合一次（版本号会抬一位）重新发；
  或在 Actions 页面对原标签点 Run workflow 重跑。
- **阿里云备用服务器不用你管**：所有者电脑上的同步任务每 15 分钟检查一次，发现新版自动转传并核对。
  不需要、也不要去要服务器密码。

## 五、出了问题怎么撤

- 合进去的某次改动有问题：`git revert -m 1 <合并提交>` 后再走一遍合并脚本。
- 已发布的版本有问题：修好后发一个新版本覆盖（老用户会自动更新到新版），不要删已发布的 Release。
