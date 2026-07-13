<div align="center">

<img src="assets/logo.png" width="120" alt="PPT Doctor logo">

# PPT Doctor

**本地 PowerPoint / Word / PDF 全文搜索 · 命中页预览 · 自动版本管理桌面应用**

记得 PPT 里写过什么字，就能搜出它在哪个文件、第几页 —— 全本地、零上传、绿色免安装。

</div>

---

## ✨ 功能

- 🔎 **内容搜索定位页**：PowerPoint / Word / PDF 统一全文搜索；搜你记得的文字 / 文件名，直接告诉你在哪个文件、第几页（FTS5 字级索引 + 原文验证，繁简通搜、子串召回）。
- 🖼️ **命中页预览**：用本机 PowerPoint 渲染命中页，最准；滚轮翻原始页序判断是不是要找的稿。
- 🛡️ **PPT 版 git（自动版本管理）**：全盘监听，改存即自动留版；改崩了、想找回旧版，一键回到任意健康历史版本，零操作负担。
- 📊 **库概览仪表盘**：KPI + 主题分布 / 最近活跃 / 库构成 / 本周改动趋势，打开即见全景。
- 🎨 **极光玻璃 UI**：10 套可切换主题 + 无边框玻璃标题栏。
- ⚡ **托盘常驻 + 全局热键**：`Alt+F` 随时唤起。

## ⚡ v1.0.6 流畅度升级

- 相关度按“文件名完全匹配 > 内容完整匹配 > 部分匹配”排序，并支持主排序 + 次排序组合。
- 大结果集只创建首屏卡片，其余按需加载；排序和筛选不再反复重建数百个 QWidget。
- 搜索状态与索引统计解耦，文件变化引发的重复搜索合并到空闲窗口。
- 预览复用安全快照、只预取一页；超时不重复等待，后台不再启动时自动预热 PowerPoint。

## 🚀 用法

绿色免安装，双击 `PPT Doctor.exe` 即用。首次自动后台全盘建索引（边扫边搜）。

源码运行：

```bash
uv run python -m pptx_finder
```

打包：

```bash
uv run pyinstaller pptx-finder.spec --noconfirm    # → dist/PPT Doctor/PPT Doctor.exe
```

## 🧱 技术栈

Python 3.12 · PySide6 · SQLite FTS5（每页一行存页码定位）· OpenCC 繁简归一化 · MinHash-LSH 版本归组 · PowerPoint COM 预览 · PyInstaller 绿色打包。

## 📁 数据

- 索引库 / 版本库：`%LOCALAPPDATA%\pptx-finder\`
- 索引范围可用 `PPTX_FINDER_ROOTS` 限定（默认全盘固定磁盘）

## 🛡️ v1.0.5 版本守护

- 保存事件先捕获为稳定临时副本；PowerPoint 原子替换或同步盘改写期间，不会把两次保存混成一个版本。
- 每个恢复点都是完整 OpenXML 包快照：跨文档对象去重，但恢复不依赖补丁链。
- 恢复/导出先在目标同目录重组、解析并核对内容哈希，全部通过后才原子替换；失败不动现有文件。
- 启动快速检查清单和对象引用，设置页可手动执行全对象哈希深检；损坏恢复点自动隔离，不再参与恢复、搜索入口或复制分支。
- 离线巡检按游标轮转覆盖全部受管文档，并能重新接管软件关闭期间在原路径重建的文件。
- 默认每份 PPT 保留 100 版：先保每次编辑会话的里程碑，再保近期细粒度保存；复制分支的基线永久受保护。
- 重型迁移与 GC 七天节流；生产规模快速审计由重复磁盘探测改为一次对象集合扫描。

详见 [CHANGELOG.md](CHANGELOG.md) 与 [docs/version-management-design.md](docs/version-management-design.md)。

---

<div align="center"><sub>PPT Doctor · 你的 PPT 小助教 🎓</sub></div>
