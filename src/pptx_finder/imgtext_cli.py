"""图片转可编辑文字的无界面入口。

    PPT-Doctor.exe --imgtext <图片> [输出.pptx] [--install-component]

两个用途：
  · 批量转换（写个 for 循环就能一次跑一叠图，不用一张张拖进窗口）
  · 打包后的链路自检——GUI 里点不出退出码，只有命令行能把「模板找不到」
    「识别组件没装」「PPTX 结构坏了」这类问题变成非零退出码。

退出码：0 成功；1 失败（原因打到 stdout）。
"""
from __future__ import annotations

import os
import sys
import time


def _emit(line: str) -> None:
    """打包成 windowed 程序时 sys.stdout 可能是 None，兜一层。"""
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001
        pass


def run_imgtext(argv: list[str]) -> int:
    i = argv.index("--imgtext")
    rest = [a for a in argv[i + 1:] if not a.startswith("--")]
    want_install = "--install-component" in argv

    from PySide6.QtWidgets import QApplication
    # 字体度量要真实系统字体：离屏平台不加载字体，字号会整体偏小两成半。
    app = QApplication.instance() or QApplication(sys.argv[:1])
    _ = app

    from . import imgtext, imgtext_ocr
    from .config import data_dir, resource_path

    _emit(f"数据目录: {data_dir()}")
    try:
        _emit(f"空白模板: {resource_path(*imgtext.TEMPLATE_ASSET)}")
    except Exception as exc:  # noqa: BLE001
        _emit(f"空白模板缺失: {exc}")
        return 1

    if want_install and not imgtext_ocr.is_installed():
        _emit("识别组件未安装，开始下载…")
        try:
            version = imgtext_ocr.install(
                progress=lambda d, t: None, cancel=lambda: False)
            _emit(f"识别组件安装完成: {version}")
        except Exception as exc:  # noqa: BLE001
            _emit(f"识别组件安装失败: {type(exc).__name__}: {exc}")
            return 1

    _emit(f"识别组件: {imgtext_ocr.self_test()}")
    if not rest:
        return 0 if imgtext_ocr.is_installed() else 1

    src = os.path.abspath(rest[0])
    if not os.path.isfile(src):
        _emit(f"找不到图片: {src}")
        return 1
    dest = os.path.abspath(rest[1]) if len(rest) > 1 else (
        os.path.splitext(src)[0] + "_可编辑.pptx")

    try:
        t0 = time.time()
        rows = imgtext_ocr.recognize_one(src)
        t1 = time.time()
        result = imgtext.convert(src, dest, rows)
        t2 = time.time()
    except Exception as exc:  # noqa: BLE001
        _emit(f"转换失败: {type(exc).__name__}: {exc}")
        return 1

    _emit(f"识别 {t1 - t0:.1f}s -> 转换 {t2 - t1:.1f}s")
    _emit(result.summary())
    if not os.path.isfile(dest):
        _emit("产物未生成")
        return 1
    _emit(f"产物: {dest} ({os.path.getsize(dest) // 1024} KB)")
    return 0
