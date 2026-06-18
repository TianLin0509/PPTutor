"""入口：`python -m pptx_finder` 与 PyInstaller 打包均用此。

multiprocessing.freeze_support() 必须最先调用——确保打包后 spawn 出的
worker 子进程被正确拦截、不会重新拉起 GUI。
"""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    # PyInstaller windowed（无控制台）下 sys.stdout/stderr 为 None，
    # 库（如 jieba 的 logging StreamHandler）写 stderr 会 AttributeError 崩溃 → 兜底重定向
    import os
    import sys
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")  # noqa: SIM115
    from pptx_finder.app import main

    raise SystemExit(main())
