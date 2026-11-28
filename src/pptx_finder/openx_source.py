"""公司内源（openx）更新源：在公司电脑上，更新清单和文件先从内源取（2026-11-23）。

照搬 AI HUB 公开版 core/app-update/openx-source.js 在公司实测过的做法：
- 内源项目 PPT-Doctor（8214）和 AI-Hub-Huawei（7938）上，每发一版 PPT Doctor 就发一个版本 ``ppt-doctor-update-<版本>``，
  附件是三份版本清单（manifest.json / manifest-Base.json / manifest-Ocr.json）、本版变化的文件块
  （附件名 = sha256，和 GitHub 发布页同名）和基础版安装包；
- 接口匿名可用（2026-11-23 公司电脑实测：版本列表一次约 2 秒，直连和公司代理都通）：
    版本列表 GET {api}by_project_id?projectId=<id>&currentPage=1&pageSize=N → data.records[{id, version, releaseTime}]
    单个版本 GET {api}by_id?id=<id> → data.binaryPackages[{originalFileName, url}]
- 只接受内源自己域名下的 https 附件地址；下载后照旧按清单核对 sha256；
- 公司外连不上内源：记下「连不上」，5 分钟内不再试，外网用户不被拖慢。

纯标准库，不依赖 Qt，便于单测。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from urllib.parse import quote, urlparse

API = "https://openx.huawei.com/openxApi/project/version/release/query/"
PROJECT_ID = 7938          # AI-Hub-Huawei（11-23 先挂在这里）
PROJECT_IDS = (8214, 7938)  # 8214 = PPT-Doctor 自己的内源项目（11-23 审核通过），优先；两边都发
# 2026-11-28：内源 11-27 起把版本列表接口（by_project_id）改成必须登录，单个版本详情（by_id）和附件仍匿名可用。
# 和 AI HUB-H（R55）一样改读「版本索引」发布：描述里「#hub-index v1」一行之后每行「<id> <版本名> [发布时间]」，新的在前，
# 由发版流水线每次发布后更新。0 = 该项目还没建索引，只能读列表（列表要登录时如实提示）。
# 7938 的 hub-index（12787）收录该项目全部非程序版本，含 ppt-doctor-update-*；但识别/公式/素材组件
# （ppt-doctor-<组件>-*）只发在 8214，所以 8214 必须有自己的索引，组件下载和 8214 的下载量才走得通。
INDEX_IDS = {8214: 12826, 7938: 12787}   # 8214 的索引 11-28 建好
INDEX_MARK = "#hub-index v1"
ALLOWED_HOST = "openx.huawei.com"
RELEASE_PREFIX = "ppt-doctor-update-"

LIST_TIMEOUT = 5.0          # 公司实测列表约 2 秒
LIST_TTL = 10 * 60
UNREACHABLE_TTL = 5 * 60
LIST_PAGE_SIZE = 100
MAX_DETAIL_QUERIES = 8      # 找一个块最多再查几个没缓存过的版本


class OpenxUnavailable(RuntimeError):
    """内源连不上 / 没有这份文件。auth=True 表示内源要求登录（不是网络问题）。"""

    auth = False


AUTH_TEXT = "公司内源的版本列表现在要求登录（不是网络问题）"


def _is_auth(e: BaseException) -> bool:
    return bool(getattr(e, "auth", False))


def _auth_error(text: str) -> OpenxUnavailable:
    err = OpenxUnavailable(text)
    err.auth = True
    return err


def _is_offline(e: BaseException) -> bool:
    """断网 / DNS 失败 / 超时：公司外的常态。索引读不到就直接按离线处理，不再白等一次列表。"""
    import socket as _socket
    import urllib.error as _ue
    if isinstance(e, (_socket.timeout, TimeoutError, ConnectionError)):
        return True
    return isinstance(e, _ue.URLError) and not isinstance(e, _ue.HTTPError)


def parse_index(text) -> list:
    """版本索引描述 → [{id, version, releaseTime}]（新的在前）；格式不对返回空表。版本名可以带空格。"""
    lines = str(text or "").replace("\r", "").split("\n")
    marks = [l.strip() for l in lines]
    if INDEX_MARK not in marks:
        return []
    out = []
    for line in lines[marks.index(INDEX_MARK) + 1:]:
        m = re.match(r"^\s*(\d+)\s+(.+?)\s*$", line)
        if not m:
            continue
        rest = m.group(2)
        t = re.search(r"\s(\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+)?(?:Z|[+-]\d{2}:?\d{2})?)$", rest)
        ver, when = (rest[:t.start()].strip(), t.group(1)) if t else (rest.strip(), "")
        if ver:
            out.append({"id": int(m.group(1)), "version": ver, "releaseTime": when})
    return out


PAGE_URL = "https://openx.huawei.com/PPT-Doctor/download"   # 内源下载页（手动下载安装包用）
EXTERNAL_SETTING = "update_external_sources"                  # ui.json 里「允许外网更新源」开关


def enabled(frozen: bool) -> bool:
    """PPTX_FINDER_OPENX=0 关、=1 开；默认只在打包版里开（单测和开发环境不碰网络）。"""
    v = os.environ.get("PPTX_FINDER_OPENX", "").strip()
    if v == "0":
        return False
    if v == "1":
        return True
    return bool(frozen)


def external_allowed(frozen: bool) -> bool:
    """GitHub / 备用服务器（阿里云）这两个外网源用不用。

    2026-11-24 田哥定：公司里默认**只走内源**，检查更新、下载更新、下载组件都不碰外网。
    外网源只在两种情况下启用：
    - 用户在设置里手动打开「允许外网更新源」（在公司外用、连不上内源时，界面就地给一键按钮）；
    - 内源本身被关掉（开发环境、单测，或 PPTX_FINDER_OPENX=0），否则就没有任何源了。
    PPTX_FINDER_UPDATE_EXTERNAL=1/0 可强制开关（测试用）。
    """
    v = os.environ.get("PPTX_FINDER_UPDATE_EXTERNAL", "").strip()
    if v in ("1", "0"):
        return v == "1"
    if not enabled(frozen):
        return True
    try:
        from .config import load_ui_settings
        return bool(load_ui_settings().get(EXTERNAL_SETTING, False))
    except Exception:  # noqa: BLE001 读不到设置按默认（只走内源）
        return False


GITHUB_PAGE = "https://github.com/TianLin0509/PPTutor/releases/latest"


def release_page(frozen: bool) -> str:
    """「手动下载」打开的页面：默认内源下载页；打开了外网源才给 GitHub 发布页。"""
    return GITHUB_PAGE if external_allowed(frozen) else PAGE_URL


def set_external_allowed(on: bool) -> None:
    from .config import update_ui_settings
    update_ui_settings(**{EXTERNAL_SETTING: bool(on)})


def _ver_key(name: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", name)[:4])


def safe_url(u: str) -> str:
    try:
        p = urlparse(str(u or ""))
    except ValueError:
        return ""
    host = (p.hostname or "").lower()
    if p.scheme == "https" and (host == ALLOWED_HOST or host.endswith("." + ALLOWED_HOST)):
        return p.geturl()
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise OpenxUnavailable(f"内源要求跳转（HTTP {code}），不跟随")


class OpenxIndex:
    """版本列表与各版本附件的缓存。一个进程一份（模块级 INDEX），线程安全。"""

    def __init__(self, api: str = API, project_id: int = PROJECT_ID, opener=None, clock=time.monotonic,
                 index_id: int | None = None):
        self.api = api
        self.project_id = project_id
        self.index_id = int((INDEX_IDS.get(project_id, 0) if index_id is None else index_id) or 0)
        self.index_mode = False
        self._opener = opener
        self._clock = clock
        self._lock = threading.Lock()
        self._list: list | None = None
        self._list_at = 0.0
        self._down_until = 0.0
        self.down_reason = ""
        self._down_auth = False
        self._details: dict = {}
        self._pkg_meta: dict = {}

    # ---- 网络 ----
    def _get_json(self, url: str, timeout: float) -> dict:
        opener = self._opener or urllib.request.build_opener(_NoRedirect)
        import urllib.error as _ue
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "PPT-Doctor-updater"})
        try:
            with opener.open(req, timeout=timeout) as r:
                raw = r.read(4 * 1024 * 1024)
        except _ue.HTTPError as e:
            # 401/403 且回的是内源自己的 JSON 才算「要求登录」；公司代理拦截页是 HTML 403，照常按 HTTP 错误处理
            ctype = str((e.headers or {}).get("Content-Type", "") or "").lower()
            if e.code in (401, 403) and "json" in ctype:
                raise _auth_error(f"{AUTH_TEXT}（HTTP {e.code}）") from e
            raise
        body = json.loads(raw.decode("utf-8-sig"))
        if isinstance(body, dict) and str(body.get("code")) in ("401", "403"):
            raise _auth_error(f"{AUTH_TEXT}（{body.get('message') or body.get('code')}）")
        if not isinstance(body, dict) or str(body.get("code")) != "0" or not body.get("data"):
            raise OpenxUnavailable("内源返回内容不对")
        return body["data"]

    def _list_records(self, timeout: float) -> list:
        data = self._get_json(f"{self.api}by_project_id?projectId={self.project_id}"
                              f"&currentPage=1&pageSize={LIST_PAGE_SIZE}", timeout)
        recs = data.get("records")
        if not isinstance(recs, list):
            raise OpenxUnavailable("内源版本列表格式不对")
        return recs

    def _index_records(self, timeout: float) -> list:
        data = self._get_json(f"{self.api}by_id?id={quote(str(self.index_id))}", timeout)
        recs = parse_index(data.get("description") if isinstance(data, dict) else "")
        if not recs:
            raise OpenxUnavailable("内源版本索引格式不对")
        return recs

    def _fetch_records(self, timeout: float) -> list:
        """索引优先（与 AI HUB-H R55 一致）：
        1. 索引读到了：里面有本产品的更新版本就用它；一条都没有（防索引没跟上）再试一次列表，列表不行仍按索引；
        2. 读索引断网 / 超时：直接按连不上处理，不再白等列表；
        3. 读索引其他失败（格式不对、要登录），或该项目还没建索引：读列表。"""
        self.index_mode = False
        if not self.index_id:
            return self._list_records(timeout)
        try:
            recs = self._index_records(timeout)
        except Exception as e:  # noqa: BLE001
            if _is_offline(e):
                raise
            index_err = e
        else:
            if not any(str(r.get("version", "")).startswith(RELEASE_PREFIX) for r in recs):
                try:
                    return self._list_records(timeout)
                except Exception:  # noqa: BLE001 列表要登录 / 失败就以索引为准
                    pass
            self.index_mode = True
            return recs
        try:
            return self._list_records(timeout)
        except Exception as e:  # noqa: BLE001
            if _is_auth(e) and not _is_auth(index_err):
                raise _auth_error(f"{e}；版本索引也没读到：{str(index_err)[:80]}") from e
            raise

    def releases(self, timeout: float = LIST_TIMEOUT) -> list:
        with self._lock:
            now = self._clock()
            if now < self._down_until:
                err = OpenxUnavailable(self.down_reason or "内源连不上（稍后自动重试）")
                err.auth = self._down_auth
                raise err
            if self._list is not None and now - self._list_at <= LIST_TTL:
                return self._list
        try:
            recs = self._fetch_records(timeout)
        except Exception as e:  # noqa: BLE001 公司外多半 DNS 失败 / 超时
            auth = _is_auth(e)
            with self._lock:
                self._down_until = self._clock() + UNREACHABLE_TTL
                self._down_auth = auth
                self.down_reason = (str(e)[:160] if auth
                                    else f"公司内源连不上：{type(e).__name__}: {str(e)[:80]}")
            err = OpenxUnavailable(self.down_reason)
            err.auth = auth
            raise err from e
        with self._lock:
            self._list = [r for r in recs if isinstance(r, dict) and r.get("id") is not None]
            self._list_at = self._clock()
            return self._list

    def clear_down(self) -> None:
        """用户手动检查时立刻重连，不等 5 分钟「连不上」缓存过期（与 AI HUB-H clearDown 一致）。"""
        with self._lock:
            self._down_until = 0.0

    def update_releases(self, timeout: float = LIST_TIMEOUT, prefix: str = RELEASE_PREFIX) -> list:
        """某一类版本（默认本产品的更新版本；组件用 ppt-doctor-<组件>-），新到旧。"""
        out = [r for r in self.releases(timeout) if str(r.get("version", "")).startswith(prefix)
               and re.fullmatch(r"\d+\.\d+\.\d+", str(r["version"])[len(prefix):])]
        return sorted(out, key=lambda r: _ver_key(str(r["version"])[len(prefix):]), reverse=True)

    def files_of(self, rec: dict, timeout: float = LIST_TIMEOUT) -> dict:
        rid = rec["id"]
        with self._lock:
            hit = self._details.get(rid)
        if hit is not None:
            return hit
        data = self._get_json(f"{self.api}by_id?id={quote(str(rid))}", timeout)
        rel_ver = str(rec.get("version") or "")
        got_ver = str(data.get("version") or "").strip() if isinstance(data, dict) else ""
        if got_ver and rel_ver and got_ver != rel_ver.strip():
            data = {}   # 索引里的编号填错（读回来的版本名对不上）：当这一版不存在，不会装错版本
        pkgs = data.get("binaryPackages") if isinstance(data, dict) else None
        files = {}
        meta = {}
        for p in pkgs if isinstance(pkgs, list) else []:
            if not isinstance(p, dict):
                continue
            url = safe_url(p.get("url"))
            name = str(p.get("originalFileName") or "").lower()
            if url and name:
                files[name] = url
                meta[name] = {
                    "project_id": self.project_id,
                    "release_id": rid,
                    "release_version": rel_ver,
                    "package_id": p.get("id"),
                    "package_tag": str(p.get("tag") or ""),
                    "url_source": p.get("urlSource"),
                    "file_name": str(p.get("originalFileName") or ""),
                }
        with self._lock:
            self._details[rid] = files
            self._pkg_meta[rid] = meta
        return files

    def find_meta(self, release_version: str = "", file_name: str = "") -> dict | None:
        """只读查已缓存的附件元数据（project_id / release_id / package_id），不发起额外网络请求。"""
        rv = str(release_version or "").strip().lower()
        fk = str(file_name or "").strip().lower()
        with self._lock:
            recs = list(self._list or [])
            pkg_meta = dict(self._pkg_meta)
        if rv:
            for rec in recs:
                if str(rec.get("version") or "").strip().lower() == rv:
                    m = pkg_meta.get(rec.get("id")) or {}
                    if fk and fk in m:
                        return dict(m[fk])
                    if fk and f"{fk}.000" in m:
                        return dict(m[f"{fk}.000"])
                    if m:
                        return dict(next(iter(m.values())))
        elif fk:
            for m in pkg_meta.values():
                if fk in m:
                    return dict(m[fk])
                if f"{fk}.000" in m:
                    return dict(m[f"{fk}.000"])
        return None

    # ---- 查找 ----
    def latest_file(self, name: str, timeout: float = LIST_TIMEOUT,
                    prefix: str = RELEASE_PREFIX) -> tuple[str, str]:
        """最新一个更新版本里的某个附件（版本清单用）。返回 (url, 版本号)。"""
        rels = self.update_releases(timeout, prefix)
        if not rels:
            raise OpenxUnavailable("内源上还没有 PPT Doctor 更新" if prefix == RELEASE_PREFIX
                                   else f"内源上还没有 {prefix}*")
        rec = rels[0]
        url = self.files_of(rec, timeout).get(name.lower())
        if not url:
            raise OpenxUnavailable(f"内源最新版本里没有 {name}")
        return url, str(rec["version"])[len(prefix):]

    def release_files(self, prefix: str, version: str, timeout: float = LIST_TIMEOUT) -> dict:
        """指定版本（如 ppt-doctor-ocr-1.2.0）的全部附件 {小写文件名: url}。"""
        want = prefix + str(version)
        for rec in self.update_releases(timeout, prefix):
            if str(rec.get("version")) == want:
                return self.files_of(rec, timeout)
        raise OpenxUnavailable(f"内源上没有 {want}")

    def find(self, name: str, timeout: float = LIST_TIMEOUT) -> str:
        """在各更新版本里按附件名找（新到旧）；找不到抛 OpenxUnavailable。"""
        key = name.lower()
        queries = 0
        for rec in self.update_releases(timeout):
            with self._lock:
                cached = rec["id"] in self._details
            if not cached:
                if queries >= MAX_DETAIL_QUERIES:
                    break
                queries += 1
            url = self.files_of(rec, timeout).get(key)
            if url:
                return url
        raise OpenxUnavailable(f"内源上没有 {name}")


class MultiIndex:
    """按顺序查几个内源项目：清单取各项目里版本最高的那份，文件按项目顺序找。"""

    def __init__(self, indexes):
        self.indexes = list(indexes)

    def clear_down(self) -> None:
        for idx in self.indexes:
            idx.clear_down()

    def latest_file(self, name: str, timeout: float = LIST_TIMEOUT,
                    prefix: str = RELEASE_PREFIX) -> tuple[str, str]:
        best, errors = None, []
        for idx in self.indexes:
            try:
                url, ver = idx.latest_file(name, timeout, prefix)
            except OpenxUnavailable as e:
                errors.append(str(e))
                continue
            if best is None or _ver_key(ver) > _ver_key(best[1]):
                best = (url, ver)
        if best is None:
            raise OpenxUnavailable(errors[0] if errors else "内源上还没有 PPT Doctor 更新")
        return best

    def release_files(self, prefix: str, version: str, timeout: float = LIST_TIMEOUT) -> dict:
        errors = []
        for idx in self.indexes:
            try:
                return idx.release_files(prefix, version, timeout)
            except OpenxUnavailable as e:
                errors.append(str(e))
        raise OpenxUnavailable(errors[0] if errors else f"内源上没有 {prefix}{version}")

    def find(self, name: str, timeout: float = LIST_TIMEOUT) -> str:
        errors = []
        for idx in self.indexes:
            try:
                return idx.find(name, timeout)
            except OpenxUnavailable as e:
                errors.append(str(e))
        raise OpenxUnavailable(errors[0] if errors else f"内源上没有 {name}")

    def find_meta(self, release_version: str = "", file_name: str = "") -> dict | None:
        for idx in self.indexes:
            hit = idx.find_meta(release_version, file_name)
            if hit:
                return hit
        return None


def component_prefix(kind: str) -> str:
    """组件在内源上的版本名前缀：ppt-doctor-ocr- / ppt-doctor-formula- / ppt-doctor-materials-。"""
    return f"ppt-doctor-{kind}-"


def component_manifest_url(kind: str, timeout: float = LIST_TIMEOUT, index=None) -> str:
    url, _ver = (index or INDEX).latest_file("component.json", timeout, component_prefix(kind))
    return url


def component_archive_urls(kind: str, version: str, archive: str, timeout: float = LIST_TIMEOUT,
                           index=None) -> list:
    """组件压缩包的下载地址：整包一个地址；超过内源单附件上限被切片的，按 .000、.001… 顺序给出
    （下载时拼回，整体核 sha256）。"""
    files = (index or INDEX).release_files(component_prefix(kind), version, timeout)
    key = archive.lower()
    if key in files:
        return [files[key]]
    parts = []
    while f"{key}.{len(parts):03d}" in files:
        parts.append(files[f"{key}.{len(parts):03d}"])
    if not parts:
        raise OpenxUnavailable(f"内源上的 {component_prefix(kind)}{version} 里没有 {archive}")
    return parts


INDEX = MultiIndex(OpenxIndex(project_id=pid) for pid in PROJECT_IDS)
