"""Hourly health probe and bounded workflow restart for the public source."""
from __future__ import annotations
import argparse
import json
import http.client
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from source_catalog import parse_date, recent, public_https

SOURCE_BASE = 'https://loloshilele.github.io/GitHubReleases-Source'
def decision(source_ok, report, runs, now=None):
    now = now or datetime.now(timezone.utc)
    def age(value):
        date = parse_date(value)
        return (now - date).total_seconds() if date else float('inf')
    active = [r for r in runs if r.get('status') != 'completed']
    if any(age(r.get('created_at')) < 45 * 60 for r in active):
        return {'action': 'wait', 'reason': 'Refresh is already running or queued'}
    generated = (report or {}).get('generated_at')
    latest = runs[0] if runs else {}
    failed = latest.get('status') == 'completed' and latest.get('conclusion') in ('failure', 'timed_out', 'cancelled', 'startup_failure')
    problems = []
    if not source_ok:
        problems.append('Public source probe failed')
    if age(generated) > 8 * 3600:
        problems.append('Snapshot is older than eight hours or report is unavailable')
    if (report or {}).get('health', {}).get('github_failed', 0):
        problems.append('Some repositories need another retry')
    if failed:
        problems.append('Latest refresh failed')
    if not problems and not active:
        return {'action': 'healthy', 'reason': 'Public source and snapshot are healthy'}
    recent_dispatch = any(r.get('event') == 'workflow_dispatch' and age(r.get('created_at')) < 2 * 3600 for r in runs)
    if recent_dispatch:
        return {'action': 'cooldown', 'reason': 'Recovery dispatch cooldown: two hours'}
    result = {'action': 'dispatch', 'reason': '; '.join(problems) or 'Refresh has been stuck for over 45 minutes'}
    if active:
        result['cancel_run_ids'] = [r['id'] for r in active]
    return result

def read_json(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {'User-Agent': 'IPA-Source-Watchdog'})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError('Health response exceeds limit')
    return json.loads(data)

def probe(loader=None, sleeper=time.sleep):
    loader = loader or read_json
    error = None
    for attempt in range(3):
        try:
            suffix = '?health=' + str(time.time_ns())
            source = loader(SOURCE_BASE + '/apps.json' + suffix)
            report = loader(SOURCE_BASE + '/catalog-report.json' + suffix)
            if not source.get('apps') or any(not a.get('versions') for a in source['apps']) or not parse_date(report.get('generated_at')):
                raise ValueError('Missing apps or report timestamp')
            if not all(recent(v.get('date')) and public_https(v.get('downloadURL'))
                       for a in source['apps'] for v in a.get('versions', [])):
                raise ValueError('Published source has invalid or expired versions')
            return True, report, None
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as exc:
            error = str(exc)
            if attempt < 2:
                sleeper(2 * (attempt + 1))
    return False, None, error

def github_api(path, method='GET', payload=None):
    repo = os.environ['GITHUB_REPOSITORY']
    request = urllib.request.Request('https://api.github.com/repos/' + repo + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
                 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                 'User-Agent': 'IPA-Source-Watchdog'})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()
    return json.loads(data) if data else {}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    ok, report, error = probe()
    runs = github_api('/actions/workflows/refresh-source.yml/runs?branch=main&per_page=20')['workflow_runs']
    result = decision(ok, report, runs)
    if error:
        result['probe_error'] = error
    if result['action'] == 'dispatch' and not args.dry_run:
        for run_id in result.get('cancel_run_ids', []):
            github_api('/actions/runs/' + str(run_id) + '/cancel', 'POST')
        github_api('/actions/workflows/refresh-source.yml/dispatches', 'POST', {'ref': 'main'})
        result['dispatched'] = True
    print(json.dumps(result, ensure_ascii=False))
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with Path(summary).open('a', encoding='utf-8') as file:
            file.write('### Public source watchdog\n\n' + result['action'] + ': ' + result['reason'] + '\n')

if __name__ == '__main__':
    main()

