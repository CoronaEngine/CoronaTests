"""Budget flexibility must not hide changes to render inputs or algorithms."""
import copy

import pytest

from scripts import workflow


def pair():
    reference = {'scene_id':'box','asset_fingerprint':'asset-A','camera':{'fov':55},
        'regions':{'center':[.25,.25,.75,.75]},'expected_diagnostics':[], 'expected_unsupported':{},
        'recipe':{'id':'pt2048','label':'PT 2048 samples','frames':2048,'warmup':64,
                  'spp_per_frame':1,'PT_settings':{'max_depth':16},'denoise':False,'accumulation':True}}
    baseline = {'input_fingerprint':workflow.fingerprint(reference),'configuration':copy.deepcopy(reference['recipe'])}
    candidate = copy.deepcopy(reference)
    candidate['recipe'].update(frames=512,warmup=16,label='PT 512 samples')
    return baseline, candidate


@pytest.mark.parametrize(('mode', 'historical'), [('gi', 'rtxpt'), ('pt2048', 'legacy'), ('pt-reblur', 'legacy')])
def test_retired_backend_matches_only_the_historical_default(mode, historical):
    key = 'VISION_RESTIR_GI_BACKEND'
    _, candidate = pair()
    candidate['recipe'].update(id=mode, environment={'OTHER': 'same'})
    reference = copy.deepcopy(candidate)
    reference['recipe']['environment'][key] = historical
    baseline = {'input_fingerprint': workflow.fingerprint(reference), 'configuration': reference['recipe']}
    assert workflow.comparison_fingerprint(candidate, baseline) == baseline['input_fingerprint']
    assert key not in candidate['recipe']['environment']
    candidate['recipe']['environment']['OTHER'] = 'different'
    assert workflow.comparison_fingerprint(candidate, baseline) != baseline['input_fingerprint']
    candidate['recipe']['environment']['OTHER'] = 'same'
    reference['recipe']['environment'][key] = 'unexpected'
    baseline['input_fingerprint'] = workflow.fingerprint(reference)
    assert workflow.comparison_fingerprint(candidate, baseline) != baseline['input_fingerprint']


def test_different_budget_can_match_the_existing_golden_input_contract():
    baseline, candidate = pair()
    assert workflow.fingerprint(candidate) != baseline['input_fingerprint']
    assert workflow.comparison_fingerprint(candidate, baseline) == baseline['input_fingerprint']


@pytest.mark.parametrize(('key', 'changed'), [
    ('VISION_RESTIR_DI_MODE', 'basic'), ('VISION_RESTIR_GI_LIGHT_FEEDBACK', '0'),
])
def test_renamed_restir_settings_match_frozen_gi_recipe_without_masking_a_change(key, changed):
    candidate = workflow.load(workflow.ROOT / 'recipes/algorithms.json')['algorithms']['gi']
    baseline = workflow.load(workflow.ROOT / 'baselines/restir-reblur-performance-v1.json')
    historical = next(row['configuration'] for row in baseline['records'].values() if row['status'] == 'measured')
    assert workflow.recipe_for_golden(candidate, historical) == historical
    candidate['environment'][key] = changed
    assert workflow.recipe_for_golden(candidate, historical) != historical


@pytest.mark.parametrize('field,value', [
    ('asset_fingerprint','asset-B'), ('camera',{'fov':40}),
    ('regions',{'corner':[0,0,.2,.2]}), ('expected_diagnostics',['NEW_DEGRADATION']),
])
def test_budget_normalization_preserves_scene_and_camera_checks(field, value):
    baseline, candidate = pair()
    candidate[field] = value
    assert workflow.comparison_fingerprint(candidate, baseline) != baseline['input_fingerprint']


@pytest.mark.parametrize('field,value', [
    ('PT_settings',{'max_depth':8}), ('denoise',True),
    ('accumulation',False), ('spp_per_frame',2),
])
def test_budget_normalization_preserves_algorithm_checks(field, value):
    baseline, candidate = pair()
    candidate['recipe'][field] = value
    assert workflow.comparison_fingerprint(candidate, baseline) != baseline['input_fingerprint']


def test_missing_reference_configuration_cannot_authorize_different_budgets():
    baseline, candidate = pair()
    baseline.pop('configuration')
    assert workflow.comparison_fingerprint(candidate, baseline) != baseline['input_fingerprint']


@pytest.mark.parametrize('authored_media', [False, True])
def test_default_pt_media_can_use_a_legacy_golden_only_for_a_vacuum_scene(tmp_path, authored_media):
    source = tmp_path / 'scene.xml'
    source.write_text('<scene><include filename="part.xml"/></scene>', encoding='utf-8')
    (tmp_path / 'part.xml').write_text(
        '<scene><medium type="homogeneous"/></scene>' if authored_media else '<scene/>', encoding='utf-8')
    baseline, candidate = pair()
    candidate['recipe']['PT_settings'].update(participating_media=True, medium_diagnostics=False)
    actual = workflow.comparison_fingerprint(candidate, baseline, scene_source=source)
    assert (actual == baseline['input_fingerprint']) is not authored_media
