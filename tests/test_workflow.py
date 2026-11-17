"""Exercise real workflow publication and failure semantics at the GPU boundary."""
import json
from pathlib import Path
import zipfile

from PIL import Image
import pytest

from scripts import workflow


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def project(tmp_path):
    write_json(tmp_path / "scenes/suite.json", {"schema": 1, "full": ["box"], "quick": ["box"],
        "scenes": {"box": {"name": "Box", "expected_unsupported": {}, "regions": {}}}})
    write_json(tmp_path / "recipes/algorithms.json", {"schema": 1, "algorithms": {
        "pt2048": {"id": "pt2048", "label": "PT2048", "frames": 3, "warmup": 0}}})
    engine = tmp_path / "runtime/engine.exe"
    engine.parent.mkdir()
    engine.write_bytes(b"fixture-runtime")
    source = tmp_path / "prepared/box/scene.xml"
    source.parent.mkdir(parents=True)
    source.write_text("<scene/>", encoding="utf-8")
    scene = {"id": "box", "name": "Box", "source": str(source), "directory": str(source.parent),
             "source_sha256": "fixture", "asset_fingerprint": "fixture-assets", "files": {}}
    return tmp_path, engine, scene


def execute(project, kind, *, extra=(), capture=None, prepare=None):
    root, engine, scene = project
    options = workflow.parse_options(kind, ["--engine-executable", str(engine), *extra], root=root)
    return workflow.execute(kind, options, root=root,
        prepare=prepare or (lambda *args, **kwargs: {"schema": 1, "scenes": [scene]}),
        capture_job=capture or good_capture, thermal=lambda: {"driver_protection": True})


def good_capture(scene, algorithm, engine, output, **kwargs):
    output.mkdir(parents=True, exist_ok=True)
    # Nonconstant reference ensures a black replacement is observable.
    image = Image.new("RGB", (1920, 1080), (160, 120, 80))
    image.paste((40, 90, 130), (0, 0, 960, 1080))
    image.save(output / "image.png")
    (output / "frames.csv").write_text("frame,renderer_ms\n0,2\n1,2\n2,2\n", encoding="utf-8")
    return {"schema": 1, "scene_id": scene["id"], "algorithm_id": algorithm["id"],
        "status": "complete", "image": "image.png", "configuration": {},
        "timing": {"frames": 3, "warmup_frames": 0, "mean_ms": 2, "median_ms": 2,
                   "total_ms": 6, "sample_ms": 2}, "elapsed_seconds": .1, "owned_engine_closed": True}


def pt_noise_capture(scene, algorithm, engine, output, **kwargs):
    import numpy as np
    row = good_capture(scene, algorithm, engine, output, **kwargs)
    row['configuration'] = dict(algorithm)
    row['timing'].update(frames=algorithm['frames'], warmup_frames=algorithm['warmup'])
    if algorithm['frames'] != 2048:
        with Image.open(output / 'image.png') as image:
            a = np.asarray(image).astype(int)
        noise = ((np.indices(a.shape[:2]).sum(axis=0) % 2) * 2 - 1) * 6
        Image.fromarray((a + noise[:, :, None]).astype(np.uint8)).save(output / 'image.png')
    return row


def pt_policy_project(project):
    from scripts import pt_policy
    root, engine, scene = project
    suite = workflow.load(root / 'scenes/suite.json')
    suite['scenes']['box']['regions'] = {'center': [.25, .25, .75, .75]}
    write_json(root / 'scenes/suite.json', suite)
    golden, code = execute(project, 'golden', extra=['--baseline-id', 'pt-reference', '--pt-spp', '2048'],
                           capture=pt_noise_capture)
    assert code == 0
    row = workflow.load(Path(golden['output']) / 'golden.json')['records'][0]
    reference = Path(golden['output']) / row['image']
    candidate_dir = root / 'calibration'
    pt_noise_capture(scene, {'id': 'pt2048', 'frames': 512, 'warmup': 0}, engine, candidate_dir)
    measured = pt_policy.measure(reference, candidate_dir / 'image.png', suite['scenes']['box']['regions'])
    policy = {'schema': 1, 'id': 'pt512-ds8-ds4-v1', 'candidate_spp': 512, 'reference_spp': 2048,
              'allowances': pt_policy.ALLOWANCES, 'cases': {row['job_id']: pt_policy.case_profile(measured, row)}}
    write_json(root / 'recipes/pt512-regression.json', policy)
    return golden, policy


def test_pt512_multiscale_drives_gate_and_report_instead_of_raw_noise(project):
    golden, policy = pt_policy_project(project)
    result, code = execute(project, 'regression', extra=['--golden', golden['output'], '--pt-spp', '512'],
                           capture=pt_noise_capture)
    row = result['records'][0]
    assert row['comparison']['mae'] > .003
    assert code == 0 and row['status'] == 'pass'
    assert row['comparison_policy']['id'] == policy['id']
    assert row['comparison']['multiscale_evaluation']['scale_verdicts'] == {'DS8': 'pass', 'DS4': 'pass'}
    output = Path(result['output'])
    assert workflow.load(output / 'pt512-policy.json') == policy
    html = (output / 'pages/box.html').read_text(encoding='utf-8')
    assert 'DS8' in html and 'DS4' in html and 'diagnostic only' in html
    assert 'Golden thresholds unchanged' not in html
    saved = workflow.load(output / row['comparison_json'])
    assert saved['multiscale_evaluation']['status'] == 'pass'


@pytest.mark.parametrize('invalid', ['missing', 'reference', 'roi'])
def test_pt512_invalid_policy_stops_before_preparation_or_gpu(project, invalid):
    golden, policy = pt_policy_project(project)
    path = project[0] / 'recipes/pt512-regression.json'
    if invalid == 'missing': path.unlink()
    else:
        if invalid == 'reference': policy['cases']['box--pt2048']['reference_sha256'] = 'c' * 64
        else: policy['cases']['box--pt2048']['scales']['DS4']['region_bounds']['center'] = [0, 0, .5, .5]
        write_json(path, policy)
    with pytest.raises((ValueError, FileNotFoundError), match='PT512|reference|pt512'):
        execute(project, 'regression', extra=['--golden', golden['output'], '--pt-spp', '512'],
                prepare=lambda *a, **k: pytest.fail('Invalid policy must fail before preparation/GPU'))


def test_non512_budget_keeps_original_thresholds(project):
    golden, _ = pt_policy_project(project)
    result, code = execute(project, 'regression', extra=['--golden', golden['output'], '--pt-spp', '1024'],
                           capture=pt_noise_capture)
    assert code != 0 and result['records'][0]['status'] == 'image-failed'
    assert 'comparison_policy' not in result['records'][0]


def test_pt512_policy_cannot_mask_execution_failure(project):
    golden, _ = pt_policy_project(project)
    def failed(scene, algorithm, engine, output, **kwargs):
        return {'scene_id': scene['id'], 'algorithm_id': algorithm['id'], 'status': 'failed',
                'error': 'fixture crash', 'owned_engine_closed': True}
    result, code = execute(project, 'regression', extra=['--golden', golden['output'], '--pt-spp', '512'], capture=failed)
    assert code != 0 and result['records'][0]['status'] == 'failed'


def test_regeneration_preserves_saved_results_and_rejects_wrong_reference(project):
    from scripts.regenerate_report import regenerate
    golden, code = execute(project, 'golden', extra=['--baseline-id', 'presentation'])
    result, code = execute(project, 'regression', extra=['--golden', golden['output']])
    assert code == 0
    output = Path(result['output'])
    for name in ('report.json', 'workflow-result.json'):
        saved = workflow.load(output / name)
        saved['records'][0]['comparison'].pop('raw_thresholds', None)
        workflow.save(output / name, saved)
    protected = {p: p.read_bytes() for p in output.rglob('*') if p.is_file() and p.suffix in ('.json', '.csv', '.png')}
    regenerate(output, golden=golden['output'])
    assert all(p.read_bytes() == before for p, before in protected.items())
    html = (output / 'pages/box.html').read_text(encoding='utf-8')
    assert '0.003000' in html
    other_golden, _ = execute(project, 'golden', extra=['--baseline-id', 'other-presentation'])
    with pytest.raises(ValueError, match='version|fingerprint'):
        regenerate(output, golden=other_golden['output'])
    reference = output / result['records'][0]['baseline_image']
    Image.new('RGB', (1920, 1080), 'black').save(reference)
    with pytest.raises(ValueError, match='reference|Golden'):
        regenerate(output, golden=golden['output'])
    with pytest.raises(ValueError, match='Sealed'):
        regenerate(golden['output'])


def test_golden_fresh_directory_is_sealed_and_can_regress(project):
    result, code = execute(project, "golden", extra=["--baseline-id", "first"])
    assert code == 0
    golden = Path(result["output"])
    manifest = json.loads((golden / "golden.json").read_text(encoding="utf-8"))
    assert manifest["valid"] is True
    assert manifest["calibrated"] is False
    assert manifest["thresholds_ready"] is True and manifest["threshold_policy"] == "fixed"
    assert len(manifest["records"]) == 1
    assert (golden / "index.html").is_file()
    assert list(golden.parent.glob("first.zip"))
    regression, code = execute(project, "regression", extra=["--golden", str(golden)])
    assert code == 0
    assert regression["records"][0]["status"] == "pass"
    assert (golden / "golden.json").read_text(encoding="utf-8") == json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def test_full_golden_captures_each_job_once_and_retains_exactly_92_pngs(project, monkeypatch):
    from collections import Counter
    import shutil
    from scripts import compare
    root, engine, source = project
    suite = workflow.load(workflow.ROOT / 'scenes/suite.json')
    write_json(root / 'scenes/suite.json', suite)
    write_json(root / 'recipes/algorithms.json', {'schema': 1, 'algorithms': {
        name: {'id': name, 'frames': 3, 'warmup': 0} for name in ('gi', 'pt-reblur', 'pt2048')}})
    prepared = [{**source, 'id': name} for name in suite['full']]
    template = root / 'capture-template'
    good_capture(source, {'id': 'pt2048'}, engine, template)
    calls = Counter()
    def capture(scene, algorithm, executable, output, **kwargs):
        job = scene['id'] + '--' + algorithm['id']
        calls[job] += 1
        output.mkdir(parents=True)
        record = {'scene_id': scene['id'], 'algorithm_id': algorithm['id'],
                  'configuration': dict(algorithm), 'owned_engine_closed': True}
        if algorithm['id'] in scene.get('expected_unsupported', {}):
            return {**record, 'status': 'expected-unsupported', 'reason': 'fixture-supported-boundary'}
        shutil.copyfile(template / 'image.png', output / 'image.png')
        shutil.copyfile(template / 'frames.csv', output / 'frames.csv')
        return {**record, 'status': 'complete', 'image': 'image.png', 'timing': {
            'frames': 3, 'warmup_frames': 0, 'mean_ms': 2, 'median_ms': 2, 'total_ms': 6, 'sample_ms': 2}}
    monkeypatch.setattr(compare, 'calibrate', lambda *a, **k: pytest.fail('Golden must not run repeated noise calibration'))
    result, code = execute(project, 'golden', extra=['--baseline-id', 'full-once'], capture=capture,
        prepare=lambda *a, **k: {'schema': 1, 'scenes': prepared})
    assert code == 0
    assert len(calls) == 105 and set(calls.values()) == {1}
    assert result['summary'] == {'complete': 92, 'expected-unsupported': 13}
    output = Path(result['output'])
    images = list(output.rglob('*.png'))
    assert len(images) == 92
    assert not list(output.rglob('repeat-1')) and not list(output.rglob('repeat-2'))
    with zipfile.ZipFile(output.with_suffix('.zip')) as archive:
        assert len([name for name in archive.namelist() if name.endswith('.png')]) == 92
    # Completed resume must reuse the sealed bundle without any capture calls.
    resumed, code = execute(project, 'golden', extra=['--baseline-id', 'full-once', '--resume'],
        capture=lambda *a, **k: pytest.fail('Completed golden resume must not recapture'),
        prepare=lambda *a, **k: {'schema': 1, 'scenes': prepared})
    assert code == 0 and resumed['summary'] == result['summary']


def test_failed_capture_never_publishes_valid_golden(project):
    def failed(scene, algorithm, engine, output, **kwargs):
        output.mkdir(parents=True, exist_ok=True)
        return {"scene_id": scene["id"], "algorithm_id": algorithm["id"], "status": "failed",
                "error": "actual capture failed", "owned_engine_closed": True}
    report, code = execute(project, "golden", extra=["--baseline-id", "bad"], capture=failed)
    assert code != 0
    assert json.loads((Path(report["output"]) / "golden.json").read_text())["valid"] is False
    assert not (Path(report["output"]).parent / "bad.zip").exists()


def test_single_capture_does_not_require_empirical_calibration(project):
    calls = []
    def white_capture(scene, algorithm, engine, output, **kwargs):
        calls.append((scene['id'], algorithm['id']))
        record = good_capture(scene, algorithm, engine, output, **kwargs)
        Image.new('RGB', (1920, 1080), 'white').save(output / 'image.png')
        return record
    result, code = execute(project, 'golden', extra=['--baseline-id', 'single'], capture=white_capture)
    assert calls == [('box', 'pt2048')]
    assert code == 0 and result['valid'] is True and result['calibrated'] is False
    assert result['thresholds_ready'] is True and result['threshold_policy'] == 'fixed'
    output = Path(result['output'])
    html = (output / 'index.html').read_text(encoding='utf8')
    assert 'Thresholds: fixed' in html and 'calibration-incomplete' not in html
    assert len(list(output.rglob('*.png'))) == 1
    assert (output.parent / 'single.zip').is_file()


def test_existing_golden_is_not_overwritten(project):
    execute(project, "golden", extra=["--baseline-id", "kept"])
    with pytest.raises(FileExistsError):
        execute(project, "golden", extra=["--baseline-id", "kept"])


def test_corrupted_golden_png_is_rejected_before_gpu(project):
    report, _ = execute(project, "golden", extra=["--baseline-id", "corrupt"])
    golden = Path(report["output"])
    image = golden / json.loads((golden / "golden.json").read_text())["records"][0]["image"]
    image.write_bytes(b"not PNG")
    with pytest.raises(ValueError, match="[Hh]ash|[Ii]ntegrity|[Cc]orrupt"):
        execute(project, "regression", extra=["--golden", str(golden)],
                capture=lambda *args, **kwargs: pytest.fail("GPU must not run for corrupt golden"))


def test_resource_fingerprint_mismatch_is_incomparable(project):
    report, _ = execute(project, "golden", extra=["--baseline-id", "assets"])
    root, engine, scene = project
    changed = {**scene, "asset_fingerprint": "different-assets"}
    report, code = execute(project, "regression", extra=["--golden", report["output"]],
        prepare=lambda *args, **kwargs: {"schema": 1, "scenes": [changed]},
        capture=lambda *args, **kwargs: pytest.fail("incomparable input must not run GPU"))
    assert code != 0
    assert report["records"][0]["status"] == "incomparable"


def test_resume_rejects_changed_capture_artifact(project):
    report, _ = execute(project, "golden", extra=["--baseline-id", "resume"])
    output = Path(report["output"])
    next(output.glob("images/*.png")).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="[Hh]ash|[Aa]rtifact|[Ii]ntegrity"):
        execute(project, "golden", extra=["--baseline-id", "resume", "--resume"],
                capture=lambda *args, **kwargs: pytest.fail("tampered complete result must be rejected"))


def test_selector_rejects_unknown_or_empty_tasks(project):
    with pytest.raises(ValueError, match="[Uu]nknown|[Ss]elected"):
        execute(project, "golden", extra=["--scene", "typo", "--baseline-id", "empty"])
    with pytest.raises(ValueError, match="[Bb]aseline|[Ii]dentifier"):
        execute(project, "golden", extra=["--baseline-id", "../escape"])


def test_changed_region_contract_is_incomparable_before_gpu(project):
    report, _ = execute(project, "golden", extra=["--baseline-id", "regions"])
    root, _, _ = project
    suite = json.loads((root / "scenes/suite.json").read_text())
    suite["scenes"]["box"]["regions"] = {"corner": [0, 0, .2, .2]}
    write_json(root / "scenes/suite.json", suite)
    result, code = execute(project, "regression", extra=["--golden", report["output"]],
        capture=lambda *args, **kwargs: pytest.fail("changed region contract must not launch GPU"))
    assert code != 0
    assert result["records"][0]["status"] == "incomparable"


def test_resume_keeps_sealed_golden_manifest_and_archive_unchanged(project):
    report, _ = execute(project, "golden", extra=["--baseline-id", "sealed"])
    output = Path(report["output"])
    original = (output / "golden.json").read_bytes()
    archive = output.parent / "sealed.zip"
    archive_original = archive.read_bytes()
    _, code = execute(project, "golden", extra=["--baseline-id", "sealed", "--resume"],
        capture=lambda *args, **kwargs: pytest.fail("sealed golden must not render again"))
    assert code == 0
    assert (output / "golden.json").read_bytes() == original
    assert archive.read_bytes() == archive_original


def test_resume_preserves_interrupted_attempt_and_completes(project):
    def interrupted(scene, algorithm, engine, output, **kwargs):
        output.mkdir(parents=True, exist_ok=True)
        (output / "interrupted.log").write_text("retained evidence")
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        execute(project, "golden", extra=["--baseline-id", "interrupted"], capture=interrupted)
    report, code = execute(project, "golden", extra=["--baseline-id", "interrupted", "--resume"])
    assert code == 0
    retained = report['metadata']['recovered_attempts']
    assert len(retained) == 1 and retained[0]['category'] == 'interrupted'
    assert retained[0]['logs']['interrupted.log'] == 'retained evidence'
    assert not (Path(report['output'])/'runs').exists()


def test_resume_repairs_missing_publication_after_sealing(project):
    result, _ = execute(project, "golden", extra=["--baseline-id", "publication"])
    root, _, _ = project
    output = Path(result["output"])
    original_manifest = (output / "golden.json").read_bytes()
    (output.parent / "publication.zip").unlink()
    (root / "baselines/publication.json").unlink()
    (root / "baselines/publication.thresholds.json").unlink()
    result, code = execute(project, "golden", extra=["--baseline-id", "publication", "--resume"])
    assert code == 0
    assert (output.parent / "publication.zip").is_file()
    assert (root / "baselines/publication.json").is_file()
    assert (root / "baselines/publication.thresholds.json").is_file()
    assert (output / "golden.json").read_bytes() == original_manifest


def test_baseline_id_cannot_repoint_existing_published_index(project):
    execute(project, "golden", extra=["--baseline-id", "unique"])
    root, _, _ = project
    original_index = (root / "baselines/unique.json").read_bytes()
    with pytest.raises(ValueError, match="[Bb]aseline.*[Aa]lready|[Bb]aseline.*[Pp]ublished"):
        execute(project, "golden", extra=["--baseline-id", "unique", "--output", str(root / "new-output")],
            capture=lambda *args, **kwargs: pytest.fail("duplicate ID must not launch GPU"))
    assert (root / "baselines/unique.json").read_bytes() == original_index


def test_resume_retries_failed_capture_and_retains_failure_evidence(project):
    def failure(scene, algorithm, engine, output, **kwargs):
        output.mkdir(parents=True, exist_ok=True)
        (output / "failed.log").write_text("transient failure")
        return {"schema": 1, "scene_id": scene["id"], "algorithm_id": algorithm["id"],
                "status": "failed", "error": "transient", "owned_engine_closed": True}
    _, code = execute(project, "golden", extra=["--baseline-id", "retry"], capture=failure)
    assert code != 0
    result, code = execute(project, "golden", extra=["--baseline-id", "retry", "--resume"])
    assert code == 0
    assert result['metadata']['recovered_attempts'][0]['logs']['failed.log'] == 'transient failure'
    assert not (Path(result['output'])/'runs').exists()


def test_resume_rejects_archive_missing_images_and_report(project):
    result, _ = execute(project, "golden", extra=["--baseline-id", "zip-members"])
    output = Path(result["output"])
    with zipfile.ZipFile(output.parent / "zip-members.zip", "w") as zipped:
        zipped.writestr("golden.json", (output / "golden.json").read_bytes())
    with pytest.raises(ValueError, match="[Aa]rchive.*[Ii]ntegrity|[Aa]rchive.*[Mm]ember"):
        execute(project, "golden", extra=["--baseline-id", "zip-members", "--resume"])


def test_sealed_golden_with_missing_workflow_result_cannot_publish(project):
    result, _ = execute(project, "golden", extra=["--baseline-id", "seal"])
    root, _, _ = project
    output = Path(result["output"])
    (output / "workflow-result.json").unlink()
    (output.parent / "seal.zip").unlink()
    (root / "baselines/seal.json").unlink()
    with pytest.raises(ValueError, match="[Ii]ntegrity|[Aa]rtifact"):
        execute(project, "golden", extra=["--baseline-id", "seal", "--resume"])
    assert not (output.parent / "seal.zip").exists()
    assert not (root / "baselines/seal.json").exists()


def test_default_render_commands_select_all_scenes_and_quick_requires_explicit_flag(tmp_path):
    root = Path(__file__).resolve().parents[1]
    base = ["--config", str(tmp_path / "no-local-config.json")]
    for kind in ("golden", "regression"):
        options = workflow.parse_options(kind, base, root=root)
        _, _, scenes, algorithms = workflow.selection(root, options)
        assert len(scenes) == 35, kind
        assert len(scenes) * len(algorithms) == 105, kind
        quick = workflow.parse_options(kind, [*base, "--suite", "quick"], root=root)
        _, _, quick_scenes, quick_algorithms = workflow.selection(root, quick)
        assert len(quick_scenes) * len(quick_algorithms) == 18


def test_custom_sampling_golden_compares_other_budgets_without_relaxing_thresholds(project):
    captures = []
    def sampled_capture(scene, algorithm, engine, output, **kwargs):
        captures.append((algorithm['frames'], algorithm['warmup']))
        record = good_capture(scene, algorithm, engine, output, **kwargs)
        record['configuration'] = dict(algorithm)
        frames, warmup = algorithm['frames'], algorithm['warmup']
        record['timing'].update(frames=frames, warmup_frames=warmup, total_ms=frames * 2)
        (output / 'frames.csv').write_text('frame,renderer_ms\n' + ''.join(f'{i},2\n' for i in range(1, frames + 1)))
        return record
    sampling = ['--pt-spp', '5', '--warmup-frames', '1']
    result, code = execute(project, 'golden', extra=['--baseline-id', 'five-spp', *sampling], capture=sampled_capture)
    assert code == 0 and captures == [(5, 1)]
    output = Path(result['output'])
    manifest_before = (output / 'golden.json').read_bytes()
    thresholds_before = (output / 'thresholds.json').read_bytes()
    row = json.loads(manifest_before)['records'][0]
    assert row['configuration']['frames'] == row['timing']['frames'] == 5
    html = (output / 'pages/box.html').read_text(encoding='utf8')
    assert '<h2>PT5 ' in html and '5-sample total' in html
    _, code = execute(project, 'regression', extra=['--golden', str(output), *sampling], capture=sampled_capture)
    assert code == 0 and captures == [(5, 1)] * 2
    other, code = execute(project, 'regression', extra=['--golden', str(output), '--pt-spp', '7',
        '--warmup-frames', '0'], capture=sampled_capture)
    assert code == 0 and captures[-1] == (7, 0)
    compared = other['records'][0]
    assert compared['status'] == 'pass' and compared['input_fingerprint'] != row['input_fingerprint']
    assert compared['sampling_comparison']['golden'] == {'frames': 5, 'warmup_frames': 1}
    assert compared['sampling_comparison']['candidate'] == {'frames': 7, 'warmup_frames': 0}
    assert compared['sampling_comparison']['threshold_policy'] == 'golden-unchanged'
    def black_capture(*args, **kwargs):
        record = sampled_capture(*args, **kwargs)
        Image.new('RGB', (1920, 1080), 'black').save(args[3] / 'image.png')
        return record
    degraded, code = execute(project, 'regression', extra=['--golden', str(output), '--pt-spp', '7',
        '--warmup-frames', '0'], capture=black_capture)
    assert code == 1 and degraded['records'][0]['status'] == 'image-failed'
    assert degraded['records'][0]['failures']
    def unexpected_capture(*args, **kwargs):
        pytest.fail('Changed-budget resume must not launch the GPU')
    with pytest.raises(ValueError, match='fingerprint'):
        execute(project, 'golden', extra=['--baseline-id', 'five-spp', '--resume', '--pt-spp', '7',
            '--warmup-frames', '1'], capture=unexpected_capture)
    assert (output / 'golden.json').read_bytes() == manifest_before
    assert (output / 'thresholds.json').read_bytes() == thresholds_before
