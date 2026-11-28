"""2026-11-28：内源版本列表改成要登录后，改读「版本索引」发布（与 AI HUB-H R55 同一格式、同一取舍）。"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from pptx_finder import openx_recorder, openx_source

API = "https://openx.huawei.com/openxApi/project/version/release/query/"
F = "https://file.openx.huawei.com/openx-file/"
IDX = 900


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _http_error(url, code, ctype):
    body = json.dumps({"code": str(code), "message": "未认证的请求"}).encode() if "json" in ctype else b"<html>blocked</html>"
    return urllib.error.HTTPError(url, code, "x", {"Content-Type": ctype}, io.BytesIO(body))


class Opener:
    """index=None 表示索引不存在；list_mode: ok / auth401 / auth_code / proxy403。"""

    def __init__(self, releases, files, *, index=None, list_mode="ok", offline=False, versions=None):
        self.releases, self.files, self.index = releases, files, index
        self.list_mode, self.offline = list_mode, offline
        self.versions = versions or {}
        self.calls = []

    def open(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        if self.offline:
            raise urllib.error.URLError("getaddrinfo failed")
        if "by_project_id" in url:
            if self.list_mode == "auth401":
                raise _http_error(url, 401, "application/json;charset=UTF-8")
            if self.list_mode == "proxy403":
                raise _http_error(url, 403, "text/html")
            if self.list_mode == "auth_code":
                return _Resp(json.dumps({"code": "401", "message": "未认证的请求"}).encode())
            data = {"records": [{"id": i, "version": v} for i, v in self.releases]}
            return _Resp(json.dumps({"code": "0", "data": data}).encode())
        rid = int(url.rsplit("=", 1)[1])
        if rid == IDX:
            if self.index is None:
                return _Resp(json.dumps({"code": "0", "data": {"description": "随便写的说明"}}).encode())
            desc = "AI HUB 版本索引，Hub 自动读取\n#hub-index v1\n" + "\n".join(
                f"{i} {v} 2026-11-28T00:00:00Z" for i, v in self.index)
            return _Resp(json.dumps({"code": "0", "data": {"version": "hub-index", "description": desc}}).encode())
        data = {"version": self.versions.get(rid, ""),
                "binaryPackages": [{"originalFileName": n, "url": u, "id": rid * 10} for n, u in self.files.get(rid, [])]}
        return _Resp(json.dumps({"code": "0", "data": data}).encode())


def _ix(op, index_id=IDX):
    return openx_source.OpenxIndex(api=API, opener=op, index_id=index_id)


RELS = [(3, "ppt-doctor-update-2.1.20"), (2, "ppt-doctor-update-2.1.19"), (7, "v5.6.140")]
FILES = {3: [("manifest-Base.json", F + "m20")], 2: [("manifest-Base.json", F + "m19")]}


def test_index_is_read_first_and_list_is_never_asked():
    op = Opener(RELS, FILES, index=RELS, list_mode="auth401")
    ix = _ix(op)
    assert ix.latest_file("manifest-Base.json") == (F + "m20", "2.1.20")
    assert ix.index_mode is True
    assert not any("by_project_id" in u for u in op.calls)


def test_recorder_ids_come_from_index_path():
    """下载量记录要的 release_id / package_id，走索引时一样拿得到。"""
    op = Opener(RELS, FILES, index=RELS, list_mode="auth401")
    ix = _ix(op)
    ix.latest_file("manifest-Base.json")
    meta = ix.find_meta("ppt-doctor-update-2.1.20", "manifest-Base.json")
    assert meta["release_id"] == 3 and meta["package_id"] == 30


def test_list_401_without_index_says_login_not_network():
    op = Opener(RELS, FILES, list_mode="auth401")
    ix = _ix(op, index_id=0)
    with pytest.raises(openx_source.OpenxUnavailable) as e:
        ix.releases()
    assert e.value.auth is True
    assert openx_source.AUTH_TEXT in str(e.value) and "连不上" not in str(e.value)


def test_code_401_in_body_is_also_login():
    ix = _ix(Opener(RELS, FILES, list_mode="auth_code"), index_id=0)
    with pytest.raises(openx_source.OpenxUnavailable) as e:
        ix.releases()
    assert e.value.auth is True


def test_proxy_block_page_403_is_not_mistaken_for_login():
    ix = _ix(Opener(RELS, FILES, list_mode="proxy403"), index_id=0)
    with pytest.raises(openx_source.OpenxUnavailable) as e:
        ix.releases()
    assert e.value.auth is False and "连不上" in str(e.value)


def test_offline_on_index_does_not_wait_for_list_again():
    op = Opener(RELS, FILES, index=RELS, offline=True)
    with pytest.raises(openx_source.OpenxUnavailable):
        _ix(op).releases()
    assert len(op.calls) == 1


def test_broken_index_falls_back_to_list():
    op = Opener(RELS, FILES, index=None, list_mode="ok")
    ix = _ix(op)
    assert ix.latest_file("manifest-Base.json")[1] == "2.1.20"
    assert ix.index_mode is False


def test_index_without_any_doctor_update_tries_list_once_then_keeps_index():
    only_hub = [(7, "v5.6.140")]
    op = Opener(RELS, FILES, index=only_hub, list_mode="ok")
    assert _ix(op).latest_file("manifest-Base.json")[1] == "2.1.20"   # 列表恢复匿名时能看到新版
    op2 = Opener(RELS, FILES, index=only_hub, list_mode="auth401")
    with pytest.raises(openx_source.OpenxUnavailable):
        _ix(op2).latest_file("manifest-Base.json")                     # 列表也不行：按索引（没有更新）


def test_wrong_id_in_index_is_treated_as_missing_not_installed():
    op = Opener(RELS, FILES, index=RELS, list_mode="auth401", versions={3: "ppt-doctor-update-2.1.5"})
    with pytest.raises(openx_source.OpenxUnavailable):
        _ix(op).latest_file("manifest-Base.json")


def test_both_projects_project_8214_without_index_falls_through_to_7938_index():
    """8214 还没建索引：它的列表 401；7938 的 hub-index 里有 PPT Doctor 更新，照样能更新。"""
    no_index = Opener(RELS, FILES, list_mode="auth401")
    with_index = Opener(RELS, FILES, index=RELS, list_mode="auth401")
    multi = openx_source.MultiIndex([_ix(no_index, index_id=0), _ix(with_index)])
    assert multi.latest_file("manifest-Base.json") == (F + "m20", "2.1.20")


def test_default_index_ids():
    assert openx_source.OpenxIndex(project_id=7938).index_id == 12787
    assert openx_source.OpenxIndex(project_id=8214).index_id == openx_source.INDEX_IDS[8214]


def test_parse_index_matches_pipeline_format():
    desc = "说明\r\n#hub-index v1\r\n12593 ppt-doctor-update-2.1.19 2026-11-25 10:00:00\r\n12710 v5.6.127 完整安装包\r\n11976 feedback-receiver 2026-11-19\r\n坏行"
    assert openx_source.parse_index(desc) == [
        {"id": 12593, "version": "ppt-doctor-update-2.1.19", "releaseTime": "2026-11-25 10:00:00"},
        {"id": 12710, "version": "v5.6.127 完整安装包", "releaseTime": ""},
        {"id": 11976, "version": "feedback-receiver", "releaseTime": "2026-11-19"},
    ]
    assert openx_source.parse_index("没有标记行\n1 a") == []


def test_recorder_notifies_listener_once_for_thanks(tmp_path):
    rec = openx_recorder.OpenxDownloadRecorder(data_dir=tmp_path, run_session=lambda item: {"ok": True, "method": "api-download1"})
    got = []
    rec.add_listener(got.append)
    rec.add_listener(lambda _v: (_ for _ in ()).throw(RuntimeError("监听方出错不影响记录")))
    r = rec.record(release_version="ppt-doctor-update-2.1.20", release_id=3, package_id=30, background=False)
    assert r["status"] == "recorded" and got == ["ppt-doctor-update-2.1.20"]
    assert rec.is_recorded("ppt-doctor-update-2.1.20") and not rec.is_recorded("ppt-doctor-update-2.1.19")
    rec.record(release_version="ppt-doctor-update-2.1.20", background=False)   # 已记过：不重复感谢
    assert got == ["ppt-doctor-update-2.1.20"]
