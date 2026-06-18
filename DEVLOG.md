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

## 真机修复（用户首试反馈，2026-06-18 晨）
用户双击 exe 搜「算力」命中 0。诊断：**开了两个 exe 实例并发写库 → database is locked → 索引大量失败**；且热键失败信息盖住了索引进度栏，看不出在工作。
真实库其实数据完好（2286 文件 / 10782 页 / 「算力」351 页）。已修：
- 单实例锁（QLocalServer/Socket）：第二个实例激活已有窗口并退出（验证：启动两次只剩 1 进程）
- db `busy_timeout=8000`：遇锁等待而非失败
- 热键失败信息移到右下专用标签，不再霸占索引进度
- 扫描阶段进度可见（"扫描磁盘中…可边扫边搜"）
- `config.ext_path`：超长中文路径(>260)加 `\\?\` 前缀，修 WPS 云盘文件 Errno 22
验证：真实库搜「算力」命中 200、「昇腾」100；状态栏正常显示「2286 文件就绪」。截图 `artifacts/verify_real.png`。

---

## 时间线
- M0：环境确认、uv 打包式项目初始化、spec 落定、依赖安装完成。基础模块 models/config/text_tokenize 写入。
