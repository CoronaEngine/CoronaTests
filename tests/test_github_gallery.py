"""GitHub Markdown galleries reference the formal originals without copies."""
import hashlib
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from PIL import Image


BEGIN = '<!-- BEGIN GOLDEN PREVIEW -->'
END = '<!-- END GOLDEN PREVIEW -->'


class Images(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        if tag == 'img':
            self.paths.extend(value for key, value in attrs if key == 'src')


def image_paths(text):
    parser = Images()
    parser.feed(text)
    return parser.paths + re.findall(r'!\[[^\]]*\]\(([^\s)]+)\)', text)


def manifest(root):
    records = []
    filenames = {'gi': 'gi.png', 'pt-reblur': 'pt-reblur.png', 'pt2048': 'pt.png'}
    for scene in ('room', 'dragon', 'hair'):
        for algorithm in filenames:
            row = {'scene_id': scene, 'scene_name': scene.title(), 'algorithm_id': algorithm,
                   'job_id': scene + '--' + algorithm, 'status': 'complete',
                   'configuration': {'frames': 512 if algorithm == 'pt2048' else 192, 'warmup': 64},
                   'timing': {'frames': 512 if algorithm == 'pt2048' else 192,
                              'warmup_frames': 64, 'mean_ms': 12.5,
                              'shown_frame_ms': 13.75, 'median_ms': 12.25,
                              'total_ms': 4000, 'sample_ms': 7.8125},
                   'elapsed_seconds': 6.5, 'engine_fingerprint': 'capture-build'}
            if scene == 'hair':
                row.update(status='expected-unsupported', reason='UNSUPPORTED_HAIR')
                row.pop('timing')
            else:
                relative = scene + '/' + filenames[algorithm]
                row['image'] = relative
                path = root / 'golden' / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                Image.new('RGB', (24, 16), (90, 120, 160)).save(path)
                row['image_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            records.append(row)
    return {'id': 'golden-test', 'created_utc': '2026-11-18T04:45:12+00:00',
            'suite': 'full', 'valid': True, 'calibrated': False,
            'coverage': {'scenes': ['room', 'dragon', 'hair'], 'algorithms': list(filenames), 'jobs': 9},
            'metadata': {'engine_fingerprint': 'default-build'}, 'records': records}


def write(root, value):
    from scripts.github_gallery import write_github_gallery
    return write_github_gallery(root, value)


def test_summary_links_every_scene_and_details_show_original_images_once(tmp_path):
    value = manifest(tmp_path)
    before = {p.relative_to(tmp_path): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in tmp_path.rglob('*.png')}
    write(tmp_path, value)
    summary = (tmp_path / 'GALLERY.md').read_text(encoding='utf-8')
    detail_images = []
    for scene in ('room', 'dragon', 'hair'):
        assert 'docs/golden/' + scene + '.md' in summary
        page = tmp_path / 'docs/golden' / (scene + '.md')
        text = page.read_text(encoding='utf-8')
        for relative in image_paths(text):
            assert relative.startswith('../../golden/' + scene + '/')
            url = urlsplit(relative)
            assert not url.scheme and not url.netloc
            target = (page.parent / unquote(url.path)).resolve()
            assert target.is_relative_to(tmp_path.resolve()) and target.is_file()
            detail_images.append(target.relative_to(tmp_path.resolve()))
    assert len(detail_images) == len(set(detail_images)) == 6
    assert set(detail_images) == set(before)
    after = {p.relative_to(tmp_path): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in tmp_path.rglob('*.png')}
    assert after == before
    assert not (tmp_path / 'README.md').exists()


def test_scene_pages_show_budget_timing_dynamic_pt_label_and_unsupported_reason(tmp_path):
    write(tmp_path, manifest(tmp_path))
    detail = (tmp_path / 'docs/golden/dragon.md').read_text(encoding='utf-8')
    assert 'GI' in detail and 'PT+REBLUR' in detail
    assert re.search(r'PT\s*512', detail)
    assert '192' in detail and '64' in detail
    assert '12.5' in detail and re.search(r'\bms\b', detail)
    assert '2048' not in detail
    unsupported = (tmp_path / 'docs/golden/hair.md').read_text(encoding='utf-8')
    assert 'UNSUPPORTED_HAIR' in unsupported
    assert image_paths(unsupported) == []


def test_readme_preview_prefers_dragon_and_preserves_surrounding_prose(tmp_path):
    value = manifest(tmp_path)
    original = '# Project\n\nIntro stays.\n\n' + BEGIN + '\nStale preview\n' + END + '\n\nFooter stays.\n'
    readme = tmp_path / 'README.md'
    readme.write_text(original, encoding='utf-8')
    write(tmp_path, value)
    actual = readme.read_text(encoding='utf-8')
    assert actual.partition(BEGIN)[0] == original.partition(BEGIN)[0]
    assert actual.partition(END)[2] == original.partition(END)[2]
    assert 'Stale preview' not in actual
    assert len(image_paths(actual)) == 3
    assert all(path.startswith('golden/dragon/') for path in image_paths(actual))
    assert all((tmp_path / unquote(path)).is_file() for path in image_paths(actual))


def test_readme_preview_falls_back_to_available_scene_without_placeholder_images(tmp_path):
    value = manifest(tmp_path)
    value['records'] = [row for row in value['records'] if row['scene_id'] != 'dragon']
    (tmp_path / 'README.md').write_text(BEGIN + '\nold\n' + END, encoding='utf-8')
    write(tmp_path, value)
    preview = (tmp_path / 'README.md').read_text(encoding='utf-8')
    assert len(image_paths(preview)) == 3
    assert all(path.startswith('golden/room/') for path in image_paths(preview))
    assert 'docs/golden/hair.md' in (tmp_path / 'GALLERY.md').read_text(encoding='utf-8')
