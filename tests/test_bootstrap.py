"""A new checkout must support tmp_path without manually preparing scratch dirs."""
from pathlib import Path
import shutil
import subprocess
import sys


def test_first_pytest_run_creates_scratch_parent(tmp_path):
    root = Path(__file__).resolve().parents[1]
    shutil.copy2(root / 'pytest.ini', tmp_path / 'pytest.ini')
    tests = tmp_path / 'tests'
    tests.mkdir()
    if (root / 'tests/conftest.py').is_file():
        shutil.copy2(root / 'tests/conftest.py', tests / 'conftest.py')
    (tests / 'test_fresh.py').write_text('def test_fresh(tmp_path):\n    assert tmp_path.is_dir()\n')
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q'], cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
