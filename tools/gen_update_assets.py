# -*- coding: utf-8 -*-
r"""生成「GitHub Release 更新源」需要的资产：manifest.json + 变化块。

用法：
    uv run python tools/gen_update_assets.py                # 与上一个已发布版比
    uv run python tools/gen_update_assets.py <旧清单或旧zip>  # 与指定的旧版比

产物落在 artifacts/update-assets/，随发布一起 `gh release upload` 上去。

为什么需要这个：自动更新原本只有一个自建源，而那台服务器上的清单长期停在 v1.2.7
——`compare()` 只在远端版本更高时才返回更新，于是 >=1.2.7 的用户永远收不到更新，
且完全没有报错。加了 GitHub Release 作为第二个源之后，更新器会挑**版本最高**的源，
不再被单点拖死。

GitHub 的资产是平铺的（没有 files/ 子目录），所以块直接以 sha256 为资产名上传。
实测相邻两版之间只有 2 个文件真的变（exe + base_library.zip），增量 7.0 MB /
全量 91.3 MB = 7.6%，所以每次发布只需多传 3 个小资产。

落后好几版的用户需要的块可能没随最新一版发布，那种情况更新器会自动回落到整包
（zip 本来就是发布资产），所以这里**不必**为历史版本补块。
"""
from __future__ import annotations

import json
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pptx_finder import __version__  # noqa: E402
from pptx_finder.config import DIST_DIR_NAME  # noqa: E402
from pptx_finder.updater import GITHUB_RELEASE_LATEST, MANIFEST_NAME  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

DIST = ROOT / "dist" / DIST_DIR_NAME
OUT = ROOT / "artifacts" / "update-assets"


def _manifest_from_zip(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.replace("\\", "/").endswith("/" + MANIFEST_NAME):
                return json.loads(z.read(name).decode("utf-8"))
    raise SystemExit(f"[!] {path} 里没有 {MANIFEST_NAME}")


def _published_manifest() -> dict | None:
    """已发布的最新清单。第一次跑时它还不存在（404），属正常。"""
    url = f"{GITHUB_RELEASE_LATEST}/{MANIFEST_NAME}"
    try:
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  (线上还没有 {MANIFEST_NAME}：HTTP {e.code} —— 首次发布是正常的)")
    except Exception as e:  # noqa: BLE001
        print(f"  (拉线上清单失败：{type(e).__name__}: {e})")
    return None


def main() -> int:
    local_path = DIST / MANIFEST_NAME
    if not local_path.is_file():
        print(f"[!] 找不到 {local_path}\n  先跑: uv run python tools/package_dist.py")
        return 2
    new = json.loads(local_path.read_text(encoding="utf-8"))
    if str(new.get("version")) != __version__:
        print(f"[!] dist 清单是 v{new.get('version')}，包版本是 v{__version__}；请先重新打包")
        return 1

    if len(sys.argv) > 1:
        arg = Path(sys.argv[1])
        old = _manifest_from_zip(arg) if arg.suffix.lower() == ".zip" else \
            json.loads(arg.read_text(encoding="utf-8"))
        print(f"[*] 与 {arg.name}（v{old.get('version')}）比对")
    else:
        old = _published_manifest()
        if old is not None:
            print(f"[*] 与线上已发布的 v{old.get('version')} 比对")

    of = (old or {}).get("files", {})
    nf = new["files"]
    changed = [(rel, meta) for rel, meta in nf.items()
               if of.get(rel, {}).get("hash") != meta["hash"]]

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local_path, OUT / MANIFEST_NAME)

    total = 0
    for rel, meta in changed:
        src = DIST / rel.replace("/", "\\")
        if not src.is_file():
            print(f"[!] dist 里缺少 {rel}")
            return 1
        # GitHub 资产平铺，块名就是 sha256（更新器按同样规则拼 URL）
        shutil.copy2(src, OUT / meta["hash"])
        total += meta["size"]

    full = sum(m["size"] for m in nf.values())
    print(f"[OK] {OUT}")
    print(f"  manifest.json + {len(changed)} 个变化块")
    print(f"  增量 {total/1048576:.2f} MB / 全量 {full/1048576:.1f} MB"
          f"（{total/full*100:.1f}%）" if full else "")
    for rel, meta in sorted(changed, key=lambda x: -x[1]["size"])[:6]:
        print(f"     {meta['size']/1024:9.0f} KB  {rel}")
    print()
    print("  上传（把它们加进本次 release 的资产）：")
    print(f'    gh release upload v{__version__} "{OUT}\\*" --clobber')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
