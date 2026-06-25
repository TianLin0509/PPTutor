"""入口：`python -m pptx_finder` 与 PyInstaller 打包均用此。

multiprocessing.freeze_support() 必须最先调用——确保打包后 spawn 出的
worker 子进程被正确拦截、不会重新拉起 GUI。
"""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    # PyInstaller windowed（无控制台）下 sys.stdout/stderr 为 None，
    # 库的 logging StreamHandler 写 stderr 会 AttributeError 崩溃 → 兜底重定向
    import os
    import sys
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")  # noqa: SIM115

    # 打包后端到端自检：`pptx-finder.exe --selftest <pptx_dir> <report.json>`
    # 在真实 frozen 环境建索引 + 搜，验证字级召回与 OpenCC 繁简可用（不弹 GUI）。
    if "--selftest" in sys.argv:
        from pptx_finder.selftest import run_selftest
        raise SystemExit(run_selftest(sys.argv))

    # 增量更新链路自检：`PPTutor.exe --update-check <base_url> <report.json>`
    # 在 frozen 真实环境验证 urllib/清单比对/增量下载/sha256（headless，不应用、不弹 GUI）。
    if "--update-check" in sys.argv:
        from pptx_finder.updater import run_update_check
        raise SystemExit(run_update_check(sys.argv))

    # Isolated PowerPoint renderer host. The GUI process starts this same exe
    # with a localhost IPC port so COM crashes do not bring down the main UI.
    if "--renderer-worker" in sys.argv:
        from pptx_finder.render_service import main as render_worker_main
        raise SystemExit(render_worker_main(sys.argv))

    from pptx_finder.app import main

    raise SystemExit(main())
