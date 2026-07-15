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
