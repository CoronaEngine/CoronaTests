"""Low-SPP noise envelopes retain small regression allowances and strict guards."""
import numpy as np
from PIL import Image
import pytest

from scripts import compare


def api():
    from scripts import pt_policy
    return pt_policy


def images(tmp_path):
    a = np.full((64, 64, 3), 120, dtype=np.uint8)
    a[:, :32] = (40, 90, 130)
    noise = ((np.indices((64, 64)).sum(axis=0) % 2) * 2 - 1) * 6
    b = np.uint8(a.astype(int) + noise[:, :, None])
    reference, candidate = tmp_path / 'reference.png', tmp_path / 'candidate.png'
    Image.fromarray(a).save(reference); Image.fromarray(b).save(candidate)
    return reference, candidate


def test_area_means_are_exact_and_do_not_quantize():
    module = api()
    x = np.repeat(np.arange(64, dtype=np.float64).reshape(8, 8, 1), 3, axis=2)
    assert module.area_mean(x, 8)[0, 0, 0] == 31.5
    np.testing.assert_array_equal(module.area_mean(x, 4)[:, :, 0], [[13.5, 17.5], [45.5, 49.5]])
    with pytest.raises(ValueError): module.area_mean(np.zeros((9, 8, 3)), 4)


def test_measured_noise_passes_but_black_and_exposure_changes_fail(tmp_path):
    module = api(); reference, candidate = images(tmp_path)
    regions = {'center': [.25, .25, .75, .75]}
    measured = module.measure(reference, candidate, regions)
    case = module.case_profile(measured, {'image_sha256': 'a' * 64, 'input_fingerprint': 'b' * 64})
    assert compare.compare_images(reference, candidate)['mae'] > .003
    assert module.evaluate(measured, case, module.ALLOWANCES)['status'] == 'pass'
    for name, data in [('black', np.zeros((64, 64, 3))), ('exposure', np.full((64, 64, 3), 150))]:
        path = tmp_path / (name + '.png'); Image.fromarray(np.uint8(data)).save(path)
        assert module.evaluate(module.measure(reference, path, regions), case, module.ALLOWANCES)['status'] == 'image-failed'


@pytest.mark.parametrize('change', ['missing-scale', 'wrong-factor', 'missing-region', 'nonfinite', 'missing-mean'])
def test_missing_or_invalid_multiscale_evidence_cannot_pass(tmp_path, change):
    module = api(); reference, candidate = images(tmp_path)
    measured = module.measure(reference, candidate, {'center': [.25, .25, .75, .75]})
    case = module.case_profile(measured, {'image_sha256': 'a' * 64, 'input_fingerprint': 'b' * 64})
    if change == 'missing-scale': del measured['DS4']
    elif change == 'wrong-factor': measured['DS4']['factor'] = 8
    elif change == 'missing-region': measured['DS4']['regions'] = {}
    elif change == 'nonfinite': measured['DS8']['mae'] = float('nan')
    else: del measured['DS8']['candidate_mean_rgb']
    assert module.evaluate(measured, case, module.ALLOWANCES)['status'] == 'uncalibrated'


def test_wrong_reference_hash_is_rejected(tmp_path):
    module = api(); reference, candidate = images(tmp_path)
    row = {'job_id': 'box--pt2048', 'image_sha256': 'a' * 64, 'input_fingerprint': 'b' * 64}
    case = module.case_profile(module.measure(reference, candidate, {'center': [.25, .25, .75, .75]}), row)
    policy = {'schema': 1, 'id': 'pt512-ds8-ds4-v1', 'candidate_spp': 512, 'reference_spp': 2048,
              'allowances': module.ALLOWANCES, 'cases': {'box--pt2048': case}}
    module.validate_references(policy, [row])
    with pytest.raises(ValueError, match='reference'):
        module.validate_references(policy, [{**row, 'image_sha256': 'c' * 64}])


def test_brightness_guard_rejects_changes_that_reduce_reference_error(tmp_path):
    module = api(); regions = {'center': [.25, .25, .75, .75]}
    ref, low, changed = (tmp_path / name for name in ('reference.png', 'low.png', 'changed.png'))
    for path, value in ((ref, 100), (low, 80), (changed, 90)):
        Image.new('RGB', (64, 64), (value, value, value)).save(path)
    normal = module.measure(ref, low, regions)
    case = module.case_profile(normal, {'image_sha256': 'a' * 64, 'input_fingerprint': 'b' * 64})
    bad = module.measure(ref, changed, regions)
    assert bad['DS8']['mae'] < normal['DS8']['mae']
    verdict = module.evaluate(bad, case, module.ALLOWANCES)
    assert verdict['status'] == 'image-failed'
    assert any('mean' in failure or 'luminance' in failure for failure in verdict['failures'])


def test_published_policy_covers_all_matching_pt_references_and_default_budget():
    import json
    from pathlib import Path
    module = api()
    root = Path(__file__).resolve().parents[1]
    read = lambda path: json.loads(path.read_text(encoding='utf-8-sig'))
    baseline_id = read(root / 'baselines/default.json')['id']
    manifest = read(root / 'baselines' / (baseline_id + '.manifest.json'))
    references = [r for r in manifest['records'] if r['algorithm_id'] == 'pt2048' and r['status'] == 'complete']
    policy = read(root / 'recipes/pt512-regression.json')
    matching = [r for r in references if r['configuration']['frames'] == policy['reference_spp']]
    assert len(references) == 31 and set(policy['cases']) == {r['job_id'] for r in matching}
    module.validate_references(policy, matching)
    assert policy['allowances'] == module.ALLOWANCES
    assert read(root / 'config.regression.example.json')['sampling']['pt_spp'] == 512
    assert read(root / 'config.golden.example.json')['sampling']['pt_spp'] == 2048
