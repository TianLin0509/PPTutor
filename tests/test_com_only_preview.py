from __future__ import annotations

import os

import xxhash

from pptx_finder import renderer, thumbnailer


def test_active_powerpoint_never_calls_a_non_com_fallback(tmp_path, monkeypatch):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"pptx")
    monkeypatch.setattr(renderer, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(renderer, "_ipc_enabled", lambda: False)
    monkeypatch.setattr(renderer, "_powerpoint_active", lambda **_kwargs: True)
    assert renderer._render_page_direct(
        str(source),
        2,
        cache_key="strict-com",
        long_edge=1920,
        hi_priority=True,
        use_snapshot=True,
    ) is None


def test_render_cache_key_has_a_com_only_generation_namespace():
    path = os.path.abspath("deck.pptx")
    legacy = xxhash.xxh64(f"{path}|123.5|456".encode()).hexdigest()

    current = renderer.cache_key_for_metadata(path, 123.5, 456)

    assert current != legacy
    assert current == renderer.cache_key_for_metadata(path, 123.5, 456)


def test_non_com_page_api_is_removed():
    assert not hasattr(thumbnailer, "text_" + "page_preview")
    assert not hasattr(thumbnailer, "find_non_com_" + "page_preview")
