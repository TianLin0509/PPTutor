# pptx-finder 开发日志（自主夜班）

> 2026-06-18 夜：用户睡前下达 goal——自主做出可运行 MVP（托盘小程序）→ E2E 真实测试 → 代码审查。
> 不提问；问题累积到 `QUESTIONS.md`，明早出 HTML 汇报。

## 环境
- Python 3.12.10 + uv + git；机器装有 PowerPoint（预览走 COM）。

## 决策记录（用户已确认）
- 项目：`pptx-finder` @ `C:\Users\lintian\pptx-finder`
- 索引范围管理 → P2；P0 默认扫全部本地磁盘（内置排除系统/噪音目录）
- `.ppt` 走 A：仅文件名 + 可预览/打开，内容搜索不做
- 排序：相关度 + 修改时间
- 首次索引提速 = 重点（多进程 + lxml 直读 + 边建边搜 + 进度）

## 进度
- [x] M0 骨架 + spec(`docs/design.md`) + 依赖安装（lxml/jieba/xxhash/datasketch/PySide6/pywin32/watchdog + pytest/pytest-qt/python-pptx）
- [ ] M1 parser + tokenize（页序还原/备注/SmartArt）
- [ ] M2 db + indexer（多进程并行 + 增量）
- [ ] M3 search（AND/短语/排序/片段）
- [ ] M4 renderer（COM 渲染命中页）
- [ ] M5 UI 主窗跑通
- [ ] M6 托盘 + 全局热键
- [ ] M7 P1（版本归组/查重/历史）
- [ ] M8 PyInstaller 打包
- [ ] M9 E2E 真实测试 + 代码审查 + HTML 报告

---

## 时间线
- M0：环境确认、uv 打包式项目初始化、spec 落定、依赖安装完成。基础模块 models/config/text_tokenize 写入。
