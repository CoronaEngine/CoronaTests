"""Compact bundles retain usable references and thresholds, not raw captures."""
from pathlib import Path
import json
import zipfile

from PIL import Image
import pytest

from scripts import workflow


@pytest.fixture
def legacy_golden(tmp_path):
    source = tmp_path / 'legacy'
    image = source / 'runs/box--pt2048/repeat-0/image.png'
    image.parent.mkdir(parents=True)
    Image.new('RGB', (32, 24), (120, 80, 40)).save(image)
    for name in ('runs/box--pt2048/repeat-0/setup/image.png',
                 'runs/box--pt2048/repeat-1/image.png', 'zooms/one.png'):
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(image.read_bytes())
    workflow.save(source/'runs/box--pt2048/repeat-0/timing.json', {'frames':3})
    (source/'runs/box--pt2048/repeat-0/engine.log').write_text('diagnostic')
    (source/'runs/box--pt2048/repeat-0/frames.csv').write_text('frame,ms\n1,2\n')
    provenance = {'golden_id':'legacy','job_id':'box--pt2048',
                  'input_fingerprint':'input','engine_fingerprint':'engine'}
    profile = {'status':'calibrated','provenance':provenance,
               'thresholds':{'full':{'mae':.003}},'region_bounds':{}}
    row = {'scene_id':'box','algorithm_id':'pt2048','job_id':'box--pt2048',
           'status':'complete','input_fingerprint':'input','image':image.relative_to(source).as_posix(),
           'image_sha256':workflow.sha(image),'configuration':{'frames':3,'warmup':0,'spp_per_frame':1},
           'timing':{'frames':3,'warmup_frames':0,'mean_ms':2,'median_ms':2,'total_ms':6,'sample_ms':2},
           'calibration':profile,'evidence':['frames.csv','engine.log'],'evidence_sha256':{'engine.log':'old'}}
    result = {'kind':'golden','id':'legacy','suite':'full','created_utc':'2026-11-16T00:00:00Z',
              'output':str(source),'metadata':{'engine_fingerprint':'engine'},'records':[row],
              'summary':{'complete':1},'valid':True,'calibrated':True}
    workflow.save(source/'workflow-result.json',result)
    workflow.save(source/'report.json',result)
    workflow.save(source/'run-config.json',{'id':'legacy','contract':{'frames':3}})
    workflow.save(source/'thresholds.json',{'golden_id':'legacy','profiles':{'box--pt2048':profile}})
    workflow.save(source/'golden.json',workflow.seal({
        'schema':1,'id':'legacy','suite':'full','created_utc':result['created_utc'],
        'valid':True,'calibrated':True,'metadata':result['metadata'],
        'coverage':{'scenes':['box'],'algorithms':['pt2048'],'jobs':1},'records':[row],
        'artifacts':workflow.artifacts(source),'thresholds_sha256':workflow.sha(source/'thresholds.json')}))
    return source


def test_compact_keeps_one_original_image_summary_files_and_offline_pages(legacy_golden, tmp_path):
    from scripts.compact_golden import compact_golden
    before = workflow.sha(legacy_golden/'golden.json')
    output = tmp_path/'compact'
    manifest, result = compact_golden(legacy_golden,output,baseline_id='compact')
    files = {p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()}
    assert files == {'images/box--pt2048.png','golden.json','thresholds.json','workflow-result.json',
                     'run-config.json','timings.csv','index.html','pages/box.html',
                     'assets/report.css','assets/report.js'}
    verified, thresholds = workflow.read_golden(output)
    assert verified['id']=='compact' and verified['manifest_fingerprint']==manifest['manifest_fingerprint']
    assert workflow.sha(output/result['records'][0]['image'])==result['records'][0]['image_sha256']
    assert result['records'][0]['timing']['total_ms']==6
    assert thresholds['profiles']['box--pt2048']['thresholds']=={'full':{'mae':.003}}
    assert thresholds['profiles']['box--pt2048']['provenance']['golden_id']=='compact'
    assert 'evidence' not in result['records'][0]
    assert workflow.sha(legacy_golden/'golden.json')==before


def test_compact_is_reproducible_at_different_destination_paths(legacy_golden,tmp_path):
    from scripts.compact_golden import compact_golden
    a,b=tmp_path/'one',tmp_path/'two'
    first,_=compact_golden(legacy_golden,a,baseline_id='compact')
    second,_=compact_golden(legacy_golden,b,baseline_id='compact')
    assert first['manifest_fingerprint']==second['manifest_fingerprint']
    assert workflow.artifacts(a)==workflow.artifacts(b)


def test_compact_refuses_to_overwrite_or_replace_the_source(legacy_golden):
    from scripts.compact_golden import compact_golden
    with pytest.raises(ValueError,match='source|destination|existing'):
        compact_golden(legacy_golden,legacy_golden)


def test_published_directory_contains_only_scene_images_and_portable_metadata(legacy_golden, tmp_path):
    from scripts import compact_golden
    from scripts.download_golden import ensure_golden
    assert hasattr(compact_golden, 'publish_reference_directory'), 'Scene-directory publication is missing'
    root = tmp_path / 'repo'
    root.mkdir()
    index = compact_golden.publish_reference_directory(legacy_golden, root)
    files = {p.relative_to(root / 'golden').as_posix() for p in (root / 'golden').rglob('*') if p.is_file()}
    assert files == {'box/pt.png'}
    assert index['path'] == 'golden' and 'distribution' not in index and 'derivation' not in index
    manifest, thresholds = workflow.read_golden(root / 'golden')
    row = manifest['records'][0]
    original = workflow.load(legacy_golden / 'golden.json')['records'][0]
    assert row['image'] == 'box/pt.png' and row['image_sha256'] == original['image_sha256']
    assert row['configuration'] == original['configuration'] and row['input_fingerprint'] == original['input_fingerprint']
    assert row['timing']['total_ms'] == 6
    assert thresholds['profiles']['box--pt2048']['thresholds'] == {'full': {'mae': .003}}
    assert not {'engine', 'configuration_file', 'process_environment'} & manifest['metadata'].keys()
    assert ensure_golden(root, offline=True) == root / 'golden'
    # A clone may use LF for JSON even when the publisher ran on Windows.
    for path in (root / 'baselines').glob('*.json'):
        path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n'))
    assert workflow.read_golden(root / 'golden')[0]['id'] == 'legacy'


def test_published_scene_images_are_hash_checked_before_rendering(legacy_golden, tmp_path):
    from scripts import compact_golden
    assert hasattr(compact_golden, 'publish_reference_directory'), 'Scene-directory publication is missing'
    root = tmp_path / 'repo'
    root.mkdir()
    compact_golden.publish_reference_directory(legacy_golden, root)
    (root / 'golden/box/pt.png').write_bytes(b'broken')
    with pytest.raises(ValueError, match='[Hh]ash|[Ii]ntegrity|[Aa]rtifact'):
        workflow.read_golden(root / 'golden')


def test_published_scene_images_can_hydrate_lfs_without_release(legacy_golden, tmp_path, monkeypatch):
    from scripts import compact_golden, download_golden
    assert hasattr(compact_golden, 'publish_reference_directory'), 'Scene-directory publication is missing'
    root = tmp_path / 'repo'
    root.mkdir()
    compact_golden.publish_reference_directory(legacy_golden, root)
    image = root / 'golden/box/pt.png'
    original = image.read_bytes()
    image.write_bytes(f'version https://git-lfs.github.com/spec/v1\noid sha256:{workflow.sha(legacy_golden / "runs/box--pt2048/repeat-0/image.png")}\nsize {len(original)}\n'.encode())
    with pytest.raises(RuntimeError, match='Git LFS'):
        download_golden.ensure_golden(root, offline=True)
    def hydrate(repo, index, *, offline):
        assert repo == root and index['path'] == 'golden' and not offline
        image.write_bytes(original)
    monkeypatch.setattr(download_golden, '_hydrate_lfs', hydrate)
    assert download_golden.ensure_golden(root) == root / 'golden'


def test_compact_can_bootstrap_offline_from_the_original_release(legacy_golden,tmp_path):
    from scripts.compact_golden import compact_golden
    from scripts.download_golden import ensure_golden
    expected,_=compact_golden(legacy_golden,tmp_path/'expected',baseline_id='compact')
    root=tmp_path/'clone'
    cache=root/'.workspace/cache/golden/legacy'
    cache.mkdir(parents=True)
    archive=cache/'legacy.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as output:
        for path in legacy_golden.rglob('*'):
            if path.is_file():output.write(path,path.relative_to(legacy_golden).as_posix())
    old=workflow.load(legacy_golden/'golden.json')
    old_index={'id':'legacy','path':'golden/legacy','manifest_fingerprint':old['manifest_fingerprint'],
               'created_utc':old['created_utc'],'distribution':{
                   'schema':1,'format':'split-zip','repository':'CoronaEngine/CoronaTests',
                   'tag':'golden-legacy','private':True,
                   'archive':{'name':'legacy.zip','bytes':archive.stat().st_size,'sha256':workflow.sha(archive)},
                   'assets':[{'name':'legacy.zip.001','bytes':archive.stat().st_size,'sha256':workflow.sha(archive),
                              'url':'https://github.com/CoronaEngine/CoronaTests/releases/download/golden-legacy/legacy.zip.001',
                              'api_url':'https://api.github.com/repos/CoronaEngine/CoronaTests/releases/assets/1'}]}}
    workflow.save(root/'baselines/legacy.json',old_index)
    index={'id':'compact','path':'golden/compact','manifest_fingerprint':expected['manifest_fingerprint'],
           'created_utc':expected['created_utc'],'derivation':{'schema':1,'format':'compact-v1',
               'source_id':'legacy','source_manifest_fingerprint':old['manifest_fingerprint']}}
    workflow.save(root/'baselines/compact.json',index)
    workflow.save(root/'baselines/default.json',{'schema':1,'id':'compact'})
    installed=ensure_golden(root,offline=True)
    assert installed==root/'golden/compact'
    assert workflow.read_golden(installed)[0]['manifest_fingerprint']==expected['manifest_fingerprint']
    assert not (root/'golden/legacy').exists()
    assert not (installed/'runs').exists()
