"""GitHub-native Markdown views of the existing formal golden images."""
from html import escape
from pathlib import Path

from scripts.report import _asset
from scripts.report_pages import ALGORITHMS, grouped, label

BEGIN = '<!-- BEGIN GOLDEN PREVIEW -->'
END = '<!-- END GOLDEN PREVIEW -->'


def _number(value, unit='ms'):
    return '未记录' if value is None else f'{value:.2f} {unit}'


def _budget(row):
    config, timing = row.get('configuration', {}), row.get('timing', {})
    frames = config.get('frames', timing.get('frames'))
    warmup = config.get('warmup', timing.get('warmup_frames'))
    unit = 'spp' if row['algorithm_id'] == 'pt2048' else '帧'
    return f'{frames} {unit}，计时预热 {warmup} 帧'


def _timing(row):
    timing = row.get('timing', {})
    if row['algorithm_id'] == 'pt2048':
        total = timing.get('total_ms')
        return '累计 ' + _number(None if total is None else total / 1000, 's')
    return '平均 ' + _number(timing.get('mean_ms')) + '/帧'


def _image(root, row, prefix, width):
    path = prefix + _asset(root, 'golden/' + row['image'])
    alt = escape(f'{row.get("scene_name", row["scene_id"])} · {label(row["algorithm_id"], row)}', quote=True)
    return f'<a href="{path}"><img src="{path}" alt="{alt}" width="{width}"></a>'


def write_github_gallery(root, manifest):
    root = Path(root).resolve()
    scenes = grouped(manifest)
    complete = [r for r in manifest['records'] if r['status'] == 'complete']
    # Validate every source image before writing any generated document.
    for row in complete:
        _asset(root, 'golden/' + row['image'])
    folder = root / 'docs/golden'
    folder.mkdir(parents=True, exist_ok=True)
    summary = ['# Golden 在线图库', '',
               f'基线 `{manifest["id"]}`：**{len(complete)} 张正式图片**，{len(scenes)} 个场景。', '',
               '直接在 GitHub 点击场景名查看三种算法的效果图、采样参数和耗时；点击图片可查看原图。', '',
               '图片引用仓库内的 `golden/` 原图。访问者需要仓库读取权限。GI/去噪使用时域帧数，纯 PT 使用累计 SPP。', '',
               'GI/去噪列显示预热后的平均帧耗时；PT 列显示全部样本的累计渲染耗时。', '',
               '| 场景 | GI | PT+REBLUR | PT |', '| --- | --- | --- | --- |']
    ordered = list(scenes)
    for index, (scene, rows) in enumerate(scenes.items()):
        name = escape(next(iter(rows.values())).get('scene_name', scene)).replace('|', '\\|')
        states = [_timing(rows[algorithm]) if rows[algorithm]['status'] == 'complete' else '已知不支持'
                  for algorithm, _ in ALGORITHMS if algorithm in rows]
        summary.append(f'| [{name}](docs/golden/{scene}.md) | ' + ' | '.join(states) + ' |')
        navigation = ['[← 场景目录](../../GALLERY.md)']
        if index: navigation.append(f'[上一场景]({ordered[index-1]}.md)')
        if index + 1 < len(ordered): navigation.append(f'[下一场景]({ordered[index+1]}.md)')
        detail = [f'# {name} · Golden', '', ' · '.join(navigation), '']
        for algorithm, _ in ALGORITHMS:
            if algorithm not in rows: continue
            row = rows[algorithm]
            detail += [f'## {label(algorithm, row)}', '']
            if row['status'] != 'complete':
                detail += ['已知不支持：`' + str(row.get('reason', row['status'])).replace('`', '') + '`。', '']
                continue
            detail += [_budget(row) + '。' + _timing(row) + '。', '']
            timing = row.get('timing', {})
            if algorithm != 'pt2048':
                detail += ['所示帧 ' + _number(timing.get('shown_frame_ms')) + '；中位数 ' + _number(timing.get('median_ms')) + '。', '']
            else:
                detail += ['平均每样本 ' + _number(timing.get('sample_ms')) + '。', '']
            detail += [_image(root, row, '../../', 960), '']
        (folder / (scene + '.md')).write_text('\n'.join(detail), encoding='utf-8', newline='\n')
    gallery = root / 'GALLERY.md'
    gallery.write_text('\n'.join(summary) + '\n', encoding='utf-8', newline='\n')
    readme = root / 'README.md'
    if complete and readme.is_file():
        text = readme.read_text(encoding='utf-8')
        if BEGIN in text and END in text:
            preview_scene = 'dragon' if any(r['scene_id'] == 'dragon' for r in complete) else complete[0]['scene_id']
            rows = [r for r in scenes[preview_scene].values() if r['status'] == 'complete']
            preview = [f'**{escape(rows[0].get("scene_name", preview_scene))} · 三算法预览**', '',
                       '| ' + ' | '.join(label(r['algorithm_id'], r) for r in rows) + ' |',
                       '| ' + ' | '.join('---' for r in rows) + ' |',
                       '| ' + ' | '.join(_image(root, r, '', 280) for r in rows) + ' |', '',
                       f'[查看该场景的采样参数与耗时](docs/golden/{preview_scene}.md) · [浏览全部场景](GALLERY.md)']
            text = text.partition(BEGIN)[0] + BEGIN + '\n' + '\n'.join(preview) + '\n' + END + text.partition(END)[2]
            readme.write_text(text, encoding='utf-8', newline='\n')
    return gallery
