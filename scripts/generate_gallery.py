"""Generate offline HTML and GitHub Markdown using the formal PNGs in place."""
from collections import Counter
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.report import write_pages
from scripts.github_gallery import write_github_gallery
from scripts.workflow import ROOT, golden_files, read_golden


def write_gallery(root=ROOT):
    root = Path(root).resolve()
    manifest, _ = read_golden(root / 'golden')
    manifest_path, thresholds_path, _ = golden_files(root / 'golden')
    records = [{**row, 'image': 'golden/' + row['image']} if row.get('image') else dict(row)
               for row in manifest['records']]
    report = {**{key: manifest[key] for key in ('id', 'suite', 'created_utc', 'metadata', 'valid', 'calibrated')},
              'kind': 'golden', 'records': records,
              'summary': dict(Counter(row['status'] for row in records))}
    for key in ('threshold_policy', 'thresholds_ready'):
        if key in manifest:
            report[key] = manifest[key]
    index = write_pages(root, report, downloads=[
        (manifest_path.relative_to(root).as_posix(), 'Golden images, settings and timings'),
        (thresholds_path.relative_to(root).as_posix(), 'Comparison thresholds')])
    write_github_gallery(root, manifest)
    return index


if __name__ == '__main__':
    print(write_gallery())
