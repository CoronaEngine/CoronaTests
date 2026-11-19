"""Regenerate presentation from a completed run without rendering or rewriting evidence."""
import argparse
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.report import write_pages
from scripts.workflow import ROOT, fingerprint, inside, load, read_golden, sha


def regenerate(output, *, golden=None, performance_golden=None):
    output = Path(output).resolve()
    if (output / 'golden.json').is_file():
        raise ValueError('Sealed golden bundles must remain immutable; regenerate the repository gallery instead')
    metadata = next((output / name for name in ('workflow-result.json', 'report.json') if (output / name).is_file()), None)
    timing_path = output / 'restir-golden-comparison.json'
    timing_comparison = None
    if timing_path.is_file():
        timing_comparison = load(timing_path)
        digest = timing_comparison.pop('fingerprint', None)
        if timing_comparison.get('schema') != 1 or digest != fingerprint(timing_comparison):
            raise ValueError('ReSTIR golden timing evidence fingerprint differs')
        metadata = inside(output, timing_comparison['candidate_result'])
        if not metadata.is_file() or sha(metadata) != timing_comparison['candidate_result_sha256']:
            raise ValueError('ReSTIR golden timing evidence does not match the candidate result')
    if metadata is None:
        raise FileNotFoundError('Completed report metadata is missing: ' + str(output))
    report = load(metadata)
    if timing_comparison:
        gi_rows = {row['job_id']: row for row in report['records'] if row['algorithm_id'] == 'gi'}
        for job, comparison in timing_comparison['jobs'].items():
            if job not in gi_rows:
                raise ValueError('ReSTIR golden timing evidence contains a different GI job: ' + job)
            gi_rows[job]['restir_golden_comparison'] = comparison
    if golden is not None:
        manifest, thresholds = read_golden(Path(golden).resolve())
        contract_path = output / 'run-config.json'
        if (not contract_path.is_file()
                or load(contract_path).get('contract', {}).get('golden_fingerprint') != manifest['manifest_fingerprint']):
            raise ValueError('Golden version fingerprint differs from the saved regression contract')
        refs = {row['job_id']: row for row in manifest['records']}
        for row in report['records']:
            if not row.get('comparison'): continue
            reference = refs.get(row.get('job_id'), {})
            profile = thresholds.get('profiles', {}).get(row.get('job_id'), {})
            provenance = profile.get('provenance', {})
            if (reference.get('input_fingerprint') != row.get('comparison_input_fingerprint')
                    or provenance.get('input_fingerprint') != reference.get('input_fingerprint')
                    or provenance.get('golden_id') != manifest['id']):
                raise ValueError('Golden threshold provenance does not match saved comparison')
            if (not row.get('baseline_image')
                    or sha(inside(output, row['baseline_image'])) != reference.get('image_sha256')):
                raise ValueError('Golden reference image does not match saved comparison')
            row['comparison']['raw_thresholds'] = profile.get('thresholds', {})
    if performance_golden is not None:
        from scripts.performance_golden import annotate_report
        is_baseline = sha(metadata) == performance_golden['source']['result_sha256']
        annotate_report(report, performance_golden, is_baseline=is_baseline)
    downloads = [(metadata.relative_to(output).as_posix(), '完整结果 JSON')]
    timings = metadata.with_name('analysis-timings.csv')
    if not timings.is_file():
        timings = output / 'timings.csv'
    if timings.is_file():
        downloads.append((timings.relative_to(output).as_posix(), '耗时 CSV'))
    if timing_comparison and performance_golden is None:
        downloads.append((timing_path.name, 'Golden 阶段对比数据和来源'))
    return write_pages(output, report, downloads=downloads)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='Completed regression directory')
    parser.add_argument('--golden', type=Path, help='Verified reference for displaying limits omitted by older reports')
    parser.add_argument('--performance-golden', action='store_true',
                        help='Display the current performance Golden alongside the saved result')
    args = parser.parse_args()
    bundle = None
    if args.performance_golden:
        from scripts.performance_golden import load_default
        bundle = load_default(ROOT)
        if bundle is None:
            parser.error('No performance Golden is configured')
    print(regenerate(args.output, golden=args.golden, performance_golden=bundle))


if __name__ == '__main__':
    main()
