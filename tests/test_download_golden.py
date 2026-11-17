"""Golden distribution integrity and installation without a GPU or GitHub."""
import hashlib
import importlib
import io
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

from PIL import Image
import pytest

from scripts import workflow


def downloader():
    assert importlib.util.find_spec("scripts.download_golden"), "Golden downloader is missing"
    return importlib.import_module("scripts.download_golden")


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(scope="module")
def golden_archive(tmp_path_factory):
    root = tmp_path_factory.mktemp("golden-source")
    save(root / "scenes/suite.json", {"schema": 1, "full": ["box"], "quick": ["box"],
        "scenes": {"box": {"name": "Box", "expected_unsupported": {}, "regions": {}}}})
    save(root / "recipes/algorithms.json", {"schema": 1, "algorithms": {
        "pt2048": {"id": "pt2048", "label": "PT2048", "frames": 3, "warmup": 0}}})
    engine = root / "engine.exe"
    engine.write_bytes(b"test-runtime")
    source = root / "scene.xml"
    source.write_text("<scene/>")
    scene = {"id": "box", "name": "Box", "source": str(source), "directory": str(root),
             "source_sha256": "fixture", "asset_fingerprint": "fixture-assets", "files": {}}

    def capture(scene, algorithm, engine, output, **kwargs):
        output.mkdir(parents=True)
        image = Image.new("RGB", (1920, 1080), (160, 120, 80))
        image.paste((40, 90, 130), (0, 0, 960, 1080))
        image.save(output / "image.png")
        (output / "frames.csv").write_text("frame,renderer_ms\n0,2\n1,2\n2,2\n")
        return {"schema": 1, "scene_id": scene["id"], "algorithm_id": algorithm["id"],
            "status": "complete", "image": "image.png", "configuration": {},
            "timing": {"frames": 3, "warmup_frames": 0, "mean_ms": 2, "median_ms": 2,
                       "total_ms": 6, "sample_ms": 2}, "elapsed_seconds": .1, "owned_engine_closed": True}

    options = workflow.parse_options("golden", ["--engine-executable", str(engine), "--baseline-id", "test-golden"], root=root)
    result, code = workflow.execute("golden", options, root=root,
        prepare=lambda *args, **kwargs: {"schema": 1, "scenes": [scene]},
        capture_job=capture, thermal=lambda: {"driver_protection": True})
    assert code == 0
    index = json.loads((root / "baselines/test-golden.json").read_text())
    return Path(result["output"]).with_suffix(".zip").read_bytes(), index


@pytest.fixture
def distribution(tmp_path, golden_archive):
    data, source_index = golden_archive
    split = len(data) // 2
    parts = [data[:split], data[split:]]
    index = {**source_index, "path": ".workspace/experiments/golden/test-golden"}
    index["distribution"] = {"schema": 1, "repository": "CoronaEngine/CoronaTests",
        "tag": "golden-test-golden", "private": False, "format": "split-zip",
        "archive": {"name": "test-golden.zip", "bytes": len(data), "sha256": digest(data)},
        "assets": [{"name": f"test-golden.zip.{number:03}", "bytes": len(part), "sha256": digest(part),
            "url": f"https://github.com/CoronaEngine/CoronaTests/releases/download/golden-test-golden/test-golden.zip.{number:03}",
            "api_url": f"https://api.github.com/repos/CoronaEngine/CoronaTests/releases/assets/{number}"}
            for number, part in enumerate(parts, 1)]}
    save(tmp_path / "baselines/default.json", {"schema": 1, "id": "test-golden"})
    save(tmp_path / "baselines/test-golden.json", index)
    cache = tmp_path / ".workspace/cache/golden/test-golden"
    cache.mkdir(parents=True)
    for asset, part in zip(index["distribution"]["assets"], parts):
        (cache / asset["name"]).write_bytes(part)
    return tmp_path, index, parts


def test_offline_cache_installs_relocatable_verified_golden(distribution):
    root, index, _ = distribution
    destination = downloader().ensure_golden(root, offline=True)
    assert destination == root / index["path"]
    manifest, _ = workflow.read_golden(destination)
    assert manifest["id"] == "test-golden"
    assert json.loads((root / ".workspace/latest-golden.json").read_text())["path"] == index["path"]
    assert downloader().ensure_golden(root, offline=True) == destination


def test_offline_missing_cache_preserves_old_pointer(distribution):
    root, index, _ = distribution
    shutil.rmtree(root / ".workspace/cache/golden")
    pointer = root / ".workspace/latest-golden.json"
    save(pointer, {"id": "previous", "path": ".workspace/previous"})
    before = pointer.read_bytes()
    with pytest.raises(FileNotFoundError, match="[Oo]ffline"):
        downloader().ensure_golden(root, offline=True)
    assert pointer.read_bytes() == before
    assert not (root / index["path"]).exists()


def test_corrupt_part_never_installs_or_updates_pointer(distribution):
    root, index, parts = distribution
    asset = index["distribution"]["assets"][0]
    (root / ".workspace/cache/golden/test-golden" / asset["name"]).write_bytes(b"x" * len(parts[0]))
    with pytest.raises(ValueError, match="SHA256|checksum|[Ii]ntegrity"):
        downloader().ensure_golden(root, offline=True)
    assert not (root / index["path"]).exists()
    assert not (root / ".workspace/latest-golden.json").exists()


def test_full_archive_hash_is_checked_independently(distribution):
    root, index, _ = distribution
    index["distribution"]["archive"]["sha256"] = "0" * 64
    save(root / "baselines/test-golden.json", index)
    with pytest.raises(ValueError, match="SHA256|checksum|[Ii]ntegrity"):
        downloader().ensure_golden(root, offline=True)
    assert not (root / index["path"]).exists()


def replace_archive(root, index, data):
    asset = index["distribution"]["assets"][0]
    asset.update(bytes=len(data), sha256=digest(data))
    index["distribution"]["assets"] = [asset]
    index["distribution"]["archive"].update(bytes=len(data), sha256=digest(data))
    (root / ".workspace/cache/golden/test-golden" / asset["name"]).write_bytes(data)
    save(root / "baselines/test-golden.json", index)


@pytest.mark.parametrize("member", ["../escaped.txt", "/absolute.txt", "C:/evil.txt", "CON.txt", "folder./x", "folder /x", "a:b", "a\\..\\evil", "a//b", "a/./b", "bad?.txt"])
def test_unsafe_zip_paths_are_rejected_before_install(distribution, member):
    root, index, _ = distribution
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(member, b"bad")
    replace_archive(root, index, data.getvalue())
    with pytest.raises(ValueError, match="[Uu]nsafe|[Ii]nvalid"):
        downloader().ensure_golden(root, offline=True)
    assert not (root / index["path"]).exists()
    assert not (root / "escaped.txt").exists()


def test_case_duplicate_zip_members_are_rejected(distribution):
    root, index, _ = distribution
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("image.png", b"first")
        archive.writestr("IMAGE.PNG", b"second")
    replace_archive(root, index, data.getvalue())
    with pytest.raises(ValueError, match="[Dd]uplicate"):
        downloader().ensure_golden(root, offline=True)
    assert not (root / index["path"]).exists()


def test_symlink_zip_member_is_rejected(distribution):
    root, index, _ = distribution
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        item = zipfile.ZipInfo("link")
        item.create_system = 3
        item.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(item, "../outside")
    replace_archive(root, index, data.getvalue())
    with pytest.raises(ValueError, match="[Uu]nsafe|[Ss]ymlink"):
        downloader().ensure_golden(root, offline=True)


def test_nonmatching_existing_destination_is_preserved(distribution):
    root, index, _ = distribution
    destination = root / index["path"]
    destination.mkdir(parents=True)
    (destination / "precious.txt").write_text("keep")
    with pytest.raises((ValueError, FileExistsError), match="[Ee]xisting|[Ii]ntegrity|[Gg]olden"):
        downloader().ensure_golden(root, offline=True)
    assert (destination / "precious.txt").read_text() == "keep"


@pytest.mark.parametrize("path", ["../outside", "C:/outside", ".", "baselines/test-golden", ".workspace/cache/golden/test-golden"])
def test_index_destination_is_controlled_inside_root(distribution, path):
    root, index, _ = distribution
    index["path"] = path
    save(root / "baselines/test-golden.json", index)
    with pytest.raises(ValueError, match="[Pp]ath|[Dd]estination|[Uu]nsafe"):
        downloader().ensure_golden(root, offline=True)


def test_manifest_identity_must_match_tracked_index(distribution):
    root, index, _ = distribution
    index["manifest_fingerprint"] = "f" * 64
    save(root / "baselines/test-golden.json", index)
    with pytest.raises(ValueError, match="[Ii]ntegrity|[Ff]ingerprint"):
        downloader().ensure_golden(root, offline=True)
    assert not (root / index["path"]).exists()


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}


@pytest.mark.parametrize("range_honored", [True, False])
def test_download_resumes_206_or_restarts_200(distribution, monkeypatch, range_honored):
    root, index, parts = distribution
    module = downloader()
    asset = index["distribution"]["assets"][0]
    cache = root / ".workspace/cache/golden/test-golden"
    (cache / asset["name"]).unlink()
    (cache / (asset["name"] + ".part")).write_bytes(parts[0][:17])

    class Opener:
        def open(self, request, timeout):
            assert request.get_header("Range") == "bytes=17-"
            return Response(parts[0][17:] if range_honored else parts[0],
                206 if range_honored else 200,
                {"Content-Range": f"bytes 17-{len(parts[0])-1}/{len(parts[0])}"} if range_honored else {})

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *args: Opener())
    assert module.ensure_golden(root) == root / index["path"]
    assert (cache / asset["name"]).read_bytes() == parts[0]


def test_incomplete_http_response_retries_from_retained_offset(distribution, monkeypatch):
    root, index, parts = distribution
    module = downloader()
    asset = index["distribution"]["assets"][0]
    cache = root / ".workspace/cache/golden/test-golden"
    (cache / asset["name"]).unlink()
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request.get_header("Range"))
            if len(calls) == 1:
                return Response(parts[0][:23])
            assert request.get_header("Range") == "bytes=23-"
            return Response(parts[0][23:], 206, {"Content-Range": f"bytes 23-{len(parts[0])-1}/{len(parts[0])}"})

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *args: Opener())
    module.ensure_golden(root)
    assert calls == [None, "bytes=23-"]


def test_private_asset_uses_authenticated_api_and_no_token_is_persisted(distribution, monkeypatch):
    root, index, parts = distribution
    module = downloader()
    index["distribution"]["private"] = True
    save(root / "baselines/test-golden.json", index)
    asset = index["distribution"]["assets"][0]
    (root / ".workspace/cache/golden/test-golden" / asset["name"]).unlink()
    monkeypatch.setenv("GH_TOKEN", "test-secret-token")

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == asset["api_url"]
            assert request.get_header("Authorization") == "Bearer test-secret-token"
            assert request.get_header("Accept") == "application/octet-stream"
            return Response(parts[0])

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *args: Opener())
    module.ensure_golden(root)
    for path in root.rglob("*"):
        if path.is_file():
            assert b"test-secret-token" not in path.read_bytes()


def test_redirect_strips_credentials_when_host_changes():
    module = downloader()
    handler = module.SafeRedirectHandler()
    request = urllib.request.Request("https://api.github.com/repos/x/y/releases/assets/1",
        headers={"Authorization": "Bearer secret", "Accept": "application/octet-stream"})
    redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/a")
    assert redirected.get_header("Authorization") is None
    assert request.get_header("Authorization") == "Bearer secret"


def test_private_missing_credentials_fails_without_network(distribution, monkeypatch):
    root, index, _ = distribution
    module = downloader()
    index["distribution"]["private"] = True
    save(root / "baselines/test-golden.json", index)
    shutil.rmtree(root / ".workspace/cache/golden")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(module, "github_token", lambda: None)
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *args: pytest.fail("Must not access network without credentials"))
    with pytest.raises(RuntimeError, match="GH_TOKEN|credential|[Aa]uth"):
        module.ensure_golden(root)


def test_git_credentials_are_requested_noninteractively(monkeypatch):
    module = downloader()
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def run(command, **kwargs):
        assert command == ["git", "credential", "fill"]
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["env"]["GCM_INTERACTIVE"] == "never"
        assert kwargs["input"] == "protocol=https\nhost=github.com\npath=CoronaEngine/CoronaTests.git\n\n"
        return type("Result", (), {"returncode": 0, "stdout": "protocol=https\nhost=github.com\nusername=test\npassword=credential-secret\n"})()

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.github_token() == "credential-secret"


def test_cli_root_installs_from_an_unrelated_working_directory(distribution, tmp_path):
    root, index, _ = distribution
    script = Path(__file__).resolve().parents[1] / "scripts/download_golden.py"
    result = subprocess.run([sys.executable, "-B", str(script), "--root", str(root), "--offline"],
        cwd=root.parent, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["status"] == "ready"
    assert Path(output["golden"]) == root / index["path"]
    assert Path(output["report"]).is_file()


def test_conflicting_same_id_pointer_is_preserved(distribution):
    root, index, _ = distribution
    pointer = root / ".workspace/latest-golden.json"
    save(pointer, {"id": "test-golden", "path": "somewhere/else", "manifest_fingerprint": "f" * 64})
    before = pointer.read_bytes()
    with pytest.raises(ValueError, match="[Pp]ointer.*conflict"):
        downloader().ensure_golden(root, offline=True)
    assert pointer.read_bytes() == before
    assert not (root / index["path"]).exists()


def test_invalid_http_range_retains_no_bad_partial_or_install(distribution, monkeypatch):
    root, index, parts = distribution
    module = downloader()
    asset = index["distribution"]["assets"][0]
    cache = root / ".workspace/cache/golden/test-golden"
    (cache / asset["name"]).unlink()
    partial = cache / (asset["name"] + ".part")
    partial.write_bytes(parts[0][:17])

    class Opener:
        def open(self, request, timeout):
            return Response(parts[0][17:], 206, {"Content-Range": f"bytes 0-{len(parts[0])-1}/{len(parts[0])}"})

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError, match="Content-Range"):
        module.ensure_golden(root)
    assert not partial.exists()
    assert not (root / index["path"]).exists()


def test_failed_download_preserves_resumable_bytes_and_previous_pointer(distribution, monkeypatch):
    root, index, parts = distribution
    module = downloader()
    asset = index["distribution"]["assets"][0]
    cache = root / ".workspace/cache/golden/test-golden"
    (cache / asset["name"]).unlink()
    pointer = root / ".workspace/latest-golden.json"
    save(pointer, {"id": "previous", "path": ".workspace/previous"})
    previous = pointer.read_bytes()

    class Opener:
        def open(self, request, timeout):
            if request.get_header("Range") is None:
                return Response(parts[0][:11])
            raise urllib.error.URLError("fixture offline")

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(RuntimeError, match="3 attempts"):
        module.ensure_golden(root)
    assert (cache / (asset["name"] + ".part")).read_bytes() == parts[0][:11]
    assert pointer.read_bytes() == previous
    assert not (root / index["path"]).exists()


@pytest.mark.parametrize("field", ["url", "api_url"])
def test_private_download_rejects_credential_destination_substitution(distribution, field, monkeypatch):
    root, index, _ = distribution
    module = downloader()
    index["distribution"]["private"] = True
    index["distribution"]["assets"][0][field] = "https://attacker.example/collect"
    save(root / "baselines/test-golden.json", index)
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *args: pytest.fail("Must reject URL before network access"))
    with pytest.raises(ValueError, match="URL"):
        module.ensure_golden(root)


def test_complete_partial_part_is_promoted_offline(distribution):
    root, index, _ = distribution
    cache = root / ".workspace/cache/golden/test-golden"
    first = cache / index["distribution"]["assets"][0]["name"]
    first.rename(first.with_name(first.name + ".part"))
    assert downloader().ensure_golden(root, offline=True) == root / index["path"]


def test_zip_nul_alias_is_rejected(distribution):
    root, index, _ = distribution
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("badX.txt", b"bad")
    replace_archive(root, index, data.getvalue().replace(b"badX.txt", b"bad\x00.txt"))
    with pytest.raises(ValueError, match="[Uu]nsafe"):
        downloader().ensure_golden(root, offline=True)


@pytest.fixture
def tracked_golden(distribution):
    root, index, parts = distribution
    index["path"] = "golden/test-golden"
    save(root / "baselines/test-golden.json", index)
    destination = root / index["path"]
    with zipfile.ZipFile(io.BytesIO(b"".join(parts))) as archive:
        archive.extractall(destination)
    return root, index, destination


def lfs_pointer(path):
    original = path.read_bytes()
    path.write_bytes(("version https://git-lfs.github.com/spec/v1\n"
        + "oid sha256:" + digest(original) + "\nsize " + str(len(original)) + "\n").encode("ascii"))
    return original


def test_materialized_tracked_golden_is_ready_offline_without_git_or_network(tracked_golden, monkeypatch):
    root, index, destination = tracked_golden
    index.pop("distribution")
    save(root / "baselines/test-golden.json", index)
    module = downloader()
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: pytest.fail("Materialized evidence needs no Git"))
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *args: pytest.fail("Offline evidence needs no network"))
    assert module.ensure_golden(root, offline=True) == destination
    assert json.loads((root / ".workspace/latest-golden.json").read_text())["path"] == "golden/test-golden"
    assert workflow.read_golden(destination)[0]["manifest_fingerprint"] == index["manifest_fingerprint"]


def test_tracked_golden_migrates_matching_legacy_pointer(tracked_golden):
    root, index, destination = tracked_golden
    pointer = root / ".workspace/latest-golden.json"
    save(pointer, {**index, "path": ".workspace/experiments/golden/test-golden"})
    assert downloader().ensure_golden(root, offline=True) == destination
    assert json.loads(pointer.read_text())["path"] == "golden/test-golden"


@pytest.mark.parametrize("previous_path", ["golden/test-golden", ".workspace/experiments/golden/test-golden"])
def test_tracked_golden_preserves_conflicting_fingerprint_pointer(tracked_golden, previous_path):
    root, index, _ = tracked_golden
    pointer = root / ".workspace/latest-golden.json"
    save(pointer, {**index, "path": previous_path, "manifest_fingerprint": "f" * 64})
    before = pointer.read_bytes()
    with pytest.raises(ValueError, match="[Pp]ointer.*conflict"):
        downloader().ensure_golden(root, offline=True)
    assert pointer.read_bytes() == before


def test_tracked_golden_rejects_arbitrary_same_fingerprint_pointer_path(tracked_golden):
    root, index, _ = tracked_golden
    pointer = root / ".workspace/latest-golden.json"
    save(pointer, {**index, "path": "somewhere/else"})
    before = pointer.read_bytes()
    with pytest.raises(ValueError, match="[Pp]ointer.*conflict"):
        downloader().ensure_golden(root, offline=True)
    assert pointer.read_bytes() == before


def test_offline_tracked_lfs_pointer_has_actionable_error_without_git(tracked_golden, monkeypatch):
    root, index, destination = tracked_golden
    image = next(destination.rglob("*.png"))
    original = lfs_pointer(image)
    before = image.read_bytes()
    pointer = root / ".workspace/latest-golden.json"
    save(pointer, {"id": "previous", "path": ".workspace/previous"})
    previous = pointer.read_bytes()
    module = downloader()
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: pytest.fail("Offline must not run Git"))
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *args: pytest.fail("Offline must not access network"))
    with pytest.raises((ValueError, RuntimeError), match="git lfs pull"):
        module.ensure_golden(root, offline=True)
    assert image.read_bytes() == before
    assert pointer.read_bytes() == previous


def test_online_tracked_lfs_hydration_uses_repo_credentials_then_verifies(tracked_golden, monkeypatch):
    root, index, destination = tracked_golden
    image = next(destination.rglob("*.png"))
    original = lfs_pointer(image)
    pointer = root / ".workspace/latest-golden.json"
    save(pointer, {"id": "previous", "path": ".workspace/previous"})
    previous = pointer.read_bytes()
    module = downloader()

    def run(command, **kwargs):
        assert command == ["git", "lfs", "pull", "--include=golden/test-golden/**", "--exclude="]
        assert Path(kwargs["cwd"]).resolve() == root.resolve()
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["env"]["GCM_INTERACTIVE"] == "never"
        assert pointer.read_bytes() == previous
        image.write_bytes(original)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module, "github_token", lambda: pytest.fail("Git LFS must use existing Git credentials"))
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *args: pytest.fail("Use Git LFS transport"))
    assert module.ensure_golden(root) == destination
    assert image.read_bytes() == original
    assert workflow.read_golden(destination)[0]["manifest_fingerprint"] == index["manifest_fingerprint"]
    assert json.loads(pointer.read_text())["path"] == "golden/test-golden"


@pytest.mark.parametrize("failure", ["missing-git", "missing-lfs", "still-pointer", "wrong-content"])
def test_failed_tracked_lfs_hydration_preserves_latest_pointer(tracked_golden, monkeypatch, failure):
    root, index, destination = tracked_golden
    image = next(destination.rglob("*.png"))
    lfs_pointer(image)
    pointer = root / ".workspace/latest-golden.json"
    save(pointer, {"id": "previous", "path": ".workspace/previous"})
    previous = pointer.read_bytes()
    module = downloader()

    def run(command, **kwargs):
        if failure == "missing-git":
            raise FileNotFoundError("fixture Git unavailable")
        if failure == "missing-lfs":
            return subprocess.CompletedProcess(command, 1, "", "git: 'lfs' is not a git command")
        if failure == "wrong-content":
            image.write_bytes(b"corrupt hydration")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises((ValueError, RuntimeError), match="Git LFS|git lfs|integrity"):
        module.ensure_golden(root)
    assert pointer.read_bytes() == previous


@pytest.mark.parametrize("corruption", ["ordinary-png", "wrong-oid", "malformed-pointer", "other-artifact"])
def test_tracked_corruption_is_not_replaced_by_git_lfs(tracked_golden, monkeypatch, corruption):
    root, _, destination = tracked_golden
    image = next(destination.rglob("*.png"))
    original = lfs_pointer(image)
    if corruption == "ordinary-png":
        image.write_bytes(b"arbitrary corrupt PNG bytes")
    elif corruption == "wrong-oid":
        image.write_bytes(image.read_bytes().replace(digest(original).encode("ascii"), b"0" * 64))
    elif corruption == "malformed-pointer":
        image.write_bytes(b"version https://git-lfs.github.com/spec/v1\nnot a real pointer\n")
    else:
        (destination / "thresholds.json").write_bytes(b"unrelated corruption")
    before = image.read_bytes()
    module = downloader()
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: pytest.fail("Corruption must fail before Git LFS"))
    with pytest.raises(ValueError, match="integrity|incomplete"):
        module.ensure_golden(root)
    assert image.read_bytes() == before
    assert not (root / ".workspace/latest-golden.json").exists()
