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
- 🛡️ **PPT 版 git（自动版本管理）**：全盘监听，改存即自动留版；改崩了、想找回旧版，一键回到任意历史版本，零操作负担。
- 📊 **库概览仪表盘**：KPI + 主题分布 / 最近活跃 / 库构成 / 本周改动趋势，打开即见全景。
- 🎨 **极光玻璃 UI**：10 套可切换主题 + 无边框玻璃标题栏。
- ⚡ **托盘常驻 + 全局热键**：`Alt+F` 随时唤起。

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

---

<div align="center"><sub>PPT Doctor · 你的 PPT 小助教 🎓</sub></div>
