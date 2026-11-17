"""Prepare pytest's configured scratch parent before tmp_path is requested."""
from pathlib import Path


def pytest_configure(config):
    (Path(config.rootpath) / '.workspace/tmp').mkdir(parents=True, exist_ok=True)
