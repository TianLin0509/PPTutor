# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [
    ('assets/logo.png', 'assets'),
    ('assets/app.ico', 'assets'),
    ('assets/ui-chevron-dark.svg', 'assets'),
    ('assets/ui-chevron-light.svg', 'assets'),
    ('assets/ui-check.svg', 'assets'),
]
binaries = []
hiddenimports = []
# OpenCC 繁简转换：必须打包它的 .ocd2/.json 词典 + opencc_clib .pyd，
# 否则 frozen 下 OpenCC("t2s") 找不到词典 → 繁简归一化静默失效（搜「软件」漏掉「軟件」）。
tmp_ret = collect_all('opencc')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# pypdf（v1.0.0 PDF 内容搜索）：parse_pdf 内惰性 import pypdf，且 pypdf 用 importlib.metadata
# 读自身版本——必须带上 dist-info 元数据 + 全子模块，否则 frozen 下搜 PDF 静默失效/报错。
tmp_ret = collect_all('pypdf')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
a = Analysis(
    ['src\\pptx_finder\\__main__.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PIL', 'Pillow', 'pptx', 'tkinter', 'matplotlib', 'pandas', 'IPython', 'pytest',
              'datasketch', 'numpy', 'scipy',
              'jieba.analyse', 'jieba.posseg', 'jieba.lac_small',
              'PySide6.QtQuick', 'PySide6.QtQml', 'PySide6.QtQuickWidgets', 'PySide6.QtQuick3D',
              'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebChannel',
              'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets',
              'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtCharts', 'PySide6.QtDataVisualization',
              'PySide6.QtDesigner', 'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# —— 体积优化：剔除搜索 app 用不到的 Qt 模块 DLL + 软件 OpenGL 兜底（QWidget 纯光栅渲染）。
# 只保留 Qt6Core/Gui/Widgets/Network(单实例)/Svg 等必需项；版本归组已改为内置轻量
# MinHash，datasketch/numpy/scipy 不再进入打包产物。
_DROP = (
    'opengl32sw', 'd3dcompiler',
    # report_insights only calls basic jieba.cut. Keyword-extraction corpora,
    # POS models and Paddle/LAC weights are unreachable and cost ~23 MiB.
    'jieba/lac_small/', 'jieba/analyse/', 'jieba/posseg/',
    # Qt's OpenSSL backend can discover these x64-named DLLs dynamically, so a
    # static PE import graph cannot prove them unused.  Removing them is safe
    # only while product QtNetwork use stays limited to local IPC
    # via QLocalSocket and QLocalServer.  Python HTTPS keeps the non-suffixed pair;
    # tests/test_package_spec.py locks this boundary.  Saves about 5.8 MiB.
    'libcrypto-3-x64.dll', 'libssl-3-x64.dll',
    'opencc/clib/lib', 'opencc/clib/include', 'opencc/clib/bin',
    'qt6quick', 'qt6qml', 'qml', 'qt6pdf', 'qt6webengine', 'qt6webchannel', 'qt6websockets',
    'qt63d', 'qt6quick3d', 'qt6multimedia', 'qt6charts', 'qt6datavisualization', 'qt6designer',
    'qt6opengl', 'qt6virtualkeyboard', 'qt6sensors', 'qt6bluetooth', 'qt6nfc', 'qt6positioning',
    'qt6serialport', 'qt6serialbus', 'qt6sql', 'qt6test', 'qt6help', 'qt6spatialaudio',
    'qt6texttospeech', 'qt6remoteobjects', 'qt6scxml', 'qt6statemachine', 'qt6labs',
    'pyside6/plugins/platforms/qdirect2d', 'pyside6/plugins/platforms/qoffscreen',
    'pyside6/plugins/platforms/qminimal',
    'pyside6/plugins/imageformats/qwebp', 'pyside6/plugins/imageformats/qtiff',
    'pyside6/plugins/imageformats/qicns', 'pyside6/plugins/imageformats/qpdf',
    'pyside6/plugins/imageformats/qtga', 'pyside6/plugins/imageformats/qwbmp',
)

_KEEP_TRANSLATIONS = (
    'qtbase_zh_cn.qm',
    'qtbase_zh_tw.qm',
    'qt_zh_cn.qm',
    'qt_zh_tw.qm',
)


def _keep(entry):
    n = entry[0].replace('\\', '/').lower()
    if 'pyside6/translations/' in n:
        return n.rsplit('/', 1)[-1] in _KEEP_TRANSLATIONS
    return not any(d in n for d in _DROP)


a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PPT Doctor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\app.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PPT Doctor',
)
