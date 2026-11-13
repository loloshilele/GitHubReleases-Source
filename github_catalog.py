"""Paginated public Stars -> Releases -> IPA metadata, with per-repository diagnostics."""
from __future__ import annotations
import copy
import json
import http.client
import os
import re
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

# Optional icon overrides, never an inclusion whitelist.
REPOSITORIES = {'Predidit/Kazumi': {
    'icon': 'https://raw.githubusercontent.com/Predidit/Kazumi/main/assets/images/logo/logo_ios.png'}}
MAX_PAGES = 100

def api_json(path):
    headers = {'User-Agent': 'GitHubReleases-Source', 'Accept': 'application/vnd.github+json'}
    token = os.environ.get('GITHUB_TOKEN', '')
    if token:
        headers['Authorization'] = 'Bearer ' + token
    for attempt in range(3):
        try:
            request = urllib.request.Request('https://api.github.com' + path, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise ValueError('GitHub response exceeds limit')
            return json.loads(data)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return []
            if exc.code not in (403, 429, 500, 502, 503, 504) or attempt == 2:
                raise
            time.sleep(min(int(exc.headers.get('Retry-After', '2')), 10))
        except (OSError, TimeoutError, http.client.HTTPException):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)

def pages(path, getter=None, max_pages=MAX_PAGES):
    getter = getter or api_json
    result = []
    for page in range(1, max_pages + 1):
        batch = getter(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
        if not isinstance(batch, list):
            raise ValueError('Expected GitHub list: ' + path)
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise RuntimeError('Pagination limit reached; scan incomplete: ' + path)

def starred_repositories(owner, getter=None):
    if not re.fullmatch(r'[A-Za-z0-9-]+', owner or ''):
        raise ValueError('Invalid Stars owner')
    rows = pages('/users/' + owner + '/starred', getter)
    # Public mobile links cannot authenticate to private repositories.
    return list(dict.fromkeys(row['full_name'] for row in rows
                              if row.get('full_name') and not row.get('private', False)))

def releases(repo):
    rows = pages('/repos/' + repo + '/releases')
    for release in rows:
        if len(release.get('assets') or []) >= 30:
            release['assets'] = pages('/repos/' + repo + '/releases/' + str(release['id']) + '/assets')
    return rows

from ipa_metadata import inspect_ipa

def asset_rank(asset):
    name = asset.get('name', '').lower()
    # A mobile source can expose one download for a Bundle ID. Prefer the complete unsigned build.
    return (bool(re.search(r'lite|light|mini', name)),
            not bool(re.search(r'unsigned|no.?sign', name)), name)

def collect_repositories(seed, now, days, loader=None, inspector=None,
                         repositories=None, owner=None, stars_loader=None,
                         include_prerelease=False):
    from source_catalog import recent, public_https, parse_date
    loader, inspector = loader or releases, inspector or inspect_ipa
    names = list(REPOSITORIES) if repositories is None else list(repositories)
    reports = []
    if owner:
        try:
            stars = (stars_loader or starred_repositories)(owner)
            names.extend(stars)
            reports.append({'url': 'https://github.com/' + owner + '?tab=stars',
                            'status': 'complete', 'repositories': len(stars)})
        except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
            reports.append({'url': 'https://github.com/' + owner + '?tab=stars',
                            'status': 'failed', 'error': str(exc)})
    unique = {}
    for name in names:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', name):
            continue
        unique.setdefault(name.lower(), name)
    def scan_once(repo):
        report = {'url': 'https://github.com/' + repo + '/releases', 'repository': repo,
                  'accepted': 0, 'ipa_assets': 0, 'failures': [], 'duplicates': []}
        apps = {}
        try:
            rows = loader(repo)
            report['releases_scanned'] = len(rows)
            candidates = [r for r in rows if not r.get('draft')
                          and (include_prerelease or not r.get('prerelease'))
                          and recent(r.get('published_at'), now, days)]
            candidates.sort(key=lambda r: parse_date(r['published_at']), reverse=True)
            report['eligible_releases'] = len(candidates)
            for release in candidates:
                assets = [a for a in release.get('assets', [])
                          if str(a.get('name', '')).lower().endswith('.ipa')]
                report['ipa_assets'] += len(assets)
                for asset in sorted(assets, key=asset_rank):
                    url, size = asset.get('browser_download_url'), asset.get('size')
                    if not public_https(url) or not isinstance(size, int) or not 0 < size <= 2 * 1024**3:
                        report['failures'].append({'asset': asset.get('name'), 'error': 'Invalid IPA URL or size'})
                        continue
                    try:
                        fingerprint = [url, size, asset.get('digest'), asset.get('updated_at')]
                        cached = next((a for a in seed.get('apps', [])
                            if str(a.get('githubRepository', '')).lower() == repo.lower()
                            and a.get('githubAsset') == fingerprint), None)
                        if cached:
                            app = copy.deepcopy(cached)
                            app['versions'] = [dict(v, date=release['published_at']) for v in app['versions']
                                               if v.get('downloadURL') == url]
                        else:
                            metadata = inspector(url, size)
                            version = {'version': metadata['CFBundleShortVersionString'],
                                       'date': release['published_at'], 'downloadURL': url, 'size': size,
                                       'localizedDescription': (release.get('body') or '')[:4000],
                                       'isPrerelease': bool(release.get('prerelease'))}
                            if metadata.get('CFBundleVersion'):
                                version['buildVersion'] = str(metadata['CFBundleVersion'])
                            if metadata.get('MinimumOSVersion'):
                                version['minOSVersion'] = metadata['MinimumOSVersion']
                            app = {'name': metadata.get('CFBundleDisplayName') or metadata.get('CFBundleName') or repo.split('/')[-1],
                                   'bundleIdentifier': metadata['CFBundleIdentifier'],
                                   'developerName': repo.split('/')[0],
                                   'localizedDescription': (release.get('body') or '')[:4000],
                                   'iconURL': REPOSITORIES.get(repo, {}).get('icon') or 'https://github.com/' + repo.split('/')[0] + '.png',
                                   'versions': [version], 'githubRepository': repo, 'githubAsset': fingerprint}
                        bundle = app['bundleIdentifier']
                        if bundle in apps:
                            report['duplicates'].append({'asset': asset.get('name'), 'bundleIdentifier': bundle,
                                                         'reason': 'Same Bundle ID; prefer complete unsigned IPA'})
                            continue
                        apps[bundle] = app
                    except (OSError, ValueError, RuntimeError, KeyError, zipfile.BadZipFile, http.client.HTTPException) as exc:
                        report['failures'].append({'asset': asset.get('name'), 'error': str(exc)})
                if apps:
                    break
            report['accepted'] = len(apps)
            report['status'] = ('included' if apps else 'metadata_failed' if report['failures']
                                else 'no_ipa' if candidates else 'no_recent_release')
        except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
            report.update(status='failed', error=str(exc))
        return list(apps.values()), report
    def scan(repo):
        found, report = scan_once(repo)
        report['attempts'] = 1
        if report.get('status') in ('failed', 'metadata_failed') or report.get('failures'):
            time.sleep(1)
            retry_found, retry_report = scan_once(repo)
            retry_report['attempts'] = 2
            retry_report['recovered'] = retry_report.get('status') == 'included' and not retry_report.get('failures')
            # Do not discard applications already read successfully if a retry gets worse.
            by_bundle = {app['bundleIdentifier']: app for app in found}
            by_bundle.update({app['bundleIdentifier']: app for app in retry_found})
            found = list(by_bundle.values())
            report = retry_report
            report['accepted'] = len(found)
            if found and report.get('status') == 'failed':
                report['status'] = 'partial'
        return found, report
    apps = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for found, report in pool.map(scan, unique.values()):
            apps.extend(found)
            reports.append(report)
    return apps, reports

