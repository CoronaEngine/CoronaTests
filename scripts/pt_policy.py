"""PT512 display-domain noise envelopes, bound to the PT2048 references.

Each fixed scene/ROI has a measured error floor. Only a small extra error is
allowed; independent mean-color and luminance guards catch exposure changes
even when those changes happen to reduce error against the 2048-spp image.
"""
from __future__ import annotations

import math

import numpy as np

from scripts import compare

SCALES = {'DS8': 8, 'DS4': 4}
ALLOWANCES = {'mae': .0005, 'rmse': .001, 'ssim_loss': .005,
              'mean_rgb': .0015, 'luminance_absolute': .0005, 'luminance_relative': .015}


def area_mean(pixels, factor):
    pixels = np.asarray(pixels, dtype=np.float64)
    h, w, channels = pixels.shape
    if h % factor or w % factor:
        raise ValueError('Image dimensions must be divisible by downsampling factor')
    return pixels.reshape(h // factor, factor, w // factor, factor, channels).mean(axis=(1, 3))


def measure_arrays(reference, candidate, regions):
    if reference.shape != candidate.shape:
        raise ValueError('Image sizes differ')
    result = {}
    for name, factor in SCALES.items():
        a, b = area_mean(reference, factor), area_mean(candidate, factor)
        metrics = compare._compare_arrays(a, b, regions)
        metrics.update(factor=factor, candidate_mean_rgb=b.mean(axis=(0, 1)).tolist())
        for region, bounds in regions.items():
            left, top, right, bottom = compare.region_box(bounds, b.shape[1], b.shape[0])
            metrics['regions'][region]['candidate_mean_rgb'] = b[top:bottom, left:right].mean(axis=(0, 1)).tolist()
        result[name] = metrics
    return result


def measure(reference, candidate, regions):
    return measure_arrays(compare._load(reference), compare._load(candidate), regions)


def _groups(scale):
    return [('full', scale), *scale['regions'].items()]


def _errors(metrics):
    return {key: metrics[key] for key in ('mae', 'rmse')} | {'ssim_loss': 1 - metrics['ssim_luminance']}


def case_profile(measured, reference):
    scales = {}
    for name, metrics in measured.items():
        scales[name] = {key: metrics[key] for key in ('factor', 'width', 'height')}
        scales[name]['region_bounds'] = {key: list(box) for key, box in metrics['region_bounds'].items()}
        scales[name]['observations'] = {region: {
            **_errors(values), 'mean_rgb': values['candidate_mean_rgb'],
            'mean_luminance': values['candidate_mean_luminance']}
            for region, values in _groups(metrics)}
    return {'reference_sha256': reference['image_sha256'],
            'reference_input_fingerprint': reference['input_fingerprint'], 'scales': scales}


def _number(value, low, high):
    return (type(value) in (int, float) and math.isfinite(value) and low <= value <= high)


def _validate_case(case):
    if set(case['scales']) != set(SCALES):
        raise ValueError('Both DS8 and DS4 are required')
    for name, factor in SCALES.items():
        scale = case['scales'][name]
        if scale['factor'] != factor or any(type(scale[k]) is not int or scale[k] < 1 for k in ('width', 'height')):
            raise ValueError('Invalid scale dimensions or factor')
        bounds = scale['region_bounds']
        if not isinstance(bounds, dict) or not bounds or set(scale['observations']) != {'full', *bounds}:
            raise ValueError('Incomplete fixed ROI definitions')
        for box in bounds.values():
            compare.region_box(box, scale['width'], scale['height'])
        for observation in scale['observations'].values():
            if (set(observation) != {'mae', 'rmse', 'ssim_loss', 'mean_rgb', 'mean_luminance'}
                    or any(not _number(observation[k], 0, 2 if k == 'ssim_loss' else 1)
                           for k in ('mae', 'rmse', 'ssim_loss', 'mean_luminance'))
                    or len(observation['mean_rgb']) != 3
                    or any(not _number(x, 0, 1) for x in observation['mean_rgb'])):
                raise ValueError('Invalid noise-floor observation')
    a, b = (case['scales'][name] for name in SCALES)
    if (a['width'] * a['factor'], a['height'] * a['factor'], a['region_bounds']) != (
            b['width'] * b['factor'], b['height'] * b['factor'], b['region_bounds']):
        raise ValueError('Scale dimensions or ROI definitions differ')


def _validate_allowances(allowances):
    if set(allowances) != set(ALLOWANCES) or any(
            not _number(allowances[k], 0, limit) for k, limit in ALLOWANCES.items()):
        raise ValueError('Allowances exceed the PT512 policy limits')


def validate_references(policy, references):
    """Reject stale profiles before starting the GPU; no engine-version lock."""
    if (policy.get('schema') != 1 or policy.get('id') != 'pt512-ds8-ds4-v1'
            or policy.get('candidate_spp') != 512 or policy.get('reference_spp') != 2048):
        raise ValueError('Invalid PT512 reference policy')
    try:
        _validate_allowances(policy['allowances'])
        for row in references:
            case = policy['cases'][row['job_id']]
            if (case['reference_sha256'] != row['image_sha256']
                    or case['reference_input_fingerprint'] != row['input_fingerprint']):
                raise ValueError('PT512 reference image or input fingerprint changed: ' + row['job_id'])
            _validate_case(case)
            if row.get('regions') and any(scale['region_bounds'] != row['regions'] for scale in case['scales'].values()):
                raise ValueError('PT512 reference ROI definitions changed: ' + row['job_id'])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('Invalid PT512 reference calibration: ' + str(exc)) from exc


def evaluate(measured, case, allowances):
    """Every scale and every fixed ROI must satisfy every check."""
    failures, checks, scale_verdicts = [], {}, {}
    try:
        _validate_case(case)
        _validate_allowances(allowances)
        if set(measured) != set(SCALES):
            raise ValueError('Both DS8 and DS4 comparison evidence are required')
        for name, factor in SCALES.items():
            actual, expected = measured[name], case['scales'][name]
            if (any(actual[k] != expected[k] for k in ('factor', 'width', 'height', 'region_bounds'))
                    or set(actual['regions']) != set(expected['region_bounds'])):
                raise ValueError(name + ': scale or ROI evidence differs')
            checks[name] = {}
            before = len(failures)
            for region, values in _groups(actual):
                floor = expected['observations'][region]
                errors = _errors(values)
                rgb = values['candidate_mean_rgb']
                lum = values['candidate_mean_luminance']
                if (len(rgb) != 3 or any(not _number(x, 0, 1) for x in rgb)
                        or not _number(lum, 0, 1)
                        or any(not _number(x, 0, 2) for x in errors.values())):
                    raise ValueError(name + '.' + region + ': invalid numeric evidence')
                observed = {**errors, 'mean_rgb': max(abs(x-y) for x, y in zip(rgb, floor['mean_rgb'])),
                            'luminance': abs(lum - floor['mean_luminance'])}
                limits = {k: floor[k] + allowances[k] for k in errors}
                limits.update(mean_rgb=allowances['mean_rgb'], luminance=max(
                    allowances['luminance_absolute'], floor['mean_luminance'] * allowances['luminance_relative']))
                checks[name][region] = {k: {'value': value, 'limit': limits[k], 'passed': value <= limits[k] + 1e-12}
                                        for k, value in observed.items()}
                failures.extend(f'{name}.{region}.{k}: {v["value"]:.6g} exceeds {v["limit"]:.6g}'
                                for k, v in checks[name][region].items() if not v['passed'])
            scale_verdicts[name] = 'image-failed' if len(failures) > before else 'pass'
    except (KeyError, TypeError, ValueError) as exc:
        return {'status': 'uncalibrated', 'failures': ['Incomplete PT512 evidence: ' + str(exc)]}
    return {'status': 'image-failed' if failures else 'pass', 'failures': failures,
            'scale_verdicts': scale_verdicts, 'checks': checks}
