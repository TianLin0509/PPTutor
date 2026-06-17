"""入口：`python -m pptx_finder` 与 PyInstaller 打包均用此。

multiprocessing.freeze_support() 必须最先调用——确保打包后 spawn 出的
worker 子进程被正确拦截、不会重新拉起 GUI。
"""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from pptx_finder.app import main

    raise SystemExit(main())
