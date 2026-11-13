"""Generate a public, escaped snapshot status page without exposing credentials."""
import html
from datetime import datetime, timezone
from urllib.parse import urlsplit

LABELS = {'included': '已识别', 'partial': '部分成功', 'failed': '扫描失败，等待重试',
          'metadata_failed': 'IPA 信息读取失败', 'no_ipa': '近期正式版没有 IPA',
          'no_recent_release': '没有一年内的正式版本'}
def esc(value):
    return html.escape(str(value if value is not None else ''), quote=True)

def safe_link(value):
    parsed = urlsplit(str(value or ''))
    return esc(value) if parsed.scheme == 'https' and parsed.hostname in ('github.com', 'loloshilele.github.io') else '#'

def enrich_report(source, report, previous=None):
    previous = previous or {}
    rows = [r for r in report.get('sources', []) if r.get('repository')]
    bad = [r for r in rows if r.get('status') in ('failed', 'metadata_failed', 'partial') or r.get('failures')]
    stars = [r for r in report.get('sources', []) if '?tab=stars' in r.get('url', '')]
    complete = bool(stars) and all(r.get('status') == 'complete' for r in stars) and not bad
    report['health'] = {
        'github_repositories': len(rows), 'github_failed': len(bad),
        'github_included': sum(bool(r.get('accepted')) for r in rows),
        'official_source_failures': sum(bool(r.get('error')) for r in report.get('sources', []) if not r.get('repository') and '?tab=stars' not in r.get('url', '')),
        'last_complete_scan_at': report['generated_at'] if complete else previous.get('health', {}).get('last_complete_scan_at'),
        'mode': 'complete' if complete else 'partial'}
    for row in rows:
        repo = row['repository'].lower()
        # Some official manifests win the Bundle ID merge; match actual download origin as well.
        actual = [a for a in source['apps'] if str(a.get('githubRepository', '')).lower() == repo
                  or any(('/' + repo + '/releases/') in str(v.get('downloadURL', '')).lower()
                         for v in a.get('versions', []))]
        row['published_apps'] = [{'name': a.get('name'), 'bundleIdentifier': a['bundleIdentifier'],
                                 'version': a['versions'][0]['version']} for a in actual]
        row['retained_apps'] = len(actual) if row.get('status') in ('failed', 'metadata_failed', 'partial') else 0
    return report

def render_status(source, report):
    health = report.get('health', {})
    rows = []
    for row in report.get('sources', []):
        if not row.get('repository'):
            continue
        details = []
        if row.get('retained_apps'):
            details.append('保留仍在一年内的已有应用：' + str(row['retained_apps']))
        details.extend(str(f.get('asset', '')) + '：' + str(f.get('error', '')) for f in row.get('failures', []))
        if row.get('error'):
            details.append(str(row['error']))
        if row.get('duplicates'):
            details.append('相同 Bundle ID 的附件已合并，优先完整未签名版')
        details.extend(str(a.get('name', '')) + ' ' + str(a.get('version', '')) for a in row.get('published_apps', []))
        if row.get('recovered'):
            details.append('本轮重试已恢复')
        detail = '<br>'.join(esc(d) for d in details) or '—'
        rows.append('<tr><td><a rel="noreferrer" href="' + safe_link(row.get('url')) + '">' + esc(row['repository']) +
                    '</a></td><td>' + esc(LABELS.get(row.get('status'), row.get('status'))) +
                    '</td><td>' + esc(row.get('ipa_assets', 0)) + '</td><td>' + esc(row.get('accepted', 0)) +
                    '</td><td>' + esc(row.get('attempts', 1)) + '</td><td>' + detail + '</td></tr>')
    official = ''.join('<li>' + esc(r.get('url')) + '：' + esc(r.get('error')) + '</li>'
                       for r in report.get('sources', []) if r.get('error') and not r.get('repository') and '?tab=stars' not in r.get('url', ''))
    date = esc(report['generated_at'])
    run_url = safe_link(report.get('run_url') or 'https://github.com/loloshilele/GitHubReleases-Source/actions')
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>手机源 · 同步状态</title>'
            '<style>body{font:15px/1.6 system-ui,sans-serif;color:#17243b;background:#f4f6fa;margin:0}'
            'main{max-width:1120px;margin:auto;padding:28px 20px}a{color:#2357c6}h1{font-size:28px;margin:12px 0}'
            '.cards{display:flex;gap:12px;flex-wrap:wrap}.card{padding:16px 22px;background:white;border:1px solid #dce3ed;border-radius:12px;flex:1;min-width:130px}'
            '.card strong{display:block;font-size:28px}.muted{color:#59677c}input{padding:12px;border:1px solid #b6c3d4;border-radius:8px;font:inherit;width:min(90%,380px)}'
            '.table{overflow:auto;background:white;border:1px solid #dce3ed;border-radius:10px;margin-top:16px}'
            'table{border-collapse:collapse;width:100%;min-width:760px}th,td{text-align:left;padding:12px;border-bottom:1px solid #e4e9f0;vertical-align:top}'
            'th{background:#eaf0f9}td:last-child{max-width:390px;overflow-wrap:anywhere}#freshness{padding:12px;border-radius:8px;background:#e7effc}'
            '</style></head><body><main><a href="./">← 手机源首页</a><h1>同步状态</h1>'
            '<p class="muted">这是当前已发布源的收集快照，不是实时服务器监控。每 4 小时同步，独立巡检每小时检查。</p>'
            '<p id="freshness" data-generated="' + date + '">快照生成时间：<time>' + date + '</time></p>'
            '<div class="cards"><div class="card">源内应用<strong>' + esc(len(source['apps'])) +
            '</strong></div><div class="card">扫描仓库<strong>' + esc(health.get('github_repositories', 0)) +
            '</strong></div><div class="card">有 IPA 的仓库<strong>' + esc(health.get('github_included', 0)) +
            '</strong></div><div class="card">待重试仓库<strong>' + esc(health.get('github_failed', 0)) + '</strong></div></div>'
            '<p>最近一次完整 GitHub 扫描：<time>' + esc(health.get('last_complete_scan_at') or '暂无完整记录') + '</time></p>'
            '<p><a href="' + run_url + '">本次任务 / 最新运行记录</a> · <a href="catalog-report.json">完整收集报告</a> · <a href="apps.json">手机源地址</a></p>'
            '<p>个别仓库失败不会阻止其他应用更新；旧条目只保留至真实版本发布后 365 天。Stars 列表整体失败时不发布。</p>'
            '<label>查找关注项目 <input id="filter" type="search" placeholder="输入仓库名或应用名"></label>'
            '<div class="table"><table><thead><tr><th>项目</th><th>本轮状态</th><th>IPA 附件</th><th>识别应用</th><th>尝试次数</th><th>说明 / 线上版本</th></tr></thead><tbody>' +
            ''.join(rows) + '</tbody></table></div>'
            '<details><summary>其他开发者官方源读取异常（' + esc(health.get('official_source_failures', 0)) + '）</summary><ul>' + official + '</ul></details>'
            '<p class="muted">Pages 由 GitHub 托管，无需本机启动。任务失败后仍提供最近一次成功部署的源。GitHub 平台整体故障或手机到 GitHub 的网络故障无法靠重跑程序解决。</p>'
            '<script>const f=document.getElementById("filter");f.addEventListener("input",()=>{for(const r of document.querySelectorAll("tbody tr"))r.hidden=!r.textContent.toLowerCase().includes(f.value.toLowerCase())});'
            'for(const t of document.querySelectorAll("time")){const d=new Date(t.textContent);if(!isNaN(d))t.textContent=d.toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false})+" 北京时间"}'
            'const box=document.getElementById("freshness");if(Date.now()-Date.parse(box.dataset.generated)>8*3600000){box.style.background="#fff0ce";box.append(" · 超过 8 小时未生成新快照，请查看运行记录。")}</script>'
            '</main></body></html>')

