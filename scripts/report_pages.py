"""Compact offline overview and scene pages; all images are referenced in place."""
from collections import Counter
from html import escape
from pathlib import Path
import json
import re
import shutil

from PIL import Image

from scripts.compare import DEFAULT_REGIONS, region_box
from scripts.report import _asset, _json, _number, _timing

ALGORITHMS = [('gi', 'GI'), ('pt-reblur', 'PT+REBLUR'), ('pt2048', 'PT')]
ACCEPTED = {'pass', 'complete', 'expected-unsupported'}
STATUS = {'pass': '通过', 'complete': '采集完成', 'image-failed': '图像异常', 'failed': '执行失败',
          'expected-unsupported': '已知不支持', 'incomparable': '不可比', 'uncalibrated': '阈值无效',
          'support-changed': '支持范围变化', 'missing': '缺项'}


def grouped(report):
    scenes = {}
    for row in report['records']:
        scene = row['scene_id']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', scene):
            raise ValueError('Invalid scene identifier for report page: ' + scene)
        if row['algorithm_id'] not in dict(ALGORITHMS):
            raise ValueError('Unknown report algorithm: ' + row['algorithm_id'])
        if row['algorithm_id'] in scenes.setdefault(scene, {}):
            raise ValueError('Duplicate scene/algorithm report record')
        scenes[scene][row['algorithm_id']] = row
    return scenes


def badge(status):
    style = 'known' if status == 'expected-unsupported' else 'good' if status in ACCEPTED else 'bad'
    return f'<span class="status status-{style}" title="{escape(status, quote=True)}">{escape(STATUS.get(status, status))}</span>'


def label(algorithm, row):
    if algorithm != 'pt2048':
        return dict(ALGORITHMS)[algorithm]
    config = row.get('configuration') or {}
    frames = config.get('frames', (row.get('timing') or {}).get('frames'))
    if type(frames) is int and (config.get('PT_settings') or {}).get('participating_media') is True:
        return f'PT {frames:,} spp · 参与介质开启'
    return f'PT{escape(str(frames))}' if frames is not None else 'PT'


def document(title, body, *, nested=False):
    prefix = '../' if nested else ''
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)}</title><link rel="stylesheet" href="{prefix}assets/report.css">'
            f'<script defer src="{prefix}assets/report.js"></script></head><body><main>{body}</main></body></html>')


def asset(root, relative, *, nested=False):
    return escape(('../' if nested else '') + _asset(root, relative), quote=True)


def sampling_line(report):
    budgets = {}
    for algorithm, _ in ALGORITHMS:
        values = set()
        for row in report['records']:
            if row['algorithm_id'] != algorithm: continue
            config, timing = row.get('configuration') or {}, row.get('timing') or {}
            frames = config.get('frames', timing.get('frames'))
            warmup = config.get('warmup', timing.get('warmup_frames'))
            if frames is not None:
                values.add((frames, warmup))
        if values:
            budgets[algorithm] = ' / '.join(f'{n} {"spp" if algorithm == "pt2048" else "frames"} · warmup {w}'
                                           for n, w in sorted(values, key=str))
    return ' · '.join(f'{dict(ALGORITHMS)[key]}: {value}' for key, value in budgets.items())


def render_overview(root, report, downloads=()):
    scenes = grouped(report)
    counts = Counter(row['status'] for row in report['records'])
    default = 'abnormal' if any(status not in ACCEPTED for status in counts) else 'all'
    title = f'{"回归报告" if report["kind"] == "regression" else "Golden 图库"} · {report["id"]}'
    metadata = report.get('metadata', {})
    okay = report.get('passed' if report['kind'] == 'regression' else 'valid', all(status in ACCEPTED for status in counts))
    engine = metadata.get('engine_fingerprint') or metadata.get('engine', '')
    if isinstance(engine, dict): engine = engine.get('sha256', '')
    builds = sorted({str(row.get('engine_fingerprint') or engine) for row in report['records']
                     if row['status'] in ('complete', 'pass', 'image-failed')})
    engine_text = '多构建基线（按条目记录）：' if len(builds) > 1 else '引擎版本：'
    engine_html = ' · '.join(f'<code>{escape(build)}</code>' for build in (builds or [str(engine)]))
    parts = [f'<div data-overview data-default-status="{default}"><div class="eyebrow">CORONA TESTS / OVERVIEW</div>',
             f'<h1>{escape(title)}</h1><p class="subtitle">{escape(str(report["created_utc"]))} · Suite: {escape(report["suite"])}</p>',
             f'<p><span class="status status-{"good" if okay else "bad"}">整体结果：{"通过" if okay else "未通过"}</span></p>',
             f'<p class="run-info">{engine_text}{engine_html}</p>',
             f'<p class="run-info">{escape(sampling_line(report))}</p><div class="summary">',
             f'<a href="#scene-list" data-status-filter="all"><strong>{len(report["records"])}</strong><span>全部检查 · {len(scenes)} 个场景</span></a>']
    for status, count in counts.items():
        parts.append(f'<a href="#scene-list" data-status-filter="{escape(status, quote=True)}"><strong>{count}</strong><span>{escape(STATUS.get(status, status))}</span></a>')
    parts.append('</div>')
    if report['kind'] == 'golden' and report.get('threshold_policy') == 'fixed':
        parts.append('<p class="muted">Thresholds: fixed · 每个场景、每种算法只采集一次。</p>')
    parts.append('<p class="downloads">' + ''.join(f'<a href="{asset(root, path)}" download>{escape(text)}</a>' for path, text in downloads) + '</p>')
    parts.append('<div class="toolbar"><label>场景搜索<input id="scene-search" type="search" placeholder="输入场景名称或 ID"></label><label>状态<select id="status-filter">')
    for value, text in [('all', '全部状态'), ('abnormal', '仅异常'), *[(s, STATUS.get(s, s)) for s in counts]]:
        parts.append(f'<option value="{escape(value, quote=True)}"{" selected" if value == default else ""}>{escape(text)}</option>')
    parts.append('</select></label><label>算法<select id="algorithm-filter"><option value="all">全部算法</option>')
    present = {row['algorithm_id'] for row in report['records']}
    for algorithm, _ in ALGORITHMS:
        if algorithm in present:
            row = next(row for row in report['records'] if row['algorithm_id'] == algorithm)
            parts.append(f'<option value="{algorithm}">{label(algorithm, row)}</option>')
    parts.append('</select></label></div><div class="table-scroll" id="scene-list"><table><thead><tr><th>场景</th>')
    for algorithm, _ in ALGORITHMS:
        if algorithm in present:
            row = next(row for row in report['records'] if row['algorithm_id'] == algorithm)
            parts.append(f'<th>{label(algorithm, row)}</th>')
    parts.append('<th>操作</th></tr></thead><tbody>')
    matched = 0
    ordered = sorted(scenes.items(), key=lambda pair: (0 if any(r['status'] not in ACCEPTED for r in pair[1].values()) else 2 if all(r['status'] == 'expected-unsupported' for r in pair[1].values()) else 1, pair[0]))
    for scene, rows in ordered:
        name = next((r['scene_name'] for r in rows.values() if r.get('scene_name')), scene)
        statuses = {key: row['status'] for key, row in rows.items()}
        selected = default == 'all' or any(value not in ACCEPTED for value in statuses.values())
        if selected: matched += 1
        target = f'pages/{scene}.html'
        parts.append(f'<tr data-scene="{escape(scene, quote=True)}" data-name="{escape(name, quote=True)}" data-statuses="{escape(json.dumps(statuses), quote=True)}"{"" if selected else " hidden"}><td><a class="scene-title" href="{target}">{escape(name)}</a><span class="scene-id">{escape(scene)}</span></td>')
        for algorithm, _ in ALGORITHMS:
            if algorithm not in present: continue
            row = rows.get(algorithm)
            if row is None:
                parts.append('<td><span class="muted">未选择</span></td>'); continue
            timing = row.get('timing') or {}
            time = (_number(timing.get('total_ms')) + ' / 整图') if algorithm == 'pt2048' else (_number(timing.get('mean_ms')) + ' / 帧')
            parts.append(f'<td><a href="{target}#{algorithm}">{badge(row["status"])}</a><span class="cell-time">{time if timing else "—"}</span></td>')
        parts.append(f'<td><a href="{target}">查看详情 →</a></td></tr>')
    parts.append(f'</tbody></table><p id="empty-results" class="empty" hidden>没有匹配的场景，请调整筛选条件。</p></div><p id="match-info" class="match-info">匹配 {matched} / {len(scenes)} 个场景</p>')
    parts.append('<p class="limits-note">统计覆盖整次运行，不随筛选改变。已知不支持单独统计；点击任意算法状态可直接查看对应详情。</p><noscript><style>tr[hidden]{display:table-row!important}</style>启用 JavaScript 可搜索和筛选。当前列出全部场景。</noscript></div>')
    return document(title, ''.join(parts))


def view(root, path, text, difference=False, roi=None):
    url = asset(root, path, nested=True)
    attrs = ' data-difference hidden' if difference else ''
    if roi is None:
        image = f'<a href="{url}"><img loading="lazy" src="{url}" alt="{escape(text, quote=True)}"></a>'
    else:
        with Image.open(root / path) as source:
            width, height = source.size
        left, top, right, bottom = region_box(roi, width, height)
        image = (f'<svg viewBox="{left} {top} {right-left} {bottom-top}" style="display:block;width:100%;aspect-ratio:{right-left}/{bottom-top}" role="img" aria-label="{escape(text, quote=True)}">'
                 f'<image href="{url}" width="{width}" height="{height}"/></svg>')
    return f'<figure{attrs}><figcaption>{escape(text)}</figcaption>{image}<a href="{url}" download>下载原图</a></figure>'


def metric(value, limit, *, minimum=False):
    if not isinstance(value, (int, float)):
        return '—'
    if limit is None:
        return f'{value:.6f} / 未记录'
    failed = value < limit - 1e-12 if minimum else value > limit + 1e-12
    return f'<span class="metric-{"failed" if failed else "passed"}">{value:.6f} / {"≥" if minimum else "≤"}{limit:.6f}{" 超标" if failed else ""}</span>'


def raw_metrics(row):
    comparison = row.get('comparison') or {}
    if not comparison: return ''
    limits = comparison.get('raw_thresholds') or {}
    parts = ['<div class="table-scroll"><table><thead><tr><th>区域</th><th>MAE / 限值</th><th>RMSE / 限值</th><th>SSIM / 最低值</th><th>相对亮度偏差 / 限值</th></tr></thead><tbody>']
    for name, values in [('full', comparison), *(comparison.get('regions') or {}).items()]:
        thresholds = limits.get('full', {}) if name == 'full' else limits.get('regions', {}).get(name, {})
        ssim_min = 1 - thresholds['ssim_loss'] if 'ssim_loss' in thresholds else None
        ratio = values.get('luminance_ratio')
        cells = [metric(values.get('mae'), thresholds.get('mae')), metric(values.get('rmse'), thresholds.get('rmse')),
                 metric(values.get('ssim_luminance'), ssim_min, minimum=True), metric(abs(ratio - 1) if ratio is not None else None, thresholds.get('luminance_deviation'))]
        parts.append(f'<tr><td>{escape(name)}</td>' + ''.join(f'<td>{cell}</td>' for cell in cells) + '</tr>')
    return ''.join(parts) + '</tbody></table></div>'


def multiscale(row):
    evaluation = (row.get('comparison') or {}).get('multiscale_evaluation')
    if not evaluation: return ''
    parts = ['<section class="metrics"><h3>PT512 · DS8 + DS4</h3><p class="limits-note">两个尺度的整图和所有 ROI 必须同时通过。Full-resolution metrics are diagnostic only.</p><div class="table-scroll"><table><thead><tr><th>尺度 / 区域</th><th>状态</th><th>MAE / 限值</th><th>RMSE / 限值</th><th>SSIM / 最低值</th><th>平均 RGB 变化 / 限值</th><th>亮度变化 / 限值</th></tr></thead><tbody>']
    for scale, groups in evaluation.get('checks', {}).items():
        for name, checks in groups.items():
            status = 'pass' if all(v['passed'] for v in checks.values()) else 'image-failed'
            cells = [metric(checks[k]['value'], checks[k]['limit']) for k in ('mae', 'rmse')]
            cells.append(metric(1-checks['ssim_loss']['value'], 1-checks['ssim_loss']['limit'], minimum=True))
            cells.extend(metric(checks[k]['value'], checks[k]['limit']) for k in ('mean_rgb', 'luminance'))
            parts.append(f'<tr data-metric-region="{escape(name, quote=True)}"{" hidden" if name != "full" else ""}><td>{escape(scale)} / {escape(name)}</td><td>{badge(status)}</td>' + ''.join(f'<td>{cell}</td>' for cell in cells) + '</tr>')
    parts.append('</tbody></table></div><p class="limits-note">限值 = 已验证 PT512 误差底线 + 固定余量；选择下方 ROI 可切换区域指标。</p></section>')
    return ''.join(parts)


def evidence(root, row):
    paths = {row[k]: k.replace('_', ' ') for k in ('run_json', 'comparison_json', 'profile_json', 'timing_csv', 'passes_csv', 'metadata_json') if row.get(k)}
    if row.get('image'):
        folder = (root / row['image']).parent
    else:
        folder = root / 'runs' / row['scene_id'] / 'missing'
        if row.get('job_id') and re.fullmatch(r'[A-Za-z0-9_.-]+', row['job_id']):
            folder = root / 'runs' / row['job_id'] / 'repeat-0'
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            if path.is_file() and path.suffix.lower() in ('.json', '.csv', '.log', '.jsonl', '.pfm'):
                paths[path.relative_to(root).as_posix()] = path.name
    return ''.join(f'<a href="{asset(root, path, nested=True)}">{escape(text)}</a>' for path, text in paths.items())


def algorithm_panel(root, report, algorithm, row, selected):
    parts = [f'<article class="algorithm-panel" data-algorithm="{algorithm}" id="panel-{algorithm}" role="tabpanel" aria-labelledby="tab-{algorithm}"{"" if selected else " hidden"}><h2>{label(algorithm, row)} {badge(row["status"])}</h2>']
    build = row.get('engine_fingerprint') or report.get('metadata', {}).get('engine_fingerprint')
    if build:
        parts.append(f'<p class="run-info" data-engine-fingerprint="{escape(str(build), quote=True)}">采集构建：<code>{escape(str(build))}</code></p>')
    comparison = row.get('comparison') or {}
    failures = [row[k] for k in ('reason', 'error') if row.get(k)]
    failures += row.get('failures', []) + comparison.get('evaluation', {}).get('failures', [])
    if failures:
        style = 'notice' if row['status'] == 'expected-unsupported' else 'failures'
        parts.append(f'<div class="{style}"><ul>' + ''.join(f'<li>{escape(reason)}</li>' for reason in dict.fromkeys(map(str, failures))) + '</ul></div>')
    config, timing = row.get('configuration') or {}, row.get('timing') or {}
    frames = config.get('frames', timing.get('frames')); warmup = config.get('warmup', timing.get('warmup_frames'))
    if frames is not None and warmup is not None:
        parts.append(f'<p class="sampling">Budget: {frames} frames; {warmup} warmup; 1 spp/frame.</p>')
    sampling = row.get('sampling_comparison')
    if sampling and sampling['golden'] != sampling['candidate']:
        before, after = sampling['golden'], sampling['candidate']; unit = 'spp' if algorithm == 'pt2048' else 'frames'
        policy = 'PT512 uses the fixed DS8/DS4 noise-envelope policy.' if row.get('comparison_policy') else 'Golden thresholds unchanged; sampling noise counts toward image error.'
        text = f'Golden: {before["frames"]} {unit} ({before["warmup_frames"]} warmup); candidate: {after["frames"]} {unit} ({after["warmup_frames"]} warmup). {policy}'
        parts.append(f'<p class="budget-comparison">{escape(text)}</p>')
    parts.append(_timing(row))
    performance = row.get('performance')
    if performance:
        text = f'Performance (warning-only): golden mean {_number(performance["golden_mean_ms"])} → candidate mean {_number(timing.get("mean_ms"))} ({performance["mean_change_percent"]:+.2f}%).'
        if performance.get('machine_observation_equal') is False: text += ' Hardware differs; not comparable.'
        if performance.get('sampling_budget_equal') is False: text += ' Per-frame cost; total image timings use different sampling budgets.'
        parts.append(f'<p class="performance">{escape(text)}</p>')
    parts.append(multiscale(row))
    if comparison:
        if row.get('comparison_policy'):
            parts.append('<details><summary>原分辨率诊断指标（不参与 PT512 判定）</summary>' + raw_metrics(row) + '</details>')
        else:
            parts.append('<section class="metrics"><h3>图像指标 · 实际值 / 阈值</h3>' + raw_metrics(row) + '</section>')
    roles = []
    if row.get('baseline_image'): roles.append(('Baseline · Golden', row['baseline_image'], False))
    if row.get('image'): roles.append(('Candidate · 本次结果' if report['kind'] == 'regression' else 'Golden · 正式图片', row['image'], False))
    if row.get('difference_image'): roles.append(('Difference ×4', row['difference_image'], True))
    if any(difference for _, _, difference in roles):
        parts.append('<div class="view-actions"><button data-diff-toggle aria-pressed="false">显示差分 ×4</button><span class="limits-note">绝对 RGB 差异放大 4 倍，仅用于观察。</span></div>')
    parts.append('<div class="views">' + ''.join(view(root, path, text, difference) for text, path, difference in roles) + '</div>')
    if not roles:
        parts.append('<p class="muted">此项没有正式图片，请查看状态说明和日志。</p>')
    regions = row.get('regions', DEFAULT_REGIONS)
    if roles and regions:
        parts.append('<div class="roi-controls"><label>局部检查<select data-roi-select><option value="full">整图（不显示局部放大）</option>')
        parts.extend(f'<option value="{escape(name, quote=True)}">{escape(name)}</option>' for name in regions)
        parts.append('</select></label></div>')
        for name, bounds in regions.items():
            parts.append(f'<div data-roi-panel="{escape(name, quote=True)}" hidden><h3>ROI · {escape(name)}</h3><div class="roi-views">')
            parts.extend(view(root, path, text, difference, bounds) for text, path, difference in roles)
            parts.append('</div></div>')
    details = {key: row[key] for key in ('status', 'failure_phase', 'error_type', 'exit_code', 'configuration', 'camera_before', 'comparison_policy', 'sampling_comparison') if key in row}
    parts.append(f'<details><summary>详细配置与证据</summary><div class="evidence">{evidence(root, row)}</div><pre>{_json(details)}</pre></details></article>')
    return ''.join(parts)


def render_scene(root, report, scene, scenes, downloads=()):
    rows = scenes[scene]
    name = next((r['scene_name'] for r in rows.values() if r.get('scene_name')), scene)
    ordered = list(scenes); position = ordered.index(scene)
    parts = ['<nav class="scene-nav"><a href="../index.html">← 返回场景列表</a><div>']
    if position: parts.append(f'<a href="{ordered[position-1]}.html">← 上一场景</a>')
    if position + 1 < len(ordered): parts.append(f'<a href="{ordered[position+1]}.html">下一场景 →</a>')
    parts.append(f'</div></nav><div class="eyebrow">CORONA TESTS / SCENE {position+1} OF {len(scenes)}</div><h1>{escape(name)}</h1><p class="subtitle">{escape(scene)} · {escape(report["id"])}</p>')
    selected = next((key for key, _ in ALGORITHMS if key in rows and rows[key]['status'] not in ACCEPTED), next(iter(rows)))
    parts.append('<div class="tabs" role="tablist" aria-label="渲染算法">')
    for key, _ in ALGORITHMS:
        if key in rows:
            parts.append(f'<button role="tab" id="tab-{key}" aria-controls="panel-{key}" data-tab="{key}" aria-selected="{str(key == selected).lower()}" tabindex="{0 if key == selected else -1}">{label(key, rows[key])} {badge(rows[key]["status"])}</button>')
    parts.append('</div>')
    parts.extend(algorithm_panel(root, report, key, rows[key], key == selected) for key, _ in ALGORITHMS if key in rows)
    parts.append('<details><summary>运行信息与完整结果</summary><p class="downloads">' + ''.join(f'<a href="{asset(root, path, nested=True)}">{escape(text)}</a>' for path, text in downloads) + f'</p><pre>{_json(report.get("metadata", {}))}</pre></details><noscript>启用 JavaScript 可切换算法、差分及 ROI。</noscript>')
    return document(name + ' · ' + report['id'], ''.join(parts), nested=True)


def write_pages(output, report, *, downloads=()):
    root = Path(output)
    scenes = grouped(report)
    # Check all inputs before writing any presentation file.
    for row in report['records']:
        for key in ('image', 'baseline_image', 'difference_image'):
            if row.get(key): _asset(root, row[key])
    for path, _ in downloads: _asset(root, path)
    for folder in ('assets', 'pages'):
        target = (root / folder).resolve()
        if not target.is_relative_to(root.resolve()): raise ValueError('Report directory resolves outside output')
        target.mkdir(parents=True, exist_ok=True)
    for name in ('report.css', 'report.js'):
        destination = root / 'assets' / name
        if not destination.resolve().is_relative_to(root.resolve()): raise ValueError('Report asset resolves outside output')
        shutil.copyfile(Path(__file__).with_name('report_assets') / name, destination)
    for scene in scenes:
        destination = root / 'pages' / (scene + '.html')
        if not destination.resolve().is_relative_to(root.resolve()): raise ValueError('Scene page resolves outside output')
        destination.write_text(render_scene(root, report, scene, scenes, downloads), encoding='utf-8', newline='\n')
    destination = root / 'index.html'
    if not destination.resolve().is_relative_to(root.resolve()): raise ValueError('Report index resolves outside output')
    destination.write_text(render_overview(root, report, downloads), encoding='utf-8', newline='\n')
    return destination
