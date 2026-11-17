"""Download and verify the pinned 35-scene catalog, then prepare local resources.

All persisted paths are relative to the data root. No engine/editor installation
or historical resource directory is imported. ZIP SHA256 and CRC are checked
before extraction; a ready manifest appears only after referenced assets pass.
"""
import argparse
from collections.abc import Callable
import hashlib
import http.client
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.prepare_scenes import PREPARATION_VERSION, prepare_scene, verify_references

REPO = Path(__file__).resolve().parents[1]
DATA_PATH = Path(".workspace/downloads/mitsuba")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _check_archive(path: Path, entry: dict) -> None:
    if entry.get("expected_bytes") is not None and path.stat().st_size != entry["expected_bytes"]:
        raise ValueError(f"Archive size mismatch: {entry['id']}")
    if sha256(path) != entry["sha256"]:
        raise ValueError(f"Archive SHA256 checksum mismatch: {entry['id']}")
    with zipfile.ZipFile(path) as archive:
        corrupt = archive.testzip()
        if corrupt is not None:
            raise ValueError(f"ZIP CRC mismatch: {corrupt}")


def download_archive(entry: dict, archives: Path, *, offline: bool = False) -> Path:
    archives.mkdir(parents=True, exist_ok=True)
    destination = archives / (entry["id"] + ".zip")
    if destination.exists():
        try:
            _check_archive(destination, entry)
            return destination
        except (ValueError, zipfile.BadZipFile):
            if offline:
                raise
            destination.unlink()
    if offline:
        raise FileNotFoundError(f"Offline archive missing: {destination}")
    partial = destination.with_suffix(".zip.part")
    if partial.exists() and partial.stat().st_size == entry.get("expected_bytes"):
        try:
            _check_archive(partial, entry)
            partial.replace(destination)
            return destination
        except (ValueError, zipfile.BadZipFile):
            partial.unlink()
    last_error = None
    for attempt in range(3):
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": "CoronaTests/1", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            request = urllib.request.Request(entry["url"], headers=headers)
            with urllib.request.urlopen(request, timeout=60) as response:
                append = offset > 0 and response.status == 206
                if response.status == 206:
                    content_range = response.headers.get("Content-Range", "")
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                    if match is None or int(match[1]) != offset or int(match[2]) < offset or (entry.get("expected_bytes") is not None and int(match[3]) != entry["expected_bytes"]):
                        raise ValueError("Invalid HTTP Content-Range")
                with partial.open("ab" if append else "wb") as output:
                    shutil.copyfileobj(response, output)
            if entry.get("expected_bytes") is not None and partial.stat().st_size < entry["expected_bytes"]:
                raise OSError(f"Incomplete HTTP response for {entry['id']}; retained partial for resume")
            _check_archive(partial, entry)
            partial.replace(destination)
            return destination
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code == 416 and partial.exists():
                partial.unlink()
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.IncompleteRead) as error:
            last_error = error
        except (ValueError, zipfile.BadZipFile):
            partial.unlink(missing_ok=True)
            raise
    raise RuntimeError(f"Download failed for {entry['id']} after 3 attempts: {last_error}") from last_error


def extract_archive(archive_path: Path, directory: Path, entry: dict) -> None:
    """Strip exactly the catalog prefix, rejecting unsafe and ambiguous members."""
    prefix = entry.get("archive_prefix_removed", "")
    seen = set()
    with zipfile.ZipFile(archive_path) as archive:
        for item in archive.infolist():
            name = item.filename.replace("\\", "/")
            parts = PurePosixPath(name).parts
            mode = item.external_attr >> 16
            if name.startswith("/") or ".." in parts or ":" in name or stat.S_ISLNK(mode):
                raise ValueError(f"Unsafe ZIP member: {name}")
            if prefix and not name.startswith(prefix):
                if name.rstrip("/") == prefix.rstrip("/") and item.is_dir():
                    continue
                raise ValueError(f"Unexpected archive prefix: {name}")
            relative = name[len(prefix):] if prefix else name
            if not relative:
                continue
            relative_path = PurePosixPath(relative)
            if relative_path.is_absolute():
                raise ValueError(f"Unsafe ZIP member after prefix removal: {name}")
            if any(part.endswith((".", " ")) or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))} for part in relative_path.parts):
                raise ValueError(f"Unsafe ZIP member: {name}")
            key = relative_path.as_posix().casefold().rstrip("/")
            if key in seen:
                raise ValueError(f"Duplicate ZIP member: {name}")
            seen.add(key)
            target = directory.joinpath(*relative_path.parts)
            if not target.resolve().is_relative_to(directory.resolve()):
                raise ValueError(f"Unsafe ZIP extraction target: {name}")
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)


def _files(directory: Path) -> dict:
    return {path.relative_to(directory).as_posix(): sha256(path) for path in sorted(directory.rglob("*")) if path.is_file()}


def _valid_prepared(root: Path, record: dict, entry: dict) -> bool:
    if record.get("archive_sha256") != entry["sha256"] or record.get("preparation_version") != PREPARATION_VERSION:
        return False
    directory = root / record["directory"]
    try:
        if _files(directory) != record["files"] or not record["files"]:
            return False
        verify_references(root / record["source"], directory)
        return True
    except (OSError, ValueError):
        return False


def _publish_directory(stage: Path, target: Path, *, data_root: Path) -> None:
    backup = target.with_name(target.name + ".old")
    boundary = data_root.resolve()
    # Validate every move/delete operand before any mutation, including stale
    # backups and symlinked parents. The named data root itself is never moved.
    for operand in (stage, target, backup):
        resolved = operand.resolve()
        if resolved == boundary or not resolved.is_relative_to(boundary):
            raise ValueError(f"Unsafe publication path outside data root: {operand}")
    if backup.exists():
        shutil.rmtree(backup)
    if target.exists():
        target.replace(backup)
    try:
        stage.replace(target)
    except BaseException:
        if backup.exists():
            backup.replace(target)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def ensure_scenes(root: Path, scene_ids: list[str] | None = None, *, offline: bool = False, progress: Callable[[str], None] | None = None) -> dict:
    root = Path(root).resolve()
    catalog_path = root / "scenes/catalog.lock.json"
    if not catalog_path.exists():
        catalog_path = REPO / "scenes/catalog.lock.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
    catalog_digest = sha256(catalog_path)
    entries = {entry["id"]: entry for entry in catalog["scenes"]}
    selected = list(entries) if scene_ids is None else list(dict.fromkeys(scene_ids))
    unknown = set(selected) - entries.keys()
    if unknown:
        raise ValueError(f"Unknown scene IDs: {', '.join(sorted(unknown))}")
    suite_path = catalog_path.with_name("suite.json")
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    data = root / DATA_PATH
    data.mkdir(parents=True, exist_ok=True)
    ready_path = data / "ready.json"
    prior = json.loads(ready_path.read_text(encoding="utf-8")) if ready_path.exists() else {}
    records = {record["id"]: record for record in prior.get("scenes", [])} if prior.get("catalog_sha256") == catalog_digest else {}
    result = []
    for number, scene_id in enumerate(selected, start=1):
        entry = entries[scene_id]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", scene_id):
            raise ValueError(f"Unsafe scene ID: {scene_id}")
        # Always verify the archive, including ready-cache hits. Ready metadata is
        # not a substitute for the immutable source archive or asset digests.
        prefix = f"[{number}/{len(selected)}] {scene_id}"
        if progress is not None:
            progress(f"{prefix}: verifying/downloading archive")
        archive = download_archive(entry, data / "archives", offline=offline)
        record = records.get(scene_id)
        if record is None or not _valid_prepared(root, record, entry):
            if progress is not None:
                progress(f"{prefix}: preparing assets")
            with tempfile.TemporaryDirectory(prefix=f"{scene_id}-", dir=data) as temporary:
                stage = Path(temporary)
                original = stage / "original"
                original.mkdir()
                extract_archive(archive, original, entry)
                prepared = stage / "prepared"
                shutil.copytree(original, prepared)
                source = prepare_scene(prepared, entry)
                source_relative = source.relative_to(prepared)
                files = _files(prepared)
                target = data / "prepared" / scene_id
                target.parent.mkdir(parents=True, exist_ok=True)
                original_target = data / "original" / scene_id
                original_target.parent.mkdir(parents=True, exist_ok=True)
                _publish_directory(original, original_target, data_root=data)
                _publish_directory(prepared, target, data_root=data)
            record = {"id": scene_id, "name": entry["name"], "directory": target.relative_to(root).as_posix(), "source": (target / source_relative).relative_to(root).as_posix(), "source_sha256": files[source_relative.as_posix()], "files": files, "asset_fingerprint": hashlib.sha256(_canonical(files)).hexdigest(), "archive_sha256": entry["sha256"], "preparation_version": PREPARATION_VERSION, "expected_diagnostics": suite["scenes"].get(scene_id, {}).get("expected_diagnostics", [])}
            records[scene_id] = record
        transient = dict(record)
        transient["source"] = str(root / record["source"])
        transient["directory"] = str(root / record["directory"])
        result.append(transient)
        if progress is not None:
            progress(f"{prefix}: ready")
    _save(ready_path, {"schema": 1, "catalog_sha256": catalog_digest, "scenes": [records[key] for key in entries if key in records]})
    return {"schema": 1, "catalog_sha256": catalog_digest, "scenes": result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--scenes", nargs="+")
    args = parser.parse_args()
    try:
        result = ensure_scenes(args.root, args.scenes, offline=args.offline, progress=lambda message: print(message, flush=True))
    except Exception as error:
        print(f"Scene preparation failed: {error}", file=sys.stderr)
        return 1
    count = len(result["scenes"])
    print(f"Prepared {count} scene{'s' if count != 1 else ''}. Ready manifest: {args.root.resolve() / DATA_PATH / 'ready.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
