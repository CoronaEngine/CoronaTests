"""Partition the recorded GPU dispatch means for the ReSTIR GI route."""
import math


PASS_GROUPS = {
    'pt': {'ReSTIR GI shared PT initial candidates'},
    'di': {
        'ReSTIR DI light geometry preparation',
        'ReSTIR DI surface preparation',
        'ReSTIR DI light presampling',
        'ReSTIR DI initial candidates',
        'ReSTIR DI visibility and spatiotemporal reuse',
        'ReSTIR DI final shading',
    },
    'gi': {
        'ReSTIR GI stable plane build',
        'ReSTIR GI temporal resampling',
        'ReSTIR GI spatial resampling',
        'ReSTIR GI final shading',
    },
    'reblur': {
        # Newer builds include guide conditioning inside REBLUR Pack.
        'ReSTIR GI native guide conditioning',
        'REBLUR-ComposePhysicalDI-PrivateGI',
    },
    'shared': {
        'rt_geom',
        'FrameBuffer-SaveSurfaceExtHistory',
        'combine',
        'RGBFilm-tone_mapping-tex',
        'FrameBuffer-gamma_correction-tex',
        'Corona-Vision-Resolve',
    },
}
OWNERS = {name: group for group, names in PASS_GROUPS.items() for name in names}
HISTORICAL_PASSES = {
    **{name.replace('ReSTIR DI', 'Falcor DI'): name for name in PASS_GROUPS['di']},
    **{name.replace('ReSTIR GI', 'RTXPT GI'): name for name in OWNERS if name.startswith('ReSTIR GI ')},
}


def breakdown(timing):
    """Return per-group measured passes if they reconcile with total GPU work."""
    retained = timing.get('retained') or {}
    passes = retained.get('per_pass_and_group')
    total = retained.get('GPU_dispatch_duration_sum', {}).get('mean_ms')
    if not passes or type(total) not in (int, float) or not math.isfinite(total) or total <= 0:
        return None
    groups = {key: {} for key in (*PASS_GROUPS, 'unknown')}
    for name, stats in passes.items():
        # capture.grouped_passes already includes individual NRD dispatches
        # under their context labels. These synthetic subtotals overlap them.
        if name.startswith('TOTAL NRD ') and name.endswith(' dispatch sum'):
            continue
        duration = stats.get('mean_ms')
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
            return None
        name = HISTORICAL_PASSES.get(name, name)
        owner = OWNERS.get(name, 'unknown')
        if name.startswith('NRD ') and ' / ' in name:
            # This prefix is added only to dispatches validated inside an NRD
            # REBLUR context block, including the custom HistoryFix dispatch.
            owner = 'reblur'
        groups[owner][name] = duration
    measured = math.fsum(duration for group in groups.values() for duration in group.values())
    if not math.isclose(measured, total, rel_tol=1e-9, abs_tol=1e-9):
        return None
    return groups
