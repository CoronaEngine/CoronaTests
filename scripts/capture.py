"""Independent public-API engine capture and raw GPU timestamp audit.

Importing this module does not query hardware or launch a process. The only
rendering entry points are capture() and capture_scene(); recipes freeze configuration.
"""
from __future__ import annotations

from collections import defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import statistics
import struct
import subprocess
import tempfile
import time
from urllib.request import urlopen
import xml.etree.ElementTree as ET

from scripts.restir_timing import PASS_GROUPS

ROOT = Path(__file__).resolve().parents[1]
POSE_FIELDS = ('position', 'forward', 'world_up', 'projection_x_sign', 'near_plane', 'far_plane', 'fov')
CLEAN_PREFIXES = ('VISION_', 'REBLUR_', 'RELAX_', 'CORONA_VISION_', 'CORONA_GPU_',
                  'CORONA_RESTIR_', 'CORONA_FALCOR_', 'HORIZON_BENCH_', 'SSAT_', 'RTXPT_', 'RESTIR_')
TAILS = {'NRD-EDSL-REBLUR-Unpack', 'NRD-EDSL-REBLUR-Unpack-BatchCompose', 'NRD-EDSL-REBLUR-Unpack-Compose'}
HAIR_SCENES = {'hair-curl', 'curly-hair', 'straight-hair', 'furball'}
GI = ['ReSTIR GI stable plane build', 'ReSTIR GI shared PT initial candidates',
      'ReSTIR GI temporal resampling', 'ReSTIR GI spatial resampling', 'ReSTIR GI final shading']
GI_LOG_MARKERS = (
    'GI endpoint normal diagnostic: mode=shading-facing, material_normal=true, native_grazing_adjust=true',
    'GI signal cost mode=optimized-buffer,', 'GI signal diagnostic mode=full, native_miss=true',
    'private stable planes: count=3',
    'ReSTIR GI + ReSTIR DI + REBLUR active: private_planes=3, physical_DI_contexts=1, GI_contexts=3, shared_transport=false',
)
NO_LIGHT_POWER = 'ReSTIR DI found no finite light power'
REQUIRES_DI = 'ReSTIR GI + REBLUR requires active ReSTIR DI:'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf8')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(data)
    return digest.hexdigest()


def csv_rows(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        require(reader.fieldnames is not None, 'Missing CSV header: ' + str(path))
        header = dict(zip(reader.fieldnames, reader.fieldnames))
        rows = [row for row in reader if row != header]
    require(all(None not in row and all(v is not None for v in row.values()) for row in rows), 'Malformed CSV: ' + str(path))
    return rows


def subset_equal(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset_equal(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(subset_equal(a, b) for a, b in zip(actual, expected))
    if isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, (int, float)):
        return isinstance(actual, (int, float)) and abs(actual - expected) <= max(1e-5, abs(expected) * 1e-6)
    return actual == expected


def matches_float32(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and set(actual) == set(expected) and all(matches_float32(actual[k], v) for k, v in expected.items())
    if isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, (int, float)):
        return (isinstance(actual, (int, float)) and not isinstance(actual, bool)
                and (actual == expected or actual == struct.unpack('!f', struct.pack('!f', float(expected)))[0]))
    return actual == expected


MEDIA_TELEMETRY = ('imported_media_count', 'medium_boundary_count', 'media_enabled')


def normalize_integrator_media(settings, recipe):
    """Remove only audited telemetry and disabled defaults from the frozen schema."""
    require(isinstance(settings, dict), 'Effective integrator settings must be an object')
    settings = dict(settings)
    contract = recipe.get('media_contract')
    expected = recipe['GI_settings'] if recipe['id'] == 'gi' else recipe['PT_settings']
    media_enabled = expected.get('participating_media', False)
    if contract:
        require(all(type(recipe['PT_settings'].get(key)) is bool
                    for key in ('participating_media', 'medium_diagnostics')),
                'Media capture requires explicit participating_media and medium_diagnostics settings')
        require(recipe['PT_settings']['participating_media'] is contract['enabled'] and
                recipe['PT_settings']['medium_diagnostics'] is contract['diagnostics'],
                'Explicit PT media request differs from capture contract')
        require(recipe['id'] == 'pt2048' or not contract['enabled'], 'Realtime media capture must be disabled')
    for key in ('participating_media', 'medium_diagnostics'):
        if key in settings:
            require(type(settings[key]) is bool, 'Effective media setting must be boolean: ' + key)
            if key not in expected:
                require(settings.pop(key) is False, 'Unexpected enabled media default: ' + key)
    present = set(MEDIA_TELEMETRY).intersection(settings)
    require(not present or present == set(MEDIA_TELEMETRY), 'Incomplete effective media telemetry')
    if present:
        for key in MEDIA_TELEMETRY[:2]:
            require(type(settings[key]) is int and settings[key] >= 0, 'Invalid effective media count: ' + key)
        require(type(settings['media_enabled']) is bool, 'Invalid effective media_enabled')
        require(settings['media_enabled'] is media_enabled, 'Effective media policy differs')
        if contract:
            require(all(settings[key] == contract[key] for key in MEDIA_TELEMETRY[:2]),
                    'Effective imported media/boundary counts differ')
        for key in MEDIA_TELEMETRY:
            settings.pop(key)
    else:
        require(not (contract or media_enabled), 'Media capture requires actual effective media telemetry')
    if 'medium_diagnostics_state' in settings:
        state = settings.pop('medium_diagnostics_state')
        require(isinstance(state, dict) and set(state) ==
                {'instrumentation', 'boundary_crossings', 'scattering_events', 'sampled_frames'},
                'Invalid medium diagnostics schema')
        require(type(state['instrumentation']) is bool and all(type(state[key]) is int and state[key] >= 0
                for key in ('boundary_crossings', 'scattering_events', 'sampled_frames')), 'Invalid medium diagnostics values')
        require(state['instrumentation'] is (contract['diagnostics'] if contract else False),
                'Actual medium instrumentation differs')
        if not state['instrumentation']:
            require(state['boundary_crossings'] == state['scattering_events'] == state['sampled_frames'] == 0,
                    'Disabled medium instrumentation reported samples')
    elif (contract or media_enabled) and recipe['id'] != 'gi':
        require(False, 'Media capture requires actual PT diagnostic state')
    return settings


def validate_effective_settings(actual, recipe):
    """Freeze all settings while explicitly auditing known media feedback additions."""
    expected = {'ready': True, 'settings': recipe['GI_settings'] if recipe['id'] == 'gi' else recipe['PT_settings']}
    if recipe['id'] != 'gi':
        expected['adaptive'] = recipe['PT_adaptive']
    actual = dict(actual)
    raw = actual.pop('effective', None)
    requested = actual.pop('requested', None)
    actual['settings'] = normalize_integrator_media(actual.get('settings'), recipe)
    if requested is not None:
        require(matches_float32(requested, recipe['PT_settings']), 'Requested PT settings differ from frozen recipe')
    require(matches_float32(actual, expected), 'Effective integrator settings differ from complete frozen recipe: ' + json.dumps(actual))
    if raw is not None:
        require(isinstance(raw, dict), 'Raw effective PT settings must be an object')
        raw = dict(raw)
        adaptive = raw.pop('adaptive', None)
        normalized = {'ready': actual['ready'], 'settings': normalize_integrator_media(raw, recipe), 'adaptive': adaptive}
        require(matches_float32(normalized, expected), 'Raw effective integrator settings differ from complete frozen recipe')
    return actual


def validate_media_capture(actual, recipe):
    validate_effective_settings(actual, recipe)
    contract = recipe.get('media_contract')
    if not contract:
        return None
    settings = actual.get('effective', actual['settings'])
    state = settings.get('medium_diagnostics_state', {})
    if contract['diagnostics']:
        require(state.get('boundary_crossings', 0) > 0, 'No actual medium boundary crossings recorded')
        require(state.get('scattering_events', 0) > 0, 'No actual medium scattering events recorded')
        require(state.get('sampled_frames') == recipe['frames'], 'Medium diagnostic sampled frame count differs')
    return {key: settings[key] for key in MEDIA_TELEMETRY} | state


def validate_context_evolution(before, after, parent):
    require(before['settings'] == after['settings'] == parent, 'Parent REBLUR settings changed')
    require({k: v for k, v in before.items() if k != 'contexts'} == {k: v for k, v in after.items() if k != 'contexts'}, 'Non-context REBLUR fields changed')
    b, a = before['contexts'], after['contexts']
    require(b['policy'] == a['policy'] == 'physical-di-detail-preserving-v1', 'Private context policy differs')
    require(b['gi'] == a['gi'] and a['gi']['count'] == 3, 'GI contexts changed')
    require(matches_float32(a['gi']['settings'], parent), 'GI settings differ from float32 parent')
    pb, pa = b['physical_di'], a['physical_di']
    require(set(pb) == set(pa) == {'count', 'ready', 'settings'} and pb['count'] == pa['count'] == 1, 'physical DI count differs')
    require(pa['ready'] is True, 'Final physical DI context not ready')
    expected = dict(a['gi']['settings'])
    expected['diffuse_prepass'] = 0
    expected['max_radius'] = min(10, expected['max_radius'])
    expected['min_radius'] = min(expected['min_radius'], expected['max_radius'])
    require(matches_float32(pa['settings'], expected), 'Actual physical DI settings differ from D0/R<=10')
    if pb['ready'] is True:
        require(pb == pa, 'Ready physical DI context changed')
        transition = 'ready-stable'
    else:
        require(pb['ready'] is False and pb['settings'] == {}, 'Unexpected physical DI cold state')
        transition = 'cold-to-ready'
    return {'transition': transition, 'extra_render_frames': 0, 'actual_before': b, 'actual_after': a}


def read_windows(path, frames):
    rows = csv_rows(path)
    require([int(row['frame']) for row in rows] == list(range(1, frames + 1)), 'Frame QPC sequence differs')
    result = []
    for row in rows:
        window = {k: int(row[v]) for k, v in {'frame': 'frame', 'integrator_frame': 'integrator_frame', 'begin': 'frame_qpc_begin', 'end': 'frame_qpc_end', 'frequency': 'qpc_frequency'}.items()}
        require(window['frame'] == window['integrator_frame'] and 0 < window['begin'] < window['end'] and window['frequency'] > 0, 'Invalid frame QPC window')
        if result:
            require(window['begin'] >= result[-1]['end'] and window['frequency'] == result[-1]['frequency'], 'Overlapping/incompatible frame QPC windows')
        result.append(window)
    return result


def partition_gpu_profiles(journal, captures):
    """Keep paired raw profile rows in each job's setup-to-capture QPC span."""
    names = ('gpu.csv', 'gpu-diagnostics.csv')
    gpu, diagnostics = (csv_rows(journal / name) for name in names)
    require(gpu and len(gpu) == len(diagnostics), 'Missing/unequal shared GPU diagnostic rows')
    last_end = 0
    for output, frames in captures:
        setup = read_windows(output / 'setup/stages.csv', 1)[0]
        windows = read_windows(output / 'stages.csv', frames)
        require(last_end <= setup['begin'] and setup['end'] <= windows[0]['begin']
                and setup['frequency'] == windows[-1]['frequency'], 'Overlapping or incompatible job QPC windows')
        last_end = windows[-1]['end']
        indices = [i for i, row in enumerate(diagnostics)
                   if setup['begin'] <= int(row['submit_begin_qpc']) <= int(row['fence_ready_qpc']) <= last_end]
        for name, rows in zip(names, (gpu, diagnostics)):
            with (output / name).open('w', encoding='utf8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=rows[0])
                writer.writeheader()
                writer.writerows(rows[i] for i in indices)


def map_gpu_frames(output, frames):
    output = Path(output)
    gpu, diagnostics = csv_rows(output / 'gpu.csv'), csv_rows(output / 'gpu-diagnostics.csv')
    require(gpu and len(gpu) == len(diagnostics), 'Missing/unequal GPU diagnostic rows')
    windows, setup_windows = read_windows(output / 'stages.csv', frames), read_windows(output / 'setup/stages.csv', 1)
    by_frame = {i: [] for i in range(1, frames + 1)}
    setup, outside = [], []
    for index, (row, diag) in enumerate(zip(gpu, diagnostics)):
        require((row['batch'], row['name'], row['width'], row['height']) == (diag['profile_batch'], diag['name'], diag['width'], diag['height']), 'GPU/diagnostic identity differs at row ' + str(index))
        duration = float(row['ms'])
        begin, end, frequency = (int(diag[k]) for k in ('submit_begin_qpc', 'fence_ready_qpc', 'qpc_frequency'))
        period, mask = float(diag['timestamp_period_ns']), int(diag['timestamp_mask'])
        require(math.isfinite(duration) and duration >= 0 and 0 < begin <= end and frequency > 0 and math.isfinite(period) and period > 0 and mask > 0, 'Invalid GPU timestamp/QPC row')
        actual = ((int(diag['end_tick']) - int(diag['begin_tick'])) & mask) * period / 1e6
        require(abs(actual - duration) <= 1.1e-6 + 1e-9 * abs(duration), 'GPU timestamp duration differs at row ' + str(index))
        require(frequency in {w['frequency'] for w in windows + setup_windows}, 'GPU/frame QPC clocks differ')
        matches = [(scope, w) for scope, candidates in (('capture', windows), ('setup', setup_windows)) for w in candidates if w['begin'] <= begin <= end <= w['end'] and frequency == w['frequency']]
        require(len(matches) <= 1, 'Ambiguous GPU/frame containment')
        item = {'gpu_row_index': index, 'diagnostics_row_index': index, 'batch': int(row['batch']), 'name': row['name'], 'width': int(row['width']), 'height': int(row['height']), 'ms': duration, 'submit_begin_qpc': begin, 'fence_ready_qpc': end, 'qpc_frequency': frequency}
        if not matches:
            outside.append(item)
        else:
            scope, window = matches[0]
            item.update(frame=window['frame'], integrator_frame=window['integrator_frame'])
            (by_frame[window['frame']] if scope == 'capture' else setup).append(item)
    require(setup and all(by_frame.values()), 'Missing setup/capture GPU dispatch evidence')
    return {'schema': 1, 'frames': [{'frame': i, 'integrator_frame': i, 'passes': rows} for i, rows in by_frame.items()], 'setup_rows': setup, 'outside_recorded_frame_windows': outside, 'all_gpu_rows': len(gpu), 'verified_gpu_timestamp_rows': len(gpu), 'csv_sha256': {name: sha256(output / name) for name in ('gpu.csv', 'gpu-diagnostics.csv', 'frames.csv', 'stages.csv', 'setup/frames.csv', 'setup/stages.csv')}}


def grouped_passes(passes, roles):
    result = defaultdict(float)
    inside, ordinal, count = False, 0, 0
    for item in sorted(passes, key=lambda p: p['gpu_row_index']):
        name, duration = item['name'], item['ms']
        if name == 'NRD-EDSL-REBLUR-Pack':
            require(not inside and ordinal < len(roles), 'Unexpected REBLUR context block')
            inside, count = True, 0
        if inside:
            result['NRD ' + roles[ordinal] + ' / ' + name] += duration
            result['TOTAL NRD ' + roles[ordinal] + ' dispatch sum'] += duration
            count += 1
            if name in TAILS:
                require(count == 10, 'REBLUR context must contain ten dispatches')
                inside, ordinal = False, ordinal + 1
        else:
            require(name not in TAILS and not name.startswith('NRD-EDSL-REBLUR-'), 'REBLUR pass outside context block')
            result[name] += duration
    require(not inside and ordinal == len(roles), 'Incomplete REBLUR context sequence')
    return dict(result)


def validate_route(mapping, recipe):
    mode = recipe['id']
    for frame in mapping['frames']:
        passes = frame['passes']
        names = [current_log_names(p['name']) for p in passes]
        transport = GI[1] if mode == 'gi' else 'path_tracing'
        if mode == 'gi':
            require(all(names.count(n) == 1 for n in (*PASS_GROUPS['di'], *GI)) and not any(n in names for n in ('path_tracing', 'Stable planes build', 'Stable planes residual fill')), 'GI/ReSTIR DI route differs')
        else:
            require(names.count('path_tracing') == 1 and not any('ReSTIR DI' in n or 'ReSTIR GI' in n or 'Stable planes' in n for n in names), 'PT route differs')
        require(names.count('RGBFilm-accumulation') == int(recipe['accumulation']), 'Accumulation pass count differs')
        if mode == 'pt-reblur':
            require(names.count('SVGF-CoverageResolve') == names.count('SVGF-PublishResolve') == 1, 'PT REBLUR coverage route differs')
        require(all([p['width'], p['height']] == recipe['resolution'] for name, p in zip(names, passes) if name == transport), 'Actual transport extent differs')
        grouped_passes(passes, recipe['reblur_contexts'])


def gpu_envelope(passes, diagnostics):
    selected = [diagnostics[p['diagnostics_row_index']] for p in passes]
    periods, masks = {float(p['timestamp_period_ns']) for p in selected}, {int(p['timestamp_mask']) for p in selected}
    require(len(periods) == len(masks) == 1, 'Incompatible GPU timestamp clocks')
    period, mask = next(iter(periods)), next(iter(masks))
    anchor = int(selected[0]['begin_tick'])
    starts = [(int(p['begin_tick']) - anchor) & mask for p in selected]
    ends = [(int(p['end_tick']) - anchor) & mask for p in selected]
    require(max(ends) < mask / 2, 'Unresolvable GPU timestamp order/wrap')
    return (max(ends) - min(starts)) * period / 1e6


def stats(values):
    values = list(values)
    require(values and all(math.isfinite(v) for v in values), 'Empty/nonfinite timing samples')
    return {'n': len(values), 'mean_ms': statistics.fmean(values), 'median_ms': statistics.median(values), 'stddev_ms': statistics.pstdev(values), 'total_ms': sum(values)}


def validate_frames(output, frames, denoise):
    rows = csv_rows(Path(output) / 'frames.csv')
    require(len(rows) == frames and all(int(row['frame']) == int(row['integrator_frame']) == i for i, row in enumerate(rows, 1)), 'Expected consecutive frames from reset history')
    for row in rows:
        render, nrd = float(row['render_including_denoise_ms']), float(row['denoise_ms'])
        require(math.isfinite(render) and render > 0 and math.isfinite(nrd) and nrd >= 0 and (denoise or nrd == 0), 'Invalid renderer/denoiser timing')
    require(not denoise or sum(float(r['denoise_ms']) for r in rows) > 0, 'Requested REBLUR did not execute')
    return rows


def summarize_timing(output, mapping, recipe):
    rows = validate_frames(output, recipe['frames'], recipe['denoise'])
    require([(f['frame'], f['integrator_frame']) for f in mapping['frames']] == [(i, i) for i in range(1, recipe['frames'] + 1)], 'GPU mapping frame sequence differs')
    validate_route(mapping, recipe)
    diagnostics = csv_rows(Path(output) / 'gpu-diagnostics.csv')
    samples = []
    for frame, row in zip(mapping['frames'], rows):
        samples.append({'API_render_interval': float(row['render_including_denoise_ms']), 'API_denoise_interval': float(row['denoise_ms']), 'GPU_timestamp_envelope': gpu_envelope(frame['passes'], diagnostics), 'GPU_dispatch_duration_sum': sum(p['ms'] for p in frame['passes']), 'per_pass_and_group': grouped_passes(frame['passes'], recipe['reblur_contexts'])})
    def summarize(selected):
        result = {key: stats(row[key] for row in selected) for key in ('API_render_interval', 'API_denoise_interval', 'GPU_timestamp_envelope', 'GPU_dispatch_duration_sum')}
        labels = sorted(set().union(*(row['per_pass_and_group'] for row in selected)))
        result['per_pass_and_group'] = {key: stats(row['per_pass_and_group'].get(key, 0) for row in selected) for key in labels}
        return result
    warmup = recipe['warmup']
    retained = summarize(samples[warmup:])
    total_ms = sum(row['API_render_interval'] for row in samples)
    return {
        'frames': len(rows), 'warmup_frames': warmup, 'retained_frames': len(rows) - warmup,
        'mean_ms': retained['API_render_interval']['mean_ms'],
        'median_ms': retained['API_render_interval']['median_ms'],
        'sample_ms': total_ms / len(rows), 'shown_frame_ms': samples[-1]['API_render_interval'],
        'total_ms': total_ms, 'all_frames': summarize(samples), 'retained': retained,
        'image_frame': samples[-1],
        'labels': {
            'mean_ms': 'API renderer interval after warmup, includes submission/wait',
            'sample_ms': 'All-sample API renderer cost: total_ms / frames, including warmup',
            'shown_frame_ms': 'Last displayed frame/sample API renderer interval',
            'total_ms': 'All samples API intervals summed; excludes import, compile, PNG and disk',
            'GPU_timestamp_envelope': 'First-to-last GPU timestamp span including gaps',
            'GPU_dispatch_duration_sum': 'Dispatch scope sum; not frame latency',
        },
    }


def validate_png(path, extent):
    from PIL import Image
    with Image.open(path) as image:
        require(image.format == 'PNG' and image.size == tuple(extent), 'Invalid PNG extent/format')
        image.verify()
    with Image.open(path) as image:
        image.load()


def hair_unsupported(loaded, snapshot):
    diagnostics = loaded.get('diagnostics', [])
    return (loaded.get('ok') is True and snapshot.get('actor_count') == 0 and snapshot.get('actors') == []
            and any(d.get('code') == 'UNSUPPORTED_SHAPE' and d.get('shape_type') == 'hair' and d.get('count', 0) > 0 for d in diagnostics))


def hair_support_changed(loaded, snapshot):
    actors = snapshot.get('actors')
    return (loaded.get('ok') is True and isinstance(actors, list) and len(actors) > 0
            and snapshot.get('actor_count') == len(actors)
            and not any(d.get('code') == 'UNSUPPORTED_SHAPE' and d.get('shape_type') == 'hair' for d in loaded.get('diagnostics', [])))


def current_log_names(text):
    return text.replace('Falcor DI', 'ReSTIR DI').replace('RTXPT GI', 'ReSTIR GI')


def dragon_unsupported(scene_id, mode, emitters, logs):
    logs = current_log_names(logs)
    lines = logs.splitlines()
    source_continuations = set()
    for index, line in enumerate(lines[:-2]):
        entry = re.match(r'(\[[^\]]+\]\[\d+\]\[ERROR\]\[logging\.cpp:\d+\] )' + re.escape(REQUIRES_DI), line)
        if entry:
            prefix = entry[1]
            # The native logger splits one error into three same-event lines.
            # Accept only adjacent Source/path continuations of this known error.
            if (lines[index + 1] == prefix + '    Source: '
                    and lines[index + 2].startswith(prefix)
                    and re.fullmatch(r'(?:[A-Za-z]:[\\/]|/).+\.(?:cpp|h):\d+', lines[index + 2][len(prefix):])):
                source_continuations.update((index + 1, index + 2))
    unexpected_errors = [line for index, line in enumerate(lines)
        if index not in source_continuations
        and re.search(r'\[(?:ERROR|FATAL|CRITICAL)\]|(?:^|\s)(?:ERROR|FATAL|CRITICAL):', line)
        and REQUIRES_DI not in line
        and not ('[SignalHandler:' in line and 'EXCEPTION_ACCESS_VIOLATION (Code: 3221225477)' in line)]
    return (scene_id == 'dragon' and mode == 'gi' and set(emitters) == {'directional'}
            and NO_LIGHT_POWER in logs and REQUIRES_DI in logs and not unexpected_errors)


def confirm_thermal_protection():
    result = subprocess.run(['nvidia-smi', '-q', '-x'], capture_output=True, text=True, check=True, timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    root = ET.fromstring(result.stdout)
    driver_version = root.findtext('driver_version')
    require(driver_version and driver_version != 'N/A', 'Driver version missing from NVIDIA query')
    devices = []
    for gpu in root.findall('gpu'):
        slowdown = gpu.findtext('temperature/gpu_temp_slow_threshold', 'N/A')
        shutdown = gpu.findtext('temperature/gpu_temp_max_threshold', 'N/A')
        require(re.fullmatch(r'\d+ C', slowdown) and re.fullmatch(r'\d+ C', shutdown), 'Driver did not expose thermal slowdown/shutdown thresholds')
        devices.append({'name': gpu.findtext('product_name'), 'uuid': gpu.findtext('uuid'), 'driver_version': driver_version, 'slowdown': slowdown, 'shutdown': shutdown, 'current_temperature': gpu.findtext('temperature/gpu_temp')})
    require(devices, 'No NVIDIA GPU reported by read-only driver query')
    return devices


def build_environment(recipe, output, engine, runtime_dirs, port, base=None):
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items() if not k.startswith(CLEAN_PREFIXES) and k not in ('CORONA_OPTICS_MAX_FPS', 'CORONA_BATCH_CAPTURE_DIR', 'CORONA_OPTICS_MEASURE_RENDER_TIME')}
    env.update({k: v.replace('{output}', str(output)) for k, v in recipe['environment'].items()})
    env.update(CORONA_CEF_REMOTE_DEBUG_PORT=str(port), CORONA_CEF_ROOT_CACHE=str(output / 'cef-cache'), CORONA_SWITCH_PROFILE='0', DISABLE_RTSS_LAYER='1', DISABLE_VULKAN_OBS_CAPTURE='1')
    env['PATH'] = os.pathsep.join(dict.fromkeys([str(engine.parent), *(str(p) for p in runtime_dirs), env.get('PATH', '')]))
    return env


class Frontend:
    """Small local-only CDP transport for the deployed native CEF bridge."""
    def __init__(self, port):
        import websocket
        with urlopen(f'http://127.0.0.1:{port}/json/list', timeout=3) as response:
            targets = [item for item in json.load(response) if item['type'] == 'page']
        require(len(targets) == 1 and '/CabbageEditor/Frontend/' in targets[0]['url'], 'Endpoint is not the sole engine frontend')
        self.socket = websocket.create_connection(targets[0]['webSocketDebuggerUrl'], timeout=20, suppress_origin=True)
        self.sequence = 0
        self.call('Page.enable')

    def call(self, method, params=None):
        self.sequence += 1
        self.socket.send(json.dumps({'id': self.sequence, 'method': method, 'params': params or {}}))
        while True:
            response = json.loads(self.socket.recv())
            if response.get('id') == self.sequence:
                if 'error' in response:
                    raise RuntimeError(str(response['error']))
                return response.get('result', {})
            if response.get('method') == 'Page.javascriptDialogOpening':
                raise RuntimeError('Frontend dialog: ' + str(response.get('params')))


class Session:
    def __init__(self, frontend, process, log):
        self.frontend, self.process, self.log = frontend, process, log

    def evaluate(self, expression, timeout=30):
        self.frontend.socket.settimeout(timeout + 10)
        response = self.frontend.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True, 'awaitPromise': True, 'timeout': timeout * 1000, 'userGesture': True})
        if 'exceptionDetails' in response:
            raise RuntimeError(str(response['exceptionDetails']))
        return response.get('result', {}).get('value')

    def api(self, name, args, timeout=30):
        request = json.dumps({'api': name, 'args': args})
        response = self.evaluate('new Promise((resolve,reject)=>window.cefQuery({request:' + json.dumps(request) + ',persistent:false,onSuccess:r=>resolve(JSON.parse(r)),onFailure:(c,m)=>reject(new Error(c+":"+m))}))', timeout)
        with self.log.open('a', encoding='utf8') as stream:
            stream.write(json.dumps({'api': name, 'args': args, 'response': response}, ensure_ascii=False) + '\n')
        data = response.get('data', response)
        if name == 'ProjectLauncher.open_project' and response.get('success') and data.get('status') in ('initializing', 'service_initializing'):
            return data
        if not response.get('success') or (isinstance(data, dict) and (data.get('status') in ('error', 'failed') or data.get('ok') is False)):
            raise RuntimeError(name + ': ' + json.dumps(response, ensure_ascii=False))
        return data

    def until(self, name, args, ready, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError('Owned engine exited; see engine.log')
            value = self.api(name, args)
            if ready(value):
                return value
        raise TimeoutError(name + ' did not become ready')


VIEWPORT_GATE = """(()=>{
const bridge=window.coronaBridge;
if(!bridge?.setCameraViewport)throw Error('Viewport bridge unavailable');
window.__visionBenchViewportReady=false;window.__visionBenchViewportRequests=[];
bridge.setCameraViewport=(...args)=>{window.__visionBenchViewportRequests.push(args);return true;};
window.__visionBenchAlerts=[];
window.alert=message=>{window.__visionBenchAlerts.push({message:String(message),utc:new Date().toISOString()});};
return true})()"""


def reject_load_errors(value, *, expected_codes=()):
    if value.get('failed', 0) or value.get('unresolved_actor_count', 0):
        raise RuntimeError('Asset loading failed/unresolved: ' + json.dumps(value))
    for item in value.get('diagnostics', []):
        if str(item.get('severity', '')).lower() in ('error', 'fatal') or any(token in str(item.get('code', '')).upper() for token in ('MISSING', 'NOT_FOUND', 'FAILED')):
            raise RuntimeError('Importer/resource error: ' + json.dumps(item))
        require(item.get('code') in expected_codes, 'Unexpected importer diagnostic: ' + json.dumps(item))


def screenshot(session, route, identity, output, frames, timeout):
    return session.api('SceneTools.save_screenshot', [route, str(output / 'image.png'), identity, {'render_frames': frames, 'warmup_frames': 0, 'timings_path': str(output / 'frames.csv'), 'timeout_seconds': timeout}], timeout + 20)


def integrator_configuration_operations(recipe, route, identity):
    integrator = 'restir' if recipe['id'] == 'gi' else 'pt'
    settings = recipe['GI_settings'] if recipe['id'] == 'gi' else recipe['PT_settings']
    operations = []
    if recipe.get('media_contract') and integrator == 'restir':
        operations.append(('set_pt_settings', [route, identity, recipe['PT_settings']],
                           {'vision_pt_settings': recipe['PT_settings']}))
    operations.append((f'set_{integrator}_settings', [route, identity, settings],
                       {f'vision_{integrator}_settings': settings}))
    return operations


def read_capture_effective(session, method, route, identity, recipe, timeout):
    def ready(value):
        if value.get('ready') is not True:
            return False
        if recipe.get('media_contract', {}).get('diagnostics'):
            state = value.get('effective', value.get('settings', {})).get('medium_diagnostics_state', {})
            return state.get('sampled_frames') == recipe['frames']
        return True
    return session.until(method, [route, identity], ready, timeout)


def open_scene(session, scene, record, timeout):
    session.evaluate(VIEWPORT_GATE)
    session.until('ProjectLauncher.get_project_load_status', [], lambda v: v.get('archive_service_ready'), timeout)
    loaded = session.until('ProjectLauncher.open_project', [str(scene['source']), {'load_policy': 'degraded'}], lambda v: v.get('status') not in ('initializing', 'service_initializing'), timeout)
    record['load'] = loaded
    reject_load_errors(loaded, expected_codes=scene.get('expected_diagnostics', []))
    require(loaded.get('ok') is True and isinstance(loaded.get('scene'), dict), 'Project open did not produce a scene')
    route = loaded['scene']['path']
    record['route'] = route
    if scene['id'] in HAIR_SCENES:
        snapshot = session.api('SceneTools.get_scene_snapshot', [route])
        record['scene_snapshot'] = snapshot
        if hair_unsupported(loaded, snapshot):
            record['support_evidence'] = {'kind': 'unsupported-hair', 'diagnostics': loaded['diagnostics'], 'actor_count': snapshot['actor_count'], 'actors': snapshot['actors']}
            return False
        # A changed import result needs human review, not a static skip.
        record['support_observation_changed'] = hair_support_changed(loaded, snapshot)
        require(record['support_observation_changed'], 'Hair import returned neither matching unsupported evidence nor supported actors')
    cameras = session.api('SceneTools.list_camera_views', [route])['cameras']
    require(len(cameras) == 1 and (cameras[0].get('camera_id') or cameras[0].get('id')) == 'source-camera', 'Expected sole original SourceCamera')
    original, identity = cameras[0], 'source-camera'
    pose = {k: original[k] for k in POSE_FIELDS}
    frozen = load_json(ROOT / 'scenes/cameras.json')['scenes'][scene['id']]['camera']
    require(pose == frozen, 'Original camera differs from frozen numeric pose')
    record.update(camera_id=identity, camera_source=original, source_camera_numeric=pose)
    return True


def configure_mode(session, scene, recipe, output, record, timeout, *, initialize_view):
    route, identity = record['route'], record['camera_id']
    if recipe.get('media_contract'):
        snapshot = session.api('SceneTools.get_scene_snapshot', [route])
        record['scene_snapshot'] = snapshot
        record['authored_media'] = snapshot.get('media')
        require(isinstance(record['authored_media'], dict) and record['authored_media'].get('schema') == 1,
                'Imported scene did not preserve authored media')
        require(len(record['authored_media'].get('mediums', [])) == recipe['media_contract']['imported_media_count'],
                'Imported medium definition count differs')
        require(len(record['authored_media'].get('boundaries', [])) == 1, 'Imported null boundary count differs')
        require(not any(d.get('code') == 'MEDIUM_DEFERRED' for d in record['load'].get('diagnostics', [])),
                'Supported medium was deferred during import')
    expected = dict(record['source_camera_numeric'])
    def camera():
        return session.until('SceneTools.list_camera_views', [route], lambda v: len(v.get('cameras', [])) == 1 and (v['cameras'][0].get('camera_id') or v['cameras'][0].get('id')) == identity and subset_equal(v['cameras'][0], expected), timeout)['cameras'][0]
    operations = [
        ('close_camera_view', [route, identity], {'view_open': False}),
        ('set_vision_denoiser', [route, identity, 'reblur'], {'vision_denoiser': 'reblur'}),
        ('set_vision_denoise', [route, identity, recipe['denoise']], {'vision_denoise': recipe['denoise']}),
        ('set_vision_accumulation', [route, identity, recipe['accumulation']], {'vision_accumulation': recipe['accumulation']}),
        ('set_vision_stable_planes', [route, identity, recipe['stable_planes']], {'vision_stable_planes': recipe['stable_planes']}),
        ('set_render_backend', ['vision', route, identity], {'render_backend': 'vision'}),
        ('set_vision_render_mode', [route, identity, recipe['render_mode']], {'vision_render_mode': recipe['render_mode']}),
        ('set_output_mode', [route, identity, 'final_color'], {'output_mode': 'final_color'}),
        ('update_camera_view', [route, identity, {'width': recipe['resolution'][0], 'height': recipe['resolution'][1]}], {'width': recipe['resolution'][0], 'height': recipe['resolution'][1]}),
    ]
    integrator = 'restir' if recipe['id'] == 'gi' else 'pt'
    operations.extend(integrator_configuration_operations(recipe, route, identity))
    operations.append(('set_reblur_settings', [route, identity, recipe['reblur']], {'vision_reblur_settings': recipe['reblur']}))
    for method, args, fields in operations:
        session.api('SceneTools.' + method, args)
        expected.update(fields)
        camera()
    def assets(value):
        reject_load_errors(value, expected_codes=scene.get('expected_diagnostics', []))
        return value.get('active') and not value.get('loading') and not value.get('pending') and value.get('total', 0) > 0
    record['load_status'] = session.until('ProjectLauncher.get_project_load_status', [], assets, timeout)
    if initialize_view:
        session.api('MainView.on_init', [str(scene['source'])])
        session.evaluate("(()=>{const p=" + json.dumps(str(scene['source'])) + ";localStorage.setItem('corona.activeProjectPath',p);window.dispatchEvent(new CustomEvent('corona-active-project-changed',{detail:{projectPath:p}}));location.hash='#/';return true})()")
        record['load_status'] = session.until('ProjectLauncher.get_project_load_status', [], assets, timeout)
    session.api('SceneTools.close_camera_view', [route, identity]); camera()
    setup = output / 'setup'; setup.mkdir()
    record['phase'] = 'setup-screenshot'
    record['setup_capture'] = screenshot(session, route, identity, setup, 1, timeout)
    record['phase'] = 'setup-validation'
    validate_png(setup / 'image.png', recipe['resolution']); validate_frames(setup, 1, recipe['denoise'])
    (setup / 'image.png').unlink()  # Validation-only trial image; never a retained reference.
    method = f'SceneTools.get_{integrator}_settings'
    record['effective_before'] = session.until(method, [route, identity], lambda v: v.get('ready') is True, timeout)
    validate_effective_settings(record['effective_before'], recipe)
    record['effective_reblur_before'] = session.api('SceneTools.get_reblur_settings', [route, identity])
    require(record['effective_reblur_before']['settings'] == recipe['reblur'], 'Effective parent REBLUR settings differ')
    record['camera_before'] = camera()
    record['environment_before'] = session.api('SceneTools.get_environment', [route])
    record['viewport_gate_before'] = session.evaluate('({ready:window.__visionBenchViewportReady,requests:window.__visionBenchViewportRequests})')
    require(record['viewport_gate_before']['ready'] is False, 'Viewport gate opened')
    return route, identity, camera, method


def configure(session, scene, recipe, output, record, timeout):
    if open_scene(session, scene, record, timeout):
        return configure_mode(session, scene, recipe, output, record, timeout, initialize_view=True)
    return None


def runtime_hashes(engine, runtime_dirs):
    result = {'executable': sha256(engine)}
    for index, directory in enumerate([engine.parent, *runtime_dirs]):
        for path in sorted(directory.glob('*.dll')):
            result[f'dir{index}/{path.name}'] = sha256(path)
    return result


def scene_environment(recipes):
    gi_only = {'VISION_RESTIR_PRIMARY_JITTER', 'VISION_RESTIR_DI_MODE',
               'VISION_GI_NEE_DIAGNOSTIC', 'VISION_RESTIR_GI_LIGHT_FEEDBACK'}
    keys = set().union(*(r['environment'] for r in recipes))
    merged = {}
    for name in sorted(keys):
        if name == 'VISION_DISABLE_DENOISER':
            require(all(name not in r['environment'] or (not r['denoise'] and r['environment'][name] == '1')
                        for r in recipes), 'Incompatible shared engine environment: ' + name)
            # The per-camera denoise API switches this off for the pure PT recipe.
            continue
        relevant = [r for r in recipes if (name not in gi_only or r['id'] == 'gi')
                    and (name != 'VISION_PT_PRIMARY_JITTER' or r['id'] != 'gi')]
        values = {r['environment'].get(name) for r in relevant}
        require(len(values) <= 1, 'Incompatible shared engine environment: ' + name)
        if values and None not in values:
            merged[name] = values.pop()
    return merged


def capture(scene, algorithm, engine, output, *, runtime_dirs=(), timeout=7200, port=9980):
    """Capture one recipe in a fresh engine process."""
    return capture_scene(scene, [(algorithm, output)], engine, runtime_dirs=runtime_dirs,
                         timeout=timeout, port=port)[algorithm['id']]


def capture_scene(scene, jobs, engine, *, runtime_dirs=(), timeout=7200, port=9980):
    """Open a project once, run the selected recipes, then seal their capture evidence."""
    engine = Path(engine).resolve()
    runtime_dirs = [Path(p).resolve() for p in runtime_dirs]
    jobs = [(json.loads(json.dumps(recipe)), Path(output).resolve()) for recipe, output in jobs]
    if not jobs:
        return {}
    require(len({r['id'] for r, _ in jobs}) == len(jobs), 'Duplicate algorithm in scene capture')
    require(len({output for _, output in jobs}) == len(jobs), 'Duplicate output in scene capture')
    for _, output in jobs:
        if output.exists() and any(output.iterdir()):
            raise FileExistsError('Preserving nonempty capture output: ' + str(output))
    environment = (scene_environment([r for r, _ in jobs]) if len(jobs) > 1
                   else jobs[0][0]['environment'])
    for _, output in jobs:
        output.mkdir(parents=True, exist_ok=True)
    if len(jobs) == 1:
        return run_capture_jobs(scene, jobs, engine, runtime_dirs, timeout, port, jobs[0][1], environment)
    with tempfile.TemporaryDirectory(prefix='session-', dir=jobs[0][1], ignore_cleanup_errors=True) as directory:
        return run_capture_jobs(scene, jobs, engine, runtime_dirs, timeout, port, Path(directory), environment)


def run_capture_jobs(scene, jobs, engine, runtime_dirs, timeout, port, journal, environment):
    started = time.monotonic()
    shared = len(jobs) > 1
    common = {'schema': 1, 'scene_id': scene['id'], 'status': 'failed', 'engine': str(engine), 'engine_cwd': str(engine.parent), 'runtime_dirs': [str(p) for p in runtime_dirs], 'started_utc': datetime.now(timezone.utc).isoformat(), 'owned_engine_closed': False, 'offline_pacing': 'One setup frame, then one continuous screenshot batch; no frame cap, sleeps, cooldown or thermal feedback', 'history_reset': 'Each public save_screenshot invalidates history; verify exact integrator frames 1..N'}
    records = {recipe['id']: dict(common, algorithm_id=recipe['id'], configuration=recipe,
                                  evidence=['api.jsonl', 'engine.log']) for recipe, _ in jobs}
    process = frontend = session = None
    ini = engine.with_name('CoronaEditor.ini')
    ini_before = ini.read_bytes() if ini.is_file() else None
    logdir = engine.parent / 'logs'
    logs_before = {p: p.stat().st_mtime_ns for p in logdir.glob('*_corona.log')}
    captured, unsupported_hair = set(), set()
    active = failed = None
    mode_seconds = {}
    try:
        require(engine.is_file(), 'Engine executable missing')
        require(all(p.is_dir() for p in runtime_dirs), 'Explicit runtime directory missing')
        source = Path(scene['source']).resolve()
        common['source_scene_sha256'] = sha256(source)
        require(not scene.get('source_sha256') or common['source_scene_sha256'] == scene['source_sha256'], 'Scene source hash drift')
        source_tree = ET.parse(source).getroot()
        common['source_emitter_types'] = sorted({item.get('type') for item in source_tree.iter('emitter')})
        sensor = source_tree.find('sensor')
        common['source_sensor_xml'] = ET.tostring(sensor, encoding='unicode') if sensor is not None else None
        common['runtime_sha256'] = runtime_hashes(engine, runtime_dirs)
        common['thermal'] = confirm_thermal_protection()
        require(1 <= port <= 65535, 'Invalid debug port')
        with socket.socket() as probe:
            if os.name == 'nt': probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(('127.0.0.1', port))
        env = build_environment({'environment': environment}, journal, engine, runtime_dirs, port)
        common['environment'] = {k: v for k, v in env.items() if k.startswith(CLEAN_PREFIXES) or k == 'CORONA_OPTICS_MEASURE_RENDER_TIME'}
        common['process_environment'] = {k: env[k] for k in ('PATH', 'CORONA_CEF_REMOTE_DEBUG_PORT', 'CORONA_CEF_ROOT_CACHE', 'CORONA_SWITCH_PROFILE', 'DISABLE_RTSS_LAYER', 'DISABLE_VULKAN_OBS_CAPTURE')}
        for recipe, output in jobs:
            records[recipe['id']].update(common)
            save_json(output / 'run.json', records[recipe['id']])
        with (journal / 'engine.log').open('w', encoding='utf8') as log:
            process = subprocess.Popen([str(engine)], cwd=engine.parent, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        common['pid'] = process.pid
        for recipe, output in jobs:
            record = records[recipe['id']]
            record['pid'] = process.pid
            if shared:
                record['scene_session_algorithms'] = list(records)
            save_json(output / 'run.json', record)
        deadline = time.monotonic() + min(timeout, 150)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Engine exited during startup')
            try:
                frontend = Frontend(port)
                session = Session(frontend, process, journal / 'api.jsonl')
                if session.evaluate('Boolean(window.cefQuery)'): break
            except (ConnectionError, OSError, RuntimeError, ValueError):
                pass
            if frontend: frontend.socket.close()
            frontend = session = None
            time.sleep(.1)  # Connection startup polling only, never frame pacing.
        else:
            raise TimeoutError('CEF startup timed out')
        if shared:
            loaded = {'phase': 'configure'}
            try:
                supported = open_scene(session, scene, loaded, timeout)
            finally:
                for recipe, _ in jobs:
                    records[recipe['id']].update(loaded)
                if (journal / 'api.jsonl').is_file():
                    shutil.copyfile(journal / 'api.jsonl', jobs[0][1] / 'api.jsonl')
            if not supported:
                unsupported_hair.update(records)
        for index, (recipe, output) in enumerate(jobs):
            active = recipe['id']
            if active in unsupported_hair:
                continue
            record = records[active]
            mode_started = time.monotonic()
            record['phase'] = 'configure'
            if shared:
                session.log = output / 'api.jsonl'
                configured = configure_mode(session, scene, recipe, output, record, timeout, initialize_view=index == 0)
            else:
                configured = configure(session, scene, recipe, output, record, timeout)
            save_json(output / 'run.json', record)
            if configured is None:
                unsupported_hair.add(active)
            else:
                capture_image(session, configured, recipe, output, record, timeout)
                captured.add(active)
            mode_seconds[active] = time.monotonic() - mode_started
    except Exception as error:
        failed = error
        for mode, record in records.items():
            if mode in captured or mode in unsupported_hair:
                continue
            if active is None or active == mode:
                record.update(error=str(error) or repr(error), error_type=type(error).__name__,
                              failure_phase=record.get('phase', 'startup'))
            else:
                record.update(error=f'Scene session stopped during {active}: {error}', failure_phase='scene-session')
    finally:
        if process is not None:
            if process.poll() is None and session is not None:
                try: session.api('CoronaEditor.close_process', [], 10)
                except Exception as error: common['shutdown_rpc_disconnect'] = str(error)
            try:
                process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                process.terminate()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=10)
                common['forced_shutdown'] = True
            common['exit_code'] = process.returncode
            common['owned_engine_closed'] = process.poll() is not None
            newlogs = [p for p in logdir.glob('*_corona.log') if p not in logs_before or p.stat().st_mtime_ns != logs_before[p]]
            if newlogs:
                shutil.copy2(max(newlogs, key=lambda p: p.stat().st_mtime_ns), journal / 'engine-native.log')
            if ini_before is not None: ini.write_bytes(ini_before)
            elif ini.exists(): ini.unlink()
        else:
            common['owned_engine_closed'] = True  # No process was created.
        if frontend: frontend.socket.close()
        integrity_error = None
        try:
            if 'runtime_sha256' in common:
                common['runtime_sha256_after'] = runtime_hashes(engine, runtime_dirs)
                common['source_scene_sha256_after'] = sha256(scene['source'])
                require(common['runtime_sha256_after'] == common['runtime_sha256'] and common['source_scene_sha256_after'] == common['source_scene_sha256'], 'Runtime/source changed during capture')
            if shared and captured:
                partition_gpu_profiles(journal, [(output, r['frames']) for r, output in jobs if r['id'] in captured])
        except Exception as error:
            integrity_error = error
        elapsed = time.monotonic() - started
        for recipe, output in jobs:
            mode = recipe['id']
            record = records[mode]
            record.update(common)
            record['elapsed_seconds'] = mode_seconds.get(mode, 0.0) if shared else elapsed
            if shared:
                record['scene_session_elapsed_seconds'] = elapsed
                for name in ('engine.log', 'engine-native.log'):
                    if (journal / name).is_file():
                        shutil.copyfile(journal / name, output / name)
            finalize_capture(scene, recipe, output, record, captured=mode in captured,
                             unsupported_hair=mode in unsupported_hair,
                             error=failed if active == mode else None, integrity_error=integrity_error)
    return records


def capture_image(session, configured, recipe, output, record, timeout):
    route, identity, camera, method = configured
    record['phase'] = 'capture'
    record['capture'] = screenshot(session, route, identity, output, recipe['frames'], timeout)
    validate_png(output / 'image.png', recipe['resolution'])
    validate_frames(output, recipe['frames'], recipe['denoise'])
    record['camera_after'] = camera()
    record['effective_after'] = read_capture_effective(session, method, route, identity, recipe, timeout)
    validate_effective_settings(record['effective_after'], recipe)
    if recipe.get('media_contract'):
        record['media_evidence'] = validate_media_capture(record['effective_after'], recipe)
        record['instrumented_timing'] = recipe['media_contract']['diagnostics']
        record['timing_scope'] = ('Media instrumentation enabled; elapsed time is diagnostic evidence and is not a performance baseline'
                                  if record['instrumented_timing'] else 'Medium instrumentation disabled')
    record['effective_reblur_after'] = session.api('SceneTools.get_reblur_settings', [route, identity])
    record['environment_after'] = session.api('SceneTools.get_environment', [route])
    require(record['camera_before'] == record['camera_after'] and
            validate_effective_settings(record['effective_before'], recipe) ==
            validate_effective_settings(record['effective_after'], recipe), 'Camera/integrator settings changed during capture')
    require(record['environment_before'] == record['environment_after'], 'Scene environment changed during capture')
    if recipe['id'] == 'gi':
        record['actual_context_lifecycle'] = validate_context_evolution(record['effective_reblur_before'], record['effective_reblur_after'], recipe['reblur'])
    else:
        require(record['effective_reblur_before'] == record['effective_reblur_after'] and record['effective_reblur_before']['contexts'] == {}, 'PT REBLUR effective settings/contexts changed')
    record['viewport_gate_after'] = session.evaluate('({ready:window.__visionBenchViewportReady,requests:window.__visionBenchViewportRequests})')
    require(record['viewport_gate_after']['ready'] is False, 'Viewport gate opened')


def finalize_capture(scene, recipe, output, record, *, captured, unsupported_hair, error, integrity_error):
    try:
        if integrity_error is not None:
            raise integrity_error
        logs = '\n'.join(p.read_text(encoding='utf8', errors='replace') for p in (output / 'engine.log', output / 'engine-native.log') if p.is_file())
        expected = scene.get('expected_unsupported', {}).get(recipe['id'])
        # Native ReSTIR DI termination can disconnect or time out the setup RPC.
        # Unrelated validation failures must not be excused by native log text.
        from websocket import WebSocketConnectionClosedException, WebSocketTimeoutException
        setup_transport_failure = (record.get('phase') == 'setup-screenshot'
            and isinstance(error, (ConnectionError, TimeoutError,
                                   WebSocketConnectionClosedException, WebSocketTimeoutException)))
        light_unsupported = (setup_transport_failure and bool(record.get('pid'))
            and record['owned_engine_closed'] and not record.get('forced_shutdown')
            and dragon_unsupported(scene['id'], recipe['id'], record.get('source_emitter_types', []), logs))
        if unsupported_hair or light_unsupported:
            if unsupported_hair:
                require(record.get('exit_code') == 0 and not record.get('forced_shutdown'), 'Hair import attempt did not close cleanly')
            else:
                witnesses = [line for line in logs.splitlines()
                             if any(marker in current_log_names(line) for marker in (NO_LIGHT_POWER, REQUIRES_DI))]
                record['support_evidence'] = {'kind': 'unsupported-directional-light', 'source_emitter_types': record['source_emitter_types'], 'log_witnesses': witnesses, 'engine_exit_code': record['exit_code'], 'failure_phase': record['failure_phase'], 'route': record['route'], 'error_type': record['error_type']}
            record.update(status='expected-unsupported' if expected else 'support-changed', reason=expected or record['support_evidence']['kind'])
        elif captured:
            require(record['owned_engine_closed'] and record.get('exit_code') == 0 and not record.get('forced_shutdown'), 'Capture engine did not close cleanly')
            mapping = map_gpu_frames(output, recipe['frames'])
            timing = summarize_timing(output, mapping, recipe)
            if recipe['id'] == 'gi':
                require(all(marker in logs for marker in GI_LOG_MARKERS) and 'ReSTIR DI unavailable' not in logs, 'GI default route log evidence differs')
            save_json(output / 'gpu-frame-map.json', mapping)
            save_json(output / 'timing.json', timing)
            record.update(status='support-changed' if expected or record.get('support_observation_changed') else 'complete', image='image.png', image_sha256=sha256(output / 'image.png'), timing=timing)
            if expected: record['reason'] = 'Expected limitation no longer observed: ' + expected
            record['evidence'].extend(['frames.csv', 'stages.csv', 'gpu.csv', 'gpu-diagnostics.csv', 'gpu-frame-map.json', 'timing.json'])
        elif record.get('support_observation_changed'):
            record.update(status='support-changed', reason='Hair importer support differs; capture validation did not complete')
    except Exception as failure:
        record.update(status='failed', validation_error=str(failure), error=record.get('error') or str(failure))
    record['evidence'] = [p for p in record['evidence'] if (output / p).is_file()]
    if (output / 'engine-native.log').is_file(): record['evidence'].append('engine-native.log')
    record['evidence_sha256'] = {p: sha256(output / p) for p in record['evidence']}
    save_json(output / 'run.json', record)
