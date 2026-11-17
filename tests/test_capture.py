import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/capture'


@pytest.fixture
def unused_capture_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def implementation():
    assert importlib.util.find_spec('scripts.capture'), 'Independent capture implementation missing'
    from scripts import capture
    return capture


def recipe(mode='gi'):
    return json.loads((ROOT / 'recipes/algorithms.json').read_text())['algorithms'][mode]


def test_raw_ticks_are_mapped_by_identity_and_qpc_with_four_contexts():
    capture = implementation()
    mapping = capture.map_gpu_frames(FIXTURE, 2)
    assert len(mapping['frames']) == 2
    assert mapping['verified_gpu_timestamp_rows'] == mapping['all_gpu_rows']
    timing = capture.summarize_timing(FIXTURE, mapping, {**recipe(), 'frames': 2, 'warmup': 1})
    assert timing['frames'] == 2 and timing['warmup_frames'] == 1
    groups = timing['retained']['per_pass_and_group']
    assert len([x for x in groups if x.startswith('NRD ') and ' / ' in x]) == 40
    assert timing['mean_ms'] > 0 and timing['total_ms'] > timing['sample_ms']


def test_corrupt_gpu_duration_is_rejected(tmp_path):
    capture = implementation()
    shutil.copytree(FIXTURE, tmp_path / 'capture')
    path = tmp_path / 'capture/gpu.csv'
    rows = capture.csv_rows(path)
    rows[0]['ms'] = str(float(rows[0]['ms']) + 1)
    import csv
    with path.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0]); writer.writeheader(); writer.writerows(rows)
    with pytest.raises(ValueError, match='timestamp'):
        capture.map_gpu_frames(path.parent, 2)


def test_missing_pass_or_wrong_transport_extent_is_rejected():
    capture = implementation()
    mapping = capture.map_gpu_frames(FIXTURE, 2)
    capture.validate_route(mapping, recipe())
    bad = copy.deepcopy(mapping)
    bad['frames'][0]['passes'] = [x for x in bad['frames'][0]['passes'] if x['name'] != 'Falcor DI final shading']
    with pytest.raises(ValueError, match='route'):
        capture.validate_route(bad, recipe())
    bad = copy.deepcopy(mapping)
    next(x for x in bad['frames'][0]['passes'] if x['name'] == 'RTXPT GI shared PT initial candidates')['width'] = 1
    with pytest.raises(ValueError, match='extent'):
        capture.validate_route(bad, recipe())


def test_actual_parent_and_private_context_settings_are_verified():
    capture = implementation()
    data = json.loads((FIXTURE / 'contexts.json').read_text())
    before, after = data['effective_reblur_before'], data['effective_reblur_after']
    assert capture.validate_context_evolution(before, after, recipe()['reblur'])['transition'] == 'cold-to-ready'
    assert capture.validate_context_evolution(after, after, recipe()['reblur'])['transition'] == 'ready-stable'
    bad = copy.deepcopy(after)
    bad['contexts']['physical_di']['settings']['diffuse_prepass'] = 15
    with pytest.raises(ValueError, match='physical DI'):
        capture.validate_context_evolution(before, bad, recipe()['reblur'])


def test_hair_support_needs_exact_actual_diagnostic_and_zero_actors():
    capture = implementation()
    data = json.loads((FIXTURE / 'hair-response.json').read_text())
    loaded, snapshot = data['open_project_response']['data'], data['scene_snapshot_response']['data']
    assert capture.hair_unsupported(loaded, snapshot)
    assert not capture.hair_unsupported(loaded, {'scene': 'route', 'actor_count': 1, 'actors': [{}]})
    assert not capture.hair_unsupported({'diagnostics': []}, snapshot)


def test_dragon_support_does_not_hide_arbitrary_crash_or_other_mode():
    capture = implementation()
    logs = 'Falcor DI found no finite light power\nRTXPT GI + REBLUR requires active Falcor DI:'
    assert capture.dragon_unsupported('dragon', 'gi', ['directional'], logs)
    assert not capture.dragon_unsupported('dragon', 'gi', ['directional'], 'engine crashed')
    assert not capture.dragon_unsupported('dragon', 'pt-reblur', ['directional'], logs)
    assert not capture.dragon_unsupported('dragon', 'gi', ['area'], logs)


@pytest.mark.parametrize('mutation,expected', [
    ('none', True), ('other-error', False), ('other-source-event', False),
    ('other-path-event', False), ('unattached-source', False),
])
def test_dragon_native_multiline_error_requires_exact_event_continuations(mutation, expected):
    capture = implementation()
    logs = (FIXTURE / 'dragon-setup-failure.log').read_text(encoding='utf8')
    prefix = '[2026-11-16T17:25:22.736006604][40296][ERROR][logging.cpp:19] '
    if mutation == 'other-error':
        logs += prefix + 'Unrelated API failure\n'
    elif mutation == 'other-source-event':
        logs = logs.replace(prefix + '    Source:', prefix.replace('40296', '40297') + '    Source:')
    elif mutation == 'other-path-event':
        logs = logs.replace(prefix + 'D:', prefix.replace('736006604', '736006605') + 'D:')
    elif mutation == 'unattached-source':
        logs += prefix + '    Source: \n'
    assert capture.dragon_unsupported('dragon', 'gi', ['directional'], logs) is expected


def test_environment_strips_inherited_diagnostics_and_pacing(tmp_path):
    capture = implementation()
    base = {'PATH': 'system', 'VISION_PT_COST_NEE': 'off', 'RTXPT_TEST': '1', 'RESTIR_TEST': '1', 'CORONA_OPTICS_MAX_FPS': '1', 'NORMAL': 'okay'}
    env = capture.build_environment(recipe(), tmp_path, tmp_path / 'engine.exe', [tmp_path / 'runtime'], 9980, base)
    assert not any(key in env for key in ('VISION_PT_COST_NEE', 'RTXPT_TEST', 'RESTIR_TEST', 'CORONA_OPTICS_MAX_FPS'))
    assert env['NORMAL'] == 'okay' and env['VISION_RESTIR_PRIMARY_JITTER'] == '0'
    assert env['PATH'].split(os.pathsep)[:2] == [str(tmp_path), str(tmp_path / 'runtime')]


def test_api_failure_stays_visible_and_no_fake_image_on_start_failure(tmp_path, monkeypatch):
    capture = implementation()
    source = tmp_path / 'scene.xml'; source.write_text('<scene/>')
    engine = tmp_path / 'engine.exe'; engine.write_bytes(b'not-real-engine')
    monkeypatch.setattr(capture, 'confirm_thermal_protection', lambda: [{'test_boundary': True}])
    def fail(*args, **kwargs): raise OSError('explicit transport launch failure')
    monkeypatch.setattr(capture.subprocess, 'Popen', fail)
    result = capture.capture({'id': 'kitchen', 'source': str(source)}, recipe(), engine, tmp_path / 'output')
    assert result['status'] == 'failed' and 'explicit transport launch failure' in result['error']
    assert 'image' not in result and 'timing' not in result
    assert not (tmp_path / 'output/image.png').exists()


def test_capture_options_request_one_reset_batch_without_hidden_warmup(tmp_path):
    capture = implementation()
    class Boundary:
        def api(self, name, args, timeout):
            return {'name': name, 'args': args, 'timeout': timeout}
    result = capture.screenshot(Boundary(), 'route', 'source-camera', tmp_path, 2048, 7200)
    assert result['name'] == 'SceneTools.save_screenshot'
    assert result['args'][3] == {'render_frames': 2048, 'warmup_frames': 0, 'timings_path': str(tmp_path / 'frames.csv'), 'timeout_seconds': 7200}


def test_api_rejection_logs_raw_native_response_and_raises(tmp_path):
    capture = implementation()
    class FrontendBoundary:
        class Socket:
            def settimeout(self, value): pass
        socket = Socket()
        def call(self, method, params):
            return {'result': {'value': {'success': True, 'data': {'ok': False, 'status': 'invalid_archive', 'diagnostics': [{'code': 'EXTERNAL_SCENE_INVALID'}]}}}}
    session = capture.Session(FrontendBoundary(), None, tmp_path / 'api.jsonl')
    with pytest.raises(RuntimeError, match='EXTERNAL_SCENE_INVALID'):
        session.api('ProjectLauncher.open_project', ['scene.xml'])
    assert json.loads((tmp_path / 'api.jsonl').read_text())['response']['data']['status'] == 'invalid_archive'


def test_truncated_mapping_cannot_receive_complete_timing():
    capture = implementation()
    mapping = capture.map_gpu_frames(FIXTURE, 2)
    mapping['frames'].pop()
    with pytest.raises(ValueError, match='mapping frame sequence'):
        capture.summarize_timing(FIXTURE, mapping, {**recipe(), 'frames': 2, 'warmup': 0})


def test_hair_missing_data_does_not_prove_support_changed():
    capture = implementation()
    assert hasattr(capture, 'hair_support_changed'), 'Explicit support-change evidence classifier missing'
    assert not capture.hair_support_changed({'ok': True}, {'scene': 'route'})
    assert not capture.hair_support_changed({'ok': True}, {'actors': [], 'actor_count': 0})
    assert capture.hair_support_changed({'ok': True, 'diagnostics': []}, {'actors': [{}], 'actor_count': 1})


def test_configure_failure_closes_only_owned_process_and_restores_ini(tmp_path, monkeypatch):
    capture = implementation()
    engine = tmp_path / 'engine.exe'; engine.write_bytes(b'not-real-engine')
    source = tmp_path / 'scene.xml'; source.write_text('<scene/>')
    ini = tmp_path / 'CoronaEditor.ini'; ini.write_text('original')
    class ProcessBoundary:
        pid = 987654
        returncode = None
        def poll(self): return self.returncode
        def wait(self, timeout):
            self.returncode = 0
            return 0
    process = ProcessBoundary()
    class FrontendBoundary:
        class Socket:
            def close(self): pass
        socket = Socket()
    closed = []
    class SessionBoundary:
        def __init__(self, frontend, owned, log):
            assert owned is process
        def evaluate(self, expression): return True
        def api(self, name, args, timeout):
            assert name == 'CoronaEditor.close_process'
            closed.append(process.pid)
            process.returncode = 0
    def fail(*args):
        ini.write_text('engine modified')
        raise RuntimeError('actual import transport failed')
    monkeypatch.setattr(capture, 'confirm_thermal_protection', lambda: [])
    monkeypatch.setattr(capture.subprocess, 'Popen', lambda *a, **kw: process)
    monkeypatch.setattr(capture, 'Frontend', lambda port: FrontendBoundary())
    monkeypatch.setattr(capture, 'Session', SessionBoundary)
    monkeypatch.setattr(capture, 'configure', fail)
    result = capture.capture({'id': 'dragon', 'source': str(source), 'expected_unsupported': {'gi': 'FALCOR_DI_NO_FINITE_LIGHT_POWER'}}, recipe(), engine, tmp_path / 'out')
    assert result['status'] == 'failed' and result['owned_engine_closed']
    assert closed == [987654] and ini.read_text() == 'original'
    assert 'image' not in result and 'timing' not in result


def test_new_import_warning_is_rejected_instead_of_silent_degradation():
    capture = implementation()
    loaded = json.loads((FIXTURE / 'hair-response.json').read_text())['open_project_response']['data']
    allowed = ['UNSUPPORTED_SHAPE', 'LIGHT_UNSUPPORTED']
    assert 'expected_codes' in __import__('inspect').signature(capture.reject_load_errors).parameters, 'Expected importer diagnostics must be enforced'
    capture.reject_load_errors(loaded, expected_codes=allowed)
    changed = copy.deepcopy(loaded)
    changed['diagnostics'].append({'code': 'NEW_FEATURE_DEFERRED', 'severity': 'warning', 'message': 'New importer regression'})
    with pytest.raises(ValueError, match='Unexpected importer diagnostic.*NEW_FEATURE_DEFERRED'):
        capture.reject_load_errors(changed, expected_codes=allowed)


def test_pt2048_per_sample_cost_averages_all_samples_and_keeps_last_separate(tmp_path):
    capture = implementation()
    shutil.copytree(FIXTURE / 'pt2048', tmp_path / 'timings')
    folder = tmp_path / 'timings'
    mapping = capture.map_gpu_frames(folder, 2)
    rows = capture.csv_rows(folder / 'frames.csv')
    # Deliberately controlled arithmetic fixture; never written to product results.
    rows[0]['render_including_denoise_ms'] = '1'
    rows[1]['render_including_denoise_ms'] = '3'
    import csv
    with (folder / 'frames.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0]); writer.writeheader(); writer.writerows(rows)
    timing = capture.summarize_timing(folder, mapping, {**recipe('pt2048'), 'frames': 2, 'warmup': 1})
    assert timing['total_ms'] == 4
    assert timing['sample_ms'] == 2
    assert timing['shown_frame_ms'] == 3
    assert timing['mean_ms'] == 3


@pytest.mark.parametrize('mode', ['gi', 'pt-reblur', 'pt2048'])
def test_complete_effective_settings_match_frozen_recipe(mode):
    capture = implementation()
    data = json.loads((FIXTURE / 'effective-settings.json').read_text())[mode]
    assert hasattr(capture, 'validate_effective_settings'), 'Complete effective settings audit missing'
    capture.validate_effective_settings(data['effective_before'], recipe(mode))
    capture.validate_effective_settings(data['effective_after'], recipe(mode))
    extra = copy.deepcopy(data['effective_before'])
    extra['settings']['new_transport_default'] = 1
    with pytest.raises(ValueError, match='Effective integrator settings'):
        capture.validate_effective_settings(extra, recipe(mode))


@pytest.mark.parametrize('arm', ['direct', 'indirect'])
def test_gi_diffuse_factor_default_drift_is_rejected(arm):
    capture = implementation()
    assert recipe()['GI_settings'][arm].get('diffuse_factor') == 0.3, 'Inherited diffuse_factor default must be explicit'
    data = json.loads((FIXTURE / 'effective-settings.json').read_text())['gi']['effective_before']
    data['settings'][arm]['diffuse_factor'] = 0.9
    with pytest.raises(ValueError, match='Effective integrator settings'):
        capture.validate_effective_settings(data, recipe())


def test_small_non_float32_setting_drift_is_rejected():
    capture = implementation()
    assert hasattr(capture, 'validate_effective_settings'), 'Complete effective settings audit missing'
    data = json.loads((FIXTURE / 'effective-settings.json').read_text())['gi']['effective_before']
    data['settings']['indirect']['spatial']['depth'] += 0.000001
    with pytest.raises(ValueError, match='Effective integrator settings'):
        capture.validate_effective_settings(data, recipe())


@pytest.mark.parametrize('key,value', [('enabled', True), ('available', True), ('threshold', 0.02), ('start_index', 64)])
def test_pt_adaptive_defaults_are_frozen_by_readback(key, value):
    capture = implementation()
    assert hasattr(capture, 'validate_effective_settings'), 'Complete effective settings audit missing'
    data = json.loads((FIXTURE / 'effective-settings.json').read_text())['pt2048']['effective_before']
    data['adaptive'][key] = value
    with pytest.raises(ValueError, match='Effective integrator settings'):
        capture.validate_effective_settings(data, recipe('pt2048'))


def test_thermal_evidence_includes_root_driver_version(monkeypatch):
    capture = implementation()
    # Process boundary fixture tests NVIDIA XML root field routing; no driver query.
    from types import SimpleNamespace
    xml = '<nvidia_smi_log><driver_version>123.45</driver_version><gpu><product_name>Fixture GPU</product_name><uuid>GPU-fixture</uuid><temperature><gpu_temp_slow_threshold>90 C</gpu_temp_slow_threshold><gpu_temp_max_threshold>100 C</gpu_temp_max_threshold><gpu_temp>50 C</gpu_temp></temperature></gpu></nvidia_smi_log>'
    monkeypatch.setattr(capture.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout=xml))
    hardware = capture.confirm_thermal_protection()
    assert hardware[0].get('driver_version') == '123.45'


@pytest.mark.parametrize('failure,expected', [
    ('import', 'failed'), ('config', 'failed'), ('camera', 'failed'),
    ('unknown-api', 'failed'), ('validation', 'failed'), ('forced', 'failed'),
    ('unrelated-fatal', 'failed'), ('transport', 'expected-unsupported'),
    ('websocket', 'expected-unsupported'), ('timeout', 'expected-unsupported'),
    ('shutdown-disconnect', 'expected-unsupported'),
])
def test_dragon_known_logs_only_exempt_expected_setup_termination(tmp_path, monkeypatch, unused_capture_port, failure, expected):
    """Exercise real configure/finalization; replace only process and API boundaries."""
    capture = implementation()
    source = tmp_path / 'scene.xml'
    source.write_text('<scene><emitter type="directional"/></scene>')
    engine = tmp_path / 'engine.exe'
    engine.write_bytes(b'fixture')
    original = json.loads((ROOT / 'scenes/cameras.json').read_text())['scenes']['dragon']['camera']
    camera = dict(original, camera_id='source-camera')
    route = 'fixture/dragon'
    recipe_data = recipe()

    class Process:
        pid = 987654
        returncode = None
        def poll(self): return self.returncode
        def wait(self, timeout):
            if failure == 'forced' and self.returncode is None:
                raise capture.subprocess.TimeoutExpired('fixture', timeout)
            if failure == 'shutdown-disconnect': self.returncode = 3221226505
            self.returncode = self.returncode if self.returncode is not None else 0
        def terminate(self): self.returncode = -1
    process = Process()

    class Frontend:
        class Socket:
            def close(self): pass
        socket = Socket()

    class Session:
        def __init__(self, *args): pass
        def evaluate(self, expression): return True
        def until(self, name, args, ready, timeout):
            value = self.api(name, args)
            if not ready(value):
                raise TimeoutError(name + ' did not become ready')
            return value
        def api(self, name, args, timeout=30):
            if name == 'CoronaEditor.close_process':
                if failure == 'shutdown-disconnect': raise ConnectionResetError('Shutdown disconnected')
                if failure != 'forced': process.returncode = 0
                return {}
            if name == 'ProjectLauncher.get_project_load_status':
                return {'archive_service_ready': True, 'active': True, 'total': 1}
            if name == 'ProjectLauncher.open_project':
                diagnostics = [{'code': 'NEW_FEATURE_DEFERRED', 'severity': 'warning'}] if failure == 'import' else []
                return {'ok': True, 'scene': {'path': route}, 'diagnostics': diagnostics}
            if name == 'SceneTools.list_camera_views':
                return {'cameras': [dict(camera, fov=-1) if failure == 'camera' else dict(camera)]}
            if name == 'MainView.on_init': return {}
            method = name.removeprefix('SceneTools.')
            if failure == 'config': raise RuntimeError('Unknown configuration API failure')
            fields = {'close_camera_view': {'view_open': False},
                      'set_render_backend': {'render_backend': 'vision'},
                      'set_output_mode': {'output_mode': 'final_color'},
                      'update_camera_view': dict(zip(('width', 'height'), recipe_data['resolution']))}
            if method in fields: camera.update(fields[method])
            elif method.startswith('set_'):
                key = method.removeprefix('set_')
                if key in ('restir_settings', 'reblur_settings'): key = 'vision_' + key
                camera[key] = args[-1]
            else: raise AssertionError('Unexpected fixture API: ' + name)
            return {}

    def launch(*args, **kwargs):
        logs = 'Falcor DI found no finite light power\nRTXPT GI + REBLUR requires active Falcor DI:\n'
        if expected == 'expected-unsupported':
            logs = (FIXTURE / 'dragon-setup-failure.log').read_text(encoding='utf8')
        kwargs['stdout'].write(logs)
        if failure == 'unrelated-fatal': kwargs['stdout'].write('FATAL: unrelated GPU device lost\n')
        return process

    def failed_screenshot(session, actual_route, identity, output, frames, timeout):
        assert actual_route == route and identity == 'source-camera' and frames == 1
        if failure == 'unknown-api': raise RuntimeError('Unknown screenshot API failure')
        if failure == 'validation': raise ValueError('Unexpected capture validation failure')
        if failure not in ('forced', 'shutdown-disconnect'): process.returncode = 3221226505
        if failure == 'websocket':
            from websocket import WebSocketConnectionClosedException
            raise WebSocketConnectionClosedException('Connection to remote host was lost.')
        if failure == 'timeout':
            from websocket import WebSocketTimeoutException
            raise WebSocketTimeoutException('Connection timed out')
        raise ConnectionResetError(10054, 'Remote host closed the connection')

    monkeypatch.setattr(capture, 'confirm_thermal_protection', lambda: [])
    monkeypatch.setattr(capture.subprocess, 'Popen', launch)
    monkeypatch.setattr(capture, 'Frontend', lambda port: Frontend())
    monkeypatch.setattr(capture, 'Session', Session)
    monkeypatch.setattr(capture, 'screenshot', failed_screenshot)
    result = capture.capture({'id': 'dragon', 'source': str(source),
        'expected_unsupported': {'gi': 'FALCOR_DI_NO_FINITE_LIGHT_POWER'}}, recipe_data, engine,
        tmp_path / 'out', port=unused_capture_port)
    assert result['status'] == expected, result
    assert result['owned_engine_closed']
    assert 'image' not in result and 'timing' not in result
    if expected == 'failed':
        failures = {
            'import': ('configure', 'ValueError', 'NEW_FEATURE_DEFERRED'),
            'config': ('configure', 'RuntimeError', 'Unknown configuration API failure'),
            'camera': ('configure', 'ValueError', 'Original camera differs from frozen numeric pose'),
            'unknown-api': ('setup-screenshot', 'RuntimeError', 'Unknown screenshot API failure'),
            'validation': ('setup-screenshot', 'ValueError', 'Unexpected capture validation failure'),
            'forced': ('setup-screenshot', 'ConnectionResetError', 'Remote host closed the connection'),
            'unrelated-fatal': ('setup-screenshot', 'ConnectionResetError', 'Remote host closed the connection'),
        }
        phase, error_type, message = failures[failure]
        assert result['failure_phase'] == phase, result
        assert result['error_type'] == error_type, result
        assert message in result['error'], result
        assert bool(result.get('forced_shutdown')) is (failure == 'forced')
    if expected == 'expected-unsupported':
        assert result['support_evidence']['failure_phase'] == 'setup-screenshot'
        assert result['support_evidence']['route'] == route
    if failure == 'shutdown-disconnect':
        assert result['shutdown_rpc_disconnect'] == 'Shutdown disconnected'
