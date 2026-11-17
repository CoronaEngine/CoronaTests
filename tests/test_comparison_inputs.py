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


def test_different_budget_can_match_the_existing_golden_input_contract():
    baseline, candidate = pair()
    assert workflow.fingerprint(candidate) != baseline['input_fingerprint']
    assert workflow.comparison_fingerprint(candidate, baseline) == baseline['input_fingerprint']


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
