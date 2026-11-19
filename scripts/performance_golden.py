"""Build and compare an independently versioned ReSTIR + REBLUR timing Golden."""

import argparse
import math
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import capture, workflow
from scripts.restir_timing import breakdown


DEFAULT_ID = 'restir-reblur-performance-v1'
DEFAULT_INDEX = 'restir-reblur-performance.default.json'
CONTEXTS = ('physical-DI', 'GI0', 'GI1', 'GI2')
FRAME_FIELDS = ('frames', 'warmup_frames', 'retained_frames')
MEAN_FIELDS = ('API_render_interval', 'GPU_timestamp_envelope', 'GPU_dispatch_duration_sum')
TIMING_EVIDENCE = (
    'frames.csv', 'stages.csv', 'gpu.csv', 'gpu-diagnostics.csv',
    'gpu-frame-map.json', 'timing.json', 'setup/frames.csv', 'setup/stages.csv',
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def device_identity(observations):
    """Identify the GPU when the capture records one visible device."""
    if not isinstance(observations, list) or len(observations) != 1:
        return None
    keys = ('name', 'uuid', 'driver_version')
    item = observations[0]
    if not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key] for key in keys):
        return None
    return tuple(item[key] for key in keys)


def valid_timing(timing):
    """Require all measured groups, four REBLUR contexts and matching sample counts."""
    if not isinstance(timing, dict):
        return False
    try:
        frames, warmup, retained_frames = (timing[field] for field in FRAME_FIELDS)
        if (any(type(n) is not int for n in (frames, warmup, retained_frames))
                or not 0 <= warmup < frames or retained_frames != frames - warmup):
            return False
        retained = timing['retained']
        for field in MEAN_FIELDS:
            value = retained[field]['mean_ms']
            if (type(value) not in (float, int) or not math.isfinite(value) or value <= 0
                    or retained[field]['n'] != retained_frames):
                return False
        if timing['mean_ms'] != retained['API_render_interval']['mean_ms']:
            return False
        if any(stats['n'] != retained_frames for stats in retained['per_pass_and_group'].values()):
            return False
        groups = breakdown(timing)
        if not groups or any(not groups[key] for key in ('pt', 'di', 'gi', 'reblur', 'shared')):
            return False
        return all(any(name.startswith(f'NRD {role} / ') for name in groups['reblur']) for role in CONTEXTS)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def compact_timing(timing):
    retained = timing['retained']
    return {
        **{field: timing[field] for field in (*FRAME_FIELDS, 'mean_ms')},
        'retained': {
            **{field: {key: retained[field][key] for key in ('n', 'mean_ms')} for field in MEAN_FIELDS},
            'per_pass_and_group': {
                name: {key: stats[key] for key in ('n', 'mean_ms')}
                for name, stats in retained['per_pass_and_group'].items()
            },
        },
    }


def annotate_report(report, bundle, *, is_baseline=False):
    """Attach presentation data, leaving image comparisons and verdicts intact."""
    metadata = report.setdefault('metadata', {})
    metadata['performance_golden'] = {
        'id': bundle['id'],
        'manifest_fingerprint': bundle.get('manifest_fingerprint'),
        'source_run_id': bundle['source']['run_id'],
        'engine_fingerprint': bundle['engine_fingerprint'],
    }
    for row in report['records']:
        if row.get('algorithm_id') != 'gi':
            continue
        baseline = bundle['records'].get(row['job_id'])
        if baseline and baseline['status'] == 'expected-unsupported':
            if row.get('status') == 'expected-unsupported':
                continue
            baseline = None
        value = {'baseline_id': bundle['id']}
        if not baseline:
            value.update(status='missing-golden', reason='此场景没有已验证的性能 Golden。')
        else:
            value['golden_timing'] = baseline['timing']
            current = row.get('timing')
            hardware = device_identity(row.get('thermal') or metadata.get('hardware'))
            if not valid_timing(current):
                value.update(status='missing-candidate', reason='本次采集缺少完整的逐 pass 计时。')
            elif (row.get('input_fingerprint') != baseline['input_fingerprint']
                  or row.get('configuration') != baseline['configuration']
                  or any(current[field] != baseline['timing'][field] for field in FRAME_FIELDS)):
                value.update(status='incomparable', reason='输入、采样或预热设置与性能 Golden 不同。')
            elif hardware is None or hardware != device_identity(bundle['hardware']):
                value.update(status='incomparable', reason='GPU 或驱动与性能 Golden 不同，或无法确认本次所用 GPU。')
            elif is_baseline:
                require(report.get('id') == bundle['source']['run_id']
                        and compact_timing(current) == baseline['timing'],
                        'Baseline presentation does not match its measured source')
                value.update(status='baseline', reason='本次采集建立了此版本的性能 Golden。')
            else:
                value['status'] = 'compared'
        row['restir_golden_comparison'] = value


def load_default(root):
    directory = Path(root) / 'baselines'
    index_path = directory / DEFAULT_INDEX
    if not index_path.is_file():
        return None
    index = workflow.load(index_path)
    require(index.get('schema') == 1, 'Unsupported performance Golden index schema')
    baseline_id = workflow.identifier(index['id'])
    bundle = workflow.load(directory / (baseline_id + '.json'))
    digest = bundle.get('manifest_fingerprint')
    unsigned = {key: value for key, value in bundle.items() if key != 'manifest_fingerprint'}
    require(bundle.get('schema') == 1 and bundle.get('id') == baseline_id
            and digest == index.get('manifest_fingerprint') == workflow.fingerprint(unsigned),
            'Performance Golden manifest fingerprint differs')
    require(device_identity(bundle.get('hardware')) is not None, 'Performance Golden GPU or driver record missing')
    require(bool(bundle['records']), 'Performance Golden contains no jobs')
    for job, row in bundle['records'].items():
        require(row.get('status') == 'expected-unsupported'
                or (row.get('status') == 'measured' and valid_timing(row.get('timing'))),
                f'Unusable performance Golden timing: {job}')
    return bundle


def runtime_identity(raw):
    """Use the same absolute-path key space as workflow.FrozenInputs."""
    engine = Path(raw['engine'])
    dirs = [engine.parent, *(Path(path) for path in raw['runtime_dirs'])]
    hashes = raw['runtime_sha256']
    require(raw.get('runtime_sha256_after') == hashes, 'Capture runtime changed')
    require(raw.get('source_scene_sha256') and raw.get('source_scene_sha256_after') == raw['source_scene_sha256'],
            'Capture scene source changed')
    identity = {engine: hashes['executable']}
    for key, digest in hashes.items():
        if key == 'executable':
            continue
        prefix, filename = key.split('/', 1)
        require(prefix.startswith('dir') and prefix[3:].isdigit()
                and int(prefix[3:]) < len(dirs) and Path(filename).name == filename,
                'Invalid capture runtime evidence key')
        identity[dirs[int(prefix[3:])] / filename] = digest
    return identity


def read_attempt(output, row, contract, runtime, hardware):
    job = row['job_id']
    folder = workflow.inside(output, f'runs/{job}/repeat-0')
    attempt = workflow.load(folder / 'attempt.json')
    expected = workflow.fingerprint({
        'input': contract['jobs'][job], 'engine': contract['engine_fingerprint'], 'tools': contract['tools'],
        'runtime_dirs': contract['runtime_dirs'], 'launch_environment': contract['launch_environment'],
    })
    require(attempt['_contract'] == expected and row['input_fingerprint'] == contract['jobs'][job],
            f'{job}: input or attempt contract differs')
    digest = workflow.sha(folder / 'run.json')
    require(attempt['_artifacts'].get('run.json') == digest, f'{job}: source run hash differs')
    raw = workflow.load(folder / 'run.json')
    require(row['scene_id'] == raw.get('scene_id') and raw.get('algorithm_id') == 'gi'
            and raw.get('configuration') == row.get('configuration')
            and raw.get('owned_engine_closed') and raw.get('exit_code') == 0
            and not raw.get('forced_shutdown'), f'{job}: capture identity or completion differs')
    require(runtime_identity(raw) == runtime and device_identity(raw.get('thermal')) == hardware,
            f'{job}: runtime or GPU/driver differs')
    return folder, raw, attempt['_artifacts'], {'run.json': digest}


def measured_job(output, row, formal, folder, raw, artifacts, evidence):
    job = row['job_id']
    config, timing = row['configuration'], row.get('timing')
    require(row['status'] in ('pass', 'image-failed') and raw.get('status') == 'complete'
            and row.get('comparison_input_fingerprint') == formal['input_fingerprint'],
            f'{job}: no complete capture for the formal scene/configuration')
    require((config['id'], config['frames'], config['warmup'], config['denoise']) == ('gi', 192, 64, True)
            and tuple(config['reblur_contexts']) == CONTEXTS
            and valid_timing(timing) and [timing[field] for field in FRAME_FIELDS] == [192, 64, 128],
            f'{job}: expected 192 GI frames, 64 warmup and four REBLUR contexts')
    require(workflow.sha(workflow.inside(output, row['baseline_image'])) == formal['image_sha256'],
            f'{job}: captured report used a different image Golden')
    image = workflow.inside(folder, raw['image'])
    require(workflow.inside(output, row['image']) == image
            and workflow.sha(image) == row['image_sha256'] == raw['image_sha256'] == artifacts[raw['image']],
            f'{job}: captured PNG provenance differs')
    for name in TIMING_EVIDENCE:
        expected = artifacts[name]
        require(workflow.sha(folder / name) == expected
                and raw.get('evidence_sha256', {}).get(name, expected) == expected,
                f'{job}: original timing evidence hash differs: {name}')
        evidence[name] = expected
    require(workflow.load(folder / 'timing.json') == raw.get('timing') == timing,
            f'{job}: saved timing summary differs')
    mapping = workflow.load(folder / 'gpu-frame-map.json')
    require({name: evidence[name] for name in TIMING_EVIDENCE if name.endswith('.csv')} == mapping['csv_sha256'],
            f'{job}: GPU frame map does not match source CSVs')
    require(capture.summarize_timing(folder, mapping, config) == timing,
            f'{job}: measured frame/dispatch means do not match the report')
    return {
        'status': 'measured',
        'input_fingerprint': row['input_fingerprint'],
        'configuration': config,
        'image_sha256': row['image_sha256'],
        'timing': compact_timing(timing),
        'evidence_sha256': evidence,
    }


def build(output, *, root=workflow.ROOT, baseline_id=DEFAULT_ID):
    """Read and verify a complete GI-only run without writing any files."""
    baseline_id = workflow.identifier(baseline_id)
    require((baseline_id + '.json').casefold() != DEFAULT_INDEX, 'Performance Golden ID is reserved for the index')
    output = Path(output).resolve()
    formal, _ = workflow.read_golden(Path(root) / 'golden')
    references = {row['job_id']: row for row in formal['records'] if row['algorithm_id'] == 'gi'}
    result_path, config_path = output / 'workflow-result.json', output / 'run-config.json'
    result, run_config = workflow.load(result_path), workflow.load(config_path)
    contract = run_config['contract']
    rows = {row['job_id']: row for row in result['records']}
    require(references and result['kind'] == contract['kind'] == 'regression'
            and result['suite'] == contract['suite'] == 'full'
            and len(rows) == len(result['records']) == len(references)
            and set(rows) == set(references) == set(contract['jobs'])
            and contract['algorithms'] == ['gi']
            and contract['golden_fingerprint'] == formal['manifest_fingerprint'],
            'Performance Golden needs a complete GI-only batch for the current formal image Golden')
    hardware = result['metadata']['hardware']
    identity = device_identity(hardware)
    runtime = {Path(name): digest for name, digest in run_config['runtime_sha256'].items()}
    engine = result['metadata']['engine_fingerprint']
    require(identity is not None and runtime
            and engine == contract['engine_fingerprint'] == workflow.fingerprint(run_config['runtime_sha256']),
            'Performance Golden source must have one verified engine and GPU/driver record')
    records = {}
    for job, ref in references.items():
        row = rows[job]
        require(row.get('algorithm_id') == 'gi' and row.get('scene_id') == ref['scene_id'],
                f'{job}: scene or algorithm differs from formal coverage')
        folder, raw, artifacts, evidence = read_attempt(output, row, contract, runtime, identity)
        if ref['status'] == 'expected-unsupported':
            require(row['status'] == raw['status'] == 'expected-unsupported'
                    and raw.get('support_evidence', {}).get('kind') == 'unsupported-hair',
                    f'{job}: missing observed unsupported capture evidence')
            records[job] = {'status': 'expected-unsupported', 'reason': raw['reason'],
                            'evidence_sha256': evidence}
        else:
            records[job] = measured_job(output, row, ref, folder, raw, artifacts, evidence)
    return workflow.seal({
        'schema': 1, 'id': baseline_id, 'created_utc': result['created_utc'],
        'source': {'run_id': result['id'], 'result_sha256': workflow.sha(result_path),
                   'run_config_sha256': workflow.sha(config_path),
                   'manifest_fingerprint': formal['manifest_fingerprint']},
        'engine_fingerprint': engine, 'hardware': workflow.hardware_identity(hardware),
        'records': records,
    })


def publish(output, *, root=workflow.ROOT, baseline_id=DEFAULT_ID):
    bundle = build(output, root=root, baseline_id=baseline_id)
    directory = Path(root) / 'baselines'
    path = directory / (bundle['id'] + '.json')
    if path.exists():
        require(workflow.load(path) == bundle, 'Performance Golden ID already exists with different evidence')
    else:
        workflow.save(path, bundle)
    workflow.save(directory / DEFAULT_INDEX, {
        'schema': 1, 'id': bundle['id'], 'manifest_fingerprint': bundle['manifest_fingerprint'],
    })
    require(load_default(root) == bundle, 'Published performance Golden failed verification')
    return path, bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path, help='Complete GI-only capture output')
    parser.add_argument('--id', default=DEFAULT_ID, help='Versioned performance Golden identifier')
    parser.add_argument('--verify-only', action='store_true', help='Validate and summarize without publishing')
    args = parser.parse_args()
    if args.verify_only:
        bundle = build(args.run_dir, baseline_id=args.id)
        print(bundle['manifest_fingerprint'])
    else:
        path, bundle = publish(args.run_dir, baseline_id=args.id)
        print(path)
    measured = sum(row['status'] == 'measured' for row in bundle['records'].values())
    print(f'{bundle["id"]}: {measured} measured, {len(bundle["records"]) - measured} unsupported')


if __name__ == '__main__':
    main()
