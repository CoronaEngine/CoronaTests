"""Sampling overrides must reach both workflows without changing old defaults."""
import json
from pathlib import Path

import pytest

from scripts import workflow

ROOT = Path(__file__).resolve().parents[1]


def test_golden_and_regression_read_independent_default_config_files(tmp_path):
    def write(name, spp):
        (tmp_path / name).write_text(json.dumps({'sampling': {'pt_spp': spp}}))
    write('config.local.json', 4096)
    write('config.golden.local.json', 2048)
    write('config.regression.local.json', 512)
    golden = workflow.parse_options('golden', [], root=tmp_path)
    regression = workflow.parse_options('regression', [], root=tmp_path)
    assert golden.config == tmp_path / 'config.golden.local.json'
    assert regression.config == tmp_path / 'config.regression.local.json'
    assert (golden.pt_spp, regression.pt_spp) == (2048, 512)
    write('config.golden.local.json', 1024)
    assert workflow.parse_options('regression', [], root=tmp_path).pt_spp == 512
    write('config.regression.local.json', 256)
    assert workflow.parse_options('golden', [], root=tmp_path).pt_spp == 1024
    explicit = workflow.parse_options('regression', ['--config', str(tmp_path / 'config.local.json')], root=tmp_path)
    assert explicit.pt_spp == 4096


@pytest.mark.parametrize('kind', ['golden', 'regression'])
def test_sampling_config_applies_to_both_render_entrypoints(tmp_path, kind):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'sampling': {
        'pt_spp': 512, 'gi_frames': 128, 'pt_reblur_frames': 96, 'warmup_frames': 16}}))
    options = workflow.parse_options(kind, ['--config', str(config)], root=ROOT)
    _, recipes, scenes, algorithms = workflow.selection(ROOT, options)
    assert len(scenes) * len(algorithms) == 105
    assert recipes['pt2048']['frames'] == 512
    assert recipes['pt2048']['accumulation'] is True
    assert recipes['pt2048']['spp_per_frame'] == 1
    assert recipes['gi']['frames'] == 128 and recipes['gi']['accumulation'] is False
    assert recipes['pt-reblur']['frames'] == 96 and recipes['pt-reblur']['accumulation'] is False
    assert {r['warmup'] for r in recipes.values()} == {16}


def test_cli_sampling_overrides_only_the_supplied_config_fields(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'sampling': {
        'pt_spp': 512, 'gi_frames': 128, 'pt_reblur_frames': 96, 'warmup_frames': 16}}))
    options = workflow.parse_options('golden', ['--config', str(config), '--pt-spp', '256',
        '--gi-frames', '80', '--warmup-frames', '8'], root=ROOT)
    _, recipes, _, _ = workflow.selection(ROOT, options)
    assert recipes['pt2048']['frames'] == 256
    assert recipes['gi']['frames'] == 80
    assert recipes['pt-reblur']['frames'] == 96
    assert {r['warmup'] for r in recipes.values()} == {8}


def test_absent_sampling_preserves_the_existing_golden_recipe_contract(tmp_path):
    original = json.loads((ROOT / 'recipes/algorithms.json').read_text())['algorithms']
    options = workflow.parse_options('regression', ['--config', str(tmp_path / 'absent.json')], root=ROOT)
    _, recipes, _, _ = workflow.selection(ROOT, options)
    assert recipes == original


@pytest.mark.parametrize('sampling', [
    {'pt_spp': 0}, {'pt_spp': -1}, {'pt_spp': True}, {'pt_spp': 1.5},
    {'gi_frames': '192'}, {'warmup_frames': -1}, {'pt_sp': 512}, None,
])
def test_invalid_sampling_config_is_rejected_instead_of_silently_ignored(tmp_path, sampling):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'sampling': sampling}))
    with pytest.raises(ValueError, match='sampling|SPP|frames|warmup'):
        options = workflow.parse_options('golden', ['--config', str(config)], root=ROOT)
        workflow.selection(ROOT, options)


@pytest.mark.parametrize('arguments', [
    ['--pt-spp', '64'], ['--gi-frames', '32'],
    ['--pt-reblur-frames', '8', '--warmup-frames', '8'],
])
def test_sampling_budget_requires_at_least_one_frame_after_warmup(tmp_path, arguments):
    with pytest.raises(ValueError, match='warmup'):
        options = workflow.parse_options('golden', ['--config', str(tmp_path / 'absent.json'), *arguments], root=ROOT)
        workflow.selection(ROOT, options)


def test_one_spp_pt_capture_can_explicitly_disable_statistics_warmup(tmp_path):
    options = workflow.parse_options('golden', ['--config', str(tmp_path / 'absent.json'),
        '--algorithm', 'pt2048', '--pt-spp', '1', '--warmup-frames', '0'], root=ROOT)
    _, recipes, _, algorithms = workflow.selection(ROOT, options)
    assert algorithms == ['pt2048']
    assert recipes['pt2048']['frames'] == 1 and recipes['pt2048']['warmup'] == 0


def test_unsupported_per_frame_spp_cannot_silently_change_only_the_report(tmp_path):
    (tmp_path / 'scenes').mkdir()
    (tmp_path / 'recipes').mkdir()
    (tmp_path / 'scenes/suite.json').write_text(json.dumps({'full': ['box'], 'quick': ['box']}))
    (tmp_path / 'recipes/algorithms.json').write_text(json.dumps({'algorithms': {
        'pt2048': {'frames': 512, 'warmup': 0, 'spp_per_frame': 4}}}))
    options = workflow.parse_options('golden', [], root=tmp_path)
    with pytest.raises(ValueError, match='1 spp/frame'):
        workflow.selection(tmp_path, options)
