"""Renderer child process service.

The GUI process talks to this module over a localhost socket. PowerPoint COM
automation stays in this child process, so a COM crash or hang can be isolated
from the main UI process.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any


def _json_dumps(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def handle_request(req: dict[str, Any]) -> dict[str, Any]:
    """Handle one renderer IPC request."""
    from . import renderer

    op = req.get("op")
    req_id = req.get("id")
    try:
        if op == "ping":
            return {"id": req_id, "ok": True, "pong": True}
        if op in {"render", "render_once"}:
            render_kwargs = {
                "cache_key": req.get("cache_key"),
                "long_edge": int(req.get("long_edge") or 2560),
                "hi_priority": bool(req.get("hi_priority")),
                "priority": req.get("priority"),
                "use_snapshot": bool(req.get("use_snapshot")),
                "allow_borrowed_session": bool(req.get("allow_borrowed_session")),
            }
            if req.get("existing_session_only"):
                render_kwargs["existing_session_only"] = True
            try:
                png = renderer.render_page(
                    str(req.get("path") or ""),
                    int(req.get("page_no") or 1),
                    **render_kwargs,
                )
            finally:
                if op == "render_once":
                    renderer.close_current_presentation()
            return {"id": req_id, "ok": True, "path": str(png) if png else ""}
        if op == "close_current":
            renderer.close_current_presentation()
            return {"id": req_id, "ok": True}
        if op == "release_session":
            return {
                "id": req_id,
                "ok": True,
                "released": bool(renderer.release_session()),
            }
        if op == "prewarm":
            return {"id": req_id, "ok": True, "prewarmed": bool(renderer.prewarm())}
        if op == "shutdown":
            renderer.shutdown()
            return {"id": req_id, "ok": True, "shutdown": True}
        return {"id": req_id, "ok": False, "error": f"unknown op: {op}"}
    except Exception as exc:  # noqa: BLE001 - child process must not tear down IPC on one bad file
        return {"id": req_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def serve(sock: socket.socket, token: str) -> int:
    """Serve JSON-line requests on an already connected socket."""
    # Mark this process before importing renderer in request handlers.
    os.environ["PPTUTOR_RENDERER_CHILD"] = "1"
    try:
        sock.settimeout(None)
    except OSError:
        return 0
    with sock:
        f = sock.makefile("rwb", buffering=0)
        try:
            f.write(_json_dumps({"type": "hello", "token": token, "pid": os.getpid()}))
        except OSError:
            return 0
        while True:
            try:
                line = f.readline()
            except OSError:
                break
            if not line:
                break
            try:
                req = json.loads(line.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                try:
                    f.write(_json_dumps({"id": None, "ok": False, "error": f"bad json: {exc}"}))
                except OSError:
                    break
                continue
            resp = handle_request(req)
            try:
                f.write(_json_dumps(resp))
            except OSError:
                break
            if resp.get("shutdown"):
                break
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    try:
        idx = argv.index("--renderer-worker")
    except ValueError:
        print("missing --renderer-worker", file=sys.stderr)
        return 2
    try:
        port = int(argv[idx + 1])
        token = str(argv[idx + 2])
    except (IndexError, ValueError):
        print("usage: --renderer-worker <port> <token>", file=sys.stderr)
        return 2

    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    except OSError:
        return 0
    try:
        return serve(sock, token)
    except OSError:
        return 0
    finally:
        # Best-effort direct COM cleanup in case the shutdown command was not sent.
        try:
            from . import renderer

            renderer.shutdown()
        except Exception:  # noqa: BLE001
            pass
        # Keep PyInstaller from holding cwd handles through imported modules.
        try:
            os.chdir(str(Path.home()))
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
