import importlib.util
import json
from pathlib import Path

import pytest

from pptx_finder import editions, updater


@pytest.mark.parametrize('edition,manifest,suffix',[
    ('base','manifest-Base.json','-Base'),('ocr','manifest-Ocr.json','-Ocr'),
    ('full','manifest.json','')])
def test_update_sources_preserve_the_selected_edition(edition, manifest, suffix):
    sources = updater.update_sources('https://example.test', edition=edition)
    assert all(s.manifest_url.endswith('/'+manifest) for s in sources)
    assert sources[-1].package('9.9.9').endswith('PPT-Doctor-v9.9.9'+suffix+'.zip')


def test_manifest_contains_edition_and_no_cross_edition_update(tmp_path, monkeypatch):
    (tmp_path/'_internal').mkdir()
    (tmp_path/editions.MARKER).write_text(json.dumps({'edition':'base'}))
    (tmp_path/'PPT-Doctor.exe').write_bytes(b'MZ')
    local = updater.build_manifest(tmp_path, '1.0.0')
    assert local['edition']=='base' and editions.read_edition(tmp_path)=='base'
    monkeypatch.setattr(updater,'is_frozen',lambda:True)
    monkeypatch.setattr(updater,'local_manifest',lambda:local)
    monkeypatch.setattr(editions,'current_edition',lambda:'base')
    remote=dict(local,version='2.0.0',edition='full')
    monkeypatch.setattr(updater,'fetch_manifest_from',lambda *a,**k:remote)
    assert updater.check_for_update('https://example.test') is None
    remote['edition']='base'
    assert updater.check_for_update('https://example.test').version=='2.0.0'
    # A user who installs Base over Full must remain on the Base channel.
    local['edition']='full'
    assert updater.check_for_update('https://example.test').source.manifest_url.endswith('manifest-Base.json')


def test_legacy_full_channel_and_invalid_marker(tmp_path):
    assert editions.read_edition(tmp_path)=='full'
    (tmp_path/'_internal').mkdir()
    (tmp_path/editions.MARKER).write_text('{"edition":"unknown"}')
    with pytest.raises(ValueError):editions.read_edition(tmp_path)


def test_base_builder_skips_models_and_rejects_mixed_source(tmp_path, monkeypatch):
    spec=importlib.util.spec_from_file_location('builder',Path(__file__).parents[1]/'tools/build_installer.py')
    builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
    (tmp_path/'_internal').mkdir()
    monkeypatch.setattr(builder,'check_dist',lambda:0)
    monkeypatch.setattr(builder,'find_iscc',lambda:None)
    monkeypatch.setattr(builder,'bundle_ocr',lambda *a:pytest.fail('基础版不应打包模型'))
    assert builder.main(['--edition','base','--dist',str(tmp_path)])==2
    assert editions.read_edition(tmp_path)=='base'
    bundle=tmp_path/'_internal/formula';bundle.mkdir();(bundle/'component.zip').write_bytes(b'archive')
    assert builder.main(['--edition','base','--dist',str(tmp_path)])==1
