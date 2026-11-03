import json
import os
import subprocess
import sys

import fixtures_gen as fx
import pytest

from pptx_finder import db, hub_cli, indexer, imgtext_ocr
from pptx_finder.hub_library.library import PptutorError


@pytest.fixture
def indexed(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    fx.make_pptx(docs / "无线算法.pptx", [{"body": "无线调度算法 第一页"}, {"body": "第二页 波束优化"}])
    path = tmp_path / "index.db"
    conn = db.connect(path)
    db.init_db(conn)
    indexer.update_index(conn, [str(docs)], workers=1)
    conn.close()
    return path, docs


def call(tmp_path, action, **request):
    source, output, progress = (tmp_path / n for n in ("request.json", "response.json", "progress.json"))
    source.write_text(json.dumps({"requestId": "hub-test", "apiVersion": 1, **request}, ensure_ascii=False), encoding="utf-8-sig")
    code = hub_cli.main(["--hub", action, "--in", str(source), "--out", str(output), "--progress", str(progress)])
    return code, json.loads(output.read_text("utf-8"))


def test_no_index_status_does_not_create_database(tmp_path, monkeypatch):
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(tmp_path / "empty"))
    code, response = call(tmp_path, "status")
    assert code == 0 and response["available"] is False
    assert not (tmp_path / "empty/index.db").exists()
    assert response["requestId"] == "hub-test" and response["apiVersion"] == 1
    assert "imgtext-build" in response["capabilities"]


def test_search_read_identity_and_scope(indexed, tmp_path):
    path, docs = indexed
    code, response = call(tmp_path, "search", db=str(path), queries=["无线", "波束"])
    assert code == 0 and len(response["candidates"]) == 2
    hit = response["candidates"][0]
    assert len(hit["id"].split(":")) == 3
    assert hit["stableId"].endswith(":" + str(hit["ref"]["page"]))
    code, read = call(tmp_path, "read", db=str(path), refs=[hit["ref"]])
    assert code == 0 and read["pages"][0]["id"] == hit["id"]
    assert read["pages"][0]["ref"]["page_hash"] == hit["ref"]["page_hash"]
    assert read["pages"][0]["text_truncated"] is False
    code, scoped = call(tmp_path, "search", db=str(path), queries=["无线"], folders=[str(tmp_path / "docs-neighbor")])
    assert code == 0 and scoped["candidates"] == []


@pytest.mark.parametrize("field,value,expected", [("page_hash", "wrong", "REFERENCE_CHANGED"),
    ("content_hash", "wrong", "REFERENCE_CHANGED"), ("index_id", "wrong", "WRONG_INDEX"),
    ("source_id", "wrong", "WRONG_INDEX"), ("path", "C:/wrong.pptx", "REFERENCE_CHANGED")])
def test_stale_reference_is_not_read(indexed, tmp_path, field, value, expected):
    path, _ = indexed
    _, response = call(tmp_path, "search", db=str(path), queries=["无线"])
    ref = {**response["candidates"][0]["ref"], field: value}
    code, failed = call(tmp_path, "read", db=str(path), refs=[ref])
    assert code != 0 and failed["code"] == expected


def test_missing_and_changed_source_are_visible(indexed, tmp_path):
    path, docs = indexed
    _, search = call(tmp_path, "search", db=str(path), queries=["无线"])
    ref = search["candidates"][0]["ref"]
    source = docs / "无线算法.pptx"
    source.write_bytes(source.read_bytes() + b"changed")
    _, result = call(tmp_path, "read", db=str(path), refs=[ref])
    assert result["pages"][0]["source_state"] == "source_metadata_changed"
    source.unlink()
    _, result = call(tmp_path, "read", db=str(path), refs=[ref])
    assert result["pages"][0]["source_state"] == "source_unavailable"


def test_catalog_items_and_report_verbatim_check(indexed, tmp_path):
    path, docs = indexed
    code, catalog = call(tmp_path, "catalog", db=str(path))
    assert code == 0 and len(catalog["items"]) == 1
    _, evidence = call(tmp_path, "read", db=str(path), items=[{"file": str(docs / "无线算法.pptx"), "page": 1}])
    page = evidence["pages"][0]
    decision = {"task": {"purpose": "调度汇报", "conditions": []}, "results": [{"id": page["id"],
        "quote": "无线调度算法", "relevance": "直接适用", "use": "算法背景", "reason": "主题匹配",
        "conditions": "无额外条件", "assessments": []}]}
    code, report = call(tmp_path, "report", decision=decision, evidence=evidence)
    assert code == 0 and report["recommendations"][0]["id"] == page["id"]
    decision["results"][0]["quote"] = "这句话不在原稿中"
    code, report = call(tmp_path, "report", decision=decision, evidence=evidence)
    assert code != 0 and report["code"] == "UNSUPPORTED_QUOTE"


def test_index_only_requested_folder_and_progress(indexed, tmp_path, monkeypatch):
    monkeypatch.setattr(indexer, "_set_current_process_background_mode", lambda: None)
    path, docs = indexed
    other = tmp_path / "other"
    other.mkdir()
    fx.make_pptx(other / "新稿.pptx", [{"body": "新增素材内容"}])
    code, result = call(tmp_path, "index", db=str(path), folders=[str(other)])
    assert code == 0 and result["summary"]["indexed"] == 1
    conn = db.connect_readonly(path)
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 2
    conn.close()
    assert json.loads((tmp_path / "progress.json").read_text("utf-8"))["state"] == "completed"


def test_old_index_not_migrated(indexed, tmp_path):
    path, _ = indexed
    conn = db.connect(path)
    conn.execute("UPDATE meta SET value='2' WHERE key='index_version'")
    conn.commit()
    conn.close()
    code, result = call(tmp_path, "status", db=str(path))
    assert code != 0 and result["code"] == "UNSUPPORTED_INDEX_VERSION"
    conn = db.connect_readonly(path)
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize("action,payload,expected", [("bad", {}, "UNKNOWN_ACTION"),
    ("status", {"apiVersion": 2}, "API_VERSION_UNSUPPORTED"),
    ("read", {"db": "relative"}, "INVALID_REQUEST"),
    ("status", {"requestId": ""}, "INVALID_REQUEST")])
def test_file_errors(tmp_path, action, payload, expected):
    code, result = call(tmp_path, action, **payload)
    assert code != 0 and result["ok"] is False and result["code"] == expected


def test_windowed_stdout_none_and_no_gui_import(tmp_path):
    source, output = tmp_path / "in.json", tmp_path / "out.json"
    source.write_text(json.dumps({"requestId": "windowed"}), encoding="utf-8")
    script = ("import sys,runpy; sys.stdout=sys.stderr=None; "
              "sys.argv=['pptx_finder','--hub','status','--in',sys.argv[1],'--out',sys.argv[2]]; "
              "runpy.run_module('pptx_finder',run_name='__main__')")
    env = {**os.environ, "PPTX_FINDER_DATA_DIR": str(tmp_path / "isolated")}
    result = subprocess.run([sys.executable, "-c", script, str(source), str(output)],
        env=env, capture_output=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0
    assert json.loads(output.read_text("utf-8"))["requestId"] == "windowed"


def test_old_sidecar_fails_explicitly(tmp_path, monkeypatch):
    monkeypatch.setattr(imgtext_ocr, "command", lambda: ["old.exe"])
    monkeypatch.setattr(imgtext_ocr, "assist_available", lambda: False)
    with pytest.raises(PptutorError, match="更新") as caught:
        imgtext_ocr.assist_cutout("unused", tmp_path, [])
    assert caught.value.code == "OCR_COMPONENT_TOO_OLD"


def test_malformed_cli_cannot_overwrite_request(tmp_path):
    path = tmp_path / "request.json"
    path.write_text('{"requestId":"preserve"}', encoding="utf-8")
    original = path.read_bytes()
    assert hub_cli.main(["--hub", "status", "--in", str(path), "--out", str(path), "--bad"]) != 0
    assert path.read_bytes() == original


def test_parallel_search_matches_serial(indexed):
    from pptx_finder.hub_library.library import Library
    path, _ = indexed
    with Library(path) as library:
        plan = {"queries": ["无线", "波束"]}
        parallel = library.search(plan)
        serial = library.search(plan, parallel=False)
    assert parallel == serial


def test_parallel_index_write_is_detected(indexed, monkeypatch):
    from pptx_finder.hub_library.library import Library
    import threading
    path, _ = indexed
    original = Library._search_one
    once, guard = [], threading.Lock()

    def query(reader, *args, **kwargs):
        with guard:
            if not once:
                conn = db.connect(path)
                conn.execute("UPDATE files SET indexed_at=indexed_at+1")
                conn.commit()
                conn.close()
                once.append(True)
        return original(reader, *args, **kwargs)

    monkeypatch.setattr(Library, "_search_one", query)
    with Library(path) as library, pytest.raises(PptutorError) as caught:
        library.search({"queries": ["无线", "波束"]})
    assert caught.value.code == "INDEX_CHANGED"
