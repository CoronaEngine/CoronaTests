"""Golden publication and regression orchestration, independent of engine sources."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import zipfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SAMPLING_OPTIONS = ('pt_spp', 'gi_frames', 'pt_reblur_frames', 'warmup_frames')


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def comparison_fingerprint(inputs, baseline):
    """Allow sampling budgets to differ while checking every other input."""
    current = fingerprint(inputs)
    if baseline is None or current == baseline.get("input_fingerprint"):
        return current
    reference = baseline.get("configuration") or {}
    if "frames" not in reference or "warmup" not in reference:
        return current
    recipe = dict(inputs["recipe"])
    if recipe.get("id") == "pt2048" and recipe["frames"] != reference["frames"] and "label" in reference:
        recipe["label"] = reference["label"]
    recipe.update(frames=reference["frames"], warmup=reference["warmup"])
    return fingerprint({**inputs, "recipe": recipe})


def sampling_budget(configuration, timing=None):
    timing = timing or {}
    return {"frames": configuration.get("frames", timing.get("frames")),
            "warmup_frames": configuration.get("warmup", timing.get("warmup_frames"))}


def utc():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) or value in (".", ".."):
        raise ValueError("Invalid baseline/run identifier: " + value)
    return value


def inside(root, relative):
    path = Path(relative)
    resolved = (root / path).resolve()
    if path.is_absolute() or not resolved.is_relative_to(root.resolve()):
        raise ValueError("Artifact path escapes output: " + str(relative))
    return resolved


def parse_options(kind, argv=None, *, root=None):
    root = Path(root or ROOT).resolve()
    parser = argparse.ArgumentParser(description=(
        "Generate a golden gallery with one capture per scene/algorithm and fixed thresholds." if kind == "golden"
        else "Render candidates and compare against a validated golden gallery."))
    parser.add_argument("--config", type=Path, default=root / f"config.{kind}.local.json")
    parser.add_argument("--engine-executable", type=Path)
    parser.add_argument("--runtime-dir", action="append", type=Path)
    parser.add_argument("--golden", type=Path)
    parser.add_argument("--golden-id", help="Download/use a published golden version instead of the pinned default.")
    parser.add_argument("--suite", choices=("quick", "full"), default="full",
                        help="Scene suite; default: full (all 35 scenes).")
    parser.add_argument("--scene", action="append")
    parser.add_argument("--algorithm", action="append")
    parser.add_argument("--pt-spp", type=int, help="Total accumulated PT samples, at 1 spp/frame.")
    parser.add_argument("--gi-frames", type=int, help="GI history frames; does not enable RGB accumulation.")
    parser.add_argument("--pt-reblur-frames", type=int, help="PT+REBLUR history frames.")
    parser.add_argument("--warmup-frames", type=int, help="Frames excluded only from steady timing statistics.")
    parser.add_argument("--baseline-id")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--offline", action="store_true", help="Use only verified local scenes and goldens; never access the network.")
    parser.add_argument("--port", type=int, default=9980)
    parser.add_argument("--timeout", type=int, default=7200)
    options = parser.parse_args(argv)
    config = load(options.config) if options.config.is_file() else {}
    sampling = config.get("sampling", {})
    if not isinstance(sampling, dict) or set(sampling) - set(SAMPLING_OPTIONS):
        raise ValueError("Invalid sampling configuration; use " + ", ".join(SAMPLING_OPTIONS))
    for key in SAMPLING_OPTIONS:
        value = getattr(options, key)
        if value is None:
            value = sampling.get(key)
        minimum = 0 if key == "warmup_frames" else 1
        if value is not None and (type(value) is not int or value < minimum):
            raise ValueError(f"sampling.{key} must be an integer >= {minimum}")
        setattr(options, key, value)
    def absolute(value):
        value = Path(value)
        return (root / value).resolve() if not value.is_absolute() else value.resolve()
    options.engine_executable = absolute(options.engine_executable or config.get("engine_executable", "")) if (
        options.engine_executable or config.get("engine_executable")) else None
    options.runtime_dir = [absolute(item) for item in (
        options.runtime_dir if options.runtime_dir is not None else config.get("runtime_dirs", []))]
    golden = options.golden or config.get("golden")
    if golden and options.golden_id is not None:
        raise ValueError("Cannot combine --golden/config golden path with --golden-id")
    if options.golden_id is not None:
        identifier(options.golden_id)
    if (not golden and options.golden_id is None and kind == "regression"
            and not (root / "baselines/default.json").is_file()
            and (root / ".workspace/latest-golden.json").is_file()):
        golden = load(root / ".workspace/latest-golden.json")["path"]
    options.golden = absolute(golden) if golden else None
    if options.output:
        options.output = absolute(options.output)
    if not 1 <= options.port <= 65535 or options.timeout <= 0:
        raise ValueError("Port must be 1..65535 and timeout positive")
    return options


def selection(root, options):
    suite = load(root / "scenes/suite.json")
    recipes = load(root / "recipes/algorithms.json")["algorithms"]
    selected = list(suite[options.suite])
    if options.scene:
        unknown = set(options.scene) - set(selected)
        if unknown:
            raise ValueError("Unknown scene in selected suite: " + ", ".join(sorted(unknown)))
        selected = [item for item in selected if item in options.scene]
    algorithms = list(recipes)
    if options.algorithm:
        unknown = set(options.algorithm) - set(algorithms)
        if unknown:
            raise ValueError("Unknown algorithm: " + ", ".join(sorted(unknown)))
        algorithms = [item for item in algorithms if item in options.algorithm]
    if not selected or not algorithms or len(selected) != len(set(selected)):
        raise ValueError("Selected suite must contain unique nonempty tasks")
    for algorithm, option in (("gi", "gi_frames"), ("pt-reblur", "pt_reblur_frames"), ("pt2048", "pt_spp")):
        if algorithm not in algorithms:
            continue
        recipe = recipes[algorithm]
        if type(recipe.get("spp_per_frame", 1)) is not int or recipe.get("spp_per_frame", 1) != 1:
            raise ValueError(f"{algorithm}: capture supports 1 spp/frame; configure pt_spp or history frames instead")
        frames = getattr(options, option)
        if frames is not None:
            recipe["frames"] = frames
            if algorithm == "pt2048":
                recipe["label"] = f"PT {frames} samples"
        if options.warmup_frames is not None:
            recipe["warmup"] = options.warmup_frames
        if (type(recipe["frames"]) is not int or recipe["frames"] < 1 or
                type(recipe["warmup"]) is not int or not 0 <= recipe["warmup"] < recipe["frames"]):
            raise ValueError(f"{algorithm}: warmup must be >= 0 and less than frames; use --warmup-frames for small budgets")
    return suite, recipes, selected, algorithms


@contextmanager
def gpu_lock(root):
    path = root / ".workspace/locks/gpu.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0)
        if stream.read(1) == b"":
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError("Another CoronaTests GPU queue owns this workspace") from error
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class FrozenInputs:
    """Hash immutable inputs once; reject additions/removals or stat changes during a batch."""
    def __init__(self, root, engine, runtime_dirs, scenes):
        self.root, self.engine, self.runtime_dirs = root, engine, runtime_dirs
        self.scene_files = []
        for scene in scenes:
            self.scene_files.extend(Path(scene["directory"]) / relative for relative in scene.get("files", {}))
            self.scene_files.append(Path(scene["source"]))
        self.files = self.scan()
        self.stats = {str(path): self.stat(path) for path in self.files}
        self.hashes = {str(path): sha(path) for path in self.files}

    @staticmethod
    def stat(path):
        stat = path.stat()
        return (stat.st_size, stat.st_mtime_ns)

    def scan(self):
        paths = [self.engine, *self.engine.parent.glob("*.dll"), *self.scene_files]
        for folder in self.runtime_dirs:
            if not folder.is_dir():
                raise FileNotFoundError("Runtime directory: " + str(folder))
            paths.extend(folder.glob("*.dll"))
        for folder, patterns in (("scripts", ("*.py",)), ("scenes", ("*.json",)), ("recipes", ("*.json",))):
            for pattern in patterns:
                paths.extend((self.root / folder).glob(pattern))
        return sorted(set(path.resolve() for path in paths), key=str)

    def check(self):
        if self.scan() != self.files:
            raise RuntimeError("VERSION_GUARD input files added or removed during batch")
        for path in self.files:
            if self.stat(path) != self.stats[str(path)]:
                raise RuntimeError("VERSION_GUARD immutable input changed: " + str(path))

    def runtime_hashes(self):
        dirs = {self.engine.parent.resolve(), *(path.resolve() for path in self.runtime_dirs)}
        return {str(path): self.hashes[str(path)] for path in self.files
                if path.parent in dirs and (path.suffix.lower() == ".dll" or path == self.engine)}


def artifacts(output):
    return {path.relative_to(output).as_posix(): sha(path) for path in sorted(output.rglob("*"))
            if path.is_file() and "cef-cache" not in path.parts and path.name != "attempt.json"
            and not path.name.endswith(".tmp")}


def verify_artifacts(output, expected):
    for relative, digest in expected.items():
        path = inside(output, relative)
        if not path.is_file() or sha(path) != digest:
            raise ValueError("Artifact hash integrity mismatch: " + str(path))


def verify_capture(output, record):
    if not record.get("owned_engine_closed"):
        raise ValueError("Capture did not confirm its engine was closed")
    if record.get("status") != "complete":
        return
    image = inside(output, record.get("image", "image.png"))
    with Image.open(image) as opened:
        opened.verify()
    with Image.open(image) as opened:
        if opened.size != (1920, 1080):
            raise ValueError("Capture must be actual 1920x1080")
    digest = sha(image)
    if record.get("image_sha256") and record["image_sha256"] != digest:
        raise ValueError("Capture PNG hash mismatch")
    record["image_sha256"] = digest
    timing = record.get("timing", {})
    for key in ("frames", "mean_ms", "median_ms", "total_ms", "sample_ms"):
        if key not in timing or not isinstance(timing[key], (int, float)) or timing[key] <= 0:
            raise ValueError("Missing or invalid capture timing: " + key)
    if not (output / "frames.csv").is_file():
        raise ValueError("Capture lacks raw frame evidence")


def capture_attempt(scene, algorithm, options, output, contract, frozen, capture_job):
    frozen.check()
    attempt = output / "attempt.json"
    if attempt.is_file():
        if not options.resume:
            raise FileExistsError("Capture already exists; use --resume: " + str(output))
        record = load(attempt)
        if record.get("_contract") != contract:
            raise ValueError("Resume input/version fingerprint differs: " + str(output))
        verify_artifacts(output, record["_artifacts"])
        verify_capture(output, record)
        if record.get("status") != "failed":
            return record
    if output.exists() and any(output.iterdir()):
        if not options.resume:
            raise ValueError("Incomplete capture directory retained; use --resume: " + str(output))
        unfinished = load(output / "run.json") if (output / "run.json").is_file() else {}
        if unfinished.get("pid") and not unfinished.get("owned_engine_closed"):
            raise RuntimeError("Interrupted engine closure is unverified; preserve evidence and close its recorded PID before retrying")
        # Keep the interrupted attempt beside its replacement. Both resolved
        # targets must remain inside this explicitly named job directory.
        parent = output.parent.resolve()
        category = "failed" if attempt.is_file() else "interrupted"
        retained = output.with_name(output.name + "." + category + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        if output.resolve().parent != parent or retained.resolve().parent != parent or retained.exists():
            raise ValueError("Unsafe interrupted-attempt preservation path")
        output.rename(retained)
    started = time.monotonic()
    try:
        record = capture_job(scene, algorithm, options.engine_executable, output,
            runtime_dirs=options.runtime_dir, timeout=options.timeout, port=options.port)
        if record.get("scene_id") != scene["id"] or record.get("algorithm_id") != algorithm["id"]:
            raise ValueError("Capture returned a different scene or algorithm")
        verify_capture(output, record)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        output.mkdir(parents=True, exist_ok=True)
        record = {"schema": 1, "scene_id": scene["id"], "algorithm_id": algorithm["id"],
                  "status": "failed", "error": str(error), "elapsed_seconds": time.monotonic() - started,
                  "owned_engine_closed": False}
    frozen.check()
    record.update(_contract=contract, _artifacts=artifacts(output))
    save(attempt, record)
    return record


def seal(value):
    return {**value, "manifest_fingerprint": fingerprint(value)}


def golden_files(path):
    """Locate local capture metadata or the published image-only directory."""
    path = Path(path)
    if (path / "golden.json").is_file():
        return path / "golden.json", path / "thresholds.json", None
    if path.name != "golden":
        raise FileNotFoundError(path / "golden.json")
    root = path.parent
    baseline_id = identifier(load(root / "baselines/default.json")["id"])
    index = load(root / "baselines" / (baseline_id + ".json"))
    expected = "baselines/" + baseline_id + ".manifest.json"
    if index.get("path") != "golden" or index.get("manifest") != expected or index.get("id") != baseline_id:
        raise ValueError("Invalid published golden metadata location")
    return root / expected, root / "baselines" / (baseline_id + ".thresholds.json"), index


def read_golden(path):
    path = Path(path)
    manifest_path, threshold_path, index = golden_files(path)
    manifest = load(manifest_path)
    check = dict(manifest)
    digest = check.pop("manifest_fingerprint", None)
    if digest != fingerprint(check) or not manifest.get("valid"):
        raise ValueError("Golden manifest integrity invalid or capture incomplete")
    if index is not None and any(manifest.get(key) != index.get(key)
                                 for key in ("id", "manifest_fingerprint", "created_utc")):
        raise ValueError("Golden index/manifest fingerprint integrity mismatch")
    if not (manifest.get("calibrated") is True or
            (manifest.get("thresholds_ready") is True and manifest.get("threshold_policy") == "fixed")):
        raise ValueError("Golden has no usable comparison thresholds")
    coverage = manifest.get("coverage", {})
    scenes, algorithms = coverage.get("scenes", []), coverage.get("algorithms", [])
    if any(not isinstance(items, list) or not items or
           any(not isinstance(item, str) or not item for item in items) or len(items) != len(set(items))
           for items in (scenes, algorithms)):
        raise ValueError("Golden coverage must contain unique nonempty scenes and algorithms")
    expected = {scene + "--" + algorithm for scene in scenes for algorithm in algorithms}
    records = manifest.get("records", [])
    actual = [record.get("job_id") for record in records]
    if coverage.get("jobs") != len(expected) or len(actual) != len(expected) or set(actual) != expected:
        raise ValueError("Golden record coverage is missing or duplicating jobs")
    for record in records:
        if (record.get("job_id") != record.get("scene_id", "") + "--" + record.get("algorithm_id", "")
                or record.get("status") not in ("complete", "expected-unsupported")):
            raise ValueError("Golden record coverage has invalid job identity or capture status")
    verify_artifacts(path, manifest["artifacts"])
    if index is not None:
        actual_files = {item.relative_to(path).as_posix() for item in path.rglob("*") if item.is_file()}
        if actual_files != set(manifest["artifacts"]):
            raise ValueError("Published golden must contain exactly the reference images")
        for record in records:
            if record["status"] == "complete" and manifest["artifacts"].get(record.get("image")) != record.get("image_sha256"):
                raise ValueError("Golden image/record hash integrity mismatch")
    thresholds = load(threshold_path)
    threshold_digest = fingerprint(thresholds) if index is not None else sha(threshold_path)
    expected_digest = manifest["thresholds_fingerprint"] if index is not None else manifest["thresholds_sha256"]
    if threshold_digest != expected_digest:
        raise ValueError("Golden threshold hash integrity mismatch")
    if thresholds.get("golden_id") != manifest["id"]:
        raise ValueError("Thresholds belong to another golden")
    complete = {record["job_id"]: record for record in records if record["status"] == "complete"}
    profiles = thresholds.get("profiles", {})
    if not isinstance(profiles, dict) or set(profiles) != set(complete):
        raise ValueError("Threshold profiles do not cover exactly the complete golden jobs")
    for job_id, record in complete.items():
        profile = profiles[job_id]
        if profile.get("status") not in ("fixed", "calibrated"):
            raise ValueError("Golden threshold profile is not ready: " + job_id)
        provenance = profile.get("provenance", {})
        expected_provenance = {"golden_id": manifest["id"], "job_id": job_id,
                               "input_fingerprint": record["input_fingerprint"]}
        engine_fingerprint = record.get("engine_fingerprint") or manifest.get("metadata", {}).get("engine_fingerprint")
        if engine_fingerprint is not None:
            expected_provenance["engine_fingerprint"] = engine_fingerprint
        if any(provenance.get(key) != value for key, value in expected_provenance.items()):
            raise ValueError("Threshold profile provenance differs: " + job_id)
    return manifest, thresholds


def archive(output):
    destination = output.parent / (output.name + ".zip")
    if destination.exists():
        raise FileExistsError("Archive already exists: " + str(destination))
    temporary = destination.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zipped:
        for path in sorted(output.rglob("*")):
            if path.is_file() and "cef-cache" not in path.parts and not path.name.endswith(".tmp"):
                zipped.write(path, path.relative_to(output))
    os.replace(temporary, destination)
    return destination


def publish_golden(root, output, manifest, *, update_latest=True):
    """Complete/recover publication without rewriting sealed capture evidence."""
    index_path = root / "baselines" / (manifest["id"] + ".json")
    threshold_path = root / "baselines" / (manifest["id"] + ".thresholds.json")
    index = {"id": manifest["id"], "path": os.path.relpath(output, root).replace(os.sep, "/"),
             "manifest_fingerprint": manifest["manifest_fingerprint"], "created_utc": manifest["created_utc"]}
    if index_path.exists():
        prior = load(index_path)
        if any(prior.get(key) != index[key] for key in ("id", "path", "manifest_fingerprint")):
            raise ValueError("Baseline ID already published with different content or output: " + manifest["id"])
    if threshold_path.exists() and fingerprint(load(threshold_path)) != fingerprint(load(output / "thresholds.json")):
        raise ValueError("Published baseline threshold integrity differs")
    destination = output.parent / (output.name + ".zip")
    if destination.exists():
        with zipfile.ZipFile(destination) as zipped:
            expected_files = {path.relative_to(output).as_posix(): path for path in output.rglob("*")
                              if path.is_file() and "cef-cache" not in path.parts and not path.name.endswith(".tmp")}
            names = zipped.namelist()
            if len(names) != len(set(names)) or set(names) != set(expected_files):
                raise ValueError("Published golden archive member integrity differs")
            for name, path in expected_files.items():
                digest = hashlib.sha256()
                with zipped.open(name) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != sha(path):
                    raise ValueError("Published golden archive integrity differs: " + name)
    else:
        archive(output)
    # Expose the baseline only after its self-contained archive exists.
    if not index_path.exists():
        save(index_path, index)
    if not threshold_path.exists():
        threshold_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(output / "thresholds.json", threshold_path)
    if update_latest or not (root / ".workspace/latest-golden.json").exists():
        save(root / ".workspace/latest-golden.json", index)


def hardware_identity(observation):
    if isinstance(observation, list):
        return [hardware_identity(item) for item in observation]
    if isinstance(observation, dict):
        return {key: hardware_identity(value) for key, value in observation.items()
                if key not in ("current_temperature", "temperature")}
    return observation


def execute(kind, options, *, root=ROOT, prepare=None, capture_job=None, thermal=None):
    from scripts import compare, report, pt_policy
    if prepare is None:
        from scripts.download_scenes import ensure_scenes
        prepare = ensure_scenes
    if capture_job is None or thermal is None:
        from scripts import capture
        capture_job = capture_job or capture.capture
        thermal = thermal or capture.confirm_thermal_protection
    root = Path(root).resolve()
    if not options.engine_executable or not options.engine_executable.is_file():
        raise FileNotFoundError("Specify --engine-executable or configure " + str(options.config))
    suite, recipes, selected, algorithm_ids = selection(root, options)
    run_id = identifier(options.baseline_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    output = options.output or root / ".workspace/experiments" / ("golden" if kind == "golden" else "regression") / run_id
    output = output.resolve()
    if kind == "golden":
        published_index = root / "baselines" / (run_id + ".json")
        if published_index.exists():
            prior_output = (root / load(published_index)["path"]).resolve()
            if prior_output != output:
                raise ValueError("Baseline ID already published at another output: " + run_id)
            if not options.resume:
                raise FileExistsError("Baseline ID already published; use a new ID: " + run_id)
    golden = thresholds = policy = None
    policy_jobs = set()
    if kind == "regression":
        if not options.golden:
            from scripts.download_golden import ensure_golden
            options.golden = ensure_golden(root, options.golden_id, offline=options.offline)
        if output.is_relative_to(options.golden) or options.golden.is_relative_to(output):
            raise ValueError("Regression output must be separate from golden")
        golden, thresholds = read_golden(options.golden)
        if 'pt2048' in algorithm_ids and recipes['pt2048']['frames'] == 512:
            references = [row for row in golden['records'] if row['scene_id'] in selected
                          and row['algorithm_id'] == 'pt2048' and row['status'] == 'complete'
                          and row.get('configuration', {}).get('frames') == 2048]
            if references:
                policy = load(root / 'recipes/pt512-regression.json')
                pt_policy.validate_references(policy, references)
                policy_jobs = {row['job_id'] for row in references}
    existing = output.exists() and any(output.iterdir())
    if existing and not options.resume:
        raise FileExistsError("Preserving existing output; use a new ID or --resume: " + str(output))
    output.mkdir(parents=True, exist_ok=True)
    with gpu_lock(root):
        prepared = prepare(root, selected, offline=options.offline)
        found = {scene["id"]: scene for scene in prepared["scenes"]}
        if set(found) != set(selected) or len(found) != len(prepared["scenes"]):
            raise ValueError("Prepared scene manifest missing or duplicating selected scenes")
        scenes = [{**suite["scenes"][scene_id], **found[scene_id]} for scene_id in selected]
        frozen = FrozenInputs(root, options.engine_executable, options.runtime_dir, scenes)
        engine_hashes = frozen.runtime_hashes()
        engine_fingerprint = fingerprint(engine_hashes)
        tool_hashes = {str(path.relative_to(root)): frozen.hashes[str(path)] for path in frozen.files
                       if path.is_relative_to(root) and any(part in ("scripts", "scenes", "recipes") for part in path.parts)}
        golden_by_id = {record["job_id"]: record for record in golden["records"]} if golden else {}
        comparison_inputs = {}
        jobs = []
        for scene in scenes:
            for algorithm_id in algorithm_ids:
                recipe = {**recipes[algorithm_id], "id": algorithm_id}
                inputs = {"scene_id": scene["id"], "asset_fingerprint": scene["asset_fingerprint"], "recipe": recipe,
                          "regions": scene.get("regions", {}),
                          "expected_diagnostics": scene.get("expected_diagnostics", []),
                          "expected_unsupported": scene.get("expected_unsupported", {})}
                cameras = root / "scenes/cameras.json"
                if cameras.exists():
                    camera_data = load(cameras)
                    inputs["camera"] = camera_data.get("scenes", camera_data.get("cameras", camera_data)).get(scene["id"])
                jobs.append((scene, recipe, fingerprint(inputs)))
                job_id = scene["id"] + "--" + algorithm_id
                comparison_inputs[job_id] = comparison_fingerprint(inputs, golden_by_id.get(job_id))
        batch_contract = {"kind": kind, "suite": options.suite, "selected": selected, "algorithms": algorithm_ids,
                          "engine_fingerprint": engine_fingerprint, "tools": tool_hashes,
                          "runtime_dirs": [str(path) for path in options.runtime_dir],
                          "launch_environment": {key: os.environ.get(key) for key in ("PATH", "PYTHONHOME", "PYTHONPATH", "CONDA_PREFIX")},
                          "jobs": {s["id"] + "--" + a["id"]: f for s, a, f in jobs},
                          "golden_fingerprint": golden["manifest_fingerprint"] if golden else None}
        if existing:
            run_config = load(output / "run-config.json")
            if run_config.get("contract") != batch_contract:
                raise ValueError("Resume input/version fingerprint differs")
            if options.baseline_id and run_config.get("id", run_id) != options.baseline_id:
                raise ValueError("Resume baseline identifier differs")
            run_id = run_config.get("id", run_id)
            if kind == "golden" and (output / "golden.json").is_file():
                prior = load(output / "golden.json")
                if prior.get("valid") and (prior.get("calibrated") or prior.get("thresholds_ready")):
                    read_golden(output)
                    frozen.check()
                    publish_golden(root, output, prior, update_latest=False)
                    return {**load(output / "workflow-result.json"), "output": str(output)}, 0
        else:
            run_config = {"id": run_id, "contract": batch_contract, "engine": str(options.engine_executable),
                "runtime_dirs": [str(path) for path in options.runtime_dir], "runtime_sha256": engine_hashes,
                "created_utc": utc()}
            save(output / "run-config.json", run_config)
        if policy is not None:
            save(output / 'pt512-policy.json', policy)
        thermal_evidence = thermal()
        result = {"kind": kind, "id": run_id, "suite": options.suite, "output": str(output), "created_utc": run_config["created_utc"],
                  "metadata": {"engine": str(options.engine_executable), "engine_fingerprint": engine_fingerprint,
                               "configuration_file": str(options.config),
                               "hardware": thermal_evidence, "coverage": {"scenes": selected, "algorithms": algorithm_ids},
                               "sampling": {key: {"frames": recipes[key]["frames"], "warmup_frames": recipes[key]["warmup"],
                                                  "spp_per_frame": recipes[key].get("spp_per_frame", 1)} for key in algorithm_ids},
                               "timing_policy": "Renderer envelope and dispatch sum differ; accumulated PT total covers all samples.",
                               "offline_policy": "Continuous batch, driver protections retained; no pacing."},
                  "records": [], "summary": {}}
        profiles = {}
        artifact_index = {}
        for scene, algorithm, input_fingerprint in jobs:
            job_id = scene["id"] + "--" + algorithm["id"]
            print("START " + job_id, flush=True)
            expected = scene.get("expected_unsupported", {}).get(algorithm["id"])
            contract = fingerprint({"input": input_fingerprint, "engine": engine_fingerprint, "tools": tool_hashes,
                                    "runtime_dirs": batch_contract["runtime_dirs"],
                                    "launch_environment": batch_contract["launch_environment"]})
            base_record = golden_by_id.get(job_id)
            if kind == "regression" and (base_record is None or base_record.get("input_fingerprint") != comparison_inputs[job_id]):
                row = {"scene_id": scene["id"], "algorithm_id": algorithm["id"], "job_id": job_id,
                       "configuration": algorithm, "status": "incomparable",
                       "reason": "Golden does not cover the same scene/resources/algorithm outside sampling budget"}
            else:
                folder = output / "runs" / job_id / "repeat-0"
                raw = capture_attempt(scene, algorithm, options, folder, contract, frozen, capture_job)
                row = {key: value for key, value in raw.items() if not key.startswith("_")}
                row.update(job_id=job_id, label=algorithm.get("label", algorithm["id"]),
                           scene_name=scene.get("name", scene["id"]), regions=scene.get("regions", {}),
                           input_fingerprint=input_fingerprint)
                if raw["status"] == "complete":
                    row["image"] = (folder.relative_to(output) / raw["image"]).as_posix()
                    if expected:
                        row.update(status="support-changed", reason="Previously unsupported combination now rendered")
                elif raw["status"] == "expected-unsupported" and not expected:
                    row.update(status="failed", reason="Unexpected unsupported combination")
                if kind == "golden" and row["status"] == "complete":
                    profile = compare.fixed_profile(regions=scene.get("regions", {}),
                        provenance={"golden_id": run_id, "job_id": job_id, "input_fingerprint": input_fingerprint,
                                    "engine_fingerprint": engine_fingerprint})
                    profiles[job_id] = profile
                    row["threshold_profile"] = profile
                if kind == "regression" and row["status"] == "complete":
                    if base_record["status"] != "complete":
                        row.update(status="support-changed", reason="Golden has no comparable image for this combination")
                    else:
                        reference_folder = output / "reference" / job_id
                        reference_folder.mkdir(parents=True, exist_ok=True)
                        reference = reference_folder / "image.png"
                        shutil.copy2(inside(options.golden, base_record["image"]), reference)
                        baseline_pfm = inside(options.golden, base_record["image"]).with_suffix(".pfm")
                        if baseline_pfm.is_file():
                            shutil.copy2(baseline_pfm, reference.with_suffix(".pfm"))
                        comparison_folder = output / "comparisons" / job_id
                        metrics = compare.compare_images(reference, folder / raw["image"], comparison_folder,
                                                         regions=scene.get("regions", {}))
                        profile = thresholds.get("profiles", {}).get(job_id, {})
                        provenance = profile.get("provenance", {})
                        if provenance.get("input_fingerprint") != base_record["input_fingerprint"] or provenance.get("golden_id") != golden["id"]:
                            verdict = {"status": "uncalibrated", "failures": ["Threshold profile provenance differs"]}
                        elif job_id in policy_jobs:
                            metrics['multiscale'] = pt_policy.measure(reference, folder / raw['image'], scene.get('regions', {}))
                            verdict = pt_policy.evaluate(metrics['multiscale'], policy['cases'][job_id], policy['allowances'])
                            if any(metrics.get('linear_nonfinite_values', {}).values()):
                                verdict.update(status='image-failed', failures=[*verdict['failures'], 'PFM contains nonfinite values'])
                            metrics['multiscale_evaluation'] = verdict
                            save(comparison_folder / 'comparison.json', metrics)
                            row.update(comparison_policy={'id': policy['id'], 'allowances': policy['allowances']},
                                       profile_json='pt512-policy.json')
                        else:
                            verdict = compare.evaluate(metrics, profile)
                        row.update(status=verdict["status"], comparison=metrics, failures=verdict["failures"],
                                   baseline_image=reference.relative_to(output).as_posix(),
                                   difference_image=(comparison_folder / "difference.png").relative_to(output).as_posix(),
                                   comparison_json=(comparison_folder / 'comparison.json').relative_to(output).as_posix(),
                                   comparison_input_fingerprint=comparison_inputs[job_id],
                                   sampling_comparison={"golden": sampling_budget(base_record.get("configuration") or {}, base_record.get("timing")),
                                                        "candidate": sampling_budget(algorithm, row.get("timing")),
                                                        "threshold_policy": policy['id'] if job_id in policy_jobs else "golden-unchanged"})
                        old_timing = base_record.get("timing", {})
                        if old_timing.get("mean_ms"):
                            same_hardware = hardware_identity(golden["metadata"].get("hardware")) == hardware_identity(thermal_evidence)
                            row["performance"] = {"mean_change_percent": (row["timing"]["mean_ms"] / old_timing["mean_ms"] - 1) * 100,
                                                  "policy": "warning-only", "machine_observation_equal": same_hardware,
                                                  "golden_mean_ms": old_timing["mean_ms"],
                                                  "sampling_budget_equal": row["sampling_comparison"]["golden"] == row["sampling_comparison"]["candidate"]}
                if kind == "regression" and row["status"] == "expected-unsupported" and base_record["status"] != "expected-unsupported":
                    row.update(status="support-changed", reason="Previously supported combination is now unsupported")
                for attempt_folder in (output / "runs" / job_id).glob("repeat-*"):
                    for relative, digest in artifacts(attempt_folder).items():
                        artifact_index[(attempt_folder.relative_to(output) / relative).as_posix()] = digest
                    attempt_file = attempt_folder / "attempt.json"
                    if attempt_file.exists():
                        artifact_index[attempt_file.relative_to(output).as_posix()] = sha(attempt_file)
            result["records"].append(row)
            result["summary"] = dict(Counter(item["status"] for item in result["records"]))
            save(output / "progress.json", {"records": result["records"], "summary": result["summary"]})
            print("END " + job_id + " " + row["status"], flush=True)
        frozen.check()
        selected_count = len(jobs)
        complete_coverage = len(result["records"]) == selected_count
        accepted = {"complete", "expected-unsupported"} if kind == "golden" else {"pass", "expected-unsupported"}
        valid = complete_coverage and all(row["status"] in accepted for row in result["records"])
        if kind == "golden":
            save(output / "thresholds.json", {"schema": 1, "golden_id": run_id, "profiles": profiles})
            thresholds_ready = all(profile["status"] == "fixed" for profile in profiles.values()) and valid
            result.update(valid=valid, calibrated=False, thresholds_ready=thresholds_ready, threshold_policy="fixed")
            report.write_report(output, result)
            # The sealed manifest is the final commit marker. Every publication
            # prerequisite, including its immutable report, must already exist.
            save(output / "workflow-result.json", result)
            artifact_index.update({relative: digest for relative, digest in artifacts(output).items()
                                   if relative != "golden.json"})
            manifest = seal({"schema": 1, "id": run_id, "suite": options.suite, "created_utc": result["created_utc"],
                             "valid": valid, "calibrated": False, "thresholds_ready": thresholds_ready,
                             "threshold_policy": "fixed", "metadata": result["metadata"],
                             "coverage": {"scenes": selected, "algorithms": algorithm_ids, "jobs": selected_count},
                             "records": result["records"], "artifacts": artifact_index,
                             "thresholds_sha256": sha(output / "thresholds.json")})
            save(output / "golden.json", manifest)
            if valid and thresholds_ready:
                from scripts.compact_golden import replace_with_compact, remove_raw_backup
                manifest, result, raw_backup = replace_with_compact(output)
                publish_golden(root, output, manifest)
                try:
                    remove_raw_backup(raw_backup, output)
                except OSError:
                    print("Golden published; temporary diagnostics remain at " + str(raw_backup), file=sys.stderr)
                return result, 0
            return result, 1
        result["passed"] = valid
        report.write_report(output, result)
        save(output / "workflow-result.json", result)
        return result, int(not valid)


def main(kind, argv=None):
    try:
        result, code = execute(kind, parse_options(kind, argv))
        print(json.dumps({"status": "pass" if code == 0 else "failed", "output": result["output"],
                          "report": str(Path(result["output"]) / "index.html"), "summary": result["summary"]},
                         ensure_ascii=False), flush=True)
        return code
    except (OSError, ValueError, RuntimeError, KeyError, zipfile.BadZipFile) as error:
        print("ERROR: " + str(error), file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; original evidence retained. Resume with the same ID/output.", file=sys.stderr, flush=True)
        return 130
