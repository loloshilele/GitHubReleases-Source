"""GitHub API 客户端：GraphQL 批量拉取 star 仓库 + 最新 Release。

设计要点：
- 只用标准库 urllib，零第三方依赖。
- GraphQL 一次取 100 个仓库，顺带返回 latestRelease 及其附件。
- 遇到限流/5xx 会退避重试；GraphQL 权限不足时降级到 REST /user/starred。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GRAPHQL_URL = "https://api.github.com/graphql"
REST_STARRED = "https://api.github.com/user/starred"

STARRED_QUERY = """
query($after: String) {
  viewer {
    login
    starredRepositories(first: 100, after: $after,
                        orderBy: {field: STARRED_AT, direction: DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner
        description
        url
        isArchived
        isPrivate
        isFork
        stargazerCount
        pushedAt
        primaryLanguage { name }
        latestRelease {
          tagName
          name
          publishedAt
          url
          isPrerelease
          isDraft
          description
          releaseAssets(first: 100) {
            nodes { name downloadUrl size downloadCount contentType }
          }
        }
      }
    }
  }
}
"""


class GitHubError(RuntimeError):
    """API 层错误，消息面向用户可读。"""


def _rate_limit_wait(headers) -> int | None:
    """从响应头推算需要等待的秒数；无法判断时返回 None。"""
    if not headers:
        return None
    remaining = headers.get("X-RateLimit-Remaining")
    reset = headers.get("X-RateLimit-Reset")
    if remaining == "0" and reset and reset.isdigit():
        wait = int(reset) - int(time.time())
        return max(wait, 1)
    if headers.get("Retry-After", "").isdigit():
        return int(headers["Retry-After"])
    return 60  # 二级限流通常没有明确头信息，保守等一分钟


class Client:
    def __init__(self, token: str, timeout: int = 40, verbose: bool = True):
        self.token = token.strip()
        self.timeout = timeout
        self.verbose = verbose

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, file=sys.stderr)

    def _graphql(self, query: str, variables: dict) -> dict:
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        headers = {
            "Authorization": "bearer " + self.token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "gh-star-releases/1.0",
        }
        delay = 2.0
        response = None
        for attempt in range(4):
            request = urllib.request.Request(GRAPHQL_URL, data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    remaining = resp.headers.get("X-RateLimit-Remaining")
                    limit = resp.headers.get("X-RateLimit-Limit")
                    if remaining and limit:
                        self._log(f"  · GraphQL 配额 {remaining}/{limit}")
                    response = body
                    break
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:400]
                except Exception:
                    pass
                if exc.code == 401:
                    raise GitHubError(
                        "Token 无效或已过期（401）。请检查 .env 里的 GITHUB_TOKEN。"
                    ) from None
                if exc.code in (403, 429):
                    wait = _rate_limit_wait(exc.headers)
                    if wait is not None and attempt < 3:
                        self._log(f"  · 触发限流，等待 {wait}s 后重试…")
                        time.sleep(min(wait, 120))
                        continue
                    raise GitHubError(
                        f"权限不足或被限流（{exc.code}）。常见原因：token 缺少 read:user 权限。\n{detail}"
                    ) from None
                if exc.code in (500, 502, 503, 504) and attempt < 3:
                    self._log(f"  · 服务端 {exc.code}，{int(delay)}s 后重试…")
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise GitHubError(f"请求失败（HTTP {exc.code}）：{detail}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < 3:
                    self._log(f"  · 网络异常（{exc}），{int(delay)}s 后重试…")
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise GitHubError(f"网络请求失败：{exc}") from None
        if response is None:
            raise GitHubError("多次重试后仍未拿到响应。")
        if response.get("errors"):
            messages = "；".join(str(item.get("message", "")) for item in response["errors"])[:400]
            raise GitHubError(f"GraphQL 返回错误：{messages}")
        return response.get("data") or {}

    def _rest_json(self, url: str):
        """Bounded retries for release discovery; never silently turn failures into empty data."""
        for attempt in range(4):
            request = urllib.request.Request(url, headers={
                "Authorization": "bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "User-Agent": "GitHubReleases/1.0",
            })
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                if exc.code not in (403, 408, 429, 500, 502, 503, 504) or attempt == 3:
                    raise GitHubError(f"GitHub HTTP {exc.code}: {url}") from None
                delay = min(_rate_limit_wait(exc.headers) or 2 ** attempt, 30) if exc.code in (403, 429) else 2 ** attempt
            except (OSError, TimeoutError) as exc:
                if attempt == 3:
                    raise GitHubError(f"GitHub 网络请求失败：{exc}") from None
                delay = 2 ** attempt
            time.sleep(delay)

    def fetch_ipa_releases(self, nodes: list[dict], depth: int = 0,
                           include_prerelease: bool = False,
                           repositories: list[str] | None = None) -> list[dict]:
        """Find the newest eligible IPA release, with explicit pagination diagnostics."""
        import source_catalog
        from github_catalog import asset_rank, pages
        by_repo = {n["nameWithOwner"].lower(): n for n in nodes if n.get("nameWithOwner")}
        for name in repositories or []:
            by_repo.setdefault(name.lower(), {"nameWithOwner": name, "url": f"https://github.com/{name}"})
        result, reports = [], []
        for node in by_repo.values():
            name = node["nameWithOwner"]
            report = {"repository": name, "releases_scanned": 0, "status": "no_ipa"}
            try:
                if "isPrivate" not in node:
                    details = self._rest_json(f"https://api.github.com/repos/{name}")
                    if not isinstance(details, dict):
                        raise GitHubError(f"Cannot confirm repository visibility: {name}")
                    node = {**node, "isPrivate": bool(details.get("private", True))}
                if node.get("isPrivate"):
                    report["status"] = "private_repository"
                    reports.append(report)
                    continue
                # Read every release page instead of depending on latestRelease or dashboard filters.
                rows = []
                limit = depth or 10000
                page_size = min(100, limit)
                for page in range(1, (limit + page_size - 1) // page_size + 1):
                    batch = self._rest_json(f"https://api.github.com/repos/{name}/releases?per_page={page_size}&page={page}") or []
                    rows.extend(batch[:limit-len(rows)])
                    if len(batch) < page_size:
                        break
                    if len(rows) >= limit:
                        report["truncated"] = True
                report["releases_scanned"] = len(rows)
                eligible = [r for r in rows if not r.get("draft")
                            and (include_prerelease or not r.get("prerelease"))
                            and source_catalog.recent(r.get("published_at"))]
                eligible.sort(key=lambda r: source_catalog.parse_date(r["published_at"]), reverse=True)
                if not eligible:
                    report["status"] = "no_recent_release"
                for release in eligible:
                    assets = release.get("assets") or []
                    if len(assets) >= 30 and release.get("id"):
                        assets = pages(f"/repos/{name}/releases/{release['id']}/assets",
                                       getter=lambda path: self._rest_json("https://api.github.com" + path) or [])
                    assets = sorted([a for a in assets if str(a.get("name") or "").lower().endswith(".ipa")], key=asset_rank)
                    if not assets:
                        continue
                    item = dict(node)
                    item["latestRelease"] = {
                        "tagName": release.get("tag_name"), "name": release.get("name"),
                        "publishedAt": release.get("published_at"), "url": release.get("html_url"),
                        "isPrerelease": bool(release.get("prerelease")), "description": release.get("body"),
                        "releaseAssets": {"nodes": [{"name": a.get("name"),
                            "downloadUrl": a.get("browser_download_url"), "size": a.get("size", 0),
                            "digest": a.get("digest"), "updatedAt": a.get("updated_at")} for a in assets]}}
                    result.append(item)
                    report.update(status="included", ipa_assets=len(assets), tag=release.get("tag_name"))
                    break
            except (GitHubError, OSError, ValueError, RuntimeError) as exc:
                report.update(status="failed", error=str(exc))
                result.append(node)  # Preserve a usable latestRelease on a transient discovery failure.
            reports.append(report)
            self._log(f"  · IPA {name}: {report['status']}" + (" (scan truncated)" if report.get("truncated") else ""))
        self.last_ipa_scan = reports
        return result


    def _rest_page(self, page: int, per_page: int = 100) -> tuple[list, dict]:
        query = urllib.parse.urlencode({"per_page": per_page, "page": page})
        request = urllib.request.Request(
            f"{REST_STARRED}?{query}",
            headers={
                "Authorization": "bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "User-Agent": "gh-star-releases/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8")), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            raise GitHubError(f"REST 拉取 star 列表失败（HTTP {exc.code}）。") from None

    def _rest_latest_release(self, full_name: str) -> dict | None:
        url = f"https://api.github.com/repos/{full_name}/releases/latest"
        data = self._rest_json(url)
        if data is None:
            return None
        return {
            "tagName": data.get("tag_name"),
            "name": data.get("name"),
            "publishedAt": data.get("published_at"),
            "url": data.get("html_url"),
            "isPrerelease": bool(data.get("prerelease")),
            "isDraft": bool(data.get("draft")),
            "description": data.get("body"),
            "releaseAssets": {
                "nodes": [
                    {
                        "name": asset.get("name"),
                        "downloadUrl": asset.get("browser_download_url"),
                        "size": asset.get("size") or 0,
                        "downloadCount": asset.get("download_count") or 0,
                        "contentType": asset.get("content_type") or "",
                    }
                    for asset in data.get("assets") or []
                ]
            },
        }

    def fetch_starred(self) -> tuple[str, int, list[dict]]:
        """返回 (登录名, star 总数, 规范化后的仓库节点列表)。"""
        try:
            return self._fetch_via_graphql()
        except GitHubError as exc:
            message = str(exc)
            if "权限不足" not in message and "403" not in message:
                raise
            self._log("· GraphQL 权限受限，降级到 REST 接口…")
            return self._fetch_via_rest()

    def _fetch_via_graphql(self) -> tuple[str, int, list[dict]]:
        nodes: list[dict] = []
        login = ""
        total = 0
        cursor = None
        page = 0
        while True:
            data = self._graphql(STARRED_QUERY, {"after": cursor})
            viewer = data.get("viewer") or {}
            login = viewer.get("login") or login
            block = viewer.get("starredRepositories") or {}
            total = block.get("totalCount") or total
            nodes.extend(block.get("nodes") or [])
            page += 1
            self._log(f"  · 已获取 {len(nodes)}/{total} 个 star 仓库（第 {page} 页）")
            info = block.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            cursor = info.get("endCursor")
            if not cursor:
                break
        return login, total, nodes

    def _fetch_via_rest(self) -> tuple[str, int, list[dict]]:
        nodes: list[dict] = []
        page = 1
        while True:
            batch, headers = self._rest_page(page)
            if not batch:
                break
            for repo in batch:
                language = repo.get("language")
                nodes.append(
                    {
                        "nameWithOwner": repo.get("full_name"),
                        "description": repo.get("description"),
                        "url": repo.get("html_url"),
                        "isArchived": bool(repo.get("archived")),
                        "isPrivate": bool(repo.get("private", True)),
                        "isFork": bool(repo.get("fork")),
                        "stargazerCount": repo.get("stargazers_count", 0),
                        "pushedAt": repo.get("pushed_at"),
                        "primaryLanguage": {"name": language} if language else None,
                        "latestRelease": self._rest_latest_release(repo.get("full_name", "")),
                    }
                )
            self._log(f"  · REST 已获取 {len(nodes)} 个 star 仓库（第 {page} 页）")
            if len(batch) < 100:
                break
            page += 1
            if page > 100:
                break
        return "", len(nodes), nodes
