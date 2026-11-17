"""Build a portable reference-only golden without per-capture diagnostic files."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import shutil
import tempfile
import uuid

from scripts import workflow
from scripts.report import write_report

ROW_FIELDS = (
    'schema', 'scene_id', 'algorithm_id', 'job_id', 'status', 'label', 'scene_name',
    'configuration', 'regions', 'input_fingerprint', 'engine_fingerprint', 'image_sha256', 'timing',
    'elapsed_seconds', 'reason', 'support_evidence', 'owned_engine_closed', 'exit_code',
    'source_camera_numeric', 'source_emitter_types', 'camera_before', 'camera_after',
    'effective_before', 'effective_after', 'effective_reblur_before', 'effective_reblur_after',
    'environment_before', 'environment_after', 'actual_context_lifecycle',
)


def write_json(path, value):
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf8'))


def publish_reference_directory(source: Path, root: Path):
    """Publish one original PNG per supported job; keep metadata in baselines/."""
    source, root = Path(source).resolve(), Path(root).resolve()
    destination = root / 'golden'
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Preserving existing golden directory; publish into an empty staging root')
    manifest, original_thresholds = workflow.read_golden(source)
    baseline_id = workflow.identifier(manifest['id'])
    destination.mkdir(parents=True, exist_ok=True)
    baselines = root / 'baselines'
    baselines.mkdir(parents=True, exist_ok=True)
    records, images, profiles = [], {}, {}
    fields = ('scene_id', 'algorithm_id', 'job_id', 'status', 'label', 'scene_name',
              'configuration', 'regions', 'input_fingerprint', 'engine_fingerprint', 'image_sha256',
              'elapsed_seconds', 'reason')
    filenames = {'gi': 'gi.png', 'pt-reblur': 'pt-reblur.png', 'pt2048': 'pt.png'}
    for original in manifest['records']:
        row = {key: copy.deepcopy(original[key]) for key in fields if key in original}
        if original['status'] == 'complete':
            relative = workflow.identifier(original['scene_id']) + '/' + filenames[original['algorithm_id']]
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(workflow.inside(source, original['image']), target)
            if workflow.sha(target) != original['image_sha256']:
                raise ValueError('Reference image hash differs: ' + original['job_id'])
            row['image'] = relative
            row['timing'] = {key: copy.deepcopy(value) for key, value in original.get('timing', {}).items()
                             if key in ('frames', 'warmup_frames', 'retained_frames', 'mean_ms', 'median_ms',
                                        'sample_ms', 'shown_frame_ms', 'total_ms', 'labels')}
            images[relative] = original['image_sha256']
            profile = original_thresholds['profiles'][original['job_id']]
            profiles[original['job_id']] = {key: copy.deepcopy(profile[key]) for key in
                ('status', 'thresholds', 'region_bounds', 'provenance', 'policy', 'reasons') if key in profile}
        records.append(row)
    thresholds = {'schema': 1, 'golden_id': baseline_id, 'profiles': profiles}
    metadata = {key: copy.deepcopy(manifest['metadata'][key]) for key in
                ('engine_fingerprint', 'hardware', 'sampling', 'timing_policy') if key in manifest['metadata']}
    published = workflow.seal({'schema': 2, 'id': baseline_id, 'suite': manifest['suite'],
        'created_utc': manifest['created_utc'], 'valid': True, 'calibrated': manifest.get('calibrated', False),
        'thresholds_ready': True, 'threshold_policy': manifest.get('threshold_policy', 'empirical'),
        'source_manifest_fingerprint': manifest['manifest_fingerprint'],
        'metadata': metadata, 'coverage': copy.deepcopy(manifest['coverage']), 'records': records,
        'artifacts': images, 'thresholds_fingerprint': workflow.fingerprint(thresholds)})
    index = {'id': baseline_id, 'path': 'golden', 'manifest': 'baselines/' + baseline_id + '.manifest.json',
             'manifest_fingerprint': published['manifest_fingerprint'], 'created_utc': published['created_utc']}
    write_json(root / index['manifest'], published)
    write_json(baselines / (baseline_id + '.thresholds.json'), thresholds)
    write_json(baselines / (baseline_id + '.json'), index)
    write_json(baselines / 'default.json', {'schema': 1, 'id': baseline_id})
    workflow.read_golden(destination)
    return index


def compact_golden(source: Path, destination: Path, *, baseline_id: str | None = None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ValueError('Compact source and destination must be separate directories')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Preserving existing compact destination')
    manifest, thresholds = workflow.read_golden(source)
    target_id = workflow.identifier(baseline_id or manifest['id'])
    thresholds = copy.deepcopy(thresholds)
    thresholds['golden_id'] = target_id
    for profile in thresholds['profiles'].values():
        profile['provenance']['golden_id'] = target_id
    destination.mkdir(parents=True, exist_ok=True)
    records = []
    for original in manifest['records']:
        row = {key: copy.deepcopy(original[key]) for key in ROW_FIELDS if key in original}
        if original['status'] == 'complete':
            image = workflow.inside(source, original['image'])
            relative = 'images/' + workflow.identifier(original['job_id']) + '.png'
            target = destination / relative
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(image, target)
            row['image'] = relative
            if image.with_suffix('.pfm').is_file():
                shutil.copy2(image.with_suffix('.pfm'), target.with_suffix('.pfm'))
            profile = thresholds['profiles'][original['job_id']]
            field = 'threshold_profile' if profile['status'] == 'fixed' else 'calibration'
            row[field] = {key: copy.deepcopy(profile[key]) for key in ('status', 'reasons', 'provenance') if key in profile}
        records.append(row)
    result = {'kind':'golden', 'id':target_id, 'suite':manifest['suite'],
              'created_utc':manifest['created_utc'], 'output':'.',
              'valid':True, 'calibrated':manifest.get('calibrated', False),
              'thresholds_ready':True, 'threshold_policy':manifest.get('threshold_policy', 'empirical'),
              'metadata':copy.deepcopy(manifest['metadata']), 'records':records,
              'summary':dict(workflow.Counter(row['status'] for row in records))}
    result['metadata']['retention'] = {'format':'compact-v1','source_id':manifest['id'],
        'source_manifest_fingerprint':manifest['manifest_fingerprint']}
    recovered = []
    for folder in sorted((source/'runs').glob('*/repeat-*')):
        category = next((name for name in ('failed','interrupted') if '.'+name+'-' in folder.name), None)
        if category is None:
            continue
        record_path = folder/'attempt.json' if (folder/'attempt.json').is_file() else folder/'run.json'
        record = workflow.load(record_path) if record_path.is_file() else {}
        recovered.append({'job_id':folder.parent.name,'category':category,
            **{key:record[key] for key in ('error','failure_phase','error_type','exit_code') if key in record},
            'logs':{path.name:path.read_text(encoding='utf8',errors='replace')[-4000:]
                    for path in sorted(folder.glob('*.log'))}})
    if recovered:
        result['metadata']['recovered_attempts'] = recovered
    contract = workflow.load(source/'run-config.json') if (source/'run-config.json').is_file() else {'contract':None}
    contract['id'] = target_id
    write_json(destination/'run-config.json', contract)
    write_json(destination/'thresholds.json', thresholds)
    write_report(destination,result,metadata_file='workflow-result.json')
    write_json(destination/'workflow-result.json', result)
    compact = workflow.seal({'schema':1,'id':target_id,'suite':manifest['suite'],
        'created_utc':manifest['created_utc'],'valid':True,'calibrated':result['calibrated'],
        'thresholds_ready':True,'threshold_policy':result['threshold_policy'],
        'metadata':result['metadata'],'coverage':copy.deepcopy(manifest['coverage']),
        'records':records,'artifacts':workflow.artifacts(destination),
        'thresholds_sha256':workflow.sha(destination/'thresholds.json')})
    write_json(destination/'golden.json', compact)
    workflow.read_golden(destination)
    return compact, result


def replace_with_compact(output: Path):
    """Swap only after the complete compact copy has passed manifest checks."""
    output = Path(output).resolve()
    stage = Path(tempfile.mkdtemp(prefix='compact-stage-',dir=output.parent))
    backup = output.with_name(output.name+'.raw-'+uuid.uuid4().hex)
    if stage.resolve().parent != output.parent or backup.resolve().parent != output.parent:
        raise ValueError('Compact staging path escapes output parent')
    try:
        manifest, result = compact_golden(output,stage)
        output.rename(backup)
        try:
            stage.rename(output)
        except BaseException:
            backup.rename(output)
            raise
    except BaseException:
        if stage.is_dir():
            shutil.rmtree(stage)
        raise
    return manifest, {**result,'output':str(output)}, backup


def remove_raw_backup(backup: Path, output: Path):
    backup, output = Path(backup), Path(output).resolve()
    if (backup.is_symlink() or backup.resolve().parent != output.parent
            or not backup.name.startswith(output.name+'.raw-')):
        raise ValueError('Refusing unsafe raw capture cleanup')
    shutil.rmtree(backup)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('destination',type=Path)
    parser.add_argument('--baseline-id')
    options=parser.parse_args(argv)
    manifest,_=compact_golden(options.source,options.destination,baseline_id=options.baseline_id)
    print(json.dumps({'id':manifest['id'],'files':len(manifest['artifacts'])+1,
                      'output':str(options.destination.resolve())}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
