"""Resolve portable goldens before rendering and reject invalid formal imports."""
import copy
import json
import sys
import types

from PIL import Image
import pytest

from scripts import workflow


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def install_downloader(monkeypatch, ensure):
    module = types.ModuleType("scripts.download_golden")
    module.ensure_golden = ensure
    monkeypatch.setitem(sys.modules, "scripts.download_golden", module)


def sealed_golden(folder, *, unsupported=False, inputs=None):
    job = "box--pt2048"
    fingerprint = workflow.fingerprint(inputs) if inputs else "fixture-input"
    record = {"job_id": job, "scene_id": "box", "algorithm_id": "pt2048",
              "status": "expected-unsupported" if unsupported else "complete",
              "input_fingerprint": fingerprint, "engine": "Z:/source-machine/engine.exe"}
    profiles = {} if unsupported else {job: {"status": "calibrated", "thresholds": {},
        "provenance": {"golden_id": "portable", "job_id": job,
                       "input_fingerprint": fingerprint, "engine_fingerprint": "original-engine"}}}
    thresholds = {"schema": 1, "golden_id": "portable", "profiles": profiles}
    write_json(folder / "thresholds.json", thresholds)
    artifacts = {"thresholds.json": workflow.sha(folder / "thresholds.json")}
    if not unsupported:
        Image.new("RGB", (1920, 1080), (80, 120, 160)).save(folder / "image.png")
        record["image"] = "image.png"
        artifacts["image.png"] = workflow.sha(folder / "image.png")
    manifest = {"schema": 1, "id": "portable", "suite": "full", "valid": True,
        "calibrated": True, "created_utc": "2026-11-16T00:00:00+00:00",
        "coverage": {"scenes": ["box"], "algorithms": ["pt2048"], "jobs": 1},
        "metadata": {"engine": "Z:/source-machine/engine.exe", "engine_fingerprint": "original-engine"},
        "records": [record], "artifacts": artifacts,
        "thresholds_sha256": workflow.sha(folder / "thresholds.json")}
    write_json(folder / "golden.json", workflow.seal(manifest))
    return folder


def reseal(folder, manifest, thresholds=None):
    if thresholds is not None:
        write_json(folder / "thresholds.json", thresholds)
        manifest["thresholds_sha256"] = workflow.sha(folder / "thresholds.json")
        manifest["artifacts"]["thresholds.json"] = manifest["thresholds_sha256"]
    manifest.pop("manifest_fingerprint", None)
    write_json(folder / "golden.json", workflow.seal(manifest))


def test_pinned_default_does_not_use_machine_local_latest_or_download_during_parse(tmp_path, monkeypatch):
    write_json(tmp_path / "baselines/default.json", {"schema": 1, "id": "portable"})
    write_json(tmp_path / ".workspace/latest-golden.json", {"path": "machine-only"})
    install_downloader(monkeypatch, lambda *args, **kwargs: pytest.fail("Parsing must not download"))
    options = workflow.parse_options("regression", [], root=tmp_path)
    assert options.golden is None


def test_requested_golden_id_bypasses_machine_local_latest(tmp_path):
    write_json(tmp_path / ".workspace/latest-golden.json", {"path": "machine-only"})
    options = workflow.parse_options("regression", ["--golden-id", "portable"], root=tmp_path)
    assert options.golden_id == "portable" and options.golden is None


@pytest.mark.parametrize("configured", [False, True])
def test_explicit_golden_path_cannot_be_combined_with_golden_id(tmp_path, configured):
    if configured:
        write_json(tmp_path / "config.regression.local.json", {"golden": "chosen"})
    arguments = ["--golden-id", "portable"] + ([] if configured else ["--golden", "chosen"])
    with pytest.raises(ValueError, match="golden.*golden-id|golden-id.*golden"):
        workflow.parse_options("regression", arguments, root=tmp_path)


@pytest.mark.parametrize("golden_id", ["../escape", "bad/name", "", "."])
def test_golden_id_is_validated_before_execution(tmp_path, golden_id):
    with pytest.raises(ValueError, match="identifier"):
        workflow.parse_options("regression", ["--golden-id", golden_id], root=tmp_path)


def test_legacy_latest_pointer_still_resolves_without_a_tracked_default(tmp_path):
    write_json(tmp_path / ".workspace/latest-golden.json", {"path": "legacy-golden"})
    options = workflow.parse_options("regression", [], root=tmp_path)
    assert options.golden == tmp_path / "legacy-golden"


@pytest.mark.parametrize("mode,arguments,expected_id,offline", [
    ("default", [], None, False),
    ("id", ["--golden-id", "portable", "--offline"], "portable", True),
    ("cli", [], None, False),
    ("config", ["--offline"], None, True),
])
def test_regression_obtains_selected_golden_before_preparing_scenes(tmp_path, monkeypatch, mode, arguments, expected_id, offline):
    recipe = {"id": "pt2048", "label": "PT", "frames": 3, "warmup": 0}
    write_json(tmp_path / "scenes/suite.json", {"full": ["box"], "quick": ["box"],
        "scenes": {"box": {"regions": {}, "expected_unsupported": {"pt2048": "fixture"}}}})
    write_json(tmp_path / "recipes/algorithms.json", {"algorithms": {"pt2048": recipe}})
    write_json(tmp_path / "baselines/default.json", {"schema": 1, "id": "portable"})
    engine = tmp_path / "runtime/engine.exe"
    engine.parent.mkdir()
    engine.write_bytes(b"candidate-engine")
    source = tmp_path / "prepared/scene.xml"
    source.parent.mkdir()
    source.write_text("<scene/>")
    scene = {"id": "box", "directory": str(source.parent), "source": str(source),
             "files": {}, "asset_fingerprint": "fixture-assets"}
    golden = sealed_golden(tmp_path / "downloaded", unsupported=True, inputs={
        "scene_id": "box", "asset_fingerprint": "fixture-assets", "recipe": recipe,
        "regions": {}, "expected_diagnostics": [], "expected_unsupported": {"pt2048": "fixture"}})
    if mode == "cli":
        arguments = [*arguments, "--golden", str(golden)]
    elif mode == "config":
        write_json(tmp_path / "config.regression.local.json", {"golden": str(golden)})
    original = {path.name: path.read_bytes() for path in golden.iterdir()}
    obtained = []
    def ensure(root, baseline_id=None, *, offline=False):
        assert mode in ("default", "id"), "Explicit golden paths must not invoke the downloader"
        assert root == tmp_path and baseline_id == expected_id and offline is expected_offline
        obtained.append(True)
        return golden
    expected_offline = offline
    install_downloader(monkeypatch, ensure)
    def prepare(root, selected, *, offline):
        assert bool(obtained) is (mode in ("default", "id")) and offline is expected_offline
        return {"scenes": [scene]}
    def capture(scene, algorithm, engine, output, **kwargs):
        output.mkdir(parents=True)
        return {"scene_id": "box", "algorithm_id": "pt2048", "status": "expected-unsupported",
                "owned_engine_closed": True}
    options = workflow.parse_options("regression", ["--engine-executable", str(engine), *arguments], root=tmp_path)
    result, code = workflow.execute("regression", options, root=tmp_path, prepare=prepare,
                                   capture_job=capture, thermal=lambda: {"driver_protection": True})
    assert code == 0 and result["records"][0]["status"] == "expected-unsupported"
    assert {path.name: path.read_bytes() for path in golden.iterdir()} == original


@pytest.mark.parametrize("configured", [False, True])
def test_explicit_golden_path_is_used_before_any_downloader(tmp_path, monkeypatch, configured):
    write_json(tmp_path / "baselines/default.json", {"schema": 1, "id": "portable"})
    chosen = sealed_golden(tmp_path / "chosen")
    if configured:
        write_json(tmp_path / "config.regression.local.json", {"golden": "chosen"})
    install_downloader(monkeypatch, lambda *args, **kwargs: pytest.fail("Explicit paths must not download"))
    options = workflow.parse_options("regression", [] if configured else ["--golden", str(chosen)], root=tmp_path)
    assert options.golden == chosen


@pytest.mark.parametrize("calibrated", [False, None, 1, "true"])
def test_formal_golden_requires_explicit_true_calibration(tmp_path, calibrated):
    folder = sealed_golden(tmp_path / "golden")
    manifest = workflow.load(folder / "golden.json")
    manifest["calibrated"] = calibrated
    reseal(folder, manifest)
    with pytest.raises(ValueError, match="threshold|calibrat"):
        workflow.read_golden(folder)


@pytest.mark.parametrize('ready', [True, False, None, 1, 'true'])
def test_fixed_golden_requires_explicit_true_threshold_readiness(tmp_path, ready):
    from scripts.compare import fixed_profile
    folder = sealed_golden(tmp_path / 'golden')
    manifest = workflow.load(folder / 'golden.json')
    manifest.update(calibrated=False, thresholds_ready=ready, threshold_policy='fixed')
    thresholds = workflow.load(folder / 'thresholds.json')
    previous = thresholds['profiles']['box--pt2048']
    thresholds['profiles']['box--pt2048'] = fixed_profile(provenance=previous['provenance'])
    reseal(folder, manifest, thresholds)
    if ready is True:
        golden, profiles = workflow.read_golden(folder)
        assert golden['calibrated'] is False and profiles['profiles']['box--pt2048']['status'] == 'fixed'
    else:
        with pytest.raises(ValueError, match='threshold'):
            workflow.read_golden(folder)


@pytest.mark.parametrize("change", ["duplicate-record", "missing-record", "duplicate-scene", "wrong-job-count", "wrong-job-id"])
def test_formal_golden_rejects_incomplete_or_duplicate_coverage(tmp_path, change):
    folder = sealed_golden(tmp_path / "golden")
    manifest = workflow.load(folder / "golden.json")
    if change == "duplicate-record":
        manifest["records"].append(copy.deepcopy(manifest["records"][0]))
    elif change == "missing-record":
        manifest["records"] = []
    elif change == "duplicate-scene":
        manifest["coverage"]["scenes"].append("box")
    elif change == "wrong-job-count":
        manifest["coverage"]["jobs"] = 2
    else:
        manifest["records"][0]["job_id"] = "another--pt2048"
    reseal(folder, manifest)
    with pytest.raises(ValueError, match="coverage"):
        workflow.read_golden(folder)


@pytest.mark.parametrize("change", ["missing-profile", "extra-profile", "uncalibrated-profile",
    "golden_id", "job_id", "input_fingerprint", "engine_fingerprint"])
def test_formal_golden_rejects_wrong_threshold_profile_provenance(tmp_path, change):
    folder = sealed_golden(tmp_path / "golden")
    manifest = workflow.load(folder / "golden.json")
    thresholds = workflow.load(folder / "thresholds.json")
    profile = thresholds["profiles"]["box--pt2048"]
    if change == "missing-profile":
        thresholds["profiles"] = {}
    elif change == "extra-profile":
        thresholds["profiles"]["another--pt2048"] = copy.deepcopy(profile)
    elif change == "uncalibrated-profile":
        profile["status"] = "calibration-incomplete"
    else:
        profile["provenance"][change] = "wrong"
    reseal(folder, manifest, thresholds)
    with pytest.raises(ValueError, match="[Pp]rofile|[Pp]rovenance|calibrat"):
        workflow.read_golden(folder)


def test_import_preserves_original_machine_provenance_and_allows_subset_golden(tmp_path):
    folder = sealed_golden(tmp_path / "golden")
    original = (folder / "golden.json").read_bytes()
    manifest, _ = workflow.read_golden(folder)
    assert manifest["metadata"]["engine"] == "Z:/source-machine/engine.exe"
    assert manifest["records"][0]["engine"] == "Z:/source-machine/engine.exe"
    assert (folder / "golden.json").read_bytes() == original


@pytest.mark.parametrize('profile_engine', ['updated-engine', 'original-engine'])
def test_partial_refresh_binds_thresholds_to_the_record_engine(tmp_path, profile_engine):
    folder = sealed_golden(tmp_path / 'golden')
    manifest = workflow.load(folder / 'golden.json')
    manifest['records'][0]['engine_fingerprint'] = 'updated-engine'
    thresholds = workflow.load(folder / 'thresholds.json')
    thresholds['profiles']['box--pt2048']['provenance']['engine_fingerprint'] = profile_engine
    reseal(folder, manifest, thresholds)
    if profile_engine == 'updated-engine':
        golden, _ = workflow.read_golden(folder)
        assert golden['records'][0]['engine_fingerprint'] == 'updated-engine'
        assert golden['metadata']['engine_fingerprint'] == 'original-engine'
    else:
        with pytest.raises(ValueError, match='[Pp]rovenance'):
            workflow.read_golden(folder)
