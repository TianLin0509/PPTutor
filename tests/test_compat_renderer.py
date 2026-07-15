from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtGui import QImage
from pypdf import PdfWriter

from pptx_finder import renderer


def test_find_soffice_discovers_adjacent_portable_preview_engine(tmp_path, monkeypatch):
    bundled = (
        tmp_path
        / "preview-engine"
        / "LibreOfficePortable"
        / "App"
        / "libreoffice"
        / "program"
        / "soffice.com"
    )
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"portable")
    monkeypatch.delenv("PPTUTOR_SOFFICE_PATH", raising=False)
    monkeypatch.delenv("PPTUTOR_PREVIEW_ENGINE_DIR", raising=False)
    monkeypatch.setattr(renderer, "_application_dir", lambda: tmp_path, raising=False)
    monkeypatch.setattr(renderer.shutil, "which", lambda _name: None)

    assert renderer._find_soffice() == bundled


def test_preview_engine_status_identifies_adjacent_engine(tmp_path, monkeypatch):
    bundled = (
        tmp_path
        / "preview-engine"
        / "LibreOfficePortable"
        / "App"
        / "libreoffice"
        / "program"
        / "soffice.com"
    )
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"portable")
    monkeypatch.setattr(renderer, "_application_dir", lambda: tmp_path, raising=False)
    monkeypatch.setattr(renderer, "_find_soffice", lambda: bundled, raising=False)

    status = renderer.preview_engine_status()

    assert status["available"] is True
    assert status["source"] == "bundled"
    assert status["path"] == str(bundled)
    assert any(
        "preview_engine: available=True source=bundled" in line
        for line in renderer.diagnostic_lines()
    )


def test_preview_engine_status_explains_missing_engine(monkeypatch):
    monkeypatch.setattr(renderer, "_find_soffice", lambda: None, raising=False)

    status = renderer.preview_engine_status()

    assert status == {"available": False, "source": "missing", "path": "-"}


def test_compat_renderer_converts_once_then_renders_any_page(tmp_path, monkeypatch):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"pptx")
    soffice = tmp_path / "soffice.com"
    soffice.write_bytes(b"exe")
    calls: list[list[str]] = []
    run_options: list[dict] = []

    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(renderer, "_find_soffice", lambda: soffice, raising=False)

    def fake_run(command, **kwargs):
        calls.append(list(command))
        run_options.append(dict(kwargs))
        out_dir = Path(command[command.index("--outdir") + 1])
        input_path = Path(command[-1])
        writer = PdfWriter()
        writer.add_blank_page(width=1600, height=900)
        writer.add_blank_page(width=900, height=1600)
        with (out_dir / f"{input_path.stem}.pdf").open("wb") as stream:
            writer.write(stream)
        return subprocess.CompletedProcess(command, 0, stdout="converted", stderr="")

    monkeypatch.setattr(renderer.subprocess, "run", fake_run, raising=False)

    first = renderer._render_page_compat(
        str(source),
        1,
        "compat-key",
        1000,
        tmp_path / "page-1.png",
    )
    second = renderer._render_page_compat(
        str(source),
        2,
        "compat-key",
        1000,
        tmp_path / "page-2.png",
    )

    assert first == tmp_path / "page-1.png"
    assert second == tmp_path / "page-2.png"
    assert not QImage(str(first)).isNull()
    assert not QImage(str(second)).isNull()
    assert len(calls) == 1
    assert "--headless" in calls[0]
    profile_arg = next(arg for arg in calls[0] if arg.startswith("-env:UserInstallation="))
    assert "compat_profile" in profile_arg
    assert "compat_work" not in profile_arg
    assert run_options[0]["timeout"] <= 30
    if renderer.os.name == "nt":
        assert run_options[0]["creationflags"] & subprocess.BELOW_NORMAL_PRIORITY_CLASS


def test_compat_renderer_fails_quietly_when_soffice_is_unavailable(tmp_path, monkeypatch):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"pptx")
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(renderer, "_find_soffice", lambda: None, raising=False)

    assert renderer._render_page_compat(
        str(source),
        1,
        "missing-engine",
        900,
        tmp_path / "missing.png",
    ) is None


def test_successful_conversion_survives_transient_workdir_cleanup_failure(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"pptx")
    soffice = tmp_path / "soffice.com"
    soffice.write_bytes(b"exe")
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(renderer, "_find_soffice", lambda: soffice, raising=False)

    def fake_run(command, **_kwargs):
        out_dir = Path(command[command.index("--outdir") + 1])
        input_path = Path(command[-1])
        writer = PdfWriter()
        writer.add_blank_page(width=1600, height=900)
        with (out_dir / f"{input_path.stem}.pdf").open("wb") as stream:
            writer.write(stream)
        return subprocess.CompletedProcess(command, 0, stdout="converted", stderr="")

    real_rmtree = renderer.shutil.rmtree

    def transient_cleanup_failure(path, *args, **kwargs):
        if "compat_work" in str(path):
            raise OSError(145, "directory is not empty")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(renderer.subprocess, "run", fake_run, raising=False)
    monkeypatch.setattr(renderer.shutil, "rmtree", transient_cleanup_failure)

    out = renderer._render_page_compat(
        str(source),
        1,
        "cleanup-race",
        1000,
        tmp_path / "page-1.png",
    )

    assert out == tmp_path / "page-1.png"
    assert not QImage(str(out)).isNull()
    assert (tmp_path / "cache" / "compat_pdf" / "cleanup-race.pdf").exists()


def test_active_powerpoint_falls_back_to_isolated_compat(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"pptx")
    compat = tmp_path / "compat.png"
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(renderer, "_ipc_enabled", lambda: False)
    monkeypatch.setattr(renderer, "_powerpoint_active", lambda **_kwargs: True)
    monkeypatch.setattr(renderer, "_render_page_compat", lambda *_args: compat)
    monkeypatch.setattr(
        renderer,
        "_get_app",
        lambda: (_ for _ in ()).throw(AssertionError("must not attach through ROT")),
    )

    assert renderer._render_page_direct(
        str(source),
        2,
        cache_key="compat-fallback",
        long_edge=901,
        hi_priority=True,
        use_snapshot=True,
    ) == compat


def test_active_powerpoint_never_uses_preview_handler_when_compat_is_unavailable(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"pptx")
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(renderer, "_ipc_enabled", lambda: False)
    monkeypatch.setattr(renderer, "_powerpoint_active", lambda **_kwargs: True)
    monkeypatch.setattr(
        renderer,
        "_render_page_compat",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        renderer,
        "_get_app",
        lambda: (_ for _ in ()).throw(AssertionError("must not attach to user PowerPoint")),
    )

    assert renderer._render_page_direct(
        str(source),
        2,
        cache_key="shell-fallback",
        long_edge=901,
        hi_priority=True,
        use_snapshot=True,
    ) is None


def test_compat_timeout_terminates_only_the_isolated_profile_processes(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"pptx")
    soffice = tmp_path / "soffice.com"
    soffice.write_bytes(b"exe")
    terminated: list[str] = []
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(renderer, "_find_soffice", lambda: soffice, raising=False)
    monkeypatch.setattr(
        renderer.subprocess,
        "run",
        lambda command, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(command, 30)
        ),
    )
    monkeypatch.setattr(
        renderer,
        "_terminate_compat_processes",
        lambda profile_uri: terminated.append(profile_uri) or 1,
        raising=False,
    )

    assert renderer._render_page_compat(
        str(source),
        1,
        "timeout-tree",
        1000,
        tmp_path / "timeout.png",
    ) is None
    assert len(terminated) == 1
    assert terminated[0].startswith("file:")
