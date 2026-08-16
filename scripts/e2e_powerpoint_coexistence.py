"""Real PowerPoint coexistence E2E for versioning and preview isolation.

Safety contract:
- refuses to run when any POWERPNT.EXE already exists;
- uses an isolated data directory and temporary deck;
- closes only the presentation it created;
- calls Application.Quit only if no other presentation appeared meanwhile;
- never kills a PowerPoint process.
"""
from __future__ import annotations

import csv
import gc
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


def _powerpoint_pids() -> set[int]:
    completed = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq POWERPNT.EXE", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        timeout=5,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"tasklist failed: {completed.stderr.strip()}")
    pids: set[int] = set()
    for row in csv.reader(io.StringIO(completed.stdout)):
        if len(row) >= 2 and row[0].upper() == "POWERPNT.EXE":
            try:
                pids.add(int(row[1]))
            except ValueError:
                continue
    return pids


def _norm(path: object) -> str:
    return os.path.normcase(os.path.abspath(str(path or "")))


def _wait_until(predicate, timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.08)
    return bool(predicate())


def _deck_text(path: Path) -> str:
    from pptx_finder.parser import parse_pptx

    return " ".join(page.raw_text for page in parse_pptx(str(path)).pages)


def _fresh_presentation_state(path: Path, timeout: float = 6.0):
    """Reattach for each audit so a stale automation proxy cannot hide UI state."""
    import win32com.client

    deadline = time.monotonic() + timeout
    last_error = None
    while True:
        try:
            app = win32com.client.GetActiveObject("PowerPoint.Application")
            presentations = app.Presentations
            target = _norm(path)
            found = None
            for index in range(1, int(presentations.Count) + 1):
                candidate = presentations.Item(index)
                if _norm(candidate.FullName) == target:
                    found = candidate
                    break
            if found is None:
                raise RuntimeError(
                    f"user presentation disappeared from PowerPoint: {path}"
                )
            return app, found, {
                "windows": int(app.Windows.Count),
                "presentations": int(presentations.Count),
                "active": _norm(app.ActivePresentation.FullName),
                "saved": int(found.Saved),
            }
        except Exception as exc:  # noqa: BLE001 Office may transiently reject RPC
            last_error = exc
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"PowerPoint state audit did not recover: {last_error}"
                ) from exc
            time.sleep(0.2)


def _presentation_state_from_app(app, path: Path) -> tuple[object, dict]:
    """Audit through the exact COM proxy currently doing the borrowed render."""
    presentations = app.Presentations
    target = _norm(path)
    found = None
    for index in range(1, int(presentations.Count) + 1):
        candidate = presentations.Item(index)
        if _norm(candidate.FullName) == target:
            found = candidate
            break
    if found is None:
        raise RuntimeError("borrowed renderer lost the user presentation")
    return found, {
        "windows": int(app.Windows.Count),
        "presentations": int(presentations.Count),
        "active": _norm(app.ActivePresentation.FullName),
        "saved": int(found.Saved),
    }


def _cleanup_owned_presentation_in_fresh_apartment(path: Path) -> dict:
    """Close the exact E2E deck from a fresh COM apartment.

    The renderer intentionally initializes/releases COM inside its own
    apartment.  Reusing that same Python thread for test-harness cleanup can
    leave stale pywin32 proxies even though PowerPoint itself is healthy.  A
    fresh helper mirrors the product's process boundary and never broadens the
    close target beyond this script's exact temporary path.
    """
    helper = r'''
import json, os, sys, time
import pythoncom, win32com.client
target = os.path.normcase(os.path.abspath(sys.argv[1]))
result = {"closed": False, "remaining": None, "quit": False, "error": ""}
pythoncom.CoInitialize()
try:
    last = None
    for _attempt in range(30):
        try:
            app = win32com.client.GetActiveObject("PowerPoint.Application")
            presentations = app.Presentations
            victim = None
            for index in range(1, int(presentations.Count) + 1):
                candidate = presentations.Item(index)
                full = os.path.normcase(os.path.abspath(str(candidate.FullName or "")))
                if full == target:
                    victim = candidate
                    break
            if victim is not None:
                try:
                    victim.Saved = True
                except Exception:
                    pass
                victim.Close()
                result["closed"] = True
            result["remaining"] = int(app.Presentations.Count)
            if result["remaining"] == 0:
                app.Quit()
                result["quit"] = True
            break
        except Exception as exc:
            last = exc
            time.sleep(0.2)
    else:
        result["error"] = f"{type(last).__name__}: {last}"
finally:
    pythoncom.CoUninitialize()
print(json.dumps(result))
raise SystemExit(0 if not result["error"] else 2)
'''
    try:
        completed = subprocess.run(
            [sys.executable, "-c", helper, str(path)],
            capture_output=True,
            text=True,
            timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # Office can complete Close/Quit yet keep the COM call blocked while its
        # process tears down.  ``subprocess.run`` has already terminated the
        # stuck helper; the caller separately waits for POWERPNT.EXE to exit.
        return {
            "timed_out": True,
            "stdout": str(exc.stdout or ""),
            "stderr": str(exc.stderr or ""),
        }
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        result = {}
    result["returncode"] = completed.returncode
    if completed.stderr.strip():
        result["stderr"] = completed.stderr.strip()
    return result


def main() -> int:
    artifact = ROOT / "artifacts" / "e2e_powerpoint_coexistence.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    before_pids = _powerpoint_pids()
    if before_pids:
        result = {
            "passed": False,
            "blocked": True,
            "reason": "A user PowerPoint process already exists; safety refusal",
            "preexisting_pids": sorted(before_pids),
        }
        artifact.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 3

    work = Path(tempfile.mkdtemp(prefix="pptdoctor_com_coexist_"))
    os.environ["PPTX_FINDER_DATA_DIR"] = str(work / "appdata")
    os.environ["PPTUTOR_VERSION_RECONCILE_COMMON_DIRS"] = "0"

    import fixtures_gen as fx
    import pythoncom
    import win32com.client

    from pptx_finder import renderer
    from pptx_finder.versioning import vault
    from pptx_finder.versioning.manager import VersionManager
    from pptx_finder.versioning.watcher import VaultWatcher

    checks: dict[str, bool] = {}
    metrics: dict[str, object] = {}
    app = None
    user_pres = None
    manager = None
    watcher = None
    deck = None
    shape = None
    borrowed_app = None
    hidden_snapshot = None
    pythoncom.CoInitialize()
    try:
        deck = work / "真实用户编辑.pptx"
        fx.make_pptx(deck, [{"body": "BASELINE_V1"}, {"body": "第二页"}])

        app = win32com.client.DispatchEx("PowerPoint.Application")
        app.Visible = True
        user_pres = app.Presentations.Open(str(deck), ReadOnly=0, WithWindow=1)
        user_path = _norm(user_pres.FullName)
        windows_initial = int(app.Windows.Count)
        presentations_initial = int(app.Presentations.Count)
        checks["script_owns_only_initial_presentation"] = presentations_initial == 1
        checks["user_presentation_is_active"] = (
            _norm(app.ActivePresentation.FullName) == user_path
        )

        manager = VersionManager(index_roots=[str(work)])
        first_version = manager.snapshot_now(str(deck), notify=False)
        checks["snapshot_while_powerpoint_open"] = bool(first_version)

        watcher = VaultWatcher([str(work)], manager.snapshot_now)
        watcher.start()
        shape = user_pres.Slides(1).Shapes.AddTextbox(1, 60, 60, 520, 60)
        shape.Name = "PPTDoctorE2EUserEdit"
        shape.TextFrame.TextRange.Text = "WATCHER_SAVE_V2"
        save_started = time.perf_counter()
        user_pres.Save()
        metrics["watcher_save_ms"] = round((time.perf_counter() - save_started) * 1000, 1)
        checks["watcher_captures_real_powerpoint_save"] = _wait_until(
            lambda: len(manager.list_versions(str(deck))) >= 2
        )
        watcher.stop()
        watcher = None

        # Hold parsing after the immutable copy exists, then save again through
        # real PowerPoint.  The user's Save must not wait on our slow work and
        # the captured version must remain the pre-overlap state.
        shape.TextFrame.TextRange.Text = "OVERLAP_SOURCE_V3"
        user_pres.Save()
        parse_entered = threading.Event()
        release_parse = threading.Event()
        snapshot_result: list[str | None] = []
        snapshot_error: list[str] = []
        real_parse = vault.parse_pptx

        def slow_parse(path):
            if not parse_entered.is_set():
                parse_entered.set()
                if not release_parse.wait(10):
                    raise TimeoutError("E2E parse release timed out")
            return real_parse(path)

        def take_slow_snapshot() -> None:
            try:
                snapshot_result.append(manager.snapshot_now(str(deck), notify=False))
            except Exception as exc:  # noqa: BLE001
                snapshot_error.append(f"{type(exc).__name__}: {exc}")

        vault.parse_pptx = slow_parse
        worker = threading.Thread(target=take_slow_snapshot, name="E2ESlowSnapshot")
        worker.start()
        checks["slow_snapshot_reaches_post_copy_parse"] = parse_entered.wait(5)
        shape.TextFrame.TextRange.Text = "USER_CONTINUES_V4"
        overlap_save_started = time.perf_counter()
        user_pres.Save()
        metrics["save_during_slow_snapshot_ms"] = round(
            (time.perf_counter() - overlap_save_started) * 1000,
            1,
        )
        release_parse.set()
        worker.join(timeout=20)
        vault.parse_pptx = real_parse
        checks["slow_snapshot_finishes"] = (
            not worker.is_alive()
            and not snapshot_error
            and bool(snapshot_result and snapshot_result[0])
        )
        checks["powerpoint_save_not_blocked_by_snapshot"] = (
            float(metrics["save_during_slow_snapshot_ms"]) < 5000
        )

        captured = work / "captured-v3.pptx"
        checks["captured_overlap_version_exports"] = bool(
            snapshot_result
            and snapshot_result[0]
            and manager.export(str(deck), str(snapshot_result[0]), str(captured))
        )
        captured_text = _deck_text(captured) if captured.exists() else ""
        checks["stable_copy_is_v3_not_later_v4"] = (
            "OVERLAP_SOURCE_V3" in captured_text
            and "USER_CONTINUES_V4" not in captured_text
        )
        latest_version = manager.snapshot_now(str(deck), notify=False)
        checks["later_user_save_gets_own_version"] = bool(latest_version)

        # Explicit preview may borrow the session, but only through an audited,
        # hidden, read-only snapshot.  User window/active/Saved state must not move.
        app, user_pres, before_preview = _fresh_presentation_state(deck)
        preview = renderer._render_page_direct(
            str(deck),
            1,
            cache_key="real-com-coexistence",
            long_edge=960,
            hi_priority=True,
            use_snapshot=True,
            allow_borrowed_session=True,
        )
        checks["borrowed_preview_renders"] = bool(
            preview and Path(preview).is_file() and Path(preview).stat().st_size > 0
        )
        try:
            int(app.Windows.Count)
            metrics["original_user_com_proxy_survived_preview"] = True
        except Exception:  # noqa: BLE001 PowerPoint can invalidate a secondary proxy
            metrics["original_user_com_proxy_survived_preview"] = False
        borrowed_app = getattr(renderer._state, "app", None)
        user_pres, during_preview = _presentation_state_from_app(borrowed_app, deck)
        hidden_snapshot = getattr(renderer._state, "pres", None)
        checks["preview_keeps_user_window_and_active_document"] = (
            during_preview["windows"] == before_preview["windows"]
            and during_preview["active"] == before_preview["active"]
            and during_preview["saved"] == before_preview["saved"]
        )
        metrics["presentations_during_hidden_preview"] = during_preview["presentations"]
        checks["preview_adds_only_one_hidden_snapshot"] = (
            during_preview["presentations"] == before_preview["presentations"] + 1
            and int(hidden_snapshot.Windows.Count) == 0
            and int(hidden_snapshot.ReadOnly) != 0
        )
        renderer.shutdown()
        app, user_pres, after_preview = _fresh_presentation_state(deck, timeout=10.0)
        checks["preview_cleanup_closes_only_owned_snapshot"] = (
            after_preview["presentations"] == before_preview["presentations"]
            and after_preview["windows"] == before_preview["windows"]
            and after_preview["active"] == before_preview["active"]
        )

        # A restore request against the visible user document must fail closed,
        # even though Windows may allow an ordinary shared r+b file handle.
        disk_before_restore = deck.read_bytes()
        restore_ok = manager.restore_to(str(deck), str(first_version))
        checks["restore_refuses_open_user_presentation"] = (
            restore_ok is False
            and manager.last_restore_error() == vault.REBUILD_ERR_LOCKED
            and deck.read_bytes() == disk_before_restore
        )
        app, user_pres, final_state = _fresh_presentation_state(deck)
        checks["user_state_survives_all_version_operations"] = (
            final_state["windows"] == windows_initial
            and final_state["presentations"] == presentations_initial
            and final_state["active"] == user_path
            and final_state["saved"] != 0
        )
    except Exception as exc:  # noqa: BLE001
        metrics["exception"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            vault.parse_pptx = real_parse  # type: ignore[name-defined]
        except Exception:  # noqa: BLE001
            pass
        if watcher is not None:
            watcher.stop()
        if manager is not None:
            manager.stop()
        try:
            renderer.shutdown()
        except Exception:  # noqa: BLE001
            pass
        user_pres = None
        app = None
        shape = None
        borrowed_app = None
        hidden_snapshot = None
        pythoncom.CoUninitialize()
        gc.collect()

    if deck is not None and _powerpoint_pids():
        metrics["fresh_apartment_cleanup"] = _cleanup_owned_presentation_in_fresh_apartment(
            deck
        )

    cleanup_wait_started = time.monotonic()
    cleanup_exit_observed = _wait_until(
        lambda: not (_powerpoint_pids() - before_pids),
        # On Office 365, Quit can return with Presentations.Count == 0 while
        # POWERPNT.EXE spends just over a minute tearing down add-ins/COM state.
        # Never broaden this into process termination: the E2E simply waits for
        # the process it created to leave on its own.
        timeout=90,
    )
    metrics["cleanup_wait_sec"] = round(time.monotonic() - cleanup_wait_started, 1)
    after_pids = _powerpoint_pids()
    product_passed = bool(checks) and all(checks.values())
    cleanup_completed = cleanup_exit_observed and not (after_pids - before_pids)
    result = {
        "passed": product_passed and cleanup_completed,
        "product_passed": product_passed,
        "cleanup_completed": cleanup_completed,
        "blocked": False,
        "checks": checks,
        "metrics": metrics,
        "preexisting_pids": sorted(before_pids),
        "remaining_pids": sorted(after_pids),
    }
    artifact.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    shutil.rmtree(work, ignore_errors=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
