"""Offline scene reports and unchanged machine-readable result exports."""
from __future__ import annotations

import csv
from html import escape
import json
import math
from pathlib import Path, PurePosixPath
from urllib.parse import quote

TIMING_FIELDS = ['frames', 'warmup_frames', 'shown_frame_ms', 'mean_ms', 'median_ms', 'total_ms', 'sample_ms']


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
    return write_pages(output, report, downloads=[(metadata_file, '完整结果 JSON'), ('timings.csv', '耗时 CSV')])


def render_report(output: Path, report: dict, *, downloads=()) -> str:
    """Render the overview without writing files."""
    from scripts.report_pages import render_overview
    return render_overview(Path(output), report, downloads)


def write_pages(output: Path, report: dict, *, downloads=()) -> Path:
    """Write presentation files only, referencing the existing report artifacts."""
    from scripts.report_pages import write_pages as write
    return write(output, report, downloads=downloads)
