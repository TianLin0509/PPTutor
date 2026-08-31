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


def test_frozen_selftest_covers_the_all_files_engine():
    """打包自检必须考到「全部文件」那条路——v1.5.0 的主功能走的是它。

    自检的唯一价值是「打包有没有把东西带齐」。原来它只考内容搜索，
    namestore/namequery/search_names 一条都没考到；那条路真要漏了什么，
    表现是「搜不到」，不报错，装机的人也发现不了。
    """
    from pptx_finder import selftest

    labels = " ".join(label for label, _q, _e in selftest.NAME_CASES)
    for must in ("通配符", "扩展名", "大小", "或", "非", "路径", "正则",
                 "区分大小写", "变音符号", "自动联想", "拼写近似",
                 "文件夹", "中文名"):
        assert must in labels, f"打包自检没覆盖 {must}"


def test_frozen_selftest_never_writes_into_the_real_data_dir(tmp_path, monkeypatch):
    """自检绝不能往用户真实数据目录里落索引。

    namestore 的 write() 不给 dest 时写的就是 data_dir()，还会改写指针——
    自检那样跑一次，等于把用户正在用的那份全盘索引顶掉。
    """
    import inspect

    from pptx_finder import selftest

    src = inspect.getsource(selftest._run_names)
    assert "builder.write(workdir" in src, "自检必须显式指定落盘位置"

    data_dir = tmp_path / "appdata"
    monkeypatch.setenv("PPTX_FINDER_DATA_DIR", str(data_dir))
    work = tmp_path / "work"
    work.mkdir()
    result = selftest._run_names(work)
    assert result["all_pass"] is True, result["cases"]
    # 数据目录要么根本没被创建，要么里面绝不能出现索引文件
    if data_dir.exists():
        assert not list(data_dir.glob("names*.idx"))


def test_package_has_regex_and_windows_version_metadata():
    root = Path(__file__).resolve().parents[1]
    spec = (root / "pptx-finder.spec").read_text(encoding="utf-8")
    version_info = (root / "assets" / "windows_version_info.txt").read_text(
        encoding="utf-8")
    assert "collect_all('regex')" in spec
    assert "version='assets\\\\windows_version_info.txt'" in spec

    # 原来这里写死 "1.5.1"，于是每次升版都会红一次——测的是字面量，不是不变量。
    # 真正要守的是「exe 属性页里的版本必须等于包版本」，所以对着 __version__ 比。
    from pptx_finder import __version__

    parts = tuple(int(x) for x in __version__.split("."))
    assert len(parts) == 3, f"版本号形如 x.y.z：{__version__}"
    assert f"filevers=({parts[0]}, {parts[1]}, {parts[2]}, 0)" in version_info
    assert f"prodvers=({parts[0]}, {parts[1]}, {parts[2]}, 0)" in version_info
    assert f"'FileVersion', '{__version__}'" in version_info
    assert f"'ProductVersion', '{__version__}'" in version_info
