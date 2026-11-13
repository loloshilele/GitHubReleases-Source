import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
WORKFLOW = ROOT / '.github/workflows/refresh-source.yml'
if not WORKFLOW.exists():
    WORKFLOW = ROOT / 'tools/refresh-public-source.yml'


def workflow_python(step_name):
    text = WORKFLOW.read_text(encoding='utf-8')
    section = text.split('      - name: ' + step_name + '\n', 1)[1].split('\n      - ', 1)[0]
    match = re.search(r"(?m)^          python3 - <<'PY'\n(.*?)^          PY$", section, re.S)
    if not match:
        raise AssertionError('Python step not found: ' + step_name)
    return '\n'.join(line[10:] if line.startswith('          ') else line
                     for line in match.group(1).splitlines())


class PublicSourceWorkflowTests(unittest.TestCase):
    def test_schedule_and_stars_owner(self):
        text = WORKFLOW.read_text(encoding='utf-8')
        self.assertIn("cron: '23 */4 * * *'", text)
        self.assertIn('workflow_dispatch:', text)
        self.assertIn('--stars-owner loloshilele', text)
        self.assertIn('.github/workflows/refresh-source.yml', text)
        self.assertIn("assert data == expected", text)
        self.assertNotIn("Predidit/Kazumi", workflow_python("Verify live source and release dates"))

    def run_guard(self, report, source=None):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '_site').mkdir()
            (root / '_site' / 'apps.json').write_text(
                json.dumps(source if source is not None else {'apps': [{'bundleIdentifier': 'example'}]}),
                encoding='utf-8')
            (root / '_site' / 'catalog-report.json').write_text(json.dumps(report), encoding='utf-8')
            return subprocess.run([sys.executable, '-c', workflow_python('Refuse to publish an incomplete GitHub scan')],
                                  cwd=root, capture_output=True, text=True)

    @staticmethod
    def report(status='complete', extra=None):
        return {'sources': [{'url': 'https://github.com/loloshilele?tab=stars',
                             'status': status, 'repositories': 33}] + (extra or [])}

    def test_valid_scan_and_individual_metadata_failure_can_publish(self):
        result = self.run_guard(self.report(extra=[{'repository': 'owner/repo', 'status': 'metadata_failed',
                                                   'failures': [{'asset': 'app.ipa'}]}]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('STARS_REPOSITORIES=33', result.stdout)
        self.assertIn('retry in a later run', result.stdout)

    def test_failed_or_missing_stars_scan_does_not_publish(self):
        for report in (self.report(status='failed'), {'sources': []}):
            with self.subTest(report=report):
                result = self.run_guard(report)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('keep the previous public source', result.stderr)

    def test_failed_repository_does_not_block_other_updates(self):
        result = self.run_guard(self.report(extra=[{'repository': 'owner/repo', 'status': 'failed'}]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('owner/repo', result.stdout)
        self.assertIn('publish other updates', result.stdout)

    def test_empty_source_does_not_publish(self):
        result = self.run_guard(self.report(), {'apps': []})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Source is empty', result.stderr)


if __name__ == '__main__':
    unittest.main()
