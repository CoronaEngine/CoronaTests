"""Regenerate presentation from a completed run without rendering or rewriting evidence."""
import argparse
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.report import write_pages
from scripts.workflow import inside, load, read_golden, sha


def regenerate(output, *, golden=None):
    output = Path(output).resolve()
    metadata = next((output / name for name in ('workflow-result.json', 'report.json') if (output / name).is_file()), None)
    if metadata is None:
        raise FileNotFoundError('Completed report metadata is missing: ' + str(output))
    report = load(metadata)
    if (output / 'golden.json').is_file():
        raise ValueError('Sealed golden bundles must remain immutable; regenerate the repository gallery instead')
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
    downloads = [(metadata.name, '完整结果 JSON')]
    if (output / 'timings.csv').is_file(): downloads.append(('timings.csv', '耗时 CSV'))
    return write_pages(output, report, downloads=downloads)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='Completed regression directory')
    parser.add_argument('--golden', type=Path, help='Verified reference for displaying limits omitted by older reports')
    args = parser.parse_args()
    print(regenerate(args.output, golden=args.golden))


if __name__ == '__main__':
    main()
