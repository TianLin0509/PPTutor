from __future__ import annotations

from pptx_finder import selftest


def test_qtpdf_frozen_probe_renders_a_real_page(tmp_path):
    result = selftest._qtpdf_probe(tmp_path)

    assert result["ok"] is True
    assert result["bytes"] > 0
