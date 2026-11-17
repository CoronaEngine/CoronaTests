"""Self-contained, offline scene galleries with original PNGs and timing evidence."""
from __future__ import annotations

import csv
from html import escape
import json
import math
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from PIL import Image

from scripts.compare import DEFAULT_REGIONS, region_box

ALGORITHMS = [('gi', 'ReSTIR + REBLUR'), ('pt-reblur', 'PT + REBLUR'), ('pt2048', 'PT')]
TIMING_FIELDS = ['frames', 'warmup_frames', 'shown_frame_ms', 'mean_ms', 'median_ms', 'total_ms', 'sample_ms']
CSS = """body{margin:24px;background:#11151b;color:#edf2fa;font:15px/1.5 system-ui,sans-serif}main{max-width:1920px;margin:auto}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}article{background:#1b222d;padding:12px;border-radius:8px;min-width:0}img{width:100%;height:auto;display:block}a{color:#8fc1ff}p,pre{color:#c0cede}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:350px;overflow:auto}.status{border-left:4px solid #ffcf87;padding-left:12px}section{margin:34px 0}nav a{margin-right:15px}.views{display:grid;gap:8px}figure{margin:6px 0}figcaption{font-weight:600}.zooms{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.timing{display:block;font-size:16px;color:#fff}details{margin:10px 0}code{overflow-wrap:anywhere}@media(max-width:950px){.grid{grid-template-columns:1fr}}"""


def _asset(root, value):
    """Validate archive-local links including symlinks before emitting a URL."""
    value = str(value)
    path = PurePosixPath(value)
    if (not value or '\\' in value or ':' in value or '?' in value or '#' in value
            or path.is_absolute() or '..' in path.parts):
        raise ValueError(f'Asset must be a relative path inside report: {value}')
    resolved = (root / value).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f'Asset resolves outside report: {value}')
    if not resolved.is_file():
        raise ValueError(f'Report asset is missing: {value}')
    return quote(path.as_posix(), safe='/')


def _json(value):
    return escape(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))


def _number(value, suffix=' ms'):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return 'unavailable'
    return f'{value:.3f}{suffix}'


def _timing(record):
    timing = record.get('timing') or {}
    if not timing:
        return '<strong class="timing">Timing unavailable</strong>'
    if record['algorithm_id'] == 'pt2048':
        text = f"{timing['frames']}-sample total {_number(timing.get('total_ms'))}; {_number(timing.get('sample_ms'), ' ms/sample')}"
    else:
        shown = timing.get('shown_frame_ms', timing.get('displayed_frame_ms', timing.get('last_frame_ms')))
        text = f"Shown frame {_number(shown)}; steady mean {_number(timing.get('mean_ms'))}"
    return f'<strong class="timing">{escape(text)}</strong>'


def _view(root, image, label):
    url = escape(_asset(root, image), quote=True)
    return (f'<figure><figcaption>{escape(label)}</figcaption><a href="{url}"><img loading="lazy" src="{url}" alt="{escape(label, quote=True)}"></a>'
            f'<a href="{url}" download>Download original</a></figure>')


def _zooms(root, record):
    roles = [('Candidate', record.get('image'))]
    if record.get('baseline_image'):
        roles.insert(0, ('Baseline', record['baseline_image']))
    if record.get('difference_image'):
        roles.append(('Difference x4', record['difference_image']))
    if not any(path for label, path in roles):
        return ''
    regions = record.get('regions', DEFAULT_REGIONS)
    parts = ['<details><summary>Fixed region zooms (unmodified crops)</summary>']
    for name, bounds in regions.items():
        parts.append(f'<h4>{escape(name)} <code>{escape(str(bounds))}</code></h4><div class="zooms">')
        for role, path in roles:
            if path is None:
                continue
            _asset(root, path)
            with Image.open(root / path) as source:
                width, height = source.size
                left, top, right, bottom = region_box(bounds, width, height)
            url = escape(_asset(root, path), quote=True)
            caption = escape(f'{role}: {name}')
            parts.append(f'<figure><figcaption>{caption}</figcaption>'
                         f'<svg viewBox="{left} {top} {right-left} {bottom-top}" '
                         f'style="display:block;width:100%;height:auto;aspect-ratio:{right-left}/{bottom-top}" '
                         f'role="img" aria-label="{caption}"><image href="{url}" '
                         f'width="{width}" height="{height}"/></svg></figure>')
        parts.append('</div>')
    parts.append('</details>')
    return ''.join(parts)


def _evidence(root, record):
    paths = {}
    for key in ('run_json', 'comparison_json', 'profile_json', 'timing_csv', 'passes_csv', 'metadata_json'):
        if record.get(key):
            paths[record[key]] = key.replace('_', ' ')
    # Capture writes all evidence alongside its raw image. Link only existing
    # report-local files rather than depending on the original capture location.
    if record.get('image'):
        directory = (root / record['image']).parent
        for path in sorted(directory.glob('*')):
            if path.is_file() and path.suffix.lower() in ('.json', '.csv', '.pfm'):
                paths[path.relative_to(root).as_posix()] = path.name
    return ' · '.join(f'<a href="{escape(_asset(root, path), quote=True)}">{escape(label)}</a>' for path, label in paths.items())


def _multiscale(record):
    evaluation = (record.get('comparison') or {}).get('multiscale_evaluation')
    if not evaluation:
        return ''
    parts = ['<p>PT512 vs PT2048: DS8 + DS4 and every ROI must pass. Full-resolution metrics are diagnostic only.</p>',
             '<table><tr><th>Scale</th><th>Status</th><th>MAE / limit</th><th>RMSE / limit</th><th>SSIM / minimum</th><th>ROIs passed</th></tr>']
    for scale, status in evaluation.get('scale_verdicts', {}).items():
        groups = evaluation['checks'][scale]
        full = groups['full']
        cells = [scale, status, f'{full["mae"]["value"]:.6f} / {full["mae"]["limit"]:.6f}',
                 f'{full["rmse"]["value"]:.6f} / {full["rmse"]["limit"]:.6f}',
                 f'{1-full["ssim_loss"]["value"]:.6f} / {1-full["ssim_loss"]["limit"]:.6f}',
                 f'{sum(all(v["passed"] for v in checks.values()) for name, checks in groups.items() if name != "full")} / {len(groups)-1}']
        parts.append('<tr>' + ''.join(f'<td>{escape(cell)}</td>' for cell in cells) + '</tr>')
    parts.append('</table><p>Limits = verified PT512 error floor + fixed allowance; mean color and luminance are checked independently. All ROI checks and limits are in the metrics JSON.</p>')
    return ''.join(parts)


def write_report(output: Path, report: dict, *, metadata_file='report.json') -> Path:
    """Write index.html, report metadata and timings.csv using existing images.

    Callers place capture, baseline and difference assets beneath output before
    invoking this function. Missing/nonimage records never receive placeholders.
    """
    output = Path(output)
    if metadata_file not in ('report.json', 'workflow-result.json'):
        raise ValueError('Unsupported report metadata filename')
    output.mkdir(parents=True, exist_ok=True)
    records = report['records']
    # Validate image links before writing any report artifact.
    for record in records:
        for key in ('image', 'baseline_image', 'difference_image'):
            if record.get(key):
                _asset(output, record[key])
    (output / metadata_file).write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8', newline='\n')
    fields = ['scene_id', 'algorithm_id', 'job_id', 'status', *TIMING_FIELDS, 'elapsed_seconds', 'reason']
    with (output / 'timings.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            row = {key: record.get(key, '') for key in fields}
            timing = record.get('timing') or {}
            row.update({key: timing.get(key, '') for key in TIMING_FIELDS})
            row['reason'] = record.get('reason') or record.get('error', '')
            writer.writerow(row)
    index = output / 'index.html'
    html = render_report(output, report, downloads=[(metadata_file, 'Full local metadata JSON'),
                                                  ('timings.csv', 'Timings CSV')])
    index.write_text(html, encoding='utf-8', newline='\n')
    return index


def render_report(output: Path, report: dict, *, downloads=()) -> str:
    """Render static HTML referencing existing files; never create image copies."""
    output = Path(output)
    records = report['records']
    scenes = {}
    for record in records:
        scenes.setdefault(record['scene_id'], {})[record['algorithm_id']] = record
    title = f"{'Regression' if report['kind'] == 'regression' else 'Golden baseline'} · {report['id']}"
    parts = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title><style>{CSS}</style></head><body><main>',
             f'<h1>{escape(title)}</h1><p>Suite: {escape(report["suite"])} · {escape(report["created_utc"])}</p>',
             '<p>Original display PNGs are shown without exposure adjustment or postprocessing. Difference images show absolute RGB error ×4. Timing evidence uses the capture’s measured intervals.</p>',
             '<p>' + ' · '.join(f'<a href="{escape(_asset(output, path), quote=True)}" download>{escape(label)}</a>'
                                for path, label in downloads) + '</p>',
             f'<details><summary>Hardware, engine and report metadata</summary><pre>{_json(report.get("metadata", {}))}</pre></details>',
             f'<details open><summary>{"Capture status summary" if report["kind"] == "golden" else "Status summary"}</summary><pre>{_json(report.get("summary", {}))}</pre></details>']
    if report['kind'] == 'golden' and report.get('threshold_policy') == 'fixed':
        parts.append(f'<p class="status">Capture valid: {str(report.get("valid", False)).lower()} · '
                     f'Thresholds: fixed · Ready: {str(report.get("thresholds_ready", False)).lower()} · '
                     'One capture per scene/algorithm.</p>')
    elif report['kind'] == 'golden' and 'calibrated' in report:
        calibration = report.get('calibration', {})
        status = 'calibrated' if report['calibrated'] else 'calibration-incomplete'
        parts.append(f'<p class="status">Capture valid: {str(report.get("valid", False)).lower()} · Calibration: {status}</p>')
        if calibration.get('reasons'):
            parts.append('<ul>' + ''.join(f'<li>{escape(str(reason))}</li>' for reason in calibration['reasons']) + '</ul>')
    parts.append('<nav>')
    for index, scene in enumerate(scenes):
        parts.append(f'<a href="#scene-{index}">{escape(scene)}</a>')
    parts.append('</nav>')
    for index, (scene, scene_records) in enumerate(scenes.items()):
        name = next((r.get('scene_name') for r in scene_records.values() if r.get('scene_name')), scene)
        parts.append(f'<section id="scene-{index}"><h2>{escape(name)}</h2><div class="grid">')
        for algorithm, label in ALGORITHMS:
            record = scene_records.get(algorithm)
            if record is not None and algorithm == 'pt2048':
                frames = (record.get('timing') or {}).get('frames', record.get('configuration', {}).get('frames'))
                if frames is not None:
                    label = f'PT{frames}'
            parts.append(f'<article data-scene="{escape(scene, quote=True)}" data-algorithm="{algorithm}"><h3>{label}</h3>')
            if record is None:
                parts.append('<p class="status">missing — no capture record</p></article>')
                continue
            prefix = 'Capture: ' if report['kind'] == 'golden' else ''
            parts.append(f'<p class="status">{prefix}{escape(record["status"])}</p>')
            calibration = record.get('threshold_profile') or record.get('calibration', {})
            if calibration:
                label = 'Thresholds' if calibration['status'] == 'fixed' else 'Calibration'
                parts.append(f'<p class="status">{label}: {escape(calibration["status"])}</p>')
            reasons = [record[key] for key in ('reason', 'error') if record.get(key)]
            reasons.extend(calibration.get('reasons', []))
            reasons.extend(record.get('failures', []))
            comparison = record.get('comparison') or {}
            evaluation = comparison.get('evaluation', comparison)
            reasons.extend(evaluation.get('failures', []))
            reasons = list(dict.fromkeys(map(str, reasons)))
            if reasons:
                parts.append('<ul>' + ''.join(f'<li>{escape(str(reason))}</li>' for reason in reasons) + '</ul>')
            configuration = record.get('configuration') or {}
            timing = record.get('timing') or {}
            frames = configuration.get('frames', timing.get('frames'))
            warmup = configuration.get('warmup', timing.get('warmup_frames'))
            if frames is not None and warmup is not None:
                parts.append(f'<p class="sampling">Budget: {frames} frames; {warmup} warmup; 1 spp/frame.</p>')
            sampling = record.get('sampling_comparison')
            if sampling and sampling['golden'] != sampling['candidate']:
                before, after = sampling['golden'], sampling['candidate']
                unit = 'spp' if algorithm == 'pt2048' else 'frames'
                text = (f'Golden: {before["frames"]} {unit} ({before["warmup_frames"]} warmup); '
                        f'candidate: {after["frames"]} {unit} ({after["warmup_frames"]} warmup). '
                        + ('PT512 uses the fixed DS8/DS4 noise-envelope policy.' if record.get('comparison_policy') else
                         'Golden thresholds unchanged; sampling noise counts toward image error.'))
                parts.append(f'<p class="budget-comparison">{escape(text)}</p>')
            parts.append(_multiscale(record))
            parts.append(_timing(record))
            performance = record.get('performance')
            if performance:
                text = (f'Performance (warning-only): golden mean {_number(performance["golden_mean_ms"])}'
                        f' → candidate mean {_number(record["timing"]["mean_ms"])}'
                        f' ({performance["mean_change_percent"]:+.2f}%).')
                if performance.get('machine_observation_equal') is False:
                    text += ' Hardware differs; not comparable.'
                if performance.get('sampling_budget_equal') is False:
                    text += ' Per-frame cost; total image timings use different sampling budgets.'
                parts.append(f'<p class="performance">{escape(text)}</p>')
            parts.append('<div class="views">')
            if report['kind'] == 'regression' and record.get('baseline_image'):
                parts.append(_view(output, record['baseline_image'], 'Baseline'))
            if record.get('image'):
                parts.append(_view(output, record['image'], 'Candidate' if report['kind'] == 'regression' else 'Original render'))
            if report['kind'] == 'regression' and record.get('difference_image'):
                parts.append(_view(output, record['difference_image'], 'Difference x4'))
            parts.append('</div>')
            parts.append(f'<p>{_evidence(output, record)}</p>')
            parts.append(_zooms(output, record))
            parts.append(f'<details><summary>Capture configuration, camera, status and metrics</summary><pre>{_json(record)}</pre></details></article>')
        parts.append('</div></section>')
    parts.append('</main></body></html>')
    return ''.join(parts)
