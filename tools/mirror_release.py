# -*- coding: utf-8 -*-
r"""把一个已发布版本同步到自建更新源（ECS 上的 Caddy 静态目录），作为 GitHub 之外的备用源。

为什么要它：v2.0 起客户端按「GitHub → 备用服务器」逐源回落（照 AI Hub 一键更新）。备用
服务器上的清单此前长期停在 v1.2.7、识别组件目录 404——只有每次发版都同步上去，备用源才真
的能兜住「GitHub 连不上 / 被代理拦截」的用户。

用法（上传一律等田哥点头才跑）：
    uv run --with paramiko python tools/mirror_release.py 2.0.0 --dry-run
    $env:ECS_PWD="..."; uv run --with paramiko python tools/mirror_release.py 2.0.0
    ...                                   --components        # 顺带同步 OCR / 公式组件
    ...                                   --from-dir <目录>   # 用本地产物，不从 GitHub 下
    ...                                   --baseline-zip <旧Full.zip>  # 首次迁移已保留整包
    ...                                   --refresh-full runtime-change  # 运行时变化，显式更换基线
    ...                                   --refresh-full missing-baseline  # 没有可用基线
    ...                                   --prune-full <旧ZIP相对路径> ...  # 新装/升级验收后单独清理

资产来源默认是 GitHub Release `v<版本>` 的资产（与客户端从 GitHub 拿到的逐字节相同）；
本地缓存在 artifacts/mirror-cache/v<版本>/，重跑会跳过已下好且大小对的文件。

服务器布局（https://me.lt-stockpartner.tech/pptutor ↔ C:/pptutor-updates）：
    manifest.json / manifest-Base.json / manifest-Ocr.json      三个安装版各自的清单
    files/<sha256>                                              增量块（内容寻址）
    PPT-Doctor-v<基线版本>.zip                                 仅一个Full基线
    ocr/component.json + ocr/component.zip                      文字识别组件
    formula/component.json + formula/component.zip              公式识别组件
    materials/component.json + materials/component.zip          团队素材包（不进安装包）

默认不上传本版ZIP；镜像manifest.json的full沿用已核验基线。原GitHub资产不改。
先核验三个安装版需要的全部块（缺块从同构建ZIP补出），依赖公网验证后才原子切清单。
旧基线沿用完整核验记录+本轮HEAD；新基线完整公网SHA256。旧包必须验收后显式清理。
--dry-run不连接镜像、不下载ZIP。默认仅查GitHub资产元数据，离线用--from-dir。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

HOST = "8.130.158.231"
USER = "Administrator"
REMOTE = "C:/pptutor-updates"
PUBLIC = "https://me.lt-stockpartner.tech/pptutor"
REPO = "TianLin0509/PPTutor"
COMPONENT_TAGS = {"ocr": "ocr-v1.2.0", "formula": "formula-v1.0.0"}
#: 团队素材包：内容单独发版（materials-vX.Y.Z），客户端在 /pptutor/materials/ 取最新版；
#: 标签还没发布时跳过，不影响其它组件的同步。新版本素材包发布后把这里改成新标签。
OPTIONAL_COMPONENT_TAGS = {"materials": "materials-v1.0.0"}
MANIFESTS = ("manifest.json", "manifest-Base.json", "manifest-Ocr.json")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
EDITIONS = dict(zip(MANIFESTS, ("full", "base", "ocr")))


@dataclass
class Item:
    local: Path
    remote: str          # 相对 REMOTE 的路径（正斜杠）
    size: int
    sha256: str
    last: bool = False   # 清单：最后传

    @property
    def url(self) -> str:
        return f"{PUBLIC}/{self.remote}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _github_assets(tag: str) -> list[dict]:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/tags/{tag}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "pptdoctor-mirror"})
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8")).get("assets", [])


def _download(url: str, dest: Path, size: int, *, force: bool = False) -> None:
    if not force and dest.is_file() and dest.stat().st_size == size:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    done = 0
    t0 = time.monotonic()
    with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if size > 20 << 20 and done % (50 << 20) < (1 << 20):
                rate = done / max(0.1, time.monotonic() - t0) / 1048576
                print(f"    {dest.name}: {done / 1048576:.0f}/{size / 1048576:.0f} MB · {rate:.1f} MB/s")
    if part.stat().st_size != size:
        raise SystemExit(f"[!] {dest.name} 下载不完整：{part.stat().st_size} != {size}")
    os.replace(part, dest)


def collect_from_github(version: str, cache: Path, components: bool,
                        download: bool = True, *, refresh_full: bool = False) -> list[Item]:
    """download=False（--dry-run）只按 GitHub 的资产元数据列清单，不下载几百 MB 的包。"""
    items: list[Item] = []
    print(f"[*] 读取 GitHub Release v{version} 的资产清单")
    for asset in _github_assets(f"v{version}"):
        name = asset["name"]
        if name in MANIFESTS:
            remote, last = name, True
        elif _HEX64.fullmatch(name):
            remote, last = f"files/{name}", False
        elif refresh_full and name == f"PPT-Doctor-v{version}.zip":
            remote, last = name, False
        else:
            continue          # 安装器 exe、SHA256SUMS 之类不是更新器要的
        dest = cache / name
        if download:
            _download(asset["browser_download_url"], dest, asset["size"])
        items.append(Item(dest, remote, asset["size"], _sha256(dest) if download else "", last))
    if components:
        for kind, tag in {**COMPONENT_TAGS, **OPTIONAL_COMPONENT_TAGS}.items():
            print(f"[*] 读取组件 {tag}")
            try:
                assets = _github_assets(tag)
            except urllib.error.HTTPError as exc:
                if exc.code == 404 and kind in OPTIONAL_COMPONENT_TAGS:
                    print(f"    {tag} 尚未发布，跳过")
                    continue
                raise
            for asset in assets:
                if asset["name"] not in ("component.json", "component.zip"):
                    continue
                dest = cache / kind / asset["name"]
                if download:
                    _download(asset["browser_download_url"], dest, asset["size"])
                # component.json 最后传：它指向的 zip 先到位
                items.append(Item(dest, f"{kind}/{asset['name']}", asset["size"],
                                  _sha256(dest) if download else "",
                                  asset["name"] == "component.json"))
    return items


def collect_from_dir(version: str, src: Path, components: bool, *,
                     refresh_full: bool = False) -> list[Item]:
    """本地产物目录：update-assets 的平铺块 + 清单 + 整包 zip；组件放 ocr/ formula/ 子目录。"""
    items: list[Item] = []
    for p in sorted(src.iterdir()):
        if not p.is_file():
            continue
        if p.name in MANIFESTS:
            items.append(Item(p, p.name, p.stat().st_size, _sha256(p), True))
        elif _HEX64.fullmatch(p.name):
            items.append(Item(p, f"files/{p.name}", p.stat().st_size, _sha256(p)))
        elif refresh_full and p.name == f"PPT-Doctor-v{version}.zip":
            items.append(Item(p, p.name, p.stat().st_size, _sha256(p)))
    if components:
        for kind in {**COMPONENT_TAGS, **OPTIONAL_COMPONENT_TAGS}:
            for name in ("component.zip", "component.json"):
                p = src / kind / name
                if p.is_file():
                    items.append(Item(p, f"{kind}/{name}", p.stat().st_size, _sha256(p),
                                      name == "component.json"))
    return items


def version_key(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise SystemExit(f"[!] 正式版本号必须为 x.y.z：{value!r}")
    return tuple(map(int, value.split(".")))


def safe_path(value: str) -> str:
    if (not isinstance(value, str) or not value
            or not re.fullmatch(r"[A-Za-z0-9_. /-]+", value)
            or any(p in ("", ".", "..") or p.endswith((" ", ".")) for p in value.split("/"))):
        raise SystemExit(f"[!] 不是安全相对路径：{value!r}")
    return value


def validate_full(data: dict) -> dict:
    if not isinstance(data, dict):
        raise SystemExit("[!] full 必须包含 version/path/sha256")
    version_key(data.get("version"))
    path = safe_path(data.get("path"))
    # 与Hub下载器的相对ZIP规则一致；不能生成Doctor接受而Hub拒绝的full。
    if not re.fullmatch(r"(?:[a-z0-9_-]+/)*[a-z0-9][a-z0-9._-]*\.zip", path,
                        flags=re.IGNORECASE | re.ASCII):
        raise SystemExit("[!] full 路径必须符合Hub相对ZIP规则，目录只允许字母数字下划线和短横线")
    if (PurePosixPath(path).name != f"PPT-Doctor-v{data['version']}.zip"
            or not isinstance(data.get("sha256"), str) or not _HEX64.fullmatch(data["sha256"])):
        raise SystemExit("[!] full 必须指向版本匹配的 Full ZIP，且含64位小写SHA256")
    return {k: data[k] for k in ("version", "path", "sha256")}


def validate_manifest(data: dict, edition: str) -> None:
    version_key(data.get("version"))
    if data.get("edition", "full") != edition:
        raise SystemExit(f"[!] 清单安装版应为 {edition}")
    if not isinstance(data.get("files"), dict):
        raise SystemExit("[!] 清单缺 files")
    for path, meta in data["files"].items():
        safe_path(path)
        if (not isinstance(meta, dict) or not _HEX64.fullmatch(str(meta.get("hash", "")))
                or type(meta.get("size")) is not int or meta["size"] < 0):
            raise SystemExit(f"[!] 清单文件哈希/大小无效：{path}")


def check_consistency(items: list[Item], version: str | None = None) -> None:
    """检查三渠道、路径和已选文件；远端块闭包由 publish 在切清单前核验。"""
    by_remote = {i.remote: i for i in items}
    if len(by_remote) != len(items) or not set(MANIFESTS).issubset(by_remote):
        raise SystemExit("[!] 必须有且只有一份三个安装版清单，资产路径不能重复")
    versions = set()
    for item in items:
        safe_path(item.remote)
        if item.size != item.local.stat().st_size or item.sha256 != _sha256(item.local):
            raise SystemExit(f"[!] 本地资产大小/哈希变化：{item.remote}")
        if item.remote in MANIFESTS:
            data = json.loads(item.local.read_text("utf-8"))
            validate_manifest(data, EDITIONS[item.remote])
            if "full" in data:
                if item.remote != "manifest.json":
                    raise SystemExit("[!] 不能给 Base/Ocr 清单套 Full 基线")
                validate_full(data["full"])
            versions.add(data["version"])
    if len(versions) > 1 or (version is not None and versions != {version}):
        raise SystemExit(f"[!] 三个清单版本不一致：{versions}")
    for remote, item in by_remote.items():
        if remote.startswith("files/") and remote[6:] != item.sha256:
            raise SystemExit(f"[!] 块 {remote} 的内容哈希对不上文件名")


def inspect_package(path: Path, version: str, edition: str = "full") -> dict:
    """只读 ZIP，核对安装版、逐文件哈希；不解压任意路径到文件系统。"""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if len(names) != len(set(names)):
            raise SystemExit("[!] ZIP 有重名成员")
        data = json.loads(z.read("PPT-Doctor/manifest.json"))
        validate_manifest(data, edition)
        if data["version"] != version:
            raise SystemExit("[!] ZIP 内版本与基线版本不符")
        marker = "PPT-Doctor/_internal/edition.json"
        if marker in names and json.loads(z.read(marker)).get("edition") != edition:
            raise SystemExit("[!] ZIP 安装版标记不符")
        for rel, meta in data["files"].items():
            h = hashlib.sha256()
            size = 0
            with z.open("PPT-Doctor/" + rel) as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
                    size += len(chunk)
            if h.hexdigest() != meta["hash"] or size != meta["size"]:
                raise SystemExit(f"[!] ZIP 内容与清单哈希不符：{rel}")
    return data


def blocks_from_package(path: Path, expected: dict, needed: set[str], cache: Path) -> list[Item]:
    manifest = inspect_package(path, expected["version"], expected.get("edition", "full"))
    if manifest["files"] != expected["files"]:
        raise SystemExit("[!] 补块 ZIP 与发布清单不是同一构建")
    cache.mkdir(parents=True, exist_ok=True)
    items = []
    with zipfile.ZipFile(path) as z:
        for rel, meta in manifest["files"].items():
            digest = meta["hash"]
            if digest not in needed:
                continue
            dest = cache / digest
            with z.open("PPT-Doctor/" + rel) as src, dest.open("wb") as out:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    out.write(chunk)
            if _sha256(dest) != digest or dest.stat().st_size != meta["size"]:
                raise SystemExit(f"[!] 补块哈希不符：{rel}")
            items.append(Item(dest, "files/" + digest, meta["size"], digest))
            needed = needed - {digest}
    return items


def make_mirror_manifest(items: list[Item], full: dict, cache: Path) -> list[Item]:
    full = validate_full(full)
    original = next(i for i in items if i.remote == "manifest.json")
    data = json.loads(original.local.read_text("utf-8"))
    if version_key(full["version"]) > version_key(data["version"]):
        raise SystemExit("[!] 基线版本不能高于最新增量版本")
    data["full"] = full
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / "manifest.json"
    if dest.resolve() == original.local.resolve():
        raise SystemExit("[!] 镜像清单必须使用独立副本，不能覆盖原始发布资产")
    dest.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    replacement = Item(dest, original.remote, dest.stat().st_size, _sha256(dest), True)
    return [replacement if i is original else i for i in items]


def public_manifest(name: str = "manifest.json") -> dict:
    if name not in MANIFESTS:
        raise SystemExit("[!] 未知安装版清单")
    req = urllib.request.Request(f"{PUBLIC}/{name}", headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {}
        raise


def public_size(remote: str) -> int:
    safe_path(remote)
    req = urllib.request.Request(f"{PUBLIC}/{remote}", method="HEAD",
                                 headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as response:
        if "html" in response.headers.get("Content-Type", "").lower():
            raise SystemExit(f"[!] 基线地址返回HTML：{remote}")
        size = int(response.headers.get("Content-Length", -1))
    if size <= 0:
        raise SystemExit(f"[!] 基线大小不可用：{remote}")
    return size


@dataclass
class Baseline:
    item: Item
    manifest: dict
    verification: str  # retained-record+HEAD / downloaded-sha256 / pending-upload

    @property
    def full(self) -> dict:
        return {"version": self.manifest["version"], "path": self.item.remote,
                "sha256": self.item.sha256}


def prepare_baseline(items: list[Item], previous: dict, cache: Path, record_path: Path,
                     *, refresh_full: bool = False, adopt_zip: Path | None = None) -> Baseline:
    latest = json.loads(next(i.local for i in items if i.remote == "manifest.json").read_text("utf-8"))
    if previous and version_key(previous.get("version")) > version_key(latest["version"]):
        raise SystemExit("[!] 不允许把镜像清单降到旧版本")
    if refresh_full:
        name = f"PPT-Doctor-v{latest['version']}.zip"
        item = next((i for i in items if i.remote == name), None)
        if item is None:
            raise SystemExit(f"[!] 更新基线缺 Full ZIP：{name}")
        data = inspect_package(item.local, latest["version"])
        if data != latest:
            raise SystemExit("[!] Full ZIP 与原始发布清单不是同一构建")
        if "full" in previous:
            old = validate_full(previous["full"])
            if old["path"] == item.remote and old["sha256"] != item.sha256:
                raise SystemExit("[!] 不允许覆盖同版本但哈希不同的基线ZIP")
        return Baseline(item, data, "pending-upload")
    if adopt_zip is not None:
        match = re.fullmatch(r"PPT-Doctor-v([0-9]+\.[0-9]+\.[0-9]+)\.zip", adopt_zip.name)
        if not match:
            raise SystemExit("[!] --baseline-zip 必须是已保留的 Full ZIP")
        data = inspect_package(adopt_zip, match[1])
        item = Item(adopt_zip, adopt_zip.name, adopt_zip.stat().st_size, _sha256(adopt_zip))
        if "full" in previous and validate_full(previous["full"]) != Baseline(item, data, "").full:
            raise SystemExit("[!] 已有full时不能用迁移选项换包；请显式--refresh-full")
        if version_key(data["version"]) > version_key(latest["version"]):
            raise SystemExit("[!] 基线版本不能高于最新版本")
        if verify_public([item]):
            raise SystemExit("[!] 指定的旧基线尚未在公网完整核验通过")
        return Baseline(item, data, "downloaded-sha256")
    if "full" not in previous:
        raise SystemExit("[!] 旧清单没有full：用--baseline-zip迁移已核验旧包，或--refresh-full missing-baseline")
    full = validate_full(previous["full"])
    if version_key(full["version"]) > version_key(latest["version"]):
        raise SystemExit("[!] 基线版本不能高于最新版本")
    size = public_size(full["path"])
    if record_path.is_file():
        record = json.loads(record_path.read_text("utf-8"))
        if (record.get("public") == PUBLIC and record.get("full") == full
                and record.get("size") == size and record.get("verifiedAt")):
            data = record["manifest"]
            validate_manifest(data, "full")
            if data["version"] != full["version"]:
                raise SystemExit("[!] 基线核验记录版本不一致")
            return Baseline(Item(Path(), full["path"], size, full["sha256"]), data,
                            "retained-record+HEAD")
    # 新机器或首次导入记录：只此一次下载旧基线；后续沿用完整核验记录+HEAD。
    path = cache / "baseline" / PurePosixPath(full["path"]).name
    _download(f"{PUBLIC}/{full['path']}", path, size, force=True)
    if _sha256(path) != full["sha256"]:
        raise SystemExit("[!] 公网基线SHA256与full不一致")
    data = inspect_package(path, full["version"])
    return Baseline(Item(path, full["path"], size, full["sha256"]), data, "downloaded-sha256")


def save_baseline_record(path: Path, baseline: Baseline) -> None:
    record = {"public": PUBLIC, "full": baseline.full, "size": baseline.item.size,
              "manifest": baseline.manifest, "verifiedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    # 沿用记录不能把旧的全量核验时间伪装成本轮新下载。
    if baseline.verification == "retained-record+HEAD":
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), "utf-8")
    os.replace(temp, path)


def package_for(version: str, edition: str, src: Path | None, cache: Path) -> Path:
    suffix = "" if edition == "full" else "-" + edition.title()
    name = f"PPT-Doctor-v{version}{suffix}.zip"
    if src is not None:
        path = src / name
        if not path.is_file():
            raise SystemExit(f"[!] 缺历史块，补块需要同一构建的 {path}")
        return path
    asset = next((a for a in _github_assets(f"v{version}") if a["name"] == name), None)
    if asset is None:
        raise SystemExit(f"[!] GitHub 缺补块用安装包：{name}")
    path = cache / name
    print(f"[*] 仅为修复缺块读取 {name}，不会把该ZIP加入上传计划")
    _download(asset["browser_download_url"], path, asset["size"])
    return path


def required_blocks(items: list[Item]) -> dict[str, int]:
    required = {}
    for item in items:
        if item.remote not in MANIFESTS:
            continue
        for meta in json.loads(item.local.read_text("utf-8"))["files"].values():
            digest, size = meta["hash"], meta["size"]
            if digest in required and required[digest] != size:
                raise SystemExit("[!] 相同哈希的块大小不一致")
            required[digest] = size
    return required


def _powershell(client, script: str) -> str:
    preamble = ("$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
                "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);")
    encoded = base64.b64encode((preamble + script).encode("utf-16le")).decode()
    _, stdout, stderr = client.exec_command(
        "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand " + encoded,
        timeout=120)
    output = stdout.read().decode("utf-8-sig")
    error = stderr.read().decode("utf-8", errors="replace")
    if stdout.channel.recv_exit_status() != 0:
        raise SystemExit(f"[!] 镜像文件操作失败：{error.strip()}")
    return output


def remote_hashes(client, remotes: list[str]) -> dict[str, dict]:
    """在服务器本地核对已存块，避免每版通过公网重下整个运行时。"""
    result = {}
    # OpenSSH默认经过cmd.exe；双层Base64后40块超过8191字符，16块留足余量。
    for start in range(0, len(remotes), 16):
        batch = [safe_path(p) for p in remotes[start:start + 16]]
        encoded = base64.b64encode(json.dumps(batch).encode("utf-8")).decode()
        script = ("[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
                  f"$names=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')));"
                  "$rows=@(foreach($name in $names){"
                  f"$p='{REMOTE}/'+$name;"
                  "if([IO.File]::Exists($p)){"
                  "@{path=$name;size=(Get-Item -LiteralPath $p).Length;"
                  "sha256=(Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant()}}});"
                  "ConvertTo-Json -InputObject $rows -Compress")
        for row in json.loads(_powershell(client, script)):
            if row.get("path") not in batch:
                raise SystemExit("[!] 镜像哈希结果含未知路径")
            result[row["path"]] = row
    return result


def replace_remote(client, temp: str, destination: str) -> None:
    # Windows OpenSSH不保证支持posix-rename；同卷File.Replace保持旧清单到替换成功。
    # 不使用先remove再rename，失败不会留下清单404空窗。
    for path in (temp, destination):
        if not path.startswith(REMOTE + "/"):
            raise SystemExit("[!] 远端路径不在镜像目录")
        safe_path(path[len(REMOTE) + 1:])
    script = (f"$src='{temp}';$dst='{destination}';"
              "for($attempt=0;;$attempt++){try{"
              "if([IO.File]::Exists($dst)){[IO.File]::Replace($src,$dst,[NullString]::Value)}"
              "else{[IO.File]::Move($src,$dst)};break"
              "}catch{if($attempt -ge 5){throw};Start-Sleep -Milliseconds 200}}")
    _powershell(client, script)


def _connect():
    import paramiko  # noqa: PLC0415 只有真上传才需要
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    pwd = os.environ.get("ECS_PWD")
    key = Path(os.environ.get("ECS_KEY", Path.home() / ".ssh" / "codex_aliyun"))
    if pwd:
        client.connect(HOST, 22, USER, pwd, timeout=20)
    elif key.is_file():
        client.connect(HOST, 22, USER, key_filename=str(key), look_for_keys=False,
                       allow_agent=False, timeout=20)
    else:
        raise SystemExit("[!] 需要环境变量 ECS_PWD（或 ECS_KEY 指向私钥）")
    return client


def _ensure_dir(sftp, path: str) -> None:
    parts = path.split("/")
    for i in range(2, len(parts) + 1):
        sub = "/".join(parts[:i])
        try:
            sftp.stat(sub)
        except OSError:
            sftp.mkdir(sub)


def upload(items: list[Item], client=None) -> None:
    owned = client is None
    client = client or _connect()
    sftp = client.open_sftp()
    ordered = sorted(items, key=lambda i: i.last)
    total = sum(i.size for i in ordered)
    sent = 0
    try:
        for n, item in enumerate(ordered, 1):
            remote = f"{REMOTE}/{safe_path(item.remote)}"
            _ensure_dir(sftp, remote.rsplit("/", 1)[0])
            tmp = remote + ".uploading-" + uuid.uuid4().hex
            try:
                sftp.put(str(item.local), tmp)
                replace_remote(client, tmp, remote)
            finally:
                try:
                    sftp.remove(tmp)  # 只清本轮唯一的暂存文件，不删除旧目标
                except OSError:
                    pass
            sent += item.size
            print(f"  [{n}/{len(ordered)}] {item.remote}  {item.size / 1048576:.1f} MB"
                  f"（累计 {sent / 1048576:.0f}/{total / 1048576:.0f} MB）")
    finally:
        sftp.close()
        if owned:
            client.close()


def verify_public(items: list[Item]) -> int:
    bad = 0
    for item in items:
        h = hashlib.sha256()
        got = 0
        try:
            req = urllib.request.Request(item.url, headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=60) as r:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    h.update(chunk)
                    got += len(chunk)
            ok = got == item.size and h.hexdigest() == item.sha256
            why = "" if ok else f"大小 {got}/{item.size}，sha256 {'一致' if h.hexdigest() == item.sha256 else '不一致'}"
        except Exception as exc:  # noqa: BLE001
            ok, why = False, f"{type(exc).__name__}: {exc}"
        bad += not ok
        print(f"  {'OK ' if ok else 'BAD'} {item.remote} {why}")
    return bad


def publish(items: list[Item], baseline: Baseline, cache: Path, *,
            src: Path | None = None, verify_only: bool = False) -> dict:
    required = required_blocks(items)
    audit = {"full": baseline.full, "baselineVerification": baseline.verification,
             "requiredBlocks": len(required), "uploaded": [], "complete": False}
    if verify_only:
        blocks = [Item(Path(), "files/" + h, size, h) for h, size in required.items()]
        # 显式逐块公网复核；旧基线仍按审计记录注明完整哈希或沿用记录+HEAD。
        if verify_public(blocks + [i for i in items if not i.remote.startswith("files/")]):
            raise SystemExit("[!] 公网完整复核失败")
        if baseline.verification == "pending-upload":
            baseline.verification = "downloaded-sha256"
        audit.update(complete=True, baselineVerification=baseline.verification)
        return audit
    client = _connect()
    try:
        inventory = remote_hashes(client, ["files/" + h for h in required])
        missing = {h for h, size in required.items()
                   if inventory.get("files/" + h, {}).get("sha256") != h
                   or inventory.get("files/" + h, {}).get("size") != size}
        audit["serverHashVerifiedBlocks"] = len(required) - len(missing)
        available = {i.sha256: i for i in items if i.remote.startswith("files/")}
        unrecovered = missing - available.keys()
        for item in items:
            if item.remote not in MANIFESTS or not unrecovered:
                continue
            data = json.loads(item.local.read_text("utf-8"))
            wanted = unrecovered & {m["hash"] for m in data["files"].values()}
            if not wanted:
                continue
            package = package_for(data["version"], EDITIONS[item.remote], src, cache)
            for block in blocks_from_package(package, data, wanted, cache / "repaired-blocks"):
                available[block.sha256] = block
                unrecovered.discard(block.sha256)
        if unrecovered:
            raise SystemExit(f"[!] 无法补齐 {len(unrecovered)} 个增量块；清单未更新")
        dependencies = [available[h] for h in sorted(missing)]
        if any(i.size != required[i.sha256] for i in dependencies):
            raise SystemExit("[!] 增量块大小与清单不一致；清单未更新")
        dependencies += [i for i in items if not i.last and not i.remote.startswith("files/")]
        if baseline.verification == "pending-upload" and baseline.item not in dependencies:
            raise SystemExit("[!] 新基线不在上传计划中")
        print(f"[dependencies] 已存块哈希通过 {len(required) - len(missing)}，待补 {len(missing)}")
        upload(dependencies, client)
        if verify_public(dependencies):
            raise SystemExit("[!] 依赖公网哈希不通过；保留旧清单")
        if baseline.verification == "pending-upload":
            baseline.verification = "uploaded-sha256"
        elif public_size(baseline.item.remote) != baseline.item.size:
            raise SystemExit("[!] 保留基线大小变化；清单未更新")
        manifests = [i for i in items if i.last]
        print("[manifests] 依赖已齐，开始原子替换清单")
        upload(manifests, client)
        if verify_public(manifests):
            raise SystemExit("[!] 清单公网核对失败；资产保留，可重跑恢复三渠道同步")
        audit.update(complete=True, baselineVerification=baseline.verification,
                     uploaded=[i.remote for i in dependencies + manifests])
        return audit
    finally:
        client.close()


def prune_full(paths: list[str], current: dict, *, dry_run: bool = False) -> list[str]:
    """安装/升级验收后单独运行；仅删除命令中逐一列出的旧ZIP，绝不递归清理。"""
    full = validate_full(current.get("full"))
    for path in paths:
        safe_path(path)
        match = re.fullmatch(r"PPT-Doctor-v([0-9]+\.[0-9]+\.[0-9]+)(?:-Base|-Ocr)?\.zip",
                             PurePosixPath(path).name)
        if (not match or path.casefold() == full["path"].casefold()
                or version_key(match[1]) > version_key(current["version"])):
            raise SystemExit(f"[!] 拒绝删除当前基线/非旧整包：{path}")
    print("[prune] 已验收后才执行的明确路径：" + ", ".join(paths))
    if dry_run:
        return []
    # 清理前再次证实入口所指基线仍完整，不能删除唯一可用完整包。
    client = _connect()
    try:
        found = remote_hashes(client, [full["path"]]).get(full["path"], {})
        if found.get("sha256") != full["sha256"] or public_size(full["path"]) != found.get("size"):
            raise SystemExit("[!] 当前基线核验失败，不删除任何旧包")
        sftp = client.open_sftp()
        try:
            for path in paths:
                sftp.remove(f"{REMOTE}/{path}")
        finally:
            sftp.close()
    finally:
        client.close()
    return paths


def main() -> int:
    ap = argparse.ArgumentParser(description="把已发布版本同步到自建更新源（备用服务器）")
    ap.add_argument("version", nargs="?", help="版本号，默认取源码里的 __version__")
    ap.add_argument("--from-dir", type=Path, help="用本地产物目录，不从 GitHub 下载")
    ap.add_argument("--components", action="store_true", help="顺带同步 OCR / 公式组件 / 团队素材包")
    ap.add_argument("--dry-run", action="store_true", help="只列出将上传的清单，不连服务器")
    ap.add_argument("--verify-only", action="store_true", help="不上传，只核对公网上的文件")
    policy = ap.add_mutually_exclusive_group()
    policy.add_argument("--refresh-full", choices=("runtime-change", "missing-baseline", "hub-compat"),
                        help="显式批准更换基线的原因；仅上传本版Full ZIP")
    policy.add_argument("--baseline-zip", type=Path, help="迁移镜像已保留的旧Full ZIP，不重新上传")
    ap.add_argument("--prune-full", nargs="+", help="安装/升级验收后单独删除明确列出的旧ZIP")
    ap.add_argument("--current-manifest", type=Path, help="仅供dry-run使用的镜像清单快照")
    ap.add_argument("--cache-dir", type=Path, default=ROOT / "artifacts" / "mirror-cache")
    args = ap.parse_args()
    if args.current_manifest and not args.dry_run:
        ap.error("--current-manifest只能用于dry-run，执行时必须读取实际镜像")
    if args.prune_full and (args.refresh_full or args.baseline_zip or args.verify_only or args.components):
        ap.error("清理旧包必须在安装/升级验收后单独执行")
    if not args.version:
        from pptx_finder import __version__
        args.version = __version__

    version_key(args.version)
    cache = args.cache_dir / f"v{args.version}"
    audit = {"version": args.version, "complete": False, "refreshReason": args.refresh_full}
    try:
        previous = (json.loads(args.current_manifest.read_text("utf-8"))
                    if args.current_manifest else {} if args.dry_run else public_manifest())
        if args.prune_full:
            if args.dry_run and not args.current_manifest:
                raise SystemExit("[!] 清理预览需--current-manifest指定离线镜像快照")
            audit["deleted"] = prune_full(args.prune_full, previous, dry_run=args.dry_run)
            audit["complete"] = True
            return 0
        if not args.dry_run:
            for name in MANIFESTS:
                current = previous if name == "manifest.json" else public_manifest(name)
                if current and version_key(current.get("version")) > version_key(args.version):
                    raise SystemExit(f"[!] 不允许降级镜像 {name}：{current['version']} > {args.version}")
        if args.from_dir:
            items = collect_from_dir(args.version, args.from_dir, args.components,
                                     refresh_full=bool(args.refresh_full))
        else:
            items = collect_from_github(args.version, cache, args.components,
                                        download=not args.dry_run, refresh_full=bool(args.refresh_full))
        if not set(MANIFESTS).issubset({i.remote for i in items}):
            raise SystemExit("[!] 缺少三渠道清单，版本号对吗？")
        if all(i.sha256 for i in items):
            check_consistency(items, args.version)
        if not args.dry_run:
            baseline = prepare_baseline(items, previous, cache, args.cache_dir / "baseline.json",
                                        refresh_full=bool(args.refresh_full), adopt_zip=args.baseline_zip)
            items = make_mirror_manifest(items, baseline.full, cache / "mirror-manifests")
            audit.update(full=baseline.full, baselineVerification=baseline.verification)
        print(f"\n[plan] 同步 v{args.version} → {PUBLIC}（{REMOTE}）")
        print(f"  基线策略：{args.refresh_full or ('迁移已保留ZIP' if args.baseline_zip else '沿用已核验基线')}")
        if "full" in previous:
            print("  现有full：" + json.dumps(validate_full(previous["full"]), ensure_ascii=False))
        for item in sorted(items, key=lambda i: (i.last, i.remote)):
            tag = "（依赖核验后）" if item.last else ""
            print(f"  {item.size / 1048576:9.2f} MB  {item.remote}{tag}")
        print(f"  初选 {len(items)} 文件 / {sum(i.size for i in items) / 1048576:.1f} MB；执行时另核验历史缺块")
        if args.dry_run:
            print("[dry-run] 未连接镜像、未下载ZIP；基线与依赖闭包将在执行时核验。")
            return 0
        audit.update(publish(items, baseline, cache, src=args.from_dir, verify_only=args.verify_only))
        save_baseline_record(args.cache_dir / "baseline.json", baseline)
        print("[done] 清单及依赖核验通过；旧包待安装/升级验收后单独--prune-full")
        return 0
    except (Exception, SystemExit) as error:
        audit["complete"] = False
        audit["error"] = str(error)
        print(f"[!] {error}")
        return 1
    finally:
        if not args.dry_run:
            cache.mkdir(parents=True, exist_ok=True)
            name = "prune-report.json" if args.prune_full else "publish-report.json"
            report = cache / name
            report.write_text(json.dumps(audit, ensure_ascii=False, indent=2), "utf-8")
            print(f"[report] {report}")


if __name__ == "__main__":
    raise SystemExit(main())
