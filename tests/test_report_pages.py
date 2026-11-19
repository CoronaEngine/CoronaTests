"""The overview stays small; scene details and images remain reachable offline."""
from html.parser import HTMLParser
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from urllib.parse import unquote, urlsplit

from PIL import Image
import pytest

from scripts import report as pages


class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.tags = []
    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def fixture(root, count=12, kind='regression'):
    (root / 'images').mkdir()
    Image.new('RGB', (24, 16), (100, 120, 140)).save(root / 'images/one.png')
    records = []
    for i in range(count):
        for algorithm in ('gi', 'pt-reblur', 'pt2048'):
            records.append({'scene_id': f'room-{i}', 'scene_name': f'Room {i}', 'algorithm_id': algorithm,
                'status': 'pass' if kind == 'regression' else 'complete', 'image': 'images/one.png',
                'baseline_image': 'images/one.png' if kind == 'regression' else None,
                'difference_image': 'images/one.png' if kind == 'regression' else None,
                'regions': {'center': [.25, .25, .75, .75], 'corner': [0, 0, .5, .5]},
                'timing': {'frames': 512 if algorithm == 'pt2048' else 192, 'warmup_frames': 64,
                           'mean_ms': 2, 'total_ms': 1024, 'sample_ms': 2},
                'runtime_sha256': {'DO_NOT_EMBED_ON_HOME': 'x' * 10000}})
    return {'kind': kind, 'id': 'test', 'suite': 'full', 'created_utc': '2026-11-17',
            'metadata': {'engine_fingerprint': 'engine-version'}, 'records': records, 'summary': {}}


def test_overview_has_one_progressive_pt_result_per_scene_without_record_payload(tmp_path):
    report = fixture(tmp_path)
    Image.new('RGB', (24, 16)).save(tmp_path / 'images/pt.png')
    for row in report['records']:
        if row['algorithm_id'] == 'pt2048':
            row['image'] = 'images/pt.png'
    report['records'][1].update(status='failed', error='connection reset')
    report['records'][3].update(status='expected-unsupported', reason='hair')
    index = pages.write_report(tmp_path, report)
    html = index.read_text(encoding='utf-8'); parser = Links(); parser.feed(html)
    images = [attrs for tag, attrs in parser.tags if tag == 'img']
    assert len(images) == 12
    assert all(image['src'] == 'images/pt.png' and image['loading'] == 'lazy' for image in images)
    assert 'PT512 · 本次' in html
    assert not any(tag in ('svg', 'article') for tag, _ in parser.tags)
    rows = [attrs for tag, attrs in parser.tags if tag == 'tr' and 'data-scene' in attrs]
    assert len(rows) == 12 and sum('hidden' not in row for row in rows) == 1
    assert 'data-default-status="abnormal"' in html
    assert 'DO_NOT_EMBED_ON_HOME' not in html and len(html.encode()) < 200000
    for control in ('scene-search', 'status-filter', 'algorithm-filter', 'match-info'):
        assert any(attrs.get('id') == control for _, attrs in parser.tags)
    assert not any(attrs.get('id') in ('page-prev', 'page-next', 'page-info') for _, attrs in parser.tags)
    assert 'engine-version' in html and '512' in html
    assert len(list((tmp_path / 'pages').glob('*.html'))) == 12
    assert json.loads((tmp_path / 'report.json').read_text()) == report


def test_gi_only_overview_uses_a_labeled_offline_pt_golden_preview(tmp_path, monkeypatch):
    from scripts import report_pages

    root = tmp_path / 'archive'
    root.mkdir()
    golden = tmp_path / 'golden'
    (golden / 'room-0').mkdir(parents=True)
    Image.new('RGB', (1920, 1080)).save(golden / 'room-0/pt.png')
    monkeypatch.setattr(report_pages, 'PT_GOLDEN_ROOT', golden, raising=False)
    report = fixture(root, 2)
    report['records'] = [row for row in report['records'] if row['algorithm_id'] == 'gi']

    html = pages.write_report(root, report).read_text(encoding='utf-8')
    parser = Links(); parser.feed(html)
    images = [attrs for tag, attrs in parser.tags if tag == 'img']
    assert len(images) == 1 and 'PT Golden' in images[0]['alt']
    preview = root / unquote(images[0]['src'])
    assert preview.resolve().is_relative_to(root.resolve())
    with Image.open(preview) as image:
        assert image.width <= 384 and image.height < 1080
    assert json.loads((root / 'report.json').read_text(encoding='utf-8')) == report


def test_all_pass_shows_every_matching_scene_without_pagination(tmp_path):
    html = pages.write_report(tmp_path, fixture(tmp_path)).read_text(encoding='utf-8')
    parser = Links(); parser.feed(html)
    assert 'data-default-status="all"' in html
    rows = [a for tag, a in parser.tags if tag == 'tr' and 'data-scene' in a]
    assert sum('hidden' not in row for row in rows) == 12


def test_scene_tabs_default_to_failure_and_roi_and_difference_start_hidden(tmp_path):
    report = fixture(tmp_path, 2)
    report['records'][1].update(status='image-failed', failures=['center.rmse exceeds limit'])
    pages.write_report(tmp_path, report)
    path = tmp_path / 'pages/room-0.html'
    html = path.read_text(encoding='utf-8'); parser = Links(); parser.feed(html)
    tabs = {a['data-tab']: a for tag, a in parser.tags if 'data-tab' in a}
    assert tabs['pt-reblur']['aria-selected'] == 'true'
    panels = {a['data-algorithm']: a for tag, a in parser.tags if tag == 'article'}
    assert 'hidden' not in panels['pt-reblur'] and 'hidden' in panels['gi']
    assert 'center.rmse exceeds limit' in html
    assert all('hidden' in a for _, a in parser.tags if 'data-difference' in a or 'data-roi-panel' in a)
    assert any('data-roi-select' in a for _, a in parser.tags)
    links = [a.get('href', '') for _, a in parser.tags]
    assert '../index.html' in links and 'room-1.html' in links
    assert not list(tmp_path.rglob('*crop*.png'))


def test_every_generated_link_resolves_inside_report_and_images_are_not_copied(tmp_path):
    report = fixture(tmp_path, 2)
    before = hashlib.sha256((tmp_path / 'images/one.png').read_bytes()).hexdigest()
    pages.write_report(tmp_path, report)
    for path in tmp_path.rglob('*.html'):
        parser = Links(); parser.feed(path.read_text(encoding='utf-8'))
        for _, attrs in parser.tags:
            for key in ('href', 'src'):
                value = attrs.get(key)
                if not value: continue
                url = urlsplit(value)
                assert not url.scheme and not url.netloc
                target = (path.parent / unquote(url.path)).resolve() if url.path else path.resolve()
                assert target.is_relative_to(tmp_path.resolve()) and target.is_file(), value
    assert len(list(tmp_path.rglob('*.png'))) == 1
    assert hashlib.sha256((tmp_path / 'images/one.png').read_bytes()).hexdigest() == before


def test_subset_has_no_fabricated_algorithm_panels(tmp_path):
    report = fixture(tmp_path, 1)
    report['records'] = [report['records'][2]]
    pages.write_report(tmp_path, report)
    html = (tmp_path / 'pages/room-0.html').read_text(encoding='utf-8')
    parser = Links(); parser.feed(html)
    assert [a['data-algorithm'] for tag, a in parser.tags if tag == 'article'] == ['pt2048']


def test_overview_renderer_is_pure_and_does_not_hide_unknown_status(tmp_path):
    report = fixture(tmp_path, 1)
    report['records'][0]['status'] = 'new-unrecognized-failure'
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    html = pages.render_report(tmp_path, report)
    assert 'new-unrecognized-failure' in html and 'data-default-status="abnormal"' in html
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_duplicate_scene_algorithm_is_rejected_instead_of_silently_overwritten(tmp_path):
    report = fixture(tmp_path, 1)
    report['records'].append(copy.deepcopy(report['records'][0]))
    with pytest.raises(ValueError, match='Duplicate'):
        pages.write_report(tmp_path, report)


def test_known_unsupported_reason_is_a_notice_and_metrics_show_actual_limits(tmp_path):
    report = fixture(tmp_path, 1)
    report['records'][0].update(status='expected-unsupported', reason='unsupported shape')
    report['records'][1].update(status='image-failed', comparison={
        'mae': .01, 'rmse': .02, 'ssim_luminance': .9, 'luminance_ratio': 1.1,
        'regions': {}, 'raw_thresholds': {'full': {'mae': .003, 'rmse': .006, 'ssim_loss': .015, 'luminance_deviation': .015}}})
    pages.write_report(tmp_path, report)
    html = (tmp_path / 'pages/room-0.html').read_text(encoding='utf-8')
    assert '<div class="notice"><ul><li>unsupported shape</li>' in html
    assert '0.010000 / ≤0.003000 超标' in html
    assert '0.900000 / ≥0.985000 超标' in html


@pytest.mark.skipif(shutil.which('node') is None, reason='Node is optional for offline UI event checks')
def test_offline_javascript_filters_tabs_and_roi_event_handlers():
    result = subprocess.run(['node', str(Path(__file__).with_name('report_controls.cjs'))], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_overview_keeps_saved_overall_failure_even_if_listed_rows_pass(tmp_path):
    report = fixture(tmp_path, 1)
    report['passed'] = False
    html = pages.render_report(tmp_path, report)
    assert '整体结果：未通过' in html


def test_partial_golden_refresh_displays_each_capture_build(tmp_path):
    report = fixture(tmp_path, 1, kind='golden')
    report['records'][0]['engine_fingerprint'] = 'refreshed-engine'
    overview = pages.write_report(tmp_path, report).read_text(encoding='utf-8')
    assert '多构建基线' in overview and 'refreshed-engine' in overview and 'engine-version' in overview
    html = (tmp_path / 'pages/room-0.html').read_text(encoding='utf-8')
    gi = html.split('data-algorithm="gi"', 1)[1].split('</article>', 1)[0]
    pt = html.split('data-algorithm="pt2048"', 1)[1].split('</article>', 1)[0]
    assert 'data-engine-fingerprint="refreshed-engine"' in gi
    assert 'data-engine-fingerprint="engine-version"' in pt
    report['records'] = report['records'][:1]
    overview = pages.render_report(tmp_path, report)
    assert '引擎版本：<code>refreshed-engine</code>' in overview
