"""Expire completed runs in the local regression output directory."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from uuid import uuid4


_RUN_ENTRIES = {"assets", "comparisons", "pages", "reference", "runs", "index.html", "progress.json",
                "pt512-policy.json", "report.json", "run-config.json", "timings.csv", "workflow-result.json"}


def _linked(info):
    return (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1))


def _removable_tree(path):
    pending = [path]
    while pending:
        directory = pending.pop()
        if _linked(directory.lstat()):
            return False
        with os.scandir(directory) as entries:
            for entry in entries:
                # Windows DirEntry.stat() omits the hard-link count.
                info = os.stat(entry.path, follow_symlinks=False)
                if entry.name.lower() in {".git", "golden.json"} or _linked(info):
                    return False
                if directory != path and entry.name.lower() == "run-config.json":
                    return False
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
    return True


def _completed_at(directory):
    markers = [directory / name for name in ("run-config.json", "workflow-result.json", "index.html")]
    if not all(path.is_file() and not _linked(path.lstat()) for path in markers):
        return None
    if any(entry.name not in _RUN_ENTRIES for entry in directory.iterdir()):
        return None
    config, result = [json.loads(path.read_text(encoding="utf-8-sig")) for path in markers[:2]]
    if (not isinstance(config, dict) or not isinstance(result, dict)
            or not isinstance(config.get("contract"), dict) or config["contract"].get("kind") != "regression"
            or result.get("kind") != "regression" or not isinstance(result.get("passed"), bool)
            or not config.get("id") or result.get("id") != config["id"]
            or result.get("output") != str(directory)):
        return None
    if "completed_utc" in result:
        completed = datetime.fromisoformat(result["completed_utc"])
        return completed if completed.tzinfo else None
    return datetime.fromtimestamp(markers[1].stat().st_mtime, timezone.utc)


_RETIRED = ".coronatests-retired-"
_RECEIPT = "CoronaTests retired regression\n"


def _retry_readonly(function, path, error):
    if os.name != "nt" or function != os.unlink or not isinstance(error, PermissionError):
        raise error
    info = os.lstat(path)
    if (_linked(info) or not stat.S_ISREG(info.st_mode)
            or not info.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY):
        raise error
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _finish_retirement(holder):
    # The receipt stays outside the payload so a partially removed run can be retried.
    receipt = holder / "receipt"
    if any(entry.name not in {"receipt", "result"} for entry in holder.iterdir()):
        return
    if not receipt.is_file() or _linked(receipt.lstat()) or receipt.read_text(encoding="ascii") != _RECEIPT:
        return
    payload = holder / "result"
    if payload.exists():
        if not _removable_tree(payload):
            return
        if sys.version_info >= (3, 12):
            shutil.rmtree(payload, onexc=_retry_readonly)
        else:
            shutil.rmtree(payload, onerror=lambda function, path, exc: _retry_readonly(function, path, exc[1]))
    receipt.unlink()
    holder.rmdir()


def prune(root, output, completed_utc, golden):
    managed = root / ".workspace/experiments/regression"
    try:
        if output.parent != managed or managed.resolve() != managed:
            return
        for directory in list(managed.iterdir()):
            try:
                if (directory == output or golden.is_relative_to(directory)
                        or not directory.is_dir() or _linked(directory.lstat())):
                    continue
                if re.fullmatch(re.escape(_RETIRED) + "[0-9a-f]{32}", directory.name):
                    _finish_retirement(directory)
                    continue
                finished = _completed_at(directory)
                if finished is None or finished > completed_utc or not _removable_tree(directory):
                    continue
                holder = managed / (_RETIRED + uuid4().hex)
                holder.mkdir()
                (holder / "receipt").write_text(_RECEIPT, encoding="ascii")
                directory.rename(holder / "result")
                _finish_retirement(holder)
            except (OSError, ValueError, TypeError) as error:
                print(f"Regression cleanup skipped {directory}: {error}", file=sys.stderr)
    except OSError as error:
        print(f"Regression cleanup skipped {managed}: {error}", file=sys.stderr)
