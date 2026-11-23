"""CPU contracts for media readback; importing these tests never starts a GPU."""
import copy
import json
from pathlib import Path

import pytest

from scripts import capture

ROOT = Path(__file__).resolve().parents[1]


def recipe(mode='pt2048', enabled=False, diagnostics=False):
    value = json.loads((ROOT / 'recipes/algorithms.json').read_text())['algorithms'][mode]
    value['PT_settings'].update(participating_media=enabled, medium_diagnostics=diagnostics)
    value['media_contract'] = {'enabled': enabled, 'imported_media_count': 1,
                               'medium_boundary_count': int(enabled), 'diagnostics': diagnostics}
    return value


def response(value, crossings=0, scatters=0, frames=0):
    mode = value['id']
    settings = copy.deepcopy(value['GI_settings'] if mode == 'gi' else value['PT_settings'])
    settings.update(imported_media_count=1, medium_boundary_count=int(value['media_contract']['enabled']),
                    media_enabled=value['media_contract']['enabled'])
    result = {'ready': True, 'settings': settings}
    if mode != 'gi':
        settings['medium_diagnostics_state'] = {'instrumentation': value['media_contract']['diagnostics'],
            'boundary_crossings': crossings, 'scattering_events': scatters, 'sampled_frames': frames}
        result['adaptive'] = copy.deepcopy(value['PT_adaptive'])
        result['effective'] = {**copy.deepcopy(settings), 'adaptive': copy.deepcopy(value['PT_adaptive'])}
        result['requested'] = copy.deepcopy(value['PT_settings'])
    return result


@pytest.mark.parametrize('mode', ['pt2048', 'pt-reblur', 'gi'])
def test_known_media_feedback_preserves_frozen_setting_checks(mode):
    value = recipe(mode)
    actual = response(value)
    capture.validate_effective_settings(actual, value)
    actual['settings']['unknown_transport_default'] = 1
    with pytest.raises(ValueError, match='Effective integrator settings'):
        capture.validate_effective_settings(actual, value)


def test_legacy_recipes_accept_only_new_disabled_defaults():
    value = recipe()
    actual = response(value)
    del value['media_contract']
    del value['PT_settings']['participating_media']
    del value['PT_settings']['medium_diagnostics']
    actual['requested'] = copy.deepcopy(value['PT_settings'])
    capture.validate_effective_settings(actual, value)
    actual['settings']['participating_media'] = True
    with pytest.raises(ValueError):
        capture.validate_effective_settings(actual, value)


def test_unknown_raw_effective_keys_are_rejected():
    value = recipe()
    actual = response(value)
    actual['effective']['new_kernel_default'] = 7
    with pytest.raises(ValueError):
        capture.validate_effective_settings(actual, value)


def test_media_recipe_requires_explicit_pt_switch_and_preserves_effective_denial():
    value = recipe(enabled=True, diagnostics=True)
    actual = response(value, crossings=12, scatters=8, frames=2048)
    capture.validate_effective_settings(actual, value)
    actual['effective']['participating_media'] = False
    with pytest.raises(ValueError):
        capture.validate_effective_settings(actual, value)
    del value['PT_settings']['participating_media']
    with pytest.raises(ValueError, match='explicit'):
        capture.validate_effective_settings(response(recipe()), value)


def test_diagnostic_counters_change_without_hiding_static_setting_drift():
    value = recipe(enabled=True, diagnostics=True)
    before = response(value, crossings=2, scatters=1, frames=1)
    after = response(value, crossings=120, scatters=80, frames=2048)
    assert capture.validate_effective_settings(before, value) == capture.validate_effective_settings(after, value)
    evidence = capture.validate_media_capture(after, value)
    assert evidence['boundary_crossings'] == 120 and evidence['sampled_frames'] == 2048
    after['effective']['medium_diagnostics_state']['scattering_events'] = 0
    after['settings']['medium_diagnostics_state']['scattering_events'] = 0
    with pytest.raises(ValueError, match='scattering'):
        capture.validate_media_capture(after, value)


def test_off_media_requires_real_disabled_telemetry():
    value = recipe('gi')
    actual = response(value)
    capture.validate_media_capture(actual, value)
    actual['settings']['media_enabled'] = True
    with pytest.raises(ValueError):
        capture.validate_effective_settings(actual, value)


def test_default_pure_pt_requests_media_and_audits_readback_without_scene_counts():
    value = recipe(enabled=True)
    actual = response(value)
    del value['media_contract']
    for settings in (actual['settings'], actual['effective']):
        settings.update(imported_media_count=0, medium_boundary_count=0)
    capture.validate_effective_settings(actual, value)
    actual['settings']['media_enabled'] = False
    with pytest.raises(ValueError, match='Effective media policy differs'):
        capture.validate_effective_settings(actual, value)


def test_pt_requested_snapshot_and_unknown_raw_effective_keys_are_validated():
    value = recipe()
    actual = response(value)
    actual['requested']['max_depth'] += 1
    with pytest.raises(ValueError):
        capture.validate_effective_settings(actual, value)


def test_gi_media_case_explicitly_sets_pt_disabled_before_restir_settings():
    value = recipe('gi')
    operations = capture.integrator_configuration_operations(value, 'scene', 'source-camera')
    assert [operation[0] for operation in operations] == ['set_pt_settings', 'set_restir_settings']
    assert operations[0][1][2]['participating_media'] is False
    assert operations[0][1][2]['medium_diagnostics'] is False
    assert operations[1][1][2] == value['GI_settings']


def test_final_readback_waits_for_diagnostic_frame_publication():
    value = recipe(enabled=True, diagnostics=True)
    stale, final = response(value, 2, 1, 1), response(value, 120, 80, 2048)
    class Session:
        def until(self, method, arguments, ready, timeout):
            assert not ready(stale)
            assert ready(final)
            return final
    assert capture.read_capture_effective(Session(), 'get_pt_settings', 'scene', 'camera', value, 10) == final
