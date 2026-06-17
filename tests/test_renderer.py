"""renderer 测试：真实调用 PowerPoint COM 渲染一页（机器需装 Office）。

标记 slow：共享一个 PowerPoint 实例，整模块结束才 shutdown，避免反复启停。
日常快速迭代用 `pytest -m "not slow"` 跳过。
"""
from __future__ import annotations

import pytest

import fixtures_gen as fx

from pptx_finder import renderer

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module", autouse=True)
def _shutdown_renderer():
    yield
    renderer.shutdown()


def test_render_real_page(tmp_path):
    p = tmp_path / "r.pptx"
    fx.make_pptx(p, [{"body": "第一页 ALPHA"}, {"body": "第二页 BETA 昇腾算力"}])
    out = renderer.render_page(str(p), 2, cache_key="rtest")
    if out is None:
        pytest.skip("PowerPoint COM 不可用，跳过真实渲染（E2E 阶段再验）")
    assert out.exists() and out.stat().st_size > 1000, "应导出有内容的 PNG"
    # 第二次命中缓存，返回同一路径
    assert renderer.render_page(str(p), 2, cache_key="rtest") == out


def test_render_out_of_range(tmp_path):
    """越界页应返回 None（无论 COM 是否可用都不崩）。"""
    p = tmp_path / "r.pptx"
    fx.make_pptx(p, [{"body": "only one"}])
    assert renderer.render_page(str(p), 99, cache_key="oor") is None
