import copy
import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import github_catalog
import source_catalog
import source_status
import source_watchdog as watchdog

NOW = datetime(2026, 11, 13, 7, tzinfo=timezone.utc)
STAMP = NOW.isoformat()
def app():
    return {'name': 'Example', 'bundleIdentifier': 'app.example', 'githubRepository': 'a/b',
            'versions': [{'version': '1', 'date': STAMP,
                          'downloadURL': 'https://github.com/a/b/releases/download/v1/App.ipa', 'size': 12}]}
def report():
    return {'generated_at': STAMP, 'max_age_days': 365, 'app_count': 1,
            'sources': [{'url': 'https://github.com/user?tab=stars', 'status': 'complete'},
                        {'repository': 'a/b', 'url': 'https://github.com/a/b/releases',
                         'status': 'failed', 'error': 'network unavailable', 'attempts': 2}]}

class SourceRecoveryTests(unittest.TestCase):
    def test_failed_repository_does_not_block_other_repositories_and_retries(self):
        calls = []
        def loader(repo):
            calls.append(repo)
            if repo == 'a/b':
                raise OSError('offline')
            return [{'published_at': STAMP, 'assets': [{'name':'App.ipa','size':12,
                'browser_download_url':'https://github.com/c/d/releases/download/v1/App.ipa'}]}]
        with patch.object(github_catalog.time, 'sleep'):
            found, reports = github_catalog.collect_repositories({},NOW,365, repositories=['a/b','c/d'],
                loader=loader, inspector=lambda *_: {'CFBundleIdentifier':'app.new','CFBundleShortVersionString':'2'})
        self.assertEqual(len(found), 1)
        self.assertEqual(calls.count('a/b'), 2)
        self.assertEqual(calls.count('c/d'), 1)
        self.assertEqual(next(r for r in reports if r['repository']=='a/b')['attempts'], 2)

    def test_transient_repository_failure_recovers(self):
        rows = [{'published_at':STAMP,'assets':[{'name':'App.ipa','size':12,
            'browser_download_url':'https://github.com/a/b/releases/download/v1/App.ipa'}]}]
        with patch.object(github_catalog.time,'sleep'), patch.object(github_catalog,'releases',side_effect=[OSError('offline'),rows]):
            found, reports = github_catalog.collect_repositories({},NOW,365,repositories=['a/b'],
                inspector=lambda *_:{'CFBundleIdentifier':'app.real','CFBundleShortVersionString':'1'})
        self.assertEqual(len(found),1)
        self.assertTrue(reports[0]['recovered'])

    def test_valid_cached_application_retained_and_expired_one_dropped(self):
        old = app()
        expired = copy.deepcopy(old)
        expired['bundleIdentifier'] = 'expired'
        expired['versions'][0]['date'] = '2024-01-01'
        source, _ = source_catalog.collect({'apps':[old, expired]},urls=[],now=NOW,log=lambda _:None)
        self.assertEqual([a['bundleIdentifier'] for a in source['apps']],['app.example'])
        details = source_status.enrich_report(source,report(),{'health':{'last_complete_scan_at':'2026-11-12'}})
        self.assertEqual(details['health']['mode'],'partial')
        self.assertEqual(details['sources'][1]['retained_apps'],1)
        self.assertEqual(details['health']['last_complete_scan_at'],'2026-11-12')

    def test_status_page_escapes_upstream_content(self):
        details = report()
        details['sources'][1]['error'] = '<script>alert(1)</script>'
        source_status.enrich_report({'apps':[app()]},details)
        page = source_status.render_status({'apps':[app()]},details)
        self.assertNotIn('<script>alert(1)</script>',page)
        self.assertIn('&lt;script&gt;',page)
        self.assertIn('等待重试',page)
        self.assertIn('a/b',page)

    def test_write_site_includes_status_and_home_link(self):
        with tempfile.TemporaryDirectory() as folder:
            details = report()
            source_status.enrich_report({'apps':[app()]},details)
            source_catalog.write_site(folder, {'apps':[app()]},details)
            self.assertTrue((Path(folder)/'status.html').exists())
            self.assertIn('status.html',(Path(folder)/'index.html').read_text(encoding='utf-8'))

    def test_healthy_site_needs_no_restart(self):
        self.assertEqual(watchdog.decision(True,report(),[],NOW)['action'],'healthy')

    def test_unreachable_or_stale_source_triggers_refresh(self):
        for ok, details in [(False,None),(True,{'generated_at':(NOW-timedelta(hours=9)).isoformat()})]:
            self.assertEqual(watchdog.decision(ok,details,[],NOW)['action'],'dispatch')

    def test_failed_task_triggers_recovery_even_with_fresh_source(self):
        runs=[{'status':'completed','conclusion':'failure','created_at':STAMP,'event':'schedule'}]
        self.assertEqual(watchdog.decision(True,report(),runs,NOW)['action'],'dispatch')

    def test_pending_repository_retries_trigger_recovery(self):
        details=report()
        details['health']={'github_failed':1}
        self.assertEqual(watchdog.decision(True,details,[],NOW)['action'],'dispatch')

    def test_running_task_is_not_duplicated(self):
        runs=[{'id':1,'status':'in_progress','created_at':STAMP}]
        self.assertEqual(watchdog.decision(False,None,runs,NOW)['action'],'wait')

    def test_hung_run_is_cancelled_before_dispatch(self):
        runs=[{'id':1,'status':'queued','created_at':(NOW-timedelta(hours=2)).isoformat()}]
        result=watchdog.decision(False,None,runs,NOW)
        self.assertEqual(result['action'],'dispatch')
        self.assertEqual(result['cancel_run_ids'],[1])

    def test_recovery_has_cooldown(self):
        runs=[{'status':'completed','event':'workflow_dispatch','conclusion':'failure','created_at':STAMP}]
        self.assertEqual(watchdog.decision(False,None,runs,NOW)['action'],'cooldown')

    def test_probe_retries_before_recovery(self):
        calls=[]
        def fail(url):
            calls.append(url)
            raise OSError('unreachable')
        ok, _, error=watchdog.probe(loader=fail,sleeper=lambda _:None)
        self.assertFalse(ok)
        self.assertEqual(len(calls),3)
        self.assertIn('unreachable',error)

    def test_recovery_dispatches_default_branch_without_replaying_old_sha(self):
        with patch.object(watchdog,'probe',return_value=(False,None,'unreachable')), \
             patch.object(watchdog,'github_api',side_effect=[{'workflow_runs':[]},{}]) as api, \
             patch('sys.argv',['watchdog']), patch('sys.stdout',new_callable=io.StringIO), \
             patch.dict('os.environ',{'GITHUB_STEP_SUMMARY':''}):
            watchdog.main()
        self.assertEqual(api.call_args.args,('/actions/workflows/refresh-source.yml/dispatches','POST',{'ref':'main'}))

if __name__ == '__main__':
    unittest.main()

