"""The actual no-argument CMD entrypoints must select independent settings."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != 'nt', reason='Windows double-click CMD entrypoints')
@pytest.mark.parametrize('entry,kind,spp', [
    ('generate_golden.cmd','golden',2048),
    ('run_regression.cmd','regression',512),
    ('run_full_regression.cmd','regression',512),
])
def test_double_click_routes_to_its_own_config_and_keeps_full_coverage(tmp_path, entry, kind, spp):
    sandbox = tmp_path / 'launch with spaces'
    (sandbox / 'scripts').mkdir(parents=True)
    for name in ('generate_golden.cmd','run_regression.cmd','run_full_regression.cmd','scripts/desktop_run.cmd'):
        shutil.copyfile(ROOT / name, sandbox / name)
    for name, samples in (('config.local.json',128), ('config.golden.local.json',2048),
                          ('config.regression.local.json',512)):
        (sandbox / name).write_text(json.dumps({'sampling':{'pt_spp':samples}}))
    recorder = (
        'from pathlib import Path\nimport sys,json\n'
        f'sys.path.insert(0,{str(ROOT)!r})\n'
        'from scripts import workflow\n'
        "p=Path(__file__).resolve().parents[1]\nkind='golden' if Path(__file__).name=='generate_golden.py' else 'regression'\n"
        'o=workflow.parse_options(kind,sys.argv[1:],root=p)\n'
        f'_,recipes,scenes,algorithms=workflow.selection(Path({str(ROOT)!r}),o)\n'
        "(p/'actual.json').write_text(json.dumps({'config':str(o.config),'spp':recipes['pt2048']['frames'],'jobs':len(scenes)*len(algorithms)}))\n")
    for name in ('generate_golden.py','run_regression.py'):
        (sandbox / 'scripts' / name).write_text(recorder)
    command = f'"{os.environ["COMSPEC"]}" /d /c ""{sandbox/entry}""'
    result = subprocess.run(command, input='\r\n', capture_output=True, text=True, errors='replace',
                            timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout + result.stderr
    actual = json.loads((sandbox / 'actual.json').read_text())
    assert Path(actual['config']) == sandbox / f'config.{kind}.local.json'
    assert actual['spp'] == spp and actual['jobs'] == 105
    assert '. . .' in result.stdout
