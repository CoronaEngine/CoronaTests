"""Fetch a pinned golden distribution and atomically install verified evidence.

Only the Python standard library is needed for transport. Golden validation is
shared lazily with the regression workflow, so both entry points accept exactly
the same sealed evidence and comparison thresholds.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
GITHUB_HOSTS = {"github.com", "api.github.com"}
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10))}


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError("Invalid baseline identifier")
    return value


def default_baseline_id(root: Path) -> str:
    value = _load(Path(root) / "baselines/default.json")
    if value.get("schema") != 1:
        raise ValueError("Invalid default baseline schema")
    return _identifier(value.get("id"))


def _safe_parts(name: str) -> tuple[str, ...]:
    """Reject Windows aliases as well as archive traversal on every platform."""
    if not isinstance(name, str) or not name or "\\" in name or name.startswith("/"):
        raise ValueError("Unsafe path: " + str(name))
    parts = name.split("/")
    if any(not part or part in (".", "..") or part.endswith((".", " "))
           or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
           or part.split(".")[0].upper() in WINDOWS_RESERVED for part in parts):
        raise ValueError("Unsafe path: " + name)
    return tuple(parts)


def _inside(root: Path, parts: tuple[str, ...]) -> Path:
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Unsafe path resolves outside root")
    # Never use a symlink/junction to turn an index or cache path into another
    # directory, even when that directory happens to remain inside the root.
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("Unsafe path contains a symlink or junction")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_file(path: Path, entry: dict) -> None:
    if not path.is_file() or path.is_symlink() or path.stat().st_size != entry["bytes"]:
        raise ValueError("Golden distribution size integrity mismatch: " + entry["name"])
    if _sha256(path) != entry["sha256"]:
        raise ValueError("Golden distribution SHA256 integrity mismatch: " + entry["name"])


def _file_entry(entry, name: str) -> None:
    if (not isinstance(entry, dict) or entry.get("name") != name
            or type(entry.get("bytes")) is not int or entry["bytes"] <= 0
            or not isinstance(entry.get("sha256"), str)
            or not re.fullmatch("[0-9a-f]{64}", entry["sha256"])):
        raise ValueError("Invalid golden distribution file metadata")


def _distribution(index: dict, baseline_id: str) -> dict:
    distribution = index.get("distribution")
    if (not isinstance(distribution, dict) or distribution.get("schema") != 1
            or distribution.get("format") != "split-zip"
            or distribution.get("repository") != "CoronaEngine/CoronaTests"
            or distribution.get("tag") != "golden-" + baseline_id
            or type(distribution.get("private")) is not bool):
        raise ValueError("Invalid or missing golden distribution metadata")
    _file_entry(distribution.get("archive"), baseline_id + ".zip")
    assets = distribution.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ValueError("Golden distribution assets are missing")
    for number, asset in enumerate(assets, 1):
        name = baseline_id + f".zip.{number:03}"
        _file_entry(asset, name)
        if asset["bytes"] > 1024 ** 3:
            raise ValueError("Golden distribution chunk exceeds 1 GiB")
        expected_url = (f"https://github.com/{distribution['repository']}/releases/download/"
                        f"{distribution['tag']}/{name}")
        expected_api = f"https://api.github.com/repos/{distribution['repository']}/releases/assets/"
        if (asset.get("url") != expected_url or not isinstance(asset.get("api_url"), str)
                or not re.fullmatch(re.escape(expected_api) + r"[1-9][0-9]*", asset["api_url"])):
            raise ValueError("Invalid GitHub distribution asset URL")
    if sum(asset["bytes"] for asset in assets) != distribution["archive"]["bytes"]:
        raise ValueError("Golden distribution archive and part sizes differ")
    return distribution


def github_token() -> str | None:
    """Read existing credentials only; never trigger a login or save a token."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token.strip() or None
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    try:
        result = subprocess.run(["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\npath=CoronaEngine/CoronaTests.git\n\n", capture_output=True, text=True,
            env=environment, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if values.get("password"):
            return values["password"]
    return None


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """GitHub assets redirect to storage hosts that must never receive tokens."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ValueError("Unsafe golden download redirect")
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected is not None and (
                urllib.parse.urlsplit(request.full_url).netloc.lower() != parsed.netloc.lower()
                or parsed.hostname not in GITHUB_HOSTS):
            redirected.remove_header("Authorization")
        return redirected


def _download_part(entry: dict, cache: Path, *, private: bool, token: str | None) -> Path:
    destination = cache / entry["name"]
    partial = destination.with_name(destination.name + ".part")
    if partial.is_symlink():
        raise ValueError("Unsafe golden partial cache symlink")
    if partial.exists() and partial.stat().st_size >= entry["bytes"]:
        try:
            _check_file(partial, entry)
            partial.replace(destination)
            return destination
        except ValueError:
            partial.unlink()
    opener = urllib.request.build_opener(SafeRedirectHandler())
    for attempt in range(3):
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": "CoronaTests/1", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        if private:
            headers.update(Authorization="Bearer " + token, Accept="application/octet-stream")
        request = urllib.request.Request(entry["api_url"] if private else entry["url"], headers=headers)
        try:
            with opener.open(request, timeout=60) as response:
                if response.status not in (200, 206):
                    raise ValueError("Unexpected golden download HTTP status")
                append = response.status == 206
                if append:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                    if (not match or int(match[1]) != offset or int(match[2]) < offset
                            or int(match[2]) >= entry["bytes"] or int(match[3]) != entry["bytes"]):
                        raise ValueError("Invalid HTTP Content-Range for golden asset")
                with partial.open("ab" if append else "wb") as output:
                    while block := response.read(1024 * 1024):
                        output.write(block)
                        if output.tell() > entry["bytes"]:
                            raise ValueError("Golden download exceeds pinned size")
            if partial.stat().st_size < entry["bytes"]:
                raise OSError("Incomplete golden download; partial retained for resume")
            _check_file(partial, entry)
            partial.replace(destination)
            return destination
        except urllib.error.HTTPError as error:
            if error.code == 416:
                partial.unlink(missing_ok=True)
            if error.code in (401, 403, 404):
                raise RuntimeError("Golden asset unavailable; check repository access and GitHub credentials") from None
        except (urllib.error.URLError, OSError, http.client.IncompleteRead):
            pass
        except ValueError:
            partial.unlink(missing_ok=True)
            raise
    # Do not echo transport exceptions: a server/proxy error may contain secrets.
    raise RuntimeError("Golden asset download failed after 3 attempts; partial retained for resume") from None


def _archive(distribution: dict, cache: Path, *, offline: bool) -> Path:
    destination = cache / distribution["archive"]["name"]
    if destination.exists():
        _check_file(destination, distribution["archive"])
        return destination
    missing = []
    for asset in distribution["assets"]:
        path = cache / asset["name"]
        partial = path.with_name(path.name + ".part")
        if not path.exists() and partial.exists() and partial.stat().st_size == asset["bytes"]:
            _check_file(partial, asset)
            partial.replace(path)
        if path.exists():
            _check_file(path, asset)
        else:
            missing.append(asset)
    if offline and missing:
        raise FileNotFoundError("Offline golden asset missing: " + missing[0]["name"])
    token = github_token() if missing and distribution["private"] else None
    if missing and distribution["private"] and not token:
        raise RuntimeError("Private golden download needs GH_TOKEN, GITHUB_TOKEN, or an existing GitHub git credential")
    for asset in missing:
        _download_part(asset, cache, private=distribution["private"], token=token)
    temporary = destination.with_name(destination.name + ".part")
    if temporary.is_symlink():
        raise ValueError("Unsafe golden archive cache symlink")
    with temporary.open("wb") as output:
        for asset in distribution["assets"]:
            with (cache / asset["name"]).open("rb") as source:
                shutil.copyfileobj(source, output, 1024 * 1024)
    _check_file(temporary, distribution["archive"])
    temporary.replace(destination)
    return destination


def _extract(archive_path: Path, staging: Path) -> None:
    seen = set()
    with zipfile.ZipFile(archive_path) as archive:
        # Validate every member before writing any bytes. Validate implicit
        # directories too, so a file cannot also be a directory by case alias.
        files, directories = set(), set()
        for item in archive.infolist():
            if item.orig_filename != item.filename:
                raise ValueError("Unsafe ZIP member name alias")
            name = item.filename[:-1] if item.is_dir() else item.filename
            parts = _safe_parts(name)
            key = "/".join(parts).casefold()
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError("Unsafe ZIP member type: " + name)
            if key in seen:
                raise ValueError("Duplicate ZIP member: " + name)
            seen.add(key)
            if item.is_dir():
                directories.add(key)
            else:
                files.add(key)
            directories.update("/".join(parts[:n]).casefold() for n in range(1, len(parts)))
        if files & directories:
            raise ValueError("Duplicate ZIP file/directory path")
        for item in archive.infolist():
            name = item.filename[:-1] if item.is_dir() else item.filename
            target = staging.joinpath(*_safe_parts(name))
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)


def _verify_golden(path: Path, index: dict) -> None:
    if __package__ in (None, ""):
        sys.path.insert(0, str(ROOT))
    from scripts.workflow import read_golden
    try:
        manifest, _ = read_golden(path)
    except FileNotFoundError as error:
        raise ValueError("Existing or extracted golden evidence is incomplete") from error
    if (manifest.get("id") != index["id"] or manifest.get("manifest_fingerprint") != index["manifest_fingerprint"]
            or manifest.get("created_utc") != index["created_utc"]):
        raise ValueError("Golden index/manifest fingerprint integrity mismatch")


def _has_lfs_pointers(path: Path, index: dict) -> bool:
    """Permit hydration only for sealed PNG pointers, never other corruption."""
    from scripts.workflow import fingerprint, golden_files
    manifest_path, _, _ = golden_files(path)
    manifest = _load(manifest_path)
    check = dict(manifest)
    sealed = check.pop("manifest_fingerprint", None)
    if (sealed != fingerprint(check) or sealed != index["manifest_fingerprint"]
            or manifest.get("id") != index["id"] or manifest.get("created_utc") != index["created_utc"]):
        raise ValueError("Golden index/manifest fingerprint integrity mismatch")
    found = False
    for relative, digest in manifest["artifacts"].items():
        artifact = _inside(path, _safe_parts(relative))
        if not artifact.is_file():
            raise ValueError("Existing golden evidence is incomplete: " + relative)
        if _sha256(artifact) == digest:
            continue
        pointer = (artifact.read_bytes() if artifact.suffix.lower() == ".png"
                   and artifact.stat().st_size <= 1024 else b"")
        if not re.fullmatch(rb"version https://git-lfs.github.com/spec/v1\n"
                + rb"oid sha256:" + digest.encode("ascii") + rb"\nsize [1-9][0-9]*\n", pointer):
            raise ValueError("Artifact hash integrity mismatch: " + relative)
        found = True
    return found


def _hydrate_lfs(root: Path, index: dict, *, offline: bool) -> None:
    command = ["git", "lfs", "pull", "--include=" + index["path"] + "/**", "--exclude="]
    action = " ".join(command)
    if offline:
        raise RuntimeError("Offline golden contains unresolved Git LFS PNG pointers; run " + action
                           + " from the repository, then retry --offline")
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    try:
        result = subprocess.run(command, cwd=root, capture_output=True, text=True,
                                env=environment, check=False)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("Git LFS unavailable; install Git LFS, then run " + action) from None
    if result.returncode != 0:
        # Never echo credential helpers or transport output, which may contain secrets.
        raise RuntimeError("Git LFS pull failed; check Git LFS installation, repository access and existing Git credentials; run "
                           + action) from None


def _latest(root: Path, index: dict) -> None:
    pointer = _inside(root, (".workspace", "latest-golden.json"))
    value = {key: index[key] for key in ("id", "path", "manifest_fingerprint", "created_utc")}
    pointer.parent.mkdir(parents=True, exist_ok=True)
    # A uniquely named sibling prevents concurrent failed attempts from
    # destroying another install's pointer temporary file.
    handle, temporary_name = tempfile.mkstemp(prefix="latest-golden-", suffix=".tmp", dir=pointer.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, pointer)
    finally:
        temporary.unlink(missing_ok=True)


def _derive_compact(root: Path, destination: Path, index: dict, *, offline: bool) -> None:
    derivation = index.get("derivation")
    if (not isinstance(derivation, dict) or derivation.get("schema") != 1
            or derivation.get("format") != "compact-v1"):
        raise ValueError("Invalid compact golden derivation metadata")
    source_id = _identifier(derivation.get("source_id"))
    if source_id == index["id"]:
        raise ValueError("Compact golden derivation cannot refer to itself")
    source_index = _load(root / "baselines" / (source_id + ".json"))
    if (source_index.get("id") != source_id or source_index.get("manifest_fingerprint")
            != derivation.get("source_manifest_fingerprint")):
        raise ValueError("Compact source fingerprint differs from pinned index")
    distribution = _distribution(source_index, source_id)
    source_cache = _inside(root, (".workspace", "cache", "golden", source_id))
    source_cache.mkdir(parents=True, exist_ok=True)
    archive = _archive(distribution, source_cache, offline=offline)
    cache = _inside(root, (".workspace", "cache", "golden", index["id"]))
    cache.mkdir(parents=True, exist_ok=True)
    from scripts.compact_golden import compact_golden
    with tempfile.TemporaryDirectory(prefix="compact-", dir=cache) as temporary:
        source, compact = Path(temporary) / "source", Path(temporary) / "compact"
        source.mkdir()
        _extract(archive, source)
        _verify_golden(source, source_index)
        compact_golden(source, compact, baseline_id=index["id"])
        _verify_golden(compact, index)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            _verify_golden(destination, index)
        else:
            compact.rename(destination)


def ensure_golden(root: Path, baseline_id: str | None = None, *, offline: bool = False) -> Path:
    root = Path(root).resolve()
    baseline_id = _identifier(baseline_id) if baseline_id is not None else default_baseline_id(root)
    index = _load(root / "baselines" / (baseline_id + ".json"))
    if (index.get("id") != baseline_id or not isinstance(index.get("manifest_fingerprint"), str)
            or not re.fullmatch("[0-9a-f]{64}", index["manifest_fingerprint"])
            or not isinstance(index.get("created_utc"), str)):
        raise ValueError("Invalid golden baseline index")
    parts = _safe_parts(index.get("path"))
    if parts == ("golden",):
        destination = _inside(root, parts)
        if not destination.is_dir():
            raise FileNotFoundError("Published golden directory missing; restore it with git checkout and git lfs pull")
        try:
            _verify_golden(destination, index)
        except ValueError:
            if not _has_lfs_pointers(destination, index):
                raise
            _hydrate_lfs(root, index, offline=offline)
            _verify_golden(destination, index)
        _latest(root, index)
        return destination
    tracked_parts = ("golden", baseline_id)
    legacy_parts = (".workspace", "experiments", "golden", baseline_id)
    if parts not in (tracked_parts, legacy_parts):
        raise ValueError("Golden destination path must be golden/<baseline-id> or .workspace/experiments/golden/<baseline-id>")
    destination = _inside(root, parts)
    pointer = _inside(root, (".workspace", "latest-golden.json"))
    if pointer.exists():
        previous = _load(pointer)
        if previous.get("id") == baseline_id:
            migration = parts == tracked_parts and previous.get("path") == "/".join(legacy_parts)
            if (previous.get("manifest_fingerprint") != index["manifest_fingerprint"]
                    or (previous.get("path") != index["path"] and not migration)):
                raise ValueError("Existing golden pointer conflicts with pinned baseline")
    if destination.exists():
        try:
            _verify_golden(destination, index)
        except ValueError:
            if parts != tracked_parts or not _has_lfs_pointers(destination, index):
                raise
            _hydrate_lfs(root, index, offline=offline)
            _verify_golden(destination, index)
        _latest(root, index)
        return destination
    if "derivation" in index:
        _derive_compact(root, destination, index, offline=offline)
        _latest(root, index)
        return destination
    distribution = _distribution(index, baseline_id)
    cache = _inside(root, (".workspace", "cache", "golden", baseline_id))
    cache.mkdir(parents=True, exist_ok=True)
    archive_path = _archive(distribution, cache, offline=offline)
    with tempfile.TemporaryDirectory(prefix="extract-", dir=cache) as temporary:
        staging = Path(temporary) / "golden"
        staging.mkdir()
        _extract(archive_path, staging)
        _verify_golden(staging, index)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            _verify_golden(destination, index)
        else:
            staging.rename(destination)
    _latest(root, index)
    return destination


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Download and verify the pinned golden gallery and thresholds.")
    parser.add_argument("--baseline-id")
    parser.add_argument("--offline", action="store_true", help="Install or reuse verified local golden evidence only.")
    parser.add_argument("--root", type=Path, default=ROOT, help="CoronaTests repository/data root.")
    options = parser.parse_args(argv)
    try:
        path = ensure_golden(options.root, options.baseline_id, offline=options.offline)
        result = {"status": "ready", "golden": str(path)}
        if (path / "index.html").is_file():
            result["report"] = str(path / "index.html")
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, zipfile.BadZipFile) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
