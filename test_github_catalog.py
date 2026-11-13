import io
import plistlib
import unittest
import zipfile
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
import github_catalog as catalog
import ipa_metadata
from github_client import Client

NOW = datetime(2026, 11, 13, tzinfo=timezone.utc)
def asset(name='App.ipa', url=None):
    return {'name': name, 'size': 123, 'browser_download_url': url or 'https://github.com/a/b/releases/download/v2/' + name}
def release(assets=None, **kwargs):
    return dict({'published_at': '2026-11-12T00:00:00Z', 'assets': assets or []}, **kwargs)
def metadata(url, size):
    return {'CFBundleIdentifier': 'app.real', 'CFBundleShortVersionString': '2.0.0',
            'CFBundleDisplayName': 'Real App', 'MinimumOSVersion': '15.0'}

class StarsDiscoveryTests(unittest.TestCase):
    def test_stars_paginates_and_deduplicates(self):
        calls = []
        def get(path):
            calls.append(path)
            return ([{'full_name': f'a/repo{i}'} for i in range(100)] if '&page=1' in path
                    else [{'full_name': 'a/repo100'}, {'full_name': 'a/repo0'}, {'full_name': 'private/repo', 'private': True}])
        rows = catalog.starred_repositories('someone', get)
        self.assertEqual(len(rows), 101)
        self.assertIn('page=2', calls[1])
        self.assertNotIn('private/repo', rows)

    def test_truncation_is_explicit(self):
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            catalog.pages('/test', lambda _: [{}] * 100, max_pages=1)

    def test_releases_and_asset_pagination(self):
        def get(path):
            if '/assets' in path:
                return [asset('last.IPA')]
            if '&page=1' in path:
                return [release(id=i, assets=[asset('not-ipa.txt')]*100) for i in range(100)]
            return [release(id=101, assets=[asset()])]
        with patch.object(catalog, 'api_json', side_effect=get):
            rows = catalog.releases('a/b')
        self.assertEqual(len(rows), 101)
        self.assertEqual(rows[0]['assets'][0]['name'], 'last.IPA')

    def collect(self, rows, inspector=metadata, seed=None, **kwargs):
        return catalog.collect_repositories(seed or {}, NOW, 365, loader=lambda _: rows,
                                             inspector=inspector, repositories=['a/b'], **kwargs)

    def test_all_starred_repos_instead_of_whitelist(self):
        apps, reports = catalog.collect_repositories({}, NOW, 365,
            loader=lambda _: [release([asset()])], inspector=metadata,
            repositories=[], owner='someone', stars_loader=lambda _: ['a/b','A/B','c/d'])
        self.assertEqual(len(apps), 2)
        self.assertEqual(reports[0]['repositories'], 3)
        self.assertEqual({a['githubRepository'] for a in apps}, {'a/b','c/d'})

    def test_latest_without_ipa_older_release_still_detected(self):
        apps, _ = self.collect([release([asset('x.zip')]), release([asset('older.IPA')], published_at='2026-11-11')])
        self.assertTrue(apps[0]['versions'][0]['downloadURL'].endswith('older.IPA'))

    def test_standard_preferred_to_lite_for_same_bundle(self):
        apps, reports = self.collect([release([asset('App-lite-unsigned.ipa'),asset('App-unsigned.ipa')])])
        self.assertEqual(len(apps), 1)
        self.assertTrue(apps[0]['versions'][0]['downloadURL'].endswith('App-unsigned.ipa'))
        self.assertEqual(len(reports[0]['duplicates']), 1)
        self.assertEqual(reports[0]['ipa_assets'], 2)

    def test_individual_asset_failure_does_not_hide_other_ipa(self):
        def inspect(url, size):
            if 'broken' in url:
                raise ValueError('broken plist')
            return metadata(url,size)
        apps, reports = self.collect([release([asset('broken.ipa'), asset('working.ipa')])], inspect)
        self.assertEqual(len(apps), 1)
        self.assertEqual(len(reports[0]['failures']), 1)

    def test_multiple_apps_in_one_release(self):
        def inspect(url, size):
            return dict(metadata(url,size), CFBundleIdentifier=url)
        apps, _ = self.collect([release([asset('One.ipa'), asset('Two.ipa')])], inspect)
        self.assertEqual(len(apps), 2)

    def test_old_and_prerelease_excluded(self):
        apps, reports = self.collect([release([asset()], published_at='2024-01-01'), release([asset()], prerelease=True)],
                                    lambda *_: self.fail('must not inspect'))
        self.assertEqual(apps, [])
        self.assertEqual(reports[0]['status'], 'no_recent_release')

    def test_failure_report_retains_diagnostic(self):
        def fail(_):
            raise OSError('offline')
        apps, reports = catalog.collect_repositories({}, NOW,365, repositories=['a/b'],loader=fail)
        self.assertEqual(apps, [])
        self.assertEqual(reports[0]['status'], 'failed')

    def test_asset_update_invalidates_metadata_cache(self):
        row = release([dict(asset(), digest='sha256:1')])
        apps, _ = self.collect([row])
        cached, _ = self.collect([row],lambda *_: self.fail('identical asset must use cache'), {'apps':apps})
        self.assertEqual(len(cached),1)
        row['assets'][0]['digest'] = 'sha256:2'
        calls = []
        self.collect([row], lambda *args: calls.append(args) or metadata(*args), {'apps':apps})
        self.assertEqual(len(calls),1)

    def test_client_searches_beyond_first_page(self):
        client = Client('token', verbose=False)
        def get(url):
            if '&page=1' in url:
                return [release([asset('not.zip')])] * 100
            return [release([asset('valid.IPA')], tag_name='v2')]
        with patch.object(client, '_rest_json', side_effect=get):
            result = client.fetch_ipa_releases([{'nameWithOwner':'a/b','isPrivate':False,'isArchived':True}])
        self.assertEqual(result[0]['latestRelease']['tagName'],'v2')
        self.assertEqual(client.last_ipa_scan[0]['releases_scanned'],101)

    def test_real_root_application_plist_over_nested_watch_app(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('Payload/App.app/Watch/Watch.app/Info.plist',plistlib.dumps({'CFBundleIdentifier':'wrong'}))
            archive.writestr('Payload/App.app/Info.plist',plistlib.dumps({'CFBundleIdentifier':'correct'}))
        data.seek(0)
        with zipfile.ZipFile(data) as archive:
            self.assertEqual(ipa_metadata.read_metadata(archive)['CFBundleIdentifier'],'correct')

    def test_missing_application_plist_rejected(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data,'w') as archive:
            archive.writestr('file.txt','bad')
        data.seek(0)
        with zipfile.ZipFile(data) as archive, self.assertRaises(ValueError):
            ipa_metadata.read_metadata(archive)

if __name__ == '__main__':
    unittest.main()

