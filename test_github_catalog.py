import unittest
from datetime import datetime, timezone
from github_catalog import collect_repositories

class GithubCatalogTests(unittest.TestCase):
    def test_actual_ipa_version_and_fresh_release(self):
        calls = []
        def inspect(url, size):
            calls.append(url)
            return {'CFBundleIdentifier': 'com.example.kazumi', 'CFBundleShortVersionString': '2.4.4', 'MinimumOSVersion': '15.0'}
        releases = [
            {'published_at': '2024-01-01T00:00:00Z', 'assets': []},
            {'published_at': '2026-11-12T10:27:44Z', 'tag_name': 'misleading-tag',
             'assets': [{'name': 'Kazumi.ipa', 'size': 123, 'browser_download_url': 'https://github.com/Predidit/Kazumi/releases/download/2.4.4/Kazumi.ipa'}]}]
        apps, report = collect_repositories({}, datetime(2026,11,13,tzinfo=timezone.utc),365,
                                           loader=lambda _: releases, inspector=inspect)
        self.assertEqual(apps[0]['versions'][0]['version'], '2.4.4')
        self.assertEqual(apps[0]['bundleIdentifier'], 'com.example.kazumi')
        self.assertEqual(len(calls), 1)
        self.assertEqual(report[0]['accepted'], 1)

    def test_old_and_prerelease_are_not_downloaded(self):
        rows = [{'published_at': '2024-01-01T00:00:00Z'}, {'published_at': '2026-11-12T00:00:00Z','prerelease': True}]
        apps, report = collect_repositories({}, datetime(2026,11,13,tzinfo=timezone.utc),365,
                                           loader=lambda _: rows,
                                           inspector=lambda *_: self.fail('must not inspect excluded release'))
        self.assertEqual(apps, [])

    def test_upstream_failure_is_reported(self):
        def fail(_):
            raise OSError('unavailable')
        apps, report = collect_repositories({}, datetime(2026,11,13,tzinfo=timezone.utc),365, loader=fail)
        self.assertEqual(apps, [])
        self.assertIn('error', report[0])

if __name__ == '__main__':
    unittest.main()
