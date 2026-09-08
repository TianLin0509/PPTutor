# pptx-finder（PPT Doctor）项目规则

- 本地 PPT 全文搜索托盘工具；环境用 uv，测试用 pytest。
- 发布 = PyInstaller 打包 + gh release。
- 阿里云 ECS 增量更新用 `.selftest/release.py`（需要 ECS_PWD，必须用户点头才跑）；ECS 更新源目录为 `C:/pptutor-updates`。
