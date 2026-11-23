"""把某版的 Base 安装包放到备用服务器的 openx-stage/，供公司电脑转传到内源（公司直连 GitHub 只有约 50 KB/s，200 MB 安装包常超时）。

openx-stage/ 只留这一个安装包：放新的同时删掉旧的（谁放谁淘汰）。用法：
    uv run --frozen --with paramiko python tools/openx_stage.py 2.1.11
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mirror_release as mr  # noqa: E402

STAGE = "openx-stage"
CACHE = Path(__file__).resolve().parents[1] / "artifacts" / "mirror-cache"


def main(version: str) -> int:
    version = version.lstrip("vV")
    name = f"PPT-Doctor-Setup-v{version}-Base.exe"
    asset = next((a for a in mr._github_assets(f"v{version}") if a.get("name") == name), None)
    if not asset or not str(asset.get("digest") or "").startswith("sha256:"):
        print(f"[!] v{version} 发布页没有带摘要的 {name}")
        return 1
    want = asset["digest"][7:]
    local = CACHE / f"v{version}" / name
    mr._download(asset["browser_download_url"], local, int(asset["size"]))
    if mr._sha256(local) != want:
        mr._download(asset["browser_download_url"], local, int(asset["size"]), force=True)
        if mr._sha256(local) != want:
            print(f"[!] {name} 摘要不符")
            return 1
    client = mr._connect()
    try:
        sftp = client.open_sftp()
        remote_dir = f"{mr.REMOTE}/{STAGE}"
        mr._ensure_dir(sftp, remote_dir)
        tmp = f"{remote_dir}/{name}.uploading"
        sftp.put(str(local), tmp)
        mr.replace_remote(client, tmp, f"{remote_dir}/{name}")
        for f in sftp.listdir(remote_dir):
            if f != name:
                sftp.remove(f"{remote_dir}/{f}")
        sftp.close()
    finally:
        client.close()
    print(f"[ok] {mr.PUBLIC}/{STAGE}/{name}  {int(asset['size']) / 1048576:.1f} MB")
    try:
        local.unlink()  # 本机缓存可重建，用完删
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
