"""Pin distinct approved denoiser roles rather than sharing a generic preset."""
import json
from pathlib import Path


def test_detail_pt_and_gi_parent_keep_distinct_spatial_filter_contracts():
    root = Path(__file__).resolve().parents[1]
    recipes = json.loads((root / 'recipes/algorithms.json').read_text(encoding='utf-8'))['algorithms']
    pt = recipes['pt-reblur']
    gi = recipes['gi']
    assert pt['frames'] == 192 and pt['warmup'] == 64
    assert pt['denoise'] is True and pt['accumulation'] is False
    assert (pt['reblur']['diffuse_prepass'], pt['reblur']['max_radius']) == (0, 10)
    assert (gi['reblur']['diffuse_prepass'], gi['reblur']['max_radius']) == (15, 30)
