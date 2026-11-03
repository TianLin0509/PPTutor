"""Versioned, file-only integration with AI Hub. Never starts the GUI."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from . import __version__
from .hub_library.library import Library, PptutorError

API_VERSION = 1
ACTIONS = ("status", "search", "read", "catalog", "report", "index",
           "imgtext", "imgtext-seed", "imgtext-build")
CAPABILITIES = (*ACTIONS, "serve")


class HubError(PptutorError):
    pass


def retry_file_change(operation, *args):
    """Retry brief Windows file sharing locks; never fall back to truncation."""
    for attempt in range(20):
        try:
            return operation(*args)
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.015)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                        allow_nan=False), encoding="utf-8")
        retry_file_change(os.replace, temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def absolute_path(value, label: str) -> Path:
    if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
        raise HubError("INVALID_REQUEST", f"{label} 必须是绝对路径")
    return Path(value).resolve()


def _roots(request):
    roots = request.get("folders", request.get("roots", []))
    if "folders" in request and "roots" in request and request["folders"] != request["roots"]:
        raise HubError("INVALID_REQUEST", "folders 与 roots 不一致")
    if not isinstance(roots, list) or len(roots) > 32:
        raise HubError("INVALID_REQUEST", "folders 应为最多 32 个目录的数组")
    return [str(absolute_path(p, "folders")) for p in roots]


def _db_path(request):
    from .config import db_path
    return absolute_path(request["db"], "db") if "db" in request else db_path()


def _identify(result, lib):
    """Keep legacy evidence fields, qualify IDs once, never drop hashes."""
    for key in ("candidates", "pages"):
        for item in result.get(key, []):
            ref = item["ref"]
            ref["source_id"] = lib.index_id
            item["id"] = f"{lib.index_id}:{ref['file_id']}:{ref['page']}"
            canonical = os.path.normcase(os.path.abspath(ref["path"])).lower()
            item["stableId"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16] + f":{ref['page']}"
            from .renderer import find_cached_render, cache_key_for_metadata
            thumbnail = find_cached_render(ref["path"], ref["page"],
                cache_key=cache_key_for_metadata(ref["path"], ref["mtime"], ref["size"]))
            item["thumbnail"] = str(thumbnail) if thumbnail else None
            for duplicate in item.get("duplicate_refs", []):
                duplicate["source_id"] = lib.index_id
    return result


def _refs(request, lib):
    refs = request.get("refs", request.get("items"))
    if not isinstance(refs, list) or not 1 <= len(refs) <= 40:
        raise HubError("INVALID_REFERENCE", "read 需要 1–40 个 refs 或 items")
    output = []
    for value in refs:
        if not isinstance(value, dict):
            raise HubError("INVALID_REFERENCE", "每个引用必须是对象")
        ref = dict(value.get("ref", value))
        if "file" in value:
            file = str(absolute_path(value["file"], "file"))
            if ref.get("path") and os.path.normcase(ref["path"]) != os.path.normcase(file):
                raise HubError("INVALID_REFERENCE", "file 与 ref.path 不一致")
            row = lib.conn.execute("SELECT id FROM files WHERE lower(path)=lower(?)", (file,)).fetchone()
            if row is None:
                raise HubError("PAGE_NOT_FOUND", f"文件尚未在索引中：{file}")
            if ref.get("file_id") is not None and ref["file_id"] != row[0]:
                raise HubError("REFERENCE_CHANGED", "文件索引编号已变化，请重新搜索")
            ref.update(file_id=row[0], path=file, page=value.get("page", ref.get("page")))
        if ref.get("source_id", lib.index_id) != lib.index_id:
            raise HubError("WRONG_INDEX", "引用来自另一索引")
        if isinstance(ref.get("file_id"), bool) or not isinstance(ref.get("file_id"), int):
            raise HubError("INVALID_REFERENCE", "file_id 必须为整数")
        if isinstance(ref.get("page"), bool) or not isinstance(ref.get("page"), int):
            raise HubError("INVALID_REFERENCE", "page 必须为整数")
        output.append(ref)
    return output


def _status(request, path, roots):
    from . import imgtext_ocr
    result = {"ok": True, "available": False, "indexPath": str(path),
              "indexUpdatedAt": None, "indexed_pages": 0, "file_status_counts": [],
              "scope": {"index_path": str(path), "roots": roots, "read_only": True},
              "capabilities": list(CAPABILITIES), "warnings": [],
              "ocr": {"installed": imgtext_ocr.is_installed(),
                      "version": imgtext_ocr.installed_version(), "assistAvailable": False}}
    # Read manifest only; no subprocess, download or bundled extraction on status.
    result["ocr"]["assistAvailable"] = imgtext_ocr.assist_available()
    if not path.is_file():
        result["warnings"].append("尚无素材索引，请先选择文件夹建立索引")
        return result
    with Library(path, roots) as lib:
        result.update(lib.status())
        result["available"] = True
        result["indexUpdatedAt"] = lib.meta.get("last_completed_scan_at")
        if request.get("includePaths"):
            predicates, params = lib.filters()
            result["documents"] = [dict(row) for row in lib.conn.execute(
                f"SELECT path,page_count FROM files f WHERE {' AND '.join(predicates)} AND status='ok'", params)]
    return result


def execute(action, request, progress=lambda *a, **kw: None):
    if action not in ACTIONS:
        raise HubError("UNKNOWN_ACTION", f"不支持的动作：{action}")
    roots = _roots(request)
    path = _db_path(request)
    if action == "status":
        return _status(request, path, roots)
    if action == "report":
        from .hub_library.report import prepare, render
        checked = prepare(request["decision"], request["evidence"])
        conditions = {c["id"]: c["text"] for c in checked["task"]["conditions"]}
        return {"report": render(request["decision"], request["evidence"], request.get("detail", "brief")),
                "verified_rows": [{"id": row["id"], "relevance": row["effective_relevance"],
                    "use": row["use"], "reason": row["reason"], "quote": row["quote"],
                    "conditions": [{"text": conditions[c["condition_id"]], "status": c["status"]}
                                   for c in row["assessments"]]}
                    for row, _ in checked["usable"] + checked["excluded"]],
                "recommendations": [{"id": row["id"], "relevance": row["effective_relevance"]}
                                    for row, _ in checked["usable"]]}
    if action == "index":
        from . import db, indexer
        if not roots or any(not Path(p).is_dir() for p in roots):
            raise HubError("INVALID_REQUEST", "index 需要存在的非空 folders")
        if path.is_file():
            # An old DB must never be silently migrated/cleared by Hub.
            with Library(path, roots):
                pass
        indexer._set_current_process_background_mode()
        conn = db.connect(path)
        try:
            db.init_db(conn)
            summary = indexer.update_index(conn, roots, workers=1, supported_exts=(".pptx", ".ppt"),
                progress_cb=lambda done, total, current: progress(
                    "index", "正在更新 PPT 素材索引", done=done,
                    total=total if total >= 0 else None))
        finally:
            conn.close()
        warnings = []
        if summary.get("errors") or summary.get("unreadable_dirs"):
            warnings.append("部分文件或目录未完成索引，请查看 summary")
        return {"summary": summary, "warnings": warnings,
                "scope": {"index_path": str(path), "roots": roots, "read_only": False}}
    if action.startswith("imgtext"):
        from . import imgtext_assist
        if action == "imgtext-seed":
            return imgtext_assist.seed(absolute_path(request.get("image"), "image"),
                                       absolute_path(request.get("workdir"), "workdir"), progress=progress)
        if action == "imgtext-build":
            output = absolute_path(request["output"], "output") if request.get("output") else None
            return imgtext_assist.build(absolute_path(request.get("workdir"), "workdir"),
                request.get("ops", {}), seed_hash=request.get("seedHash"), output=output, progress=progress)
        from . import imgtext, imgtext_ocr
        image = absolute_path(request.get("image"), "image")
        output = absolute_path(request.get("output"), "output")
        if output.exists():
            raise HubError("OUTPUT_EXISTS", "输出已存在，请选择新文件名")
        imgtext_assist.ensure_qt()
        progress("ocr", "正在识别图片文字")
        rows = imgtext_ocr.recognize_one(image)
        progress("assemble", "正在生成可编辑 PPT")
        result = imgtext.convert(image, output, rows)
        return {"pptx": str(output), "summary": result.summary()}
    with Library(path, roots) as lib:
        if action == "search":
            plan = request.get("plan") or {"query": request.get("query", ""), "queries": request.get("queries", [])}
            return _identify(lib.search(plan, request.get("limit", 30), request.get("perQuery", 60)), lib)
        if action == "catalog":
            return lib.catalog(request.get("fileId"), request.get("offset", 0), request.get("limit", 30))
        return _identify(lib.read(_refs(request, lib), request.get("neighbors", 0)), lib)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise HubError("INVALID_REQUEST", message)


def main(argv=None, *, allow_server=True) -> int:
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if allow_server and "--hub" in argv and argv[argv.index("--hub") + 1:][:1] == ["serve"]:
        from .hub_server import main as serve_main
        return serve_main(argv)
    start = time.monotonic()
    output = None
    # Recover --out even when another CLI argument is invalid.
    if "--out" in argv and argv.index("--out") + 1 < len(argv):
        candidate = Path(argv[argv.index("--out") + 1])
        if candidate.is_absolute():
            output = candidate
    if output and "--in" in argv and argv.index("--in") + 1 < len(argv):
        if Path(argv[argv.index("--in") + 1]).resolve() == output.resolve():
            # Even malformed argument lists must never replace their request.
            output = None
            return 2
    request, action, progress_path = {}, "", None

    def envelope():
        return {"apiVersion": API_VERSION, "doctorVersion": __version__,
                "requestId": request.get("requestId", ""), "action": action,
                "elapsedMs": round((time.monotonic() - start) * 1000)}

    def progress(stage, message, *, done=0, total=None, state="running"):
        if progress_path:
            write_json(progress_path, {**envelope(), "stage": stage, "message": message,
                                      "done": done, "total": total, "state": state})

    try:
        parser = _Parser(add_help=False)
        parser.add_argument("--hub", required=True)
        parser.add_argument("--in", dest="input", required=True)
        parser.add_argument("--out", required=True)
        parser.add_argument("--progress")
        args = parser.parse_args(argv)
        action = args.hub
        source = absolute_path(args.input, "--in")
        output = absolute_path(args.out, "--out")
        if source == output:
            output = None
            raise HubError("INVALID_REQUEST", "请求与结果文件不能相同")
        if args.progress:
            progress_path = absolute_path(args.progress, "--progress")
            if progress_path in (source, output):
                progress_path = None
                raise HubError("INVALID_REQUEST", "进度、请求、结果文件必须各不相同")
        if source.stat().st_size > 8 * 1024 * 1024:
            raise HubError("INVALID_REQUEST", "请求超过 8 MB")
        request = json.loads(retry_file_change(source.read_text, "utf-8-sig"))
        if not isinstance(request, dict):
            request = {}
            raise HubError("INVALID_REQUEST", "请求必须是 JSON 对象")
        if request.get("apiVersion", API_VERSION) != API_VERSION:
            raise HubError("API_VERSION_UNSUPPORTED", "仅支持 apiVersion=1")
        if not isinstance(request.get("requestId"), str) or not 1 <= len(request["requestId"]) <= 128:
            raise HubError("INVALID_REQUEST", "requestId 需要 1–128 个字符")
        progress("start", "正在处理 PPT 请求")
        result = execute(action, request, progress)
        write_json(output, {**result, **envelope(), "ok": True})
        progress("done", "已完成", done=1, total=1, state="completed")
        return 0
    except Exception as exc:
        from .imgtext_ocr import OcrUnavailable
        if isinstance(exc, PptutorError):
            code = exc.code
        elif isinstance(exc, OcrUnavailable):
            code = "OCR_UNAVAILABLE"
        elif isinstance(exc, (ValueError, TypeError, KeyError)):
            code = "INVALID_REQUEST"
        elif isinstance(exc, OSError):
            code = "IO_ERROR"
        elif isinstance(exc, sqlite3.Error):
            code = "INDEX_READ_FAILED"
        else:
            code = "INTERNAL_ERROR"
        message = str(exc) or type(exc).__name__
        payload = {**envelope(), "ok": False, "code": code,
                   "error": {"code": code, "message": message},
                   "retryable": isinstance(exc, sqlite3.OperationalError)}
        try:
            if output:
                write_json(output, payload)
            progress("failed", message, state="failed")
        except OSError:
            return 4
        if code in {"INVALID_REQUEST", "API_VERSION_UNSUPPORTED", "UNKNOWN_ACTION", "INVALID_OPS", "INVALID_REFERENCE"}:
            return 2
        return 4 if code == "INTERNAL_ERROR" else 3
