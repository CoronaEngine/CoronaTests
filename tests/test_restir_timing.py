"""Regression reports account for ReSTIR dispatch time once."""
import pytest

from scripts import regenerate_report, workflow
from scripts.report_pages import algorithm_panel, render_overview


def test_gi_panel_reconciles_dispatches_without_assuming_unknown_ownership(tmp_path):
    timings = {
        'ReSTIR GI shared PT initial candidates': 2.0,
        'ReSTIR DI final shading': 1.0,
        'ReSTIR GI temporal resampling': 3.0,
        'NRD GI0 / NRD-EDSL-REBLUR-Pack': 4.0,
        'TOTAL NRD GI0 dispatch sum': 4.0,
        'ReSTIR GI native guide conditioning': 0.5,
        'ReSTIR DI surface preparation': 1.5,
        'ReSTIR GI stable plane build': 0.5,
        'REBLUR-ComposePhysicalDI-PrivateGI': 0.25,
        'combine': 0.125,
        'later <unclassified> kernel': 0.25,
    }
    row = {
        'scene_id': 'room', 'algorithm_id': 'gi', 'status': 'pass', 'timing': {
            'mean_ms': 15.0, 'warmup_frames': 64,
            'retained': {
                'GPU_timestamp_envelope': {'mean_ms': 14.0},
                'GPU_dispatch_duration_sum': {'mean_ms': 13.125},
                'per_pass_and_group': {k: {'mean_ms': v} for k, v in timings.items()},
            },
        },
    }
    html = algorithm_panel(tmp_path, {'kind': 'regression'}, 'gi', row, True)
    assert html.index('整帧总耗时') < html.index('ReSTIR + REBLUR') < html.index('初始候选')
    assert '渲染 API</span></th><td>15.000 ms' in html
    assert '初始候选</th><td>2.000 ms' in html
    assert 'ReSTIR DI</summary></details></th><td>2.500 ms' in html
    assert 'ReSTIR GI</summary></details></th><td>3.500 ms' in html
    assert 'REBLUR</summary></details></th><td>4.750 ms' in html
    assert '公共开销</summary></details></th><td>0.125 ms' in html
    assert '未分类</summary></details></th><td>0.250 ms' in html
    assert 'GPU dispatch 合计</span></th><td>13.125 ms' in html
    assert 'ReSTIR DI final shading</code></th><td>1.000 ms' in html
    assert 'ReSTIR GI temporal resampling</code></th><td>3.000 ms' in html
    assert 'GI 0 去噪</th><td>4.000 ms' in html
    assert 'ReSTIR GI native guide conditioning</code></th><td>0.500 ms' in html
    di = html.split('<tbody id="restir-stage-di"', 1)[1].split('</tbody>', 1)[0]
    gi = html.split('<tbody id="restir-stage-gi"', 1)[1].split('</tbody>', 1)[0]
    reblur = html.split('<tbody id="restir-stage-reblur"', 1)[1].split('</tbody>', 1)[0]
    shared = html.split('<tbody id="restir-stage-shared"', 1)[1].split('</tbody>', 1)[0]
    assert 'ReSTIR DI surface preparation</code><span class="restir-note">DI / GI 共用</span></th><td>1.500 ms' in di
    assert html.count('ReSTIR DI surface preparation') == 1
    assert 'ReSTIR GI stable plane build</code><span class="restir-note">也为 GI 的 REBLUR 提供引导数据</span></th><td>0.500 ms' in gi
    assert html.count('ReSTIR GI stable plane build') == 1
    assert 'REBLUR-ComposePhysicalDI-PrivateGI</code><span class="restir-note">去噪结果合成</span></th><td>0.250 ms' in reblur
    assert html.count('REBLUR-ComposePhysicalDI-PrivateGI') == 1
    assert '<code>combine</code></th><td>0.125 ms' in shared
    assert 'later &lt;unclassified&gt; kernel</code></th><td>0.250 ms' in html
    assert 'GPU 时间跨度</th><td>14.000 ms' in html
    assert '无法单独测得纯 PT' in html

    # Incomplete profiling should not present an apparently complete split.
    row['timing']['retained']['GPU_dispatch_duration_sum']['mean_ms'] = 14.0
    html = algorithm_panel(tmp_path, {'kind': 'regression'}, 'gi', row, True)
    assert 'ReSTIR + REBLUR' not in html


def test_golden_comparison_shows_signed_deltas_and_missing_evidence(tmp_path):
    def timing(pt, surface_preparation, di='ReSTIR DI', gi='ReSTIR GI'):
        return {'mean_ms': pt + surface_preparation, 'retained': {
            'GPU_dispatch_duration_sum': {'mean_ms': pt + surface_preparation},
            'per_pass_and_group': {
                f'{gi} shared PT initial candidates': {'mean_ms': pt},
                f'{di} surface preparation': {'mean_ms': surface_preparation},
            },
        }}

    row = {'scene_id': 'room', 'algorithm_id': 'gi', 'status': 'pass', 'timing': timing(3, 1)}
    row['restir_golden_comparison'] = {'status': 'compared', 'golden_timing': timing(2, 2, 'Falcor DI', 'RTXPT GI')}
    html = algorithm_panel(tmp_path, {'kind': 'regression'}, 'gi', row, True)
    assert '初始候选</th><td>2.000 ms</td><td>3.000 ms</td><td>+1.000 ms</td><td>+50.0%' in html
    assert 'ReSTIR DI</summary></details></th><td>2.000 ms</td><td>1.000 ms</td><td>-1.000 ms</td><td>-50.0%' in html
    assert 'DI / GI 共用</span></th><td>2.000 ms</td><td>1.000 ms</td><td>-1.000 ms' in html

    row['restir_golden_comparison'] = {'status': 'missing-golden', 'reason': '未找到正式采集的逐 pass 记录'}
    html = algorithm_panel(tmp_path, {'kind': 'regression'}, 'gi', row, True)
    assert '未找到正式采集的逐 pass 记录' in html and '+50.0%' not in html
    assert '初始候选</th><td>—</td><td>3.000 ms' in html


def test_performance_golden_measurement_has_no_self_comparison(tmp_path):
    timing = {
        'mean_ms': 4.0, 'retained': {
            'GPU_dispatch_duration_sum': {'mean_ms': 3.0},
            'GPU_timestamp_envelope': {'mean_ms': 3.5},
            'per_pass_and_group': {
                'NRD physical-DI / NRD-EDSL-REBLUR-Pack': {'mean_ms': 0.2},
                'NRD physical-DI / NRD-EDSL-REBLUR-Blur': {'mean_ms': 0.3},
                'NRD GI1 / NRD-EDSL-REBLUR-Pack': {'mean_ms': 1.0},
                'NRD GI2 / NRD-EDSL-REBLUR-Blur': {'mean_ms': 1.5},
            },
        },
    }
    row = {
        'scene_id': 'room', 'algorithm_id': 'gi', 'status': 'pass', 'timing': timing,
        'restir_golden_comparison': {'status': 'baseline', 'golden_timing': timing, 'baseline_id': 'gi-v1'},
    }
    html = algorithm_panel(tmp_path, {'kind': 'regression'}, 'gi', row, True)
    assert '性能 Golden</th>' in html and '<code>gi-v1</code>' in html
    assert '本次</th>' not in html and '差值</th>' not in html
    assert 'GPU dispatch 合计</span></th><td>3.000 ms</td></tr>' in html
    assert 'DI 去噪</th><td>0.500 ms</td></tr>' in html
    assert 'GI 1 去噪</th><td>1.000 ms</td></tr>' in html
    assert 'GI 2 去噪</th><td>1.500 ms</td></tr>' in html


def test_overview_keeps_report_title_with_timing_availability(tmp_path):
    report = {'kind': 'regression', 'id': 'sample', 'created_utc': '', 'suite': 'full', 'records': [
        {'scene_id': 'room', 'algorithm_id': 'gi', 'status': 'pass',
         'restir_golden_comparison': {'status': 'compared'}},
    ]}
    html = render_overview(tmp_path, report)
    assert '<title>回归报告 · sample</title>' in html
    assert '<h3>有逐阶段对比</h3>' in html


def test_regenerate_binds_stage_comparisons_to_verified_candidate_result(tmp_path, monkeypatch):
    (tmp_path / 'workflow-result.json').write_text('{}', encoding='utf-8')
    relative = 'analysis/analysis-result.json'
    source = tmp_path / relative
    job = {'job_id': 'room--gi', 'algorithm_id': 'gi'}
    workflow.save(source, {'kind': 'regression', 'records': [job]})
    merged_bytes = source.read_bytes()
    comparison = {'status': 'missing-golden', 'reason': 'No original GPU pass recording'}
    bundle = {
        'schema': 1, 'golden_id': 'formal', 'golden_manifest_fingerprint': 'a' * 64,
        'candidate_result': relative, 'candidate_result_sha256': workflow.sha(source),
        'jobs': {job['job_id']: comparison},
    }
    workflow.save(tmp_path / 'restir-golden-comparison.json',
                  {**bundle, 'fingerprint': workflow.fingerprint(bundle)})

    def write_pages(output, report, *, downloads):
        assert report['records'][0]['restir_golden_comparison'] == comparison
        assert any(path == relative for path, _ in downloads)
        return output / 'index.html'

    monkeypatch.setattr(regenerate_report, 'write_pages', write_pages)
    regenerate_report.regenerate(tmp_path)
    assert source.read_bytes() == merged_bytes
    source.write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='candidate result'):
        regenerate_report.regenerate(tmp_path)
