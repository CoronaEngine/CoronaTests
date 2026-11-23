from copy import deepcopy

import pytest

from scripts import performance_golden as perf
from scripts import workflow


DEVICE = [{'name': 'RTX fixture', 'uuid': 'GPU-fixture', 'driver_version': '123', 'current_temperature': '58 C'}]
GI_RECIPE = {'id': 'gi', 'frames': 192, 'warmup': 64, 'denoise': True,
             'reblur_contexts': ['physical-DI', 'GI0', 'GI1', 'GI2']}
PASS_NAMES = [
    'ReSTIR GI shared PT initial candidates',
    'ReSTIR DI final shading',
    'ReSTIR GI final shading',
    'rt_geom',
    *(f'NRD {role} / NRD-EDSL-REBLUR-Pack' for role in GI_RECIPE['reblur_contexts']),
]
TIMING = {
    'frames': 192, 'warmup_frames': 64, 'retained_frames': 128, 'mean_ms': 10.0,
    'retained': {
        'API_render_interval': {'n': 128, 'mean_ms': 10.0},
        'GPU_timestamp_envelope': {'n': 128, 'mean_ms': 9.0},
        'GPU_dispatch_duration_sum': {'n': 128, 'mean_ms': 8.0},
        'per_pass_and_group': {name: {'n': 128, 'mean_ms': 1.0} for name in PASS_NAMES},
    },
}


def fixture():
    report = {
        'id': 'candidate-run',
        'metadata': {'hardware': deepcopy(DEVICE), 'engine_fingerprint': 'c' * 64},
        'records': [
            {'job_id': 'room--gi', 'scene_id': 'room', 'algorithm_id': 'gi', 'status': 'image-failed',
             'input_fingerprint': 'a' * 64, 'configuration': deepcopy(GI_RECIPE), 'timing': deepcopy(TIMING)},
            {'job_id': 'room--pt2048', 'algorithm_id': 'pt2048', 'status': 'pass'},
        ],
    }
    baseline = {
        'schema': 1, 'id': 'restir-reblur-performance-v1', 'created_utc': '2026-11-19T08:00:00+00:00',
        'source': {'run_id': 'capture-run', 'result_sha256': 'e' * 64, 'manifest_fingerprint': 'f' * 64},
        'engine_fingerprint': 'b' * 64, 'hardware': deepcopy(DEVICE),
        'records': {
            'room--gi': {'status': 'measured', 'input_fingerprint': 'a' * 64, 'configuration': deepcopy(GI_RECIPE),
                          'image_sha256': 'd' * 64, 'timing': deepcopy(TIMING), 'evidence_sha256': {'run.json': '1' * 64}},
        },
    }
    return report, baseline


def test_retired_backend_requires_a_verified_input_hash_and_identical_timing_recipe():
    report, bundle = fixture()
    baseline = bundle['records']['room--gi']
    baseline['configuration']['environment'] = {'VISION_RESTIR_GI_BACKEND': 'rtxpt', 'OTHER': 'same'}
    row = report['records'][0]
    row['configuration']['environment'] = {'OTHER': 'same'}
    row['input_fingerprint'] = '9' * 64
    assert not perf.matching_inputs(row, baseline)
    row['comparison_input_fingerprint'] = baseline['input_fingerprint']
    assert perf.matching_inputs(row, baseline)
    row['configuration']['warmup'] -= 1
    assert not perf.matching_inputs(row, baseline)


def test_comparison_accepts_changed_engine_and_temperature(tmp_path):
    report, baseline = fixture()
    report['metadata']['hardware'][0]['current_temperature'] = '75 C'
    perf.annotate_report(report, baseline)
    value = report['records'][0]['restir_golden_comparison']
    assert value['status'] == 'compared'
    assert value['baseline_id'] == baseline['id']
    assert value['golden_timing'] == TIMING
    assert 'restir_golden_comparison' not in report['records'][1]


@pytest.mark.parametrize('conditions, expected', [
    ('gpu', 'incomparable'), ('driver', 'incomparable'), ('input', 'incomparable'),
    ('warmup', 'incomparable'), ('no_timing', 'missing-candidate'),
    ('no_baseline', 'missing-golden'),
])
def test_comparison_refuses_invalid_delta(conditions, expected):
    report, baseline = fixture()
    row = report['records'][0]
    if conditions in ('gpu', 'driver'):
        report['metadata']['hardware'][0]['uuid' if conditions == 'gpu' else 'driver_version'] = 'changed'
    elif conditions == 'input':
        row['input_fingerprint'] = '9' * 64
    elif conditions == 'warmup':
        row['configuration']['warmup'] = 32
    elif conditions == 'no_timing':
        row.pop('timing')
    else:
        baseline['records'].clear()
    perf.annotate_report(report, baseline)
    assert row['restir_golden_comparison']['status'] == expected


def test_default_index_verifies_bundle_fingerprint(tmp_path):
    _, baseline = fixture()
    baseline = workflow.seal(baseline)
    directory = tmp_path / 'baselines'
    file = directory / (baseline['id'] + '.json')
    workflow.save(file, baseline)
    index = directory / 'restir-reblur-performance.default.json'
    workflow.save(index, {'schema': 1, 'id': baseline['id'], 'manifest_fingerprint': baseline['manifest_fingerprint']})
    assert perf.load_default(tmp_path) == baseline
    baseline['records']['room--gi']['timing']['mean_ms'] = 12.0
    workflow.save(file, baseline)
    with pytest.raises(ValueError, match='fingerprint'):
        perf.load_default(tmp_path)


def test_same_visible_gpu_list_does_not_establish_active_adapter():
    report, baseline = fixture()
    hardware = DEVICE + [{'name': 'Other GPU', 'uuid': 'GPU-other', 'driver_version': '123'}]
    report['metadata']['hardware'] = hardware
    baseline['hardware'] = hardware
    perf.annotate_report(report, baseline)
    assert report['records'][0]['restir_golden_comparison']['status'] == 'incomparable'


def test_index_filename_is_reserved_for_the_pointer(tmp_path):
    with pytest.raises(ValueError, match='reserved'):
        perf.publish(tmp_path / 'capture', root=tmp_path, baseline_id='restir-reblur-performance.default')


def test_source_report_is_labeled_as_baseline_without_a_self_comparison():
    report, baseline = fixture()
    with pytest.raises(ValueError, match='measured source'):
        perf.annotate_report(report, baseline, is_baseline=True)
    report['id'] = baseline['source']['run_id']
    perf.annotate_report(report, baseline, is_baseline=True)
    assert report['records'][0]['restir_golden_comparison']['status'] == 'baseline'


def test_partial_batch_cannot_publish_a_performance_baseline(tmp_path, monkeypatch):
    run = tmp_path / 'capture'
    job = 'room--gi'
    formal = {'manifest_fingerprint': 'f' * 64, 'records': [
        {'job_id': job, 'algorithm_id': 'gi'},
        {'job_id': 'other--gi', 'algorithm_id': 'gi'},
    ]}
    monkeypatch.setattr(workflow, 'read_golden', lambda path: (formal, {}))
    workflow.save(run / 'workflow-result.json', {
        'kind': 'regression', 'suite': 'full', 'records': [{'job_id': job}],
    })
    workflow.save(run / 'run-config.json', {'contract': {
        'kind': 'regression', 'suite': 'full', 'algorithms': ['gi'],
        'jobs': {job: 'a' * 64}, 'golden_fingerprint': formal['manifest_fingerprint'],
    }})
    with pytest.raises(ValueError, match='complete GI-only batch'):
        perf.publish(run, root=tmp_path)
    assert not (tmp_path / 'baselines').exists()
