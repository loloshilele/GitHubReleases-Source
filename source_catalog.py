"""Curated public AltSource aggregation and strict release-age filtering (stdlib only)."""
from __future__ import annotations

import argparse
import copy
import html
import ipaddress
import json
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

MAX_AGE_DAYS = 365
# Sources published by their developers or included in SideStore's default-source registry.
DEFAULT_SOURCES = [
    'https://raw.githubusercontent.com/claration/Feather/main/app-repo.json',
    'https://apps.altstore.io',
    'https://community-apps.sidestore.io/sidecommunity.json',
    'https://stikdebug.xyz/index.json',
    'https://github.com/LiveContainer/LiveContainer/releases/download/1.0/apps.json',
    'https://apps.manicemu.site/altstore',
    'https://git.ryujinx.app/melonx/emu/-/raw/XC-ios-ht/source.json',
    'https://github.com/chachillie/Flycast-iOS/raw/refs/heads/main/flycast-ios.json',
    'https://ios-repo.geode-sdk.org/altsource/main.json',
    'https://repo.untitledcharts.com',
    'https://provenance-emu.com/apps.json',
    'https://neoarz.github.io/Countdown-App/Countdown.json',
    'https://altstore.oatmealdome.me',
    'https://alt.getutm.app',
    'https://alt.crystall1ne.dev',
    'https://pokemmo.eu/altstore/',
    'https://theodyssey.dev/altstore/odysseysource.json',
    'https://repos.yattee.stream/alt/apps.json',
    'https://altstore.ignitedemulator.com/',
    'https://alt.thatstel.la/',
    'https://raw.githubusercontent.com/delia-cheminot/mona-hrt/refs/heads/main/ios_source.json',
    'https://flyinghead.github.io/flycast-builds/altstore.json',
]


def parse_date(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def recent(value, now=None, days=MAX_AGE_DAYS):
    now = now or datetime.now(timezone.utc)
    date = parse_date(value)
    return date is not None and now - timedelta(days=days) <= date <= now + timedelta(days=1)


def public_https(value):
    try:
        parsed = urlsplit(str(value))
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            return False
        if parsed.hostname.lower() in ('localhost',) or parsed.hostname.lower().endswith(('.local', '.localhost')):
            return False
        try:
            return ipaddress.ip_address(parsed.hostname).is_global
        except ValueError:
            return True
    except ValueError:
        return False


def fresh_apps(apps, now=None, days=MAX_AGE_DAYS, base='', include_prerelease=False):
    """Retain real dated versions only; never substitute scan time/file mtime for release date."""
    result = []
    now = now or datetime.now(timezone.utc)
    for app in apps:
        if not isinstance(app, dict) or not app.get('bundleIdentifier') or app.get('patreon'):
            continue
        versions = app.get('versions')
        if not isinstance(versions, list):
            versions = [{'version': app.get('version'), 'date': app.get('versionDate'),
                         'downloadURL': app.get('downloadURL'), 'size': app.get('size')}]
        valid = []
        for item in versions:
            if not isinstance(item, dict) or not item.get('version') or not recent(item.get('date'), now, days):
                continue
            if not include_prerelease and (item.get('isPrerelease') or item.get('beta') or
                    re.search(r'alpha|beta|nightly|preview|(?:^|[.-])rc', str(item['version']), re.I)):
                continue
            if not item.get('downloadURL'):
                continue
            copied = copy.deepcopy(item)
            copied['downloadURL'] = urljoin(base, str(copied.get('downloadURL') or ''))
            if not copied['downloadURL']:
                continue
            valid.append(copied)
        if not valid:
            continue
        valid.sort(key=lambda v: parse_date(v['date']), reverse=True)
        entry = copy.deepcopy(app)
        entry.pop('marketplaceID', None)
        entry.pop('patreon', None)
        entry['versions'] = valid[:5]
        for key in ('version', 'versionDate', 'downloadURL', 'size'):
            entry.pop(key, None)
        if base and entry.get('iconURL'):
            entry['iconURL'] = urljoin(base, entry['iconURL'])
        result.append(entry)
    return result


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def expired_ids(root):
    try:
        return set(json.loads((Path(root) / '.metadata/expired-bundles.json').read_text(encoding='utf-8')))
    except (OSError, ValueError, TypeError):
        return set()


def set_active(root, bundles):
    blocked = expired_ids(root) - set(bundles)
    atomic_json(Path(root) / '.metadata/expired-bundles.json', sorted(blocked))


def prune_file(path, now=None, days=MAX_AGE_DAYS):
    path = Path(path)
    if not path.exists():
        return 0
    raw = json.loads(path.read_text(encoding='utf-8'))
    original = raw.get('apps') or []
    kept = fresh_apps(original, now, days, include_prerelease=True)
    live = {app['bundleIdentifier'] for app in kept}
    blocked = expired_ids(path.parent)
    blocked.update(app['bundleIdentifier'] for app in original
                   if isinstance(app, dict) and app.get('bundleIdentifier') and app['bundleIdentifier'] not in live)
    blocked -= live
    atomic_json(path.parent / '.metadata/expired-bundles.json', sorted(blocked))
    raw['apps'] = kept
    atomic_json(path, raw)
    return len(original) - len(kept)


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not public_https(newurl):
            raise ValueError('Non-public HTTPS redirect refused')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_manifest(url):
    if not public_https(url):
        raise ValueError('Source must be public HTTPS')
    opener = urllib.request.build_opener(PublicRedirect())
    error = None
    for attempt in range(2):
        try:
            with opener.open(urllib.request.Request(url, headers={'User-Agent': 'GitHubReleases-Source/1.0'}), timeout=20) as response:
                data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise ValueError('Source JSON exceeds 8 MB')
            parsed = json.loads(data.decode('utf-8-sig'))
            if not isinstance(parsed, dict) or not isinstance(parsed.get('apps'), list):
                raise ValueError('Not an AltSource JSON object')
            return parsed
        except (OSError, ValueError) as exc:
            error = exc
            if attempt == 0:
                time.sleep(1)
    raise RuntimeError(str(error))


def collect(seed=None, urls=None, now=None, days=MAX_AGE_DAYS, loader=None, log=print, include_prerelease=False):
    now = now or datetime.now(timezone.utc)
    loader = loader or fetch_manifest
    by_bundle = {}
    for app in fresh_apps((seed or {}).get('apps') or [], now, days, include_prerelease=include_prerelease):
        app['versions'] = [v for v in app['versions'] if public_https(v['downloadURL'])]
        if app['versions']:
            by_bundle[app['bundleIdentifier']] = app
    report = {'generated_at': now.isoformat(), 'max_age_days': days, 'sources': []}
    def load(url):
        try:
            return url, loader(url), None
        except (OSError, RuntimeError, ValueError) as exc:
            return url, None, str(exc)
    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(load, list(dict.fromkeys(DEFAULT_SOURCES if urls is None else urls))))
    for url, raw, error in responses:
        if error:
            report['sources'].append({'url': url, 'error': error})
            log(f'  ! 官方源读取失败：{url}：{error}')
            continue
        accepted = 0
        for app in fresh_apps(raw.get('apps') or [], now, days, url, include_prerelease):
            versions = [v for v in app['versions'] if public_https(v['downloadURL']) and
                        isinstance(v.get('size'), int) and not isinstance(v['size'], bool) and v['size'] > 0]
            if not versions:
                continue
            app['versions'] = versions
            if not public_https(app.get('iconURL')):
                app['iconURL'] = 'https://github.com/github.png'
            current = by_bundle.get(app['bundleIdentifier'])
            if current is None or parse_date(versions[0]['date']) >= parse_date(current['versions'][0]['date']):
                by_bundle[app['bundleIdentifier']] = app
            accepted += 1
        report['sources'].append({'url': url, 'accepted': accepted})
        log(f'  · 官方源：{raw.get("name") or url}，一年内有效应用 {accepted} 个')
    result = {k: (seed or {})[k] for k in ('name', 'identifier', 'sourceURL', 'iconURL') if (seed or {}).get(k)}
    result.setdefault('name', 'GitHub Stars IPA 源')
    result.setdefault('identifier', 'local.githubreleases.stars')
    result['apps'] = sorted(by_bundle.values(), key=lambda a: parse_date(a['versions'][0]['date']), reverse=True)
    report['app_count'] = len(result['apps'])
    return result, report


def write_site(output, source, report):
    output = Path(output)
    icon = Path(__file__).with_name('source-icon.jpg')
    if icon.exists():
        import shutil
        output.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(icon, output / 'source-icon.jpg')
    atomic_json(output / 'apps.json', source)
    atomic_json(output / 'catalog-report.json', report)
    page = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>IPA 源</title>'
            '<h1>GitHub Stars IPA 源</h1>'
            f'<p>{len(source["apps"])} 个应用，仅展示最近 {report["max_age_days"]} 天有发布的版本。</p>'
            '<p>在手机软件源中添加此链接的完整地址：</p><a href="apps.json">apps.json</a>'
            '<p>应用直接从开发者的 GitHub 或官方下载地址获取，不依赖电脑开机。</p>'
            f'<p>更新于 {html.escape(report["generated_at"])}</p></html>')
    (output / 'index.html').write_text(page, encoding='utf-8')
    (output / '.nojekyll').write_text('', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', default='apps.json')
    parser.add_argument('--out', default='_site')
    parser.add_argument('--base', default='')
    parser.add_argument('--stars-owner', default='')
    args = parser.parse_args()
    path = Path(args.seed)
    seed = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    from github_catalog import collect_repositories
    github_apps, github_reports = collect_repositories(seed, datetime.now(timezone.utc), MAX_AGE_DAYS, owner=args.stars_owner)
    seed['apps'] = (seed.get('apps') or []) + github_apps
    source, report = collect(seed)
    report['sources'].extend(github_reports)
    report['github_repositories'] = len([r for r in github_reports if r.get('repository')])
    for entry in github_reports:
        print('GITHUB_SCAN=' + json.dumps(entry, ensure_ascii=False))
    if args.base:
        source['sourceURL'] = args.base.rstrip('/') + '/apps.json'
        source['iconURL'] = args.base.rstrip('/') + '/source-icon.jpg'
    if not source['apps']:
        raise RuntimeError('没有一年内的有效应用，未覆盖线上源')
    write_site(args.out, source, report)
    print(f'VALID_APPS={len(source["apps"])}')


if __name__ == '__main__':
    main()
