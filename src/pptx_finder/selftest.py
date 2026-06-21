"""打包后端到端自检：在 frozen 环境真实建索引 + 搜，验证「原文有的必搜到」与 OpenCC 繁简可用。

由 `pptx-finder.exe --selftest <pptx_dir> <report.json>` 触发，不弹 GUI。
windowed exe 无 stdout，故结果写 report.json（调用方读它判定通过）。

要点：复用与正式运行完全相同的 db / indexer / search / text_tokenize 代码路径，
所以这是对「打包是否把 OpenCC 词典、FTS5、各模块都带齐」的硬验证——
dev 下 pytest 全绿但 frozen 漏了词典，会在这里暴露。
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

# (用例名, 查询, 目标文件名, 期望命中?) —— 镜像 tests/test_recall_corner.py 的 11 条铁律
CASES = [
    ("跨词子串(明硕→小明硕士)", "明硕", "01_cross.pptx", True),
    ("run截断(小明|硕士毕业→明硕)", "明硕", "02_runsplit.pptx", True),
    ("长词内片段(人民→中华人民共和国)", "人民", "03_longword.pptx", True),
    ("多词同页(明硕 AI)", "明硕 AI", "04_samepage.pptx", True),
    ("多词只认同页·跨页不算(明硕 AI 分散不同页)", "明硕 AI", "05_crosspage.pptx", False),  # 1A 收紧
    ("精度·不相邻不误中(明硕)", "明硕", "06_precision.pptx", False),
    ("全角ＡＩ→AI", "AI", "07_fullwidth.pptx", True),
    ("繁体→简体(软件)", "软件", "08_traditional.pptx", True),
    ("大小写(gpt→GPT)", "gpt", "09_case.pptx", True),
    ("数字型号(910)", "910", "10_number.pptx", True),
]


def _run(pptx_dir: str) -> dict:
    from . import db, indexer, search
    from .text_tokenize import normalize

    src = Path(pptx_dir)
    files = sorted(src.glob("*.pptx"))
    td = tempfile.mkdtemp(prefix="pptxfinder_selftest_")
    try:
        conn = db.connect(Path(td) / "selftest.db")
        try:
            db.init_db(conn)
            # scan_iter 直接喂文件列表，绕过目录排除规则，保证测试集一定被索引
            indexer.update_index(conn, [str(src)], workers=1, scan_iter=iter(files))

            # OpenCC 繁简是否在 frozen 真实生效（词典打包成功的硬证据）
            opencc_ok = "软件" in normalize("軟件開發")

            cases = []
            for label, query, fname, expect in CASES:
                names = [r.name for r in search.search(conn, query)]
                got = fname in names
                cases.append({
                    "label": label, "query": query, "file": fname,
                    "expect": expect, "got": got, "pass": got == expect,
                })
            stats = db.stats(conn)
        finally:
            conn.close()  # WAL 模式必须先关连接，否则 Windows 下 db 文件被锁、临时目录删不掉
    finally:
        shutil.rmtree(td, ignore_errors=True)

    passed = sum(1 for c in cases if c["pass"])
    return {
        "opencc_ok": opencc_ok,
        "indexed_files": stats["file_count"],
        "indexed_pages": stats["page_count"],
        "passed": passed,
        "total": len(cases),
        "all_pass": passed == len(cases) and opencc_ok,
        "cases": cases,
    }


def run_selftest(argv: list[str]) -> int:
    """argv: [..., '--selftest', <pptx_dir>, <report_path>]。返回 0=全通过，1=有失败。"""
    i = argv.index("--selftest")
    pptx_dir = argv[i + 1] if len(argv) > i + 1 else "."
    report = argv[i + 2] if len(argv) > i + 2 else "selftest_report.json"
    try:
        data = _run(pptx_dir)
        ok = bool(data.get("all_pass"))
    except Exception as e:  # noqa: BLE001 自检兜底：任何异常都落盘成报告，便于诊断
        import traceback
        data = {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(), "all_pass": False}
        ok = False
    Path(report).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ok else 1
