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

## 进度（今晚全部完成 ✅）
- [x] M0 骨架 + spec + 依赖
- [x] M1 parser（页序还原/备注/SmartArt/加密兜底）— 5 测试
- [x] M2 db + indexer（多进程并行 + 增量改删 + .ppt 登记）— 5 测试
- [x] M3 search（AND/短语/相关度+时间排序/片段高亮）— 7 测试
- [x] M4 renderer（PowerPoint COM 真实渲染命中页 + 缓存）— 2 真实 COM 测试
- [x] M5 UI 主窗（搜索→结果→预览→打开）— 3 UI 测试
- [x] M6 托盘常驻 + 全局热键 + 关窗最小化到托盘
- [x] M7(P1) 版本归组（MinHash-LSH）— 3 测试；查重/历史收藏顺延（见 QUESTIONS）
- [x] M8 PyInstaller --onedir 绿色 exe
- [x] M9 E2E 真实冒烟（真索引+真COM渲染+截图）+ 代码审查（silent-failure-hunter 11 项已修）+ HTML 报告

**测试统计：23 快测 + 2 真实 COM 测试，全绿。**
**E2E 证据：`artifacts/smoke_main.png`（真实渲染 + 版本组）。**

---

## 时间线
- M0：环境确认、uv 打包式项目初始化、spec 落定、依赖安装完成。基础模块 models/config/text_tokenize 写入。
