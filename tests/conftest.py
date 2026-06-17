"""测试全局配置：把应用数据目录指向临时目录，避免污染真实 LOCALAPPDATA。"""
import os
import tempfile

os.environ.setdefault(
    "PPTX_FINDER_DATA_DIR", tempfile.mkdtemp(prefix="pptxfinder_test_")
)
