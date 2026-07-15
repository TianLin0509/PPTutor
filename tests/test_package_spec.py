from pathlib import Path


def test_frozen_spec_keeps_qtpdf_for_isolated_preview_fallback():
    spec = (Path(__file__).resolve().parents[1] / "pptx-finder.spec").read_text(
        encoding="utf-8"
    )

    assert "hiddenimports += ['PySide6.QtPdf']" in spec
    excludes_block = spec.split("excludes=[", 1)[1].split("],", 1)[0]
    assert "'PySide6.QtPdf'" not in excludes_block
    drop_block = spec.split("_DROP = (", 1)[1].split(")", 1)[0]
    assert "'qt6pdf'" not in drop_block


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


def test_preview_engine_pack_uses_official_portable_build_and_all_in_one_layout():
    script = (
        Path(__file__).resolve().parents[1] / "tools" / "package_preview_engine.py"
    ).read_text(encoding="utf-8")

    assert "download.documentfoundation.org/libreoffice/portable/" in script
    assert 'Path("PPT Doctor") / "preview-engine" / "LibreOfficePortable"' in script
    assert "LibreOfficePortable/App/libreoffice/program/soffice.com" in script
    assert "PPT-Doctor-v{__version__}-with-preview-engine.zip" in script
