"""E2E 真实验证：模拟用户用版本管理全流程（起真实 watcher 监听）。

隔离 data dir，不碰真实库。覆盖：
纳管 → 改存（真实文件事件，watcher 自动快照）→ 累积版本 → 恢复旧版 → 跨版本搜已删内容 → 误删找回。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="pptxver_e2e_")
os.environ["PPTX_FINDER_DATA_DIR"] = str(Path(_tmp) / "appdata")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import fixtures_gen as fx  # noqa: E402

from pptx_finder.parser import parse_pptx  # noqa: E402
from pptx_finder.versioning import vault  # noqa: E402
from pptx_finder.versioning.manager import VersionManager  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name: str, cond: bool) -> None:
    results.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'} | {name}")


def _text(path: str) -> str:
    return "".join(pg.raw_text for pg in parse_pptx(path).pages)


def main() -> int:
    work = Path(_tmp) / "work"
    work.mkdir(parents=True)
    p = work / "算力方案.pptx"
    fx.make_pptx(p, [{"body": "封面 算力方案"}, {"body": "第二页 量子计算 章节"}])

    mgr = VersionManager()
    mgr.add_root(str(work))
    check("纳管目录后自动建首版", len(mgr.list_versions(str(p))) == 1)

    did = vault.doc_id_for(str(p))
    mgr.start()  # 起真实 watcher（watchdog 后台线程）
    try:
        # 模拟用户改内容后保存（覆盖原文件 = 真实文件系统事件）
        fx.make_pptx(p, [{"body": "封面 算力方案 v2"}, {"body": "第二页 改成 经典计算"}])
        time.sleep(2.8)  # 等防抖(1.5s)+ 快照
        check("保存后 watcher 全自动记一版（用户无感）", len(mgr.list_versions(str(p))) == 2)

        fx.make_pptx(p, [{"body": "终稿封面"}, {"body": "终稿正文"}, {"body": "新增第三页"}])
        time.sleep(2.8)
        check("再次保存累积到 3 版", len(mgr.list_versions(str(p))) == 3)
    finally:
        mgr.stop()  # 停 watcher，后续用 API 验证（避免监听干扰）

    vers = mgr.list_versions(str(p))  # ts 降序
    v_first = vers[-1]["version_id"]

    # 恢复最早版本
    mgr.restore_to(str(p), v_first)
    check("恢复首版 → 内容回到最初", "算力方案" in _text(str(p)) and "终稿" not in _text(str(p)))

    # 跨版本搜：现在文件里早没有“量子计算”了，但历史版本搜得到
    hits = mgr.search_history("量子计算")
    check("跨版本搜到已删除的『量子计算』", any(h["doc_id"] == did for h in hits))

    # 误删找回
    p.unlink()
    check("原文件删除被识别", mgr.scan_deleted() >= 1)
    mgr.recover(did)
    check("从版本库找回文件（重建成功）", p.exists() and len(_text(str(p))) > 0)

    ok = sum(1 for _, c in results if c)
    print(f"\n=== E2E 版本管理: {ok}/{len(results)} 通过 ===")
    shutil.rmtree(_tmp, ignore_errors=True)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
