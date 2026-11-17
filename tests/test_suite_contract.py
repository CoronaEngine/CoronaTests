"""The shipped suite must preserve the requested coverage and exact exemptions."""
import json
from pathlib import Path


def test_full_matrix_keeps_dragon_pt_supported_and_only_documented_exemptions():
    root = Path(__file__).resolve().parents[1]
    suite = json.loads((root / 'scenes/suite.json').read_text(encoding='utf-8'))
    algorithms = json.loads((root / 'recipes/algorithms.json').read_text(encoding='utf-8'))['algorithms']
    jobs = [(scene, algorithm) for scene in suite['full'] for algorithm in algorithms]
    exemptions = {(scene, algorithm) for scene, algorithm in jobs
                  if suite['scenes'][scene]['expected_unsupported'].get(algorithm)}
    expected = {('dragon', 'gi')}
    expected.update((scene, algorithm) for scene in ('hair-curl', 'curly-hair', 'straight-hair', 'furball')
                    for algorithm in ('gi', 'pt-reblur', 'pt2048'))
    assert len(jobs) == len(set(jobs)) == 105
    assert exemptions == expected
    assert len(set(jobs) - exemptions) == 92
    quick_jobs = {(scene, algorithm) for scene in suite['quick'] for algorithm in algorithms}
    assert len(quick_jobs) == 18
    assert quick_jobs <= set(jobs) - exemptions
