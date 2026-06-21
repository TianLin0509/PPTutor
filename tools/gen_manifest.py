"""遍历 dist 目录生成 manifest.json（增量更新清单）。

用法:
    python tools/gen_manifest.py <dist_dir> [version] [notes]

默认 version 取 pptx_finder.__version__，写入 <dist_dir>/manifest.json。
打包流程：pyinstaller 出 dist/PPTutor/ 后跑本脚本，把清单塞进 dist（随包发布 +
供运行时与远端比对）。manifest.json 自身不计入清单。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pptx_finder import __version__
from pptx_finder.updater import MANIFEST_NAME, build_manifest


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python tools/gen_manifest.py <dist_dir> [version] [notes]")
        return 2
    dist = Path(sys.argv[1])
    if not dist.is_dir():
        print(f"目录不存在: {dist}")
        return 2
    version = sys.argv[2] if len(sys.argv) > 2 else __version__
    notes = sys.argv[3] if len(sys.argv) > 3 else ""
    m = build_manifest(dist, version, notes)
    out = dist / MANIFEST_NAME
    out.write_text(json.dumps(m, ensure_ascii=False, indent=0), encoding="utf-8")
    total = sum(f["size"] for f in m["files"].values())
    print(f"manifest v{version}: {len(m['files'])} 文件, {total/1024/1024:.1f} MB -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
