import ast
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


def test_qtnetwork_usage_stays_local_ipc_only_while_x64_openssl_is_pruned():
    """The x64 pair is dynamically loaded; source scope, not PE graph, is the guard."""
    root = Path(__file__).resolve().parents[1]
    spec = (root / "pptx-finder.spec").read_text(encoding="utf-8")
    allowed = {"QLocalServer", "QLocalSocket"}
    seen = set()
    for source in (root / "src" / "pptx_finder").rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "PySide6.QtNetwork":
                names = {item.name for item in node.names}
                assert "*" not in names
                assert names <= allowed, f"QtNetwork TLS/client use requires packaging review: {source}"
                seen.update(names)
            if isinstance(node, ast.Import):
                assert all(
                    item.name != "PySide6.QtNetwork" for item in node.names
                ), f"module-level QtNetwork access bypasses the packaging guard: {source}"
    assert seen == allowed
    assert "'libcrypto-3-x64.dll', 'libssl-3-x64.dll'" in spec
    assert "static PE import graph cannot prove them unused" in spec


def test_frozen_verifier_checks_tls_manifest_and_second_instance_ipc():
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "verify_frozen.py"
    ).read_text(encoding="utf-8")
    for required in (
        '"_ssl.pyd"',
        '"libssl-3.dll"',
        '"libcrypto-3.dll"',
        '"qt6network.dll"',
        '"qschannelbackend.dll"',
    ):
        assert required in script
    assert "subprocess.run([str(EXE)], env=ENV, timeout=5" in script
    assert "proc.poll() is None" in script
