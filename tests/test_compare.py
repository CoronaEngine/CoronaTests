from pathlib import Path
import json
import numpy as np
import pytest
from PIL import Image

from scripts.compare import compare_images, calibrate, evaluate


def png(root, name, pixels):
    path = root / name
    Image.fromarray(np.asarray(pixels, dtype=np.uint8)).save(path)
    return path


def textured():
    rng = np.random.default_rng(42)
    return rng.integers(50, 190, (48, 64, 3), dtype=np.uint8)


def test_metrics_are_normalized_and_regions_detect_small_occlusion(tmp_path):
    a = np.full((20, 20, 3), 100, dtype=np.uint8)
    b = a.copy()
    b[:2, :2] = 0
    ref, cand = png(tmp_path, 'a.png', a), png(tmp_path, 'b.png', b)
    result = compare_images(ref, cand, tmp_path / 'comparison', regions={'corner': [0, 0, .1, .1]})
    assert result['mae'] == pytest.approx(1 / 255)
    assert result['rmse'] == pytest.approx(10 / 255)
    assert result['regions']['corner']['mae'] == pytest.approx(100 / 255)
    assert result['regions']['corner']['luminance_ratio'] == 0
    assert (tmp_path / 'comparison' / result['difference_image']).is_file()
    assert json.loads((tmp_path / 'comparison/comparison.json').read_text())['mae'] == result['mae']
    assert 'linear_rmse' not in result


def test_identical_black_image_has_finite_identity_metrics(tmp_path):
    path = png(tmp_path, 'black.png', np.zeros((8, 8, 3)))
    metrics = compare_images(path, path)
    assert metrics['mae'] == metrics['rmse'] == 0
    assert metrics['ssim_luminance'] == 1
    assert metrics['luminance_ratio'] == 1
    json.dumps(metrics, allow_nan=False)


def test_size_and_region_errors_are_rejected(tmp_path):
    a = png(tmp_path, 'a.png', np.zeros((8, 8, 3)))
    b = png(tmp_path, 'b.png', np.zeros((9, 8, 3)))
    with pytest.raises(ValueError, match='sizes'):
        compare_images(a, b)
    with pytest.raises(ValueError, match='region'):
        compare_images(a, a, regions={'invalid': [0, 0, 0, 1]})


def test_real_linear_pfm_is_optional_and_nonfinite_is_rejected(tmp_path):
    ref = png(tmp_path, 'a.png', np.full((8, 8, 3), 100))
    cand = png(tmp_path, 'b.png', np.full((8, 8, 3), 100))
    for path, value in [(ref, 1.), (cand, 2.)]:
        path.with_suffix('.pfm').write_bytes(b'PF\n8 8\n-1.0\n' + np.full((8, 8, 3), value, dtype='<f4').tobytes())
    metrics = compare_images(ref, cand)
    assert metrics['linear_rmse'] == 1
    assert metrics['linear_relative_rmse'] == 1
    assert metrics['linear_luminance_ratio'] == 2
    cand.with_suffix('.pfm').write_bytes(b'PF\n8 8\n-1.0\n' + np.full((8, 8, 3), np.nan, dtype='<f4').tobytes())
    metrics = compare_images(ref, cand)
    assert metrics['linear_nonfinite_values']['candidate'] == 192
    assert 'linear_rmse' not in metrics
    profile = calibrate(ref, [ref, ref])
    assert evaluate(metrics, profile)['status'] == 'image-failed'


def test_calibration_accepts_noise_rejects_degradation_and_missing_region(tmp_path):
    data = textured()
    ref = png(tmp_path, 'reference.png', data)
    repeats = [png(tmp_path, f'repeat{i}.png', np.clip(data.astype(int) + i, 0, 255)) for i in (1, 2)]
    profile = calibrate(ref, repeats, provenance={'scene_id': 'cornell-box', 'algorithm_id': 'gi'})
    assert profile['status'] == 'calibrated'
    assert len(profile['noise_records']) == 3
    assert len(profile['controlled_degradation_checks']) >= 3
    assert all(check['rejected'] for check in profile['controlled_degradation_checks'])
    assert evaluate(compare_images(ref, repeats[0]), profile)['status'] == 'pass'
    for name, corrupt in [('black', np.zeros_like(data)), ('exposure', data // 2)]:
        bad = png(tmp_path, f'{name}.png', corrupt)
        assert evaluate(compare_images(ref, bad), profile)['status'] == 'image-failed'
    metrics = compare_images(ref, ref)
    metrics['regions'] = {}
    assert evaluate(metrics, profile)['status'] == 'uncalibrated'
    json.dumps(profile, allow_nan=False)


def test_incomplete_or_excessive_noise_cannot_be_blessed(tmp_path):
    ref = png(tmp_path, 'a.png', textured())
    black = png(tmp_path, 'b.png', np.zeros((48, 64, 3)))
    assert calibrate(ref, [ref])['status'] == 'calibration-incomplete'
    profile = calibrate(ref, [black, black])
    assert profile['status'] == 'calibration-incomplete'
    assert evaluate(compare_images(ref, black), profile)['status'] == 'uncalibrated'
    dark = calibrate(black, [black, black])
    assert dark['status'] == 'calibration-incomplete'


def test_fixed_profile_uses_existing_limits_without_repeat_images(tmp_path):
    from scripts import compare
    assert hasattr(compare, 'fixed_profile'), 'Single-capture threshold profile is missing'
    regions = {'center': [.25, .25, .75, .75]}
    provenance = {'golden_id': 'once', 'job_id': 'box--gi', 'input_fingerprint': 'input'}
    profile = compare.fixed_profile(regions=regions, provenance=provenance)
    assert profile['status'] == 'fixed' and profile['policy']['method'] == 'fixed-thresholds'
    assert profile['thresholds'] == {'full': compare.FLOORS, 'regions': {'center': compare.FLOORS}}
    assert profile['region_bounds'] == regions and profile['provenance'] == provenance
    assert 'noise_records' not in profile
    reference = png(tmp_path, 'reference.png', textured())
    black = png(tmp_path, 'black.png', np.zeros_like(textured()))
    assert evaluate(compare_images(reference, reference, regions=regions), profile)['status'] == 'pass'
    assert evaluate(compare_images(reference, black, regions=regions), profile)['status'] == 'image-failed'
    incomplete = compare_images(reference, reference, regions=regions)
    incomplete['regions'] = {}
    assert evaluate(incomplete, profile)['status'] == 'uncalibrated'

def test_tampered_permissive_profile_cannot_accept_black(tmp_path):
    data = textured()
    ref = png(tmp_path, 'ref.png', data)
    profile = calibrate(ref, [ref, ref])
    assert profile['status'] == 'calibrated'
    profile['thresholds']['full']['mae'] = 1
    black = png(tmp_path, 'bad.png', np.zeros_like(data))
    assert evaluate(compare_images(ref, black), profile)['status'] == 'uncalibrated'


def test_region_gate_rejects_local_change_hidden_in_full_average(tmp_path):
    data = np.full((100, 100, 3), 100, dtype=np.uint8)
    ref = png(tmp_path, 'ref.png', data)
    regions = {'small-corner': [0, 0, .02, .02]}
    profile = calibrate(ref, [ref, ref], regions=regions)
    assert profile['status'] == 'calibrated'
    changed = data.copy()
    changed[:2, :2] = 0
    cand = png(tmp_path, 'candidate.png', changed)
    metrics = compare_images(ref, cand, regions=regions)
    assert metrics['mae'] < profile['thresholds']['full']['mae']
    evaluation = evaluate(metrics, profile)
    assert evaluation['status'] == 'image-failed'
    assert any('regions.small-corner' in failure for failure in evaluation['failures'])

@pytest.mark.parametrize('mutation', ['moved-bounds', 'renamed-region', 'missing-region', 'empty-threshold-regions', 'missing-threshold', 'missing-full-metric', 'missing-region-metric', 'nonfinite-metric'])
def test_region_contract_and_required_evidence_fail_closed(tmp_path, mutation):
    import copy
    data = np.full((200, 200, 3), 100, dtype=np.uint8)
    ref = png(tmp_path, 'reference.png', data)
    corner = {'corner': [0, 0, .01, .01]}
    profile = calibrate(ref, [ref, ref], regions=corner)
    damaged = data.copy()
    damaged[:2, :2] = 0
    candidate = png(tmp_path, 'candidate.png', damaged)
    metrics = compare_images(ref, candidate, regions=corner)
    assert evaluate(metrics, profile)['status'] == 'image-failed'
    profile = copy.deepcopy(profile)
    if mutation == 'moved-bounds':
        metrics = compare_images(ref, candidate, regions={'corner': [.99, .99, 1, 1]})
    elif mutation == 'renamed-region':
        metrics['regions']['other'] = metrics['regions'].pop('corner')
    elif mutation == 'missing-region':
        metrics['regions'] = {}
    elif mutation == 'empty-threshold-regions':
        profile['thresholds']['regions'] = {}
    elif mutation == 'missing-threshold':
        del profile['thresholds']['regions']['corner']['rmse']
    elif mutation == 'missing-full-metric':
        del metrics['mae']
    elif mutation == 'missing-region-metric':
        del metrics['regions']['corner']['luminance_ratio']
    elif mutation == 'nonfinite-metric':
        metrics['psnr_db'] = float('nan')
    result = evaluate(metrics, profile)
    assert result['status'] == 'uncalibrated'
    assert result['failures']


def test_empty_regions_use_fixed_default_checks(tmp_path):
    ref = png(tmp_path, 'reference.png', textured())
    profile = calibrate(ref, [ref, ref], regions={})
    metrics = compare_images(ref, ref, regions={})
    assert profile['status'] == 'calibrated'
    assert profile['region_bounds']
    assert profile['thresholds']['regions']
    assert evaluate(metrics, profile)['status'] == 'pass'
