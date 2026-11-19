"""Partition the recorded GPU dispatch means for the ReSTIR GI route."""
import math


PASS_GROUPS = {
    'pt': {'RTXPT GI shared PT initial candidates'},
    'di': {
        'Falcor DI light geometry preparation',
        'Falcor DI surface preparation',
        'Falcor DI light presampling',
        'Falcor DI initial candidates',
        'Falcor DI visibility and spatiotemporal reuse',
        'Falcor DI final shading',
    },
    'gi': {
        'RTXPT GI stable plane build',
        'RTXPT GI temporal resampling',
        'RTXPT GI spatial resampling',
        'RTXPT GI final shading',
    },
    'reblur': {
        # Newer builds include guide conditioning inside REBLUR Pack.
        'RTXPT GI native guide conditioning',
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
