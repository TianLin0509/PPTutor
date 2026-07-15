from pathlib import Path


def test_frozen_spec_excludes_qtpdf_from_com_only_package():
    spec = (Path(__file__).resolve().parents[1] / "pptx-finder.spec").read_text(
        encoding="utf-8"
    )

    assert "hiddenimports += ['PySide6.QtPdf']" not in spec
    excludes_block = spec.split("excludes=[", 1)[1].split("],", 1)[0]
    assert "'PySide6.QtPdf'" in excludes_block
    drop_block = spec.split("_DROP = (", 1)[1].split(")", 1)[0]
    assert "'qt6pdf'" in drop_block


def test_frozen_version_watcher_probe_explicitly_enables_optional_feature():
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "verify_frozen.py"
    ).read_text(encoding="utf-8")

    assert '"version_management_enabled": True' in script
    assert 'ENV["PPTX_FINDER_SINGLETON_NAME"]' in script


def test_frozen_package_and_runtime_use_the_multisize_ico_without_fake_app_id():
    root = Path(__file__).resolve().parents[1]
    spec = (root / "pptx-finder.spec").read_text(encoding="utf-8")
    app = (root / "src" / "pptx_finder" / "app.py").read_text(encoding="utf-8")
    main_window = (
        root / "src" / "pptx_finder" / "ui" / "main_window.py"
    ).read_text(encoding="utf-8")
    shortcut = (root / "scripts" / "gen_shortcut.py").read_text(encoding="utf-8")

    assert "('assets/app.ico', 'assets')" in spec
    assert 'resource_path("assets", "app.ico")' in app
    assert "SetCurrentProcessExplicitAppUserModelID" not in app
    assert "QApplication.instance().windowIcon()" in main_window
    assert 'sc.IconLocation = f"{EXE},0"' in shortcut


def test_no_portable_preview_engine_packager_or_runtime_route_remains():
    root = Path(__file__).resolve().parents[1]
    renderer = (root / "src" / "pptx_finder" / "renderer.py").read_text(
        encoding="utf-8"
    )

    assert not (root / "tools" / ("package_" + "preview_engine.py")).exists()
    assert "LibreOffice" not in renderer
    assert ("_render_page_" + "compat") not in renderer
