from html.parser import HTMLParser
from pathlib import Path
import csv
import hashlib
import json
import numpy as np
import pytest
from PIL import Image

from scripts.report import write_report


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.tags = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        self.links.extend(attrs[key] for key in ('src', 'href') if key in attrs)


def report_fixture(root, kind='golden'):
    (root / 'jobs').mkdir()
    Image.fromarray(np.full((16, 24, 3), 120, dtype=np.uint8)).save(root / 'jobs/image.png')
    (root / 'jobs/run.json').write_text('{}')
    record = {'schema': 1, 'scene_id': 'one', 'scene_name': '<Room>', 'algorithm_id': 'gi', 'job_id': 'one-gi', 'status': 'complete', 'image': 'jobs/image.png', 'regions': {'center': [.25, .25, .75, .75]}, 'timing': {'frames': 192, 'warmup_frames': 64, 'mean_ms': 21.3, 'median_ms': 21.2, 'shown_frame_ms': 22.4, 'total_ms': 4090, 'sample_ms': 21.3}, 'run_json': 'jobs/run.json'}
    return {'kind': kind, 'id': 'sample', 'suite': 'quick', 'created_utc': '2026-11-16T00:00:00Z', 'metadata': {'hardware': 'RTX sample', 'engine': {'sha256': 'abc'}}, 'summary': {}, 'records': [record, {'scene_id': 'one', 'algorithm_id': 'pt-reblur', 'job_id': 'one-pt-reblur', 'status': 'expected-unsupported', 'reason': 'import-hair'}, {'scene_id': 'one', 'algorithm_id': 'pt2048', 'job_id': 'one-pt2048', 'status': 'failed', 'error': 'No timing evidence'}]}


def test_report_offline_assets_timing_csv_and_raw_originals(tmp_path):
    report = report_fixture(tmp_path)
    image = tmp_path / 'jobs/image.png'
    before = hashlib.sha256(image.read_bytes()).hexdigest()
    index = write_report(tmp_path, report)
    html = index.read_text(encoding='utf-8')
    parser = Links()
    parser.feed(html)
    assert sum(tag == 'article' for tag, attrs in parser.tags) == 3
    assert '&lt;Room&gt;' in html
    assert '22.400 ms' in html and '21.300 ms' in html
    assert 'import-hair' in html and 'No timing evidence' in html
    assert 'RTX sample' in html and 'abc' in html
    assert hashlib.sha256(image.read_bytes()).hexdigest() == before
    for link in parser.links:
        if link.startswith('#'):
            continue
        assert '://' not in link and not link.startswith(('/', '..'))
        assert (tmp_path / link).is_file(), link
    assert any(tag == 'a' and 'download' in attrs and attrs['href'] == 'jobs/image.png' for tag, attrs in parser.tags)
    assert '<svg viewBox="6 4 12 8"' in html
    assert not (tmp_path / 'zooms').exists()
    rows = list(csv.DictReader((tmp_path / 'timings.csv').open(newline='', encoding='utf-8')))
    assert len(rows) == 3
    assert rows[0]['mean_ms'] == '21.3'
    assert rows[1]['mean_ms'] == rows[2]['mean_ms'] == ''
    assert json.loads((tmp_path / 'report.json').read_text())['id'] == 'sample'


def test_html_renderer_references_existing_assets_without_writing_files(tmp_path):
    from scripts import report as reports
    assert hasattr(reports, 'render_report'), 'Read-only HTML rendering is missing'
    report = report_fixture(tmp_path)
    (tmp_path / 'baselines').mkdir()
    (tmp_path / 'baselines/golden.manifest.json').write_text('{}')
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    html = reports.render_report(tmp_path, report,
        downloads=[('baselines/golden.manifest.json', 'Golden metadata')])
    parser = Links()
    parser.feed(html)
    assert 'baselines/golden.manifest.json' in parser.links
    assert 'jobs/image.png' in parser.links
    assert 'report.json' not in parser.links and 'timings.csv' not in parser.links
    assert '<svg viewBox=' in html
    assert before == {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}


def test_regression_shows_baseline_candidate_diff_and_pt_total_sample(tmp_path):
    report = report_fixture(tmp_path, 'regression')
    first = report['records'][0]
    first.update(status='image-failed', baseline_image='jobs/image.png', difference_image='jobs/image.png', comparison={'evaluation': {'failures': ['regions.center.mae']}})
    report['records'][2].update(status='pass', image='jobs/image.png', timing={'frames': 2048, 'warmup_frames': 0, 'mean_ms': 30, 'median_ms': 30, 'total_ms': 61440, 'sample_ms': 30})
    html = write_report(tmp_path, report).read_text(encoding='utf-8')
    assert 'Baseline' in html and 'Candidate' in html and 'Difference' in html
    assert 'regions.center.mae' in html
    assert '61440.000 ms' in html and '30.000 ms/sample' in html


def test_report_uses_actual_sampling_budget_instead_of_fixed_pt2048_title(tmp_path):
    report = report_fixture(tmp_path)
    report['records'][0]['configuration'] = {'frames': 96, 'warmup': 16, 'spp_per_frame': 1, 'accumulation': False}
    report['records'][0]['timing'].update(frames=96, warmup_frames=16)
    report['records'][2].update(status='complete', image='jobs/image.png',
        configuration={'frames': 512, 'warmup': 16, 'spp_per_frame': 1, 'accumulation': True},
        timing={'frames': 512, 'warmup_frames': 16, 'total_ms': 1024, 'sample_ms': 2})
    html = write_report(tmp_path, report).read_text(encoding='utf8')
    assert '<h3>PT512</h3>' in html and '512-sample total 1024.000 ms' in html
    assert '96 frames' in html and '16 warmup' in html
    assert '2048-sample total' not in html


def test_report_discloses_different_golden_and_candidate_sampling_budgets(tmp_path):
    report = report_fixture(tmp_path, 'regression')
    report['records'][2].update(status='image-failed', image='jobs/image.png',
        timing={'frames':512,'warmup_frames':16,'total_ms':1024,'sample_ms':2},
        sampling_comparison={'golden':{'frames':2048,'warmup_frames':64},
                             'candidate':{'frames':512,'warmup_frames':16},
                             'threshold_policy':'golden-unchanged'})
    html = write_report(tmp_path, report).read_text(encoding='utf8')
    visible = html.split('class="budget-comparison">',1)[1].split('</p>',1)[0]
    assert '2048 spp' in visible and '512 spp' in visible and 'unchanged' in visible


def test_report_rejects_links_outside_archive(tmp_path):
    report = report_fixture(tmp_path)
    report['records'][0]['image'] = '../secret.png'
    with pytest.raises(ValueError, match='relative|outside'):
        write_report(tmp_path, report)


def test_workflow_top_level_failures_are_visible_once(tmp_path):
    report = report_fixture(tmp_path, 'regression')
    report['records'][0].update(status='image-failed', failures=['full.mae: failed', 'corner.rmse: failed'], comparison={'evaluation': {'failures': ['corner.rmse: failed']}})
    html = write_report(tmp_path, report).read_text(encoding='utf-8')
    assert html.count('<li>full.mae: failed</li>') == 1
    assert html.count('<li>corner.rmse: failed</li>') == 1


@pytest.mark.parametrize('golden,change,signed', [(20, 6.5, '+6.50%'), (24, -11.25, '-11.25%')])
@pytest.mark.parametrize('same_hardware', [True, False])
def test_regression_performance_change_is_visible_and_warning_only(tmp_path, golden, change, signed, same_hardware):
    report = report_fixture(tmp_path, 'regression')
    report['records'][0].update(status='pass', performance={
        'golden_mean_ms': golden, 'mean_change_percent': change,
        'policy': 'warning-only', 'machine_observation_equal': same_hardware})
    html = write_report(tmp_path, report).read_text(encoding='utf8')
    cards = html.split('<article')
    visible = cards[1].split('<details', 1)[0]
    assert f'golden mean {golden:.3f} ms' in visible
    assert 'candidate mean 21.300 ms' in visible
    assert signed in visible and 'warning-only' in visible
    assert ('not comparable' in visible) is (not same_hardware)
    assert 'Performance' not in cards[2].split('</article>', 1)[0]
    assert 'Performance' not in cards[3].split('</article>', 1)[0]
