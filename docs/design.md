# pptx-finder · 设计规格（spec）

> 凭你记得的几个字，找回那一页 PPT。本地、绿色免安装、托盘常驻。
> 本文件是开发契约。模块按此实现，避免漂移。

## 1. 范围与优先级（已与用户确认）

### P0 — 今晚目标（可运行 MVP）
- 全盘搜索（默认扫所有本地固定磁盘，内置排除系统/噪音目录）
- 文件名搜索
- 内容搜索 + **定位命中页码** + 命中文字上下文片段（正文 + 备注页 + SmartArt）
- 多词 **AND** + 引号 `"精确短语"`
- 默认排序：**相关度 + 修改时间**
- 按需预览命中页（PowerPoint COM 渲染，高亮命中词，多命中页可切换）
- 打开文件 / 右键打开所在文件夹 / **打开并跳到命中页**
- 首次索引：**多进程并行解析 + 进度反馈 + 边建边可搜**
- 健壮性兜底：损坏/加密/超大文件跳过不中断；文件已移走友好提示；预览失败兜底
- `.ppt`（旧格式）：**只搜文件名 + 可预览/打开**（COM 渲染），内容搜索不做
- 托盘常驻 + 全局热键唤起

### P1 — 今晚有余力则做
- 版本归组（MinHash-LSH，组内按修改时间排，标记疑似最新版）
- 重复文件检测（内容 hash 完全相同）
- 搜索历史 & 收藏
- 繁简通搜（分词副产品）
- 后台实时自动增量索引（watchdog）

### P2 — 二期（不做）
- 版本差异定位（精确到页 diff）、错字容忍/模糊搜索、OCR 搜图片字、高级筛选(时间/作者)、索引范围管理 UI

## 2. 技术栈
- 语言/环境：Python 3.12，uv 管理
- GUI：PySide6（QSystemTrayIcon 托盘 + 全局热键 + QGraphicsView 预览）
- 解析：lxml 直读 zip+XML（不用 python-pptx，因其慢且抓不到 SmartArt）
- 索引：SQLite FTS5（标准库）+ jieba 应用层预分词
- 版本聚类：datasketch（MinHash-LSH）+ 逐页 hash
- 内容 hash：xxhash
- 变更监测：os.scandir + watchdog（+ 未来 USN）
- 预览：win32com（PowerPoint COM）→ 缩略图缓存
- 打包：PyInstaller --onedir（绿色）
- 测试：pytest + pytest-qt；夹具用 python-pptx 生成

## 3. 模块与契约

```
src/pptx_finder/
  models.py      数据类（ParsedDeck / SlidePage / FileRecord / SearchHit / FileResult）
  tokenize.py    jieba 分词封装（写入/查询同一套；繁简归一）
  parser.py      parse_pptx(path) -> ParsedDeck      ← 核心：页序还原+备注+SmartArt
  scanner.py     iter_ppt_files(roots, excludes) -> Iterator[Path]
  db.py          连接 + schema + 读写 API
  indexer.py     build_index / incremental_update（多进程并行）
  search.py      search(query, scope) -> list[FileResult]（AND/短语/排序/片段）
  cluster.py     P1 版本归组
  renderer.py    render_page(path, page_no) -> png|None（COM 分层降级 + 缓存）
  config.py      路径、排除目录、缓存目录、热键
  ui/
    main_window.py  搜索栏+结果列表(分组)+预览面板
    tray.py         托盘 + 全局热键
  app.py         入口：托盘启动 + 后台索引线程
```

### 关键契约

**parser.parse_pptx(path) -> ParsedDeck**
- 页序：按 `ppt/presentation.xml` 的 `<p:sldIdLst>` 子元素顺序，经 `presentation.xml.rels` 映射 rId→`slideN.xml`。**绝不按文件名 N 排序。**
- 每页抽取：标题、正文（所有 `<a:t>`）、备注（`notesSlideN.xml`）、SmartArt（`ppt/diagrams/dataN.xml` 的 `<a:t>`）
- `ParsedDeck = {page_count, pages: [SlidePage{page_no(1-based放映序), title, body, notes, smartart, raw_text}], status('ok'|'encrypted'|'error'), error}`
- 加密（OOXML 加密为 OLE 复合文档，非 zip）→ status='encrypted'，不抛异常
- 损坏 → status='error'，记 error

**db schema**
```sql
files(id PK, path UNIQUE, name, ext, size, mtime, content_hash, page_count,
      status, error, indexed_at)
pages_fts USING fts5(content, file_id UNINDEXED, page_no UNINDEXED)  -- 存 jieba 分词文本
pages_raw(file_id, page_no, raw_text, PRIMARY KEY(file_id,page_no))  -- 原文，片段高亮用
minhash(file_id PK, sig BLOB, page_hashes TEXT, group_id)            -- P1
meta(key PK, value)
```
- 文件名搜索：`files.name LIKE`（上千量级足够快）
- 内容搜索：`pages_fts MATCH`，命中行直接得 file_id + page_no
- 片段：从 pages_raw.raw_text 用原始 query 词做高亮窗口

**search.search(query, scope=None) -> list[FileResult]**
- 解析 query：空格分词→AND；`"..."` →精确短语
- 内容命中（FTS bm25）+ 文件名命中（LIKE）合并去重
- `FileResult = {file_id, path, name, ext, mtime, page_count, status, score, name_hit:bool, hits:[SearchHit{page_no, snippet}]}`
- 排序：`score = w1*bm25_norm + w2*recency_norm + name_hit_bonus`（相关度为主，修改时间次之，文件名命中加分）
- scope: None=全部已索引；或限定路径前缀

**renderer.render_page(path, page_no) -> Path|None**
- 缓存键：`{content_hash}_{page_no}.png` 存 `%LOCALAPPDATA%/pptx-finder/cache/`
- 命中缓存直接返回
- 否则 COM：串行、超时兜底、try/finally 关闭，导出该页 PNG
- 失败返回 None（UI 显示"无法预览，可直接打开"）

## 4. 首次索引提速策略（用户重点关注）

1. **多进程并行解析**：`ProcessPoolExecutor`，worker 数 = CPU 核数。解析是 CPU 密集（解压+XML），并行收益最大。
2. **lxml 直读**：只解压读取需要的 XML part（slides/notes/diagrams），不加载整包；用 `iter` 流式抽 `<a:t>`。
3. **边建边可搜**：解析结果流式写入 FTS5，主线程/UI 可立即搜索已入库部分；不等全部完成。
4. **增量快筛**：`(size, mtime)` 先筛是否可能变化，疑似变化再算 xxhash 复核内容（避免 Office 重置 mtime 的误报/漏报）。
5. **进度反馈**：`progress_cb(done, total, current_path)`；UI 状态栏显示「正在索引 N/M」。
6. **可中断**：用户关闭/退出时优雅停止 worker。
7. **批量写库**：解析结果攒批 + 单事务写入，减少 SQLite 提交开销。
8. **扫描排除**：跳过 Windows、Program Files、AppData、$Recycle.Bin、node_modules、临时目录等，减少无效 IO。

## 5. 健壮性兜底（P0 底线）
- 解析：每文件独立 try；加密/损坏/超大（>200MB 跳过并记 status）不中断整批
- 打开：点击时校验文件存在；不存在 → 提示「文件已移动或删除」，标灰
- 预览：COM 不可用/超时/失败 → 返回 None，UI 兜底文案
- 索引中断：worker 异常不影响主流程；DB 写入事务化

## 6. 测试策略
- **单元（TDD）**：parser（页序/备注/SmartArt/加密）、tokenize、search（AND/短语/排序）、indexer（建库/增量）、cluster
- **夹具**：python-pptx 生成多页+备注 pptx；手工构造含 diagrams 的最小 pptx 验 SmartArt；构造加密/损坏样本
- **E2E（真实执行，CLAUDE.md 铁律）**：真实启动 PySide6 app，pytest-qt 模拟用户「输入→搜索→选结果→预览→打开」全链路；预览 COM 真实渲染验证产出 PNG。做不到的如实记录。
- **代码审查**：silent-failure-hunter（IPC/流/子进程静默失败）+ 多方 CLI 交叉审查。

## 7. 里程碑（今晚）
M1 parser + tokenize（TDD 绿）
M2 db + indexer（多进程，建库+增量，TDD 绿）
M3 search（AND/短语/排序/片段，TDD 绿）
M4 renderer（COM 真实渲染一页）
M5 UI 主窗（搜索→结果→预览→打开）跑通
M6 托盘 + 全局热键
M7 P1（版本归组/查重/历史）择机
M8 PyInstaller 打包 exe
M9 E2E 真实测试 + 代码审查 + HTML 验收报告
