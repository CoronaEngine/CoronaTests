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
from scripts.restir_timing import breakdown as restir_breakdown

ALGORITHMS = [('gi', 'GI'), ('pt-reblur', 'PT+REBLUR'), ('pt2048', 'PT')]
PT_GOLDEN_ROOT = Path(__file__).resolve().parents[1] / 'golden'
ACCEPTED = {'pass', 'complete', 'expected-unsupported'}
STATUS = {'pass': '通过', 'complete': '采集完成', 'image-failed': '图像异常', 'failed': '执行失败',
          'expected-unsupported': '已知不支持', 'incomparable': '不可比', 'uncalibrated': '阈值无效',
          'support-changed': '支持范围变化', 'missing': '缺项'}
RESTIR_TIMING_STYLE = """<style>
.restir-performance tbody[data-restir-pipeline]:has(details:not([open])) ~ tbody[data-restir-stage],
.restir-performance tbody[data-restir-stage]:has(details:not([open])) > tr[data-restir-pass]{display:none}
.restir-performance th{white-space:normal}
.restir-performance td{white-space:nowrap;text-align:right}
.restir-performance thead th:not(:first-child){text-align:right}
.restir-performance [data-restir-frame] th,.restir-performance [data-restir-frame] td{font-weight:650;background:#1c293b}
.restir-performance [data-restir-stage] th{padding-left:26px}
.restir-performance [data-restir-pass] th{padding-left:43px}
.restir-performance details{margin:0;padding:0;border:0}
.restir-performance .restir-note{display:block;color:var(--muted);font-weight:400}
.restir-performance [data-restir-change="faster"] td:nth-last-child(-n+2){color:var(--good)}
.restir-performance [data-restir-change="slower"] td:nth-last-child(-n+2){color:var(--bad)}
</style>"""


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


def document(title, body, *, nested=False, styles=''):
    prefix = '../' if nested else ''
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)}</title><link rel="stylesheet" href="{prefix}assets/report.css">'
            f'{styles}<script defer src="{prefix}assets/report.js"></script></head><body><main>{body}</main></body></html>')


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


def scene_preview(root, scene, name, rows, pt_goldens):
    pt = rows.get('pt2048', {})
    path = pt.get('image') or pt.get('baseline_image') or pt_goldens.get(scene)
    if not path:
        return ''
    caption = f'{label("pt2048", pt)} · 本次' if pt.get('image') else 'PT Golden'
    target = f'pages/{scene}.html' + ('#pt2048' if pt else '')
    return (f'<a class="scene-preview" href="{target}">'
            f'<img src="{asset(root, path)}" alt="{escape(name, quote=True)} · {caption}" '
            f'width="160" height="90" loading="lazy" decoding="async">'
            f'<span>{caption}</span></a>')


def render_overview(root, report, downloads=(), *, pt_goldens=None):
    scenes = grouped(report)
    pt_goldens = pt_goldens or {}
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
    timed_gi = [(scene, rows['gi']) for scene, rows in scenes.items()
                if 'gi' in rows and rows['gi'].get('restir_golden_comparison')]
    if timed_gi:
        sections = (
            ('性能 Golden', '项已测', [item for item in timed_gi
                if item[1]['restir_golden_comparison']['status'] == 'baseline']),
            ('有逐阶段对比', '项可比', [item for item in timed_gi
                if item[1]['restir_golden_comparison']['status'] == 'compared']),
            ('缺数据或不可比', '项缺数据或不可比', [item for item in timed_gi
                if item[1]['restir_golden_comparison']['status'] not in ('baseline', 'compared')]),
        )
        count_text = ' · '.join(['GI 阶段耗时', *(f'{len(entries)} {count}' for _, count, entries in sections if entries)])
        parts.append(f'<details id="restir-golden-availability"><summary>{count_text}</summary>')
        for section_title, _, entries in sections:
            if entries:
                parts.append(f'<h3>{section_title}</h3><ul>')
                for scene, row in entries:
                    name = escape(row.get('scene_name', scene))
                    reason = row['restir_golden_comparison'].get('reason')
                    detail = f' · {escape(reason)}' if reason else ''
                    parts.append(f'<li><a href="pages/{scene}.html#gi">{name}</a>{detail}</li>')
                parts.append('</ul>')
        parts.append('</details>')
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
        preview = scene_preview(root, scene, name, rows, pt_goldens) if report['kind'] == 'regression' else ''
        parts.append(f'<tr data-scene="{escape(scene, quote=True)}" data-name="{escape(name, quote=True)}" data-statuses="{escape(json.dumps(statuses), quote=True)}"{"" if selected else " hidden"}>'
                     f'<td><div class="scene-entry">{preview}<div><a class="scene-title" href="{target}">{escape(name)}</a>'
                     f'<span class="scene-id">{escape(scene)}</span></div></div></td>')
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


def reblur_context_means(passes):
    contexts = {}
    for name, mean in passes.items():
        context, separator, _ = name.partition(' / ')
        label = context.removeprefix('NRD ') if separator else name
        contexts[label] = contexts.get(label, 0.0) + mean
    return contexts


def restir_timing(timing, reference=None):
    groups = restir_breakdown(timing) or {}
    golden_timing = (reference or {}).get('golden_timing') or {}
    golden_groups = restir_breakdown(golden_timing) or {}
    if not (groups or golden_groups or reference):
        return ''

    def dispatch_mean(data, key):
        return data.get('retained', {}).get(key, {}).get('mean_ms')

    status = (reference or {}).get('status')
    total = dispatch_mean(timing, 'GPU_dispatch_duration_sum') if groups else None
    golden_total = dispatch_mean(golden_timing, 'GPU_dispatch_duration_sum') if golden_groups else None
    labels = {
        'pt': '初始候选', 'di': 'ReSTIR DI', 'gi': 'ReSTIR GI', 'reblur': 'REBLUR',
        'shared': '公共开销', 'unknown': '未分类',
    }
    parts = [
        '<section class="metrics" id="gi-performance"><h3>GI 性能对比</h3>',
        '<p class="limits-note">预热后平均 ms/帧。</p>',
    ]
    if reference and reference.get('baseline_id'):
        parts.append(f'<p class="run-info">性能 Golden <code>{escape(reference["baseline_id"])}</code></p>')
    if reference and reference.get('reason'):
        parts.append(f'<p class="limits-note">{escape(reference["reason"])}</p>')
    if not (groups or golden_groups):
        return ''.join(parts) + '</section>'
    parts.append('<div class="table-scroll"><table class="restir-performance"><thead><tr><th scope="col">阶段</th>')
    if reference is None:
        parts.append('<th scope="col">耗时</th><th scope="col">占 GPU dispatch 合计</th>')
    else:
        parts.append('<th scope="col">性能 Golden</th>')
        if status != 'baseline':
            parts.append('<th scope="col">本次</th><th scope="col">差值</th><th scope="col">变化</th>')
    parts.append('</tr></thead>')

    def group_mean(values, key):
        return sum(values[key].values()) if values.get(key) else None

    def timing_row(title, measured, baseline=None, *, compare=True, shareable=True, attrs=''):
        measured_text = _number(measured) if measured is not None else '—'
        baseline_text = _number(baseline) if baseline is not None else '—'
        heading = f'<th scope="row">{title}</th>'
        if reference is None:
            share = f'{measured / total:.1%}' if shareable and measured is not None and total else '—'
            parts.append(f'<tr{attrs}>{heading}<td>{measured_text}</td><td>{share}</td></tr>')
            return
        if status == 'baseline':
            parts.append(f'<tr{attrs}>{heading}<td>{baseline_text}</td></tr>')
            return
        change = percent = '—'
        if compare and status == 'compared' and measured is not None and baseline is not None:
            delta = measured - baseline
            change = f'{delta:+.3f} ms'
            if baseline > 0:
                percent = f'{delta / baseline:+.1%}'
            if delta:
                attrs += ' data-restir-change="' + ('slower' if delta > 0 else 'faster') + '"'
        parts.append(f'<tr{attrs}>{heading}<td>{baseline_text}</td><td>{measured_text}</td>'
                     f'<td>{change}</td><td>{percent}</td></tr>')

    def toggle(title, target, *, opened=False):
        return f'<details{" open" if opened else ""}><summary aria-controls="{target}">{title}</summary></details>'

    stages = [key for key in labels if key != 'unknown' or groups.get(key) or golden_groups.get(key)]
    targets = ' '.join(f'restir-stage-{key}' for key in stages)
    parts.append('<tbody data-restir-frame>')
    timing_row('整帧总耗时 <span class="restir-note">渲染 API</span>',
               timing.get('mean_ms'), golden_timing.get('mean_ms'), shareable=False)
    parts.append('</tbody><tbody data-restir-pipeline>')
    timing_row(toggle('ReSTIR + REBLUR', targets, opened=True)
               + '<span class="restir-note">GPU dispatch 合计</span>', total, golden_total)
    parts.append('</tbody>')

    contexts = {'physical-DI': 'DI 去噪', 'GI0': 'GI 0 去噪', 'GI1': 'GI 1 去噪', 'GI2': 'GI 2 去噪'}
    pass_notes = {
        'Falcor DI surface preparation': 'DI / GI 共用',
        'RTXPT GI stable plane build': '也为 GI 的 REBLUR 提供引导数据',
        'REBLUR-ComposePhysicalDI-PrivateGI': '去噪结果合成',
    }
    for key in stages:
        title = labels[key]
        measured = groups.get(key, {})
        baseline = golden_groups.get(key, {})
        if key == 'reblur':
            measured = reblur_context_means(measured)
            baseline = reblur_context_means(baseline)
        names = sorted(measured.keys() | baseline.keys())
        if key == 'reblur':
            names.sort(key=lambda name: list(contexts).index(name) if name in contexts else len(contexts))
        nested = key != 'pt' and bool(names)
        stage_id = f'restir-stage-{key}'
        pass_ids = [f'{stage_id}-pass-{idx}' for idx in range(len(names))] if nested else []
        parts.append(f'<tbody id="{stage_id}" data-restir-stage>')
        timing_row(toggle(title, ' '.join(pass_ids)) if nested else title,
                   group_mean(groups, key), group_mean(golden_groups, key), compare=key != 'unknown')
        for name, pass_id in zip(names, pass_ids):
            heading = f'<code>{escape(name)}</code>'
            if key == 'reblur':
                heading = contexts.get(name, heading)
            if name in pass_notes:
                heading += f'<span class="restir-note">{pass_notes[name]}</span>'
            timing_row(heading, measured.get(name), baseline.get(name), compare=key != 'unknown',
                       attrs=f' id="{pass_id}" data-restir-pass')
        parts.append('</tbody>')
    parts.append('<tbody>')
    timing_row('GPU 时间跨度', dispatch_mean(timing, 'GPU_timestamp_envelope'),
               dispatch_mean(golden_timing, 'GPU_timestamp_envelope'), shareable=False)
    parts += [
        '</tbody></table></div>',
        '<p class="limits-note">初始候选同时包含路径采样和去噪信号导出，无法单独测得纯 PT。'
        '共享的 Falcor 表面准备计入 ReSTIR DI，去噪结果合成计入 REBLUR。'
        'GPU dispatch 合计不包含 pass 之间的空隙。</p>',
    ]
    if status == 'compared':
        parts.append('<p class="limits-note">差值按本次减 Golden 计算。负数表示更快，正数表示更慢。</p>')
    parts.append('</section>')
    return ''.join(parts)


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
    performance_ref = row.get('restir_golden_comparison') if algorithm == 'gi' else None
    stage_timing = restir_timing(timing, performance_ref) if algorithm == 'gi' else ''
    parts.append(stage_timing or _timing(row))
    performance = row.get('performance')
    if performance and not performance_ref:
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
    return document(name + ' · ' + report['id'], ''.join(parts), nested=True,
                    styles=RESTIR_TIMING_STYLE if 'gi' in rows else '')


def prepare_pt_golden_previews(root, scenes):
    """Keep fallback previews inside the archive without importing full Golden images."""
    previews = {}
    for scene, rows in scenes.items():
        pt = rows.get('pt2048', {})
        if pt.get('image') or pt.get('baseline_image'):
            continue
        source = PT_GOLDEN_ROOT / scene / 'pt.png'
        if not source.is_file():
            continue
        try:
            with Image.open(source) as image:
                preview = image.convert('RGB')
                preview.thumbnail((384, 216), Image.Resampling.LANCZOS)
        except OSError:
            # An unhydrated LFS pointer provides no preview pixels.
            continue
        relative = f'assets/pt-previews/{scene}.jpg'
        destination = root / relative
        if not destination.resolve().is_relative_to(root.resolve()):
            raise ValueError('PT preview resolves outside output')
        destination.parent.mkdir(exist_ok=True)
        preview.save(destination, quality=88)
        previews[scene] = relative
    return previews


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
    pt_goldens = prepare_pt_golden_previews(root, scenes) if report['kind'] == 'regression' else None
    destination.write_text(render_overview(root, report, downloads, pt_goldens=pt_goldens), encoding='utf-8', newline='\n')
    return destination
