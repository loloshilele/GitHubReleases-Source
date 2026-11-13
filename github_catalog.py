"""Collect IPA releases that do not provide an AltSource manifest."""
import json
import plistlib
import tempfile
import urllib.request
import zipfile

REPOSITORIES = {
    'Predidit/Kazumi': {
        'icon': 'https://raw.githubusercontent.com/Predidit/Kazumi/main/assets/images/logo/logo_ios.png',
    },
}

def inspect_ipa(url, size):
    from source_catalog import PublicRedirect, public_https
    if not public_https(url) or not isinstance(size, int) or not 0 < size <= 512 * 1024 * 1024:
        raise ValueError('Invalid IPA download or size')
    opener = urllib.request.build_opener(PublicRedirect())
    with tempfile.TemporaryFile() as target:
        with opener.open(urllib.request.Request(url, headers={'User-Agent': 'GitHubReleases-Source'}), timeout=90) as response:
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > size:
                    raise ValueError('IPA exceeds declared size')
                target.write(chunk)
        if total != size:
            raise ValueError('Incomplete IPA download')
        target.seek(0)
        with zipfile.ZipFile(target) as archive:
            names = [n for n in archive.namelist() if len(n.split('/')) == 3
                     and n.startswith('Payload/') and n.endswith('.app/Info.plist')]
            if len(names) != 1:
                raise ValueError('IPA must contain one root application plist')
            info = archive.getinfo(names[0])
            if info.file_size > 2 * 1024 * 1024:
                raise ValueError('IPA plist too large')
            metadata = plistlib.loads(archive.read(info))
    for field in ('CFBundleIdentifier', 'CFBundleShortVersionString'):
        if not isinstance(metadata.get(field), str) or not metadata[field].strip():
            raise ValueError('Missing IPA metadata: ' + field)
    return metadata

def releases(repo):
    request = urllib.request.Request('https://api.github.com/repos/' + repo + '/releases?per_page=20',
                                    headers={'User-Agent': 'GitHubReleases-Source'})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError('Release response too large')
    result = json.loads(data)
    if not isinstance(result, list):
        raise ValueError('Invalid release response')
    return result

def collect_repositories(seed, now, days, loader=None, inspector=None):
    from source_catalog import recent, public_https, parse_date
    loader = loader or releases
    inspector = inspector or inspect_ipa
    apps, reports = [], []
    for repo, config in REPOSITORIES.items():
        report = {'url': 'https://github.com/' + repo + '/releases', 'accepted': 0}
        try:
            candidates = [r for r in loader(repo) if not r.get('draft') and not r.get('prerelease')
                          and recent(r.get('published_at'), now, days)]
            candidates.sort(key=lambda r: parse_date(r['published_at']), reverse=True)
            for release in candidates:
                assets = [a for a in release.get('assets', []) if a.get('name', '').lower().endswith('.ipa')
                          and public_https(a.get('browser_download_url')) and isinstance(a.get('size'), int)
                          and 0 < a['size'] <= 512 * 1024 * 1024]
                if not assets:
                    continue
                asset = sorted(assets, key=lambda a: a['name'])[0]
                url, size = asset['browser_download_url'], asset['size']
                cached = next((a for a in seed.get('apps', []) if a.get('githubRepository') == repo
                               and any(v.get('downloadURL') == url and v.get('size') == size
                                       for v in a.get('versions', []))), None)
                if cached:
                    app = dict(cached)
                    app['versions'] = [dict(v, date=release['published_at']) for v in cached['versions']
                                       if v.get('downloadURL') == url and v.get('size') == size]
                else:
                    metadata = inspector(url, size)
                    version = {'version': metadata['CFBundleShortVersionString'],
                               'date': release['published_at'], 'downloadURL': url, 'size': size,
                               'localizedDescription': release.get('body', '')[:4000]}
                    if metadata.get('CFBundleVersion'):
                        version['buildVersion'] = str(metadata['CFBundleVersion'])
                    if metadata.get('MinimumOSVersion'):
                        version['minOSVersion'] = metadata['MinimumOSVersion']
                    app = {'name': repo.split('/')[-1], 'bundleIdentifier': metadata['CFBundleIdentifier'],
                           'developerName': repo.split('/')[0], 'localizedDescription': release.get('body', '')[:4000],
                           'iconURL': config['icon'], 'versions': [version], 'githubRepository': repo}
                apps.append(app)
                report['accepted'] = 1
                break
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
            report['error'] = str(exc)
        reports.append(report)
    return apps, reports
