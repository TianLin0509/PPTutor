# pptx-finder（PPT Doctor）项目规则

- 本地 PPT 全文搜索托盘工具；环境用 uv，测试用 pytest。
- 发布 = 维护者在主干上跑 `python scripts/release.py --title ...` 推 `vX.Y.Z` 标签 → GitHub 流水线（`.github/workflows/release.yml`）打包、自检、发 Release。
- 阿里云备用服务器由项目所有者电脑上的 `tools/mirror_watch.py` 自动转传，任何人都不需要也不要去碰 ECS 密码。
- 多人维护（所有者 + 协作者都直接维护主干、都能发版）：上手看 `docs/MAINTAINERS.md`。
