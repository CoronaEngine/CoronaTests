"""Normalized display comparison with fixed thresholds and legacy calibration.

Only compare the same scene and algorithm. The orchestrator verifies those identities
and records their fingerprints in profile provenance; pixel files cannot prove identity.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from PIL import Image

LUMINANCE = np.array([.2126, .7152, .0722])
DEFAULT_REGIONS = {
    'center': [.25, .25, .75, .75],
    'upper-left': [0, 0, .5, .5], 'upper-right': [.5, 0, 1, .5],
    'lower-left': [0, .5, .5, 1], 'lower-right': [.5, .5, 1, 1],
}
# Hard ceilings: noisy runs above these values do not produce a usable profile.
CAPS = {'mae': .035, 'rmse': .07, 'ssim_loss': .12, 'luminance_deviation': .12}
FLOORS = {'mae': .003, 'rmse': .006, 'ssim_loss': .015, 'luminance_deviation': .015}


def region_box(bounds, width, height):
    """Convert a normalized fixed region to a nonempty pixel rectangle."""
    if (len(bounds) != 4 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in bounds)
            or not (0 <= bounds[0] < bounds[2] <= 1 and 0 <= bounds[1] < bounds[3] <= 1)):
        raise ValueError(f'Invalid normalized region: {bounds}')
    left, top, right, bottom = bounds
    return (min(width - 1, int(left * width)), min(height - 1, int(top * height)),
            min(width, max(int(math.ceil(right * width)), int(left * width) + 1)),
            min(height, max(int(math.ceil(bottom * height)), int(top * height) + 1)))


def _load(path):
    with Image.open(path) as image:
        return np.asarray(image.convert('RGB'), dtype=np.float64) / 255


def _mean(value, radius=3):
    size = 2 * radius + 1
    padded = np.pad(value, radius, mode='reflect')
    integral = np.pad(padded, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    return (integral[size:, size:] - integral[:-size, size:] - integral[size:, :-size] + integral[:-size, :-size]) / size**2


def _ratio(reference, candidate):
    if reference == 0:
        return 1. if candidate == 0 else float(candidate / 1e-12)
    return float(candidate / reference)


def _metrics(a, b):
    x, y = a @ LUMINANCE, b @ LUMINANCE
    mx, my = _mean(x), _mean(y)
    vx, vy = np.maximum(_mean(x*x) - mx*mx, 0), np.maximum(_mean(y*y) - my*my, 0)
    covariance = _mean(x*y) - mx*my
    ssim = ((2*mx*my + .01**2) * (2*covariance + .03**2)
            / ((mx*mx + my*my + .01**2) * (vx + vy + .03**2)))
    mse = float(np.mean((a-b)**2))
    return {'mae': float(np.mean(abs(a-b))), 'rmse': float(np.sqrt(mse)),
            'psnr_db': float(-10*np.log10(max(mse, 1e-20))),
            'ssim_luminance': float(np.clip(ssim.mean(), -1, 1)),
            'reference_mean_luminance': float(x.mean()), 'candidate_mean_luminance': float(y.mean()),
            'luminance_ratio': _ratio(float(x.mean()), float(y.mean()))}


def _compare_arrays(a, b, regions):
    if a.shape != b.shape:
        raise ValueError(f'Image sizes differ: {a.shape} vs {b.shape}')
    height, width = a.shape[:2]
    result = {'width': width, 'height': height, **_metrics(a, b), 'region_bounds': regions, 'regions': {}}
    for name, bounds in regions.items():
        left, top, right, bottom = region_box(bounds, width, height)
        result['regions'][name] = _metrics(a[top:bottom, left:right], b[top:bottom, left:right])
    return result


def _read_pfm(path):
    with path.open('rb') as stream:
        if stream.readline().strip() != b'PF':
            raise ValueError(f'Expected RGB PFM: {path}')
        line = stream.readline()
        while line.startswith(b'#'):
            line = stream.readline()
        width, height = map(int, line.split())
        scale = float(stream.readline())
        if width <= 0 or height <= 0 or not math.isfinite(scale) or scale == 0:
            raise ValueError(f'Invalid PFM header: {path}')
        pixels = np.frombuffer(stream.read(), dtype='<f4' if scale < 0 else '>f4')
        if pixels.size != width * height * 3:
            raise ValueError(f'Invalid PFM payload: {path}')
        return np.flipud(pixels.reshape(height, width, 3)).astype(np.float64) * abs(scale)


def compare_images(reference: Path, candidate: Path, output: Path | None = None, *, regions: dict | None = None) -> dict:
    """Compare unmodified sRGB display pixels; difference.png is abs(RGB) x4."""
    reference, candidate = Path(reference), Path(candidate)
    a, b = _load(reference), _load(candidate)
    result = _compare_arrays(a, b, regions or DEFAULT_REGIONS)
    linear_paths = [path.with_suffix('.pfm') for path in (reference, candidate)]
    if all(path.is_file() for path in linear_paths):
        linear_a, linear_b = map(_read_pfm, linear_paths)
        if linear_a.shape != a.shape or linear_b.shape != a.shape:
            raise ValueError('Linear and display image sizes differ')
        counts = [int(np.count_nonzero(~np.isfinite(pixels))) for pixels in (linear_a, linear_b)]
        result['linear_nonfinite_values'] = dict(zip(('reference', 'candidate'), counts))
        if not any(counts):
            rmse = float(np.sqrt(np.mean((linear_a-linear_b)**2)))
            result.update(linear_rmse=rmse,
                          linear_relative_rmse=rmse / max(float(np.sqrt(np.mean(linear_a**2))), 1e-12),
                          linear_luminance_ratio=_ratio(float((linear_a @ LUMINANCE).mean()), float((linear_b @ LUMINANCE).mean())))
    if output is not None:
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        result['difference_image'] = 'difference.png'
        Image.fromarray(np.uint8(np.clip(abs(a-b)*4, 0, 1)*255)).save(output / 'difference.png')
        (output / 'comparison.json').write_text(json.dumps(result, indent=2, allow_nan=False), encoding='utf-8')
    return result


def _errors(metrics):
    return {'mae': metrics['mae'], 'rmse': metrics['rmse'], 'ssim_loss': 1 - metrics['ssim_luminance'],
            'luminance_deviation': abs(metrics['luminance_ratio'] - 1)}


def _thresholds(records):
    return {key: max(FLOORS[key], max(_errors(record)[key] for record in records) * 1.75 + .0005)
            for key in CAPS}


def fixed_profile(*, regions: dict | None = None, provenance: dict | None = None) -> dict:
    """Use the existing fixed error limits without repeated image captures."""
    regions = {name: list(bounds) for name, bounds in (regions or DEFAULT_REGIONS).items()}
    for bounds in regions.values():
        region_box(bounds, 1, 1)
    return {'schema': 1, 'status': 'fixed', 'provenance': dict(provenance or {}),
            'thresholds': {'full': dict(FLOORS), 'regions': {name: dict(FLOORS) for name in regions}},
            'region_bounds': regions, 'policy': {'method': 'fixed-thresholds', 'captures_per_job': 1},
            'reasons': []}


def evaluate(metrics: dict, profile: dict) -> dict:
    """Require fixed or historically calibrated limits and complete region metrics."""
    if profile.get('status') not in ('fixed', 'calibrated'):
        return {'status': 'uncalibrated', 'failures': profile.get('reasons', ['Calibration is incomplete'])}
    thresholds = profile.get('thresholds', {})
    region_bounds = profile.get('region_bounds')
    regional_thresholds = thresholds.get('regions') if isinstance(thresholds, dict) else None
    measured_regions = metrics.get('regions')
    if (not isinstance(region_bounds, dict) or not region_bounds
            or not isinstance(regional_thresholds, dict) or set(regional_thresholds) != set(region_bounds)
            or not isinstance(measured_regions, dict) or set(measured_regions) != set(region_bounds)
            or metrics.get('region_bounds') != region_bounds):
        return {'status': 'uncalibrated', 'failures': ['Calibrated region names and bounds do not match complete comparison evidence']}
    try:
        for bounds in region_bounds.values():
            region_box(bounds, 1, 1)
    except (TypeError, ValueError):
        return {'status': 'uncalibrated', 'failures': ['Profile contains invalid calibrated region bounds']}
    groups = [thresholds.get('full', {}), *regional_thresholds.values()]
    for group in groups:
        if not isinstance(group, dict) or set(group) != set(CAPS) or any(
                not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value) or not 0 <= value <= CAPS[key]
                for key, value in group.items()):
            return {'status': 'uncalibrated', 'failures': ['Profile thresholds violate calibration policy']}
    required_metrics = {'mae', 'rmse', 'psnr_db', 'ssim_luminance', 'reference_mean_luminance',
                        'candidate_mean_luminance', 'luminance_ratio'}
    for label, measured in [('full', metrics), *measured_regions.items()]:
        if not isinstance(measured, dict) or not required_metrics <= measured.keys() or any(
                not isinstance(measured[key], (int, float)) or isinstance(measured[key], bool)
                or not math.isfinite(measured[key])
                for key in required_metrics):
            return {'status': 'uncalibrated', 'failures': [f'{label}: complete numeric comparison metrics are required']}
    failures = []
    groups = [('full', metrics, profile['thresholds']['full'])]
    groups += [(f'regions.{name}', metrics.get('regions', {}).get(name), thresholds)
               for name, thresholds in profile['thresholds']['regions'].items()]
    for label, measured, thresholds in groups:
        if measured is None:
            failures.append(f'{label}: missing region')
            continue
        try:
            errors = _errors(measured)
        except (KeyError, TypeError):
            failures.append(f'{label}: missing metric')
            continue
        for key, limit in thresholds.items():
            value = errors[key]
            if not math.isfinite(value) or value > limit:
                failures.append(f'{label}.{key}: {value:.6g} exceeds {limit:.6g}')
    if any(metrics.get('linear_nonfinite_values', {}).values()):
        failures.append('linear_nonfinite_values: PFM contains nonfinite values')
    return {'status': 'image-failed' if failures else 'pass', 'failures': failures}


def calibrate(reference: Path, repeats: list[Path], *, regions: dict | None = None, provenance: dict | None = None) -> dict:
    """Use reference plus two fresh captures; reject uncontrolled noise/degradation."""
    regions = regions or DEFAULT_REGIONS
    profile = {'schema': 1, 'status': 'calibration-incomplete', 'thresholds': {},
               'provenance': provenance or {}, 'noise_records': [], 'controlled_degradation_checks': [],
               'reasons': [], 'policy': {'noise_multiplier': 1.75, 'ceilings': CAPS, 'floors': FLOORS},
               'region_bounds': regions}
    if len(repeats) < 2:
        profile['reasons'].append('Reference and at least two repeat captures are required')
        return profile
    paths = [Path(reference), *map(Path, repeats)]
    for i, a in enumerate(paths):
        for j in range(i + 1, len(paths)):
            profile['noise_records'].append({'pair': [i, j], **compare_images(a, paths[j], regions=regions)})
    records = profile['noise_records']
    profile['thresholds'] = {'full': _thresholds(records), 'regions': {
        name: _thresholds([record['regions'][name] for record in records]) for name in regions}}
    for name, group in [('full', profile['thresholds']['full']), *profile['thresholds']['regions'].items()]:
        for key, value in group.items():
            if value > CAPS[key]:
                profile['reasons'].append(f'{name}.{key}: empirical threshold exceeds hard ceiling {CAPS[key]}')
    if any(any(record.get('linear_nonfinite_values', {}).values()) for record in records):
        profile['reasons'].append('Calibration PFM contains nonfinite values')
    # Test prospective thresholds even when they exceed policy; never approve them.
    prospective = {**profile, 'status': 'calibrated'}
    a = _load(reference)
    occluded = a.copy()
    height, width = a.shape[:2]
    occluded[height//4:max(height//4+1, 3*height//4), width//4:max(width//4+1, 3*width//4)] = 0
    variants = {'black': np.zeros_like(a), 'center-occlusion': occluded, 'half-exposure': a*.5,
                'bright-exposure': np.clip(a*1.5, 0, 1)}
    for name, degraded in variants.items():
        measured = _compare_arrays(a, degraded, regions)
        result = evaluate(measured, prospective)
        rejected = result['status'] == 'image-failed'
        profile['controlled_degradation_checks'].append({'name': name, 'rejected': rejected, 'metrics': measured, 'failures': result['failures']})
        if not rejected:
            profile['reasons'].append(f'Controlled degradation was not rejected: {name}')
    if not profile['reasons']:
        profile['status'] = 'calibrated'
    return profile
