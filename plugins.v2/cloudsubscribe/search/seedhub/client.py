"""SeedHub 网盘与 Magnet 搜索客户端。"""

import base64
import html
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

from ..http_client import (
    RequestGate, gated_request, normalize_proxies, normalize_proxy_address,
    proxy_server, requests,
)
from ..matching import extract_season, extract_year, title_matches, unique_texts
from ..types import resource_type_from_url
from ...utils.cache import (
    create_platform_ttl_cache,
    normalize_platform_cache_key,
)


class SeedHubError(RuntimeError):
    """SeedHub 请求或页面解析失败。"""


class _SeedHubLinkParser(HTMLParser):
    """提取详情页网盘中转项与中转页真实直链。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pan_entries: List[Dict[str, str]] = []
        self.direct_links: List[str] = []
        self._pan_list_depth = 0
        self._current: Optional[Dict[str, str]] = None
        self._current_text: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        classes = set(str(attributes.get("class") or "").split())
        if tag == "ul":
            if self._pan_list_depth:
                self._pan_list_depth += 1
            elif "pan-links" in classes:
                self._pan_list_depth = 1
        if tag != "a":
            return
        href = html.unescape(str(attributes.get("href") or "")).strip()
        if "direct-pan" in classes and href:
            self.direct_links.append(href)
        if self._pan_list_depth and "redirect_to=pan_id_" in href:
            self._current = {
                "href": href,
                "title": html.unescape(str(attributes.get("title") or "")).strip(),
                "host": str(attributes.get("data-link") or "").strip().lower(),
            }
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current is not None:
            item = dict(self._current)
            item["title"] = item["title"] or " ".join(self._current_text).strip()
            self.pan_entries.append(item)
            self._current = None
            self._current_text = []
        if tag == "ul" and self._pan_list_depth:
            self._pan_list_depth -= 1


class SeedHubClient:
    """搜索作品页并解析其中的网盘或 Magnet 链接。"""

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    _SEARCH_PATTERN = re.compile(
        r'<div class="cover">.*?title="(?P<anchor_title>[^"]*)"[^>]*'
        r'href="/movies/(?P<movie_id>\d+)/".*?'
        r'<li><h2><a[^>]+href="/movies/\d+/"[^>]*>.*?</a>\s*'
        r'(?P<title>.*?)</h2></li>\s*<li>(?P<meta>.*?)</li>'
        r'(?P<extra>.*?)</ul>\s*</div>',
        re.IGNORECASE | re.DOTALL,
    )
    _DOUBAN_PATTERN = re.compile(
        r'(?:movie\.)?douban\.com/subject/(?P<douban_id>\d+)',
        re.IGNORECASE,
    )
    _ENTRY_PATTERN = re.compile(
        r'<li>\s*(?P<a><a[^>]+href="/link_start/\?seed_id=(?P<seed>\d+)'
        r'[^"]*"[^>]*>.*?</a>)\s*/\s*'
        r'<code class="size">(?P<size>[^<]*)</code>.*?'
        r'<span class="create-time"[^>]*>(?P<updated>[^<]*)</span>',
        re.IGNORECASE | re.DOTALL,
    )

    def __init__(
            self,
            base_url: str = "https://www.seedhub.cc",
            proxy: Any = None,
            request_timeout: int = 20,
            resolve_concurrency: int = 6,
            request_interval: float = 0.3,
    ):
        self.base_url = str(base_url or "https://www.seedhub.cc").rstrip("/")
        self._proxies = normalize_proxies(proxy)
        self._browser_proxy = self._normalize_browser_proxy(proxy)
        self._request_timeout = max(5, min(int(request_timeout or 20), 60))
        self._resolve_concurrency = max(1, min(int(resolve_concurrency or 6), 8))
        self._search_cache = create_platform_ttl_cache(
            "seedhub:search", self.base_url, maxsize=128, ttl=15 * 60
        )
        self._magnet_cache = create_platform_ttl_cache(
            "seedhub:magnets", self.base_url, maxsize=1024, ttl=60 * 60
        )
        self._cache_lock = threading.RLock()
        self._search_locks = tuple(threading.Lock() for _ in range(16))
        self._browser_lock = threading.RLock()
        self._browser_state_version = 0
        self._browser_cookie_header = ""
        self._browser_user_agent = ""
        self._request_gate = RequestGate.shared(
            "SeedHub",
            f"{self.base_url}|{self._proxies}",
            request_interval=request_interval,
            minimum_interval=0.2,
            challenge_detector=self._is_challenge_response,
            serial_requests=False,
        )

    @staticmethod
    def _normalize_browser_proxy(proxy: Any) -> Optional[Dict[str, str]]:
        if isinstance(proxy, dict):
            proxy = (
                    proxy.get("https") or proxy.get("http") or proxy.get("server")
            )
        value = normalize_proxy_address(proxy)
        if not value:
            return None
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.hostname:
            return None
        result = {"server": proxy_server(value)}
        if parsed.username:
            result["username"] = unquote(parsed.username)
        if parsed.password:
            result["password"] = unquote(parsed.password)
        return result

    @staticmethod
    def _clean_text(value: object) -> str:
        return html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))).strip()

    @staticmethod
    def _is_cloudflare_challenge(
            text: str, status_code: int = 200, server: str = ""
    ) -> bool:
        lowered = str(text or "").lower()
        markers = (
            "cf-chl-",
            "cdn-cgi/challenge-platform",
            "enable javascript and cookies",
            "<title>just a moment",
        )
        return any(marker in lowered for marker in markers) or (
            status_code in {403, 503} and str(server or "").lower() == "cloudflare"
        )

    def _request_headers(self) -> Dict[str, str]:
        with self._browser_lock:
            headers = dict(self._HEADERS)
            if self._browser_user_agent:
                headers["User-Agent"] = self._browser_user_agent
            if self._browser_cookie_header:
                headers["Cookie"] = self._browser_cookie_header
            return headers

    @classmethod
    def _is_challenge_response(cls, response) -> bool:
        return cls._is_cloudflare_challenge(
            response.text or "",
            response.status_code,
            response.headers.get("Server", ""),
        )

    def _request_once(self, url: str):
        return gated_request(
            self._request_gate,
            requests.get,
            url,
            impersonate="chrome",
            headers=self._request_headers(),
            proxies=self._proxies,
            timeout=(8, self._request_timeout),
            allow_redirects=True,
        )

    def _get_browser_text(self, url: str, observed_version: int) -> str:
        with self._browser_lock:
            if self._browser_state_version != observed_version:
                try:
                    response = self._request_once(url)
                    text = response.text or ""
                    if (
                            response.ok
                            and not self._is_cloudflare_challenge(
                                text,
                                response.status_code,
                                response.headers.get("Server", ""),
                            )
                    ):
                        return text
                except requests.exceptions.RequestException:
                    pass

            from app.helper.browser import PlaywrightHelper

            def snapshot(page) -> Dict[str, Any]:
                return {
                    "text": page.content() or "",
                    "cookies": page.context.cookies(),
                    "user_agent": page.evaluate("navigator.userAgent") or "",
                }

            result = self._request_gate.run(lambda: PlaywrightHelper().action(
                url=url,
                callback=snapshot,
                proxies=self._browser_proxy,
                headless=True,
                timeout=max(30, self._request_timeout),
            ))
            if not isinstance(result, dict):
                return ""
            text = str(result.get("text") or "")
            if not text or self._is_cloudflare_challenge(text):
                self._request_gate.activate_cooldown(
                    60, reason="SeedHub 浏览器验证"
                )
                return ""
            seedhub_host = str(urlparse(self.base_url).hostname or "").lower()
            cookies = [
                f"{cookie.get('name')}={cookie.get('value')}"
                for cookie in (result.get("cookies") or [])
                if cookie.get("name") and cookie.get("value") is not None
                if (
                    not cookie.get("domain")
                    or seedhub_host == str(cookie.get("domain")).lstrip(".").lower()
                    or seedhub_host.endswith(
                        f".{str(cookie.get('domain')).lstrip('.').lower()}"
                    )
                )
            ]
            self._browser_cookie_header = "; ".join(cookies)
            self._browser_user_agent = str(result.get("user_agent") or "")
            self._browser_state_version += 1
            return text

    def _get_text(self, url: str) -> str:
        last_error = ""
        for attempt in range(2):
            try:
                with self._browser_lock:
                    browser_state_version = self._browser_state_version
                response = self._request_once(url)
                text = response.text or ""
                if self._is_cloudflare_challenge(
                        text,
                        response.status_code,
                        response.headers.get("Server", ""),
                ):
                    browser_text = self._get_browser_text(
                        url, browser_state_version
                    )
                    if browser_text:
                        return browser_text
                    raise SeedHubError("SeedHub 浏览器仿真未通过 Cloudflare 验证")
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                    if attempt == 0:
                        time.sleep(0.3)
                        continue
                response.raise_for_status()
                return text
            except SeedHubError:
                raise
            except requests.exceptions.RequestException as error:
                last_error = type(error).__name__
                if attempt == 0:
                    time.sleep(0.3)
                    continue
        raise SeedHubError(f"SeedHub 请求失败：{last_error or '未知错误'}")

    def _parse_search_candidates(self, text: str) -> List[Dict[str, str]]:
        candidates = []
        seen = set()
        for matched in self._SEARCH_PATTERN.finditer(text):
            movie_id = str(matched.group("movie_id") or "").strip()
            if not movie_id or movie_id in seen:
                continue
            seen.add(movie_id)
            meta = self._clean_text(matched.group("meta"))
            meta_parts = [part.strip() for part in meta.split("/") if part.strip()]
            douban_match = self._DOUBAN_PATTERN.search(
                html.unescape(matched.group("extra") or "")
            )
            candidates.append({
                "movie_id": movie_id,
                "title": self._clean_text(matched.group("title")),
                "anchor_title": self._clean_text(matched.group("anchor_title")),
                "year": extract_year(meta),
                "media_type": meta_parts[1] if len(meta_parts) >= 2 else "",
                "douban_id": (
                    douban_match.group("douban_id") if douban_match else ""
                ),
            })
        return candidates

    @staticmethod
    def _type_matches(value: object, media_type: str) -> bool:
        text = str(value or "").strip().casefold()
        if not text:
            return False
        if media_type == "tv":
            return any(marker in text for marker in ("剧集", "电视剧", "tv", "series"))
        return any(marker in text for marker in ("电影", "movie"))

    def _select_candidate(
            self,
            candidates: Iterable[Dict[str, str]],
            expected_titles: List[str],
            expected_year: str,
            media_type: str,
            season: Optional[int],
            douban_id: Optional[object] = None,
    ) -> Optional[Dict[str, str]]:
        expected_douban_id = str(douban_id or "").strip()
        best_id_match = None
        best_title_match = None
        for index, candidate in enumerate(candidates):
            if not self._type_matches(candidate.get("media_type"), media_type):
                continue
            candidate_titles = [candidate.get("title"), candidate.get("anchor_title")]
            exact_douban = bool(
                expected_douban_id
                and str(candidate.get("douban_id") or "").strip()
                == expected_douban_id
            )
            matched_title = any(
                title_matches(value, expected_titles) for value in candidate_titles
            )
            if not exact_douban and not matched_title:
                continue
            candidate_year = str(candidate.get("year") or "")
            if (
                    not exact_douban and expected_year and candidate_year
                    and candidate_year != expected_year
            ):
                continue
            if (
                    not exact_douban and media_type == "movie"
                    and expected_year and not candidate_year
            ):
                continue
            candidate_season = next(
                (value for value in map(extract_season, candidate_titles) if value),
                None,
            )
            if season and candidate_season and candidate_season != season:
                continue
            score = 1000 if exact_douban else 100
            if expected_year and candidate_year == expected_year:
                score += 40
            if season and candidate_season == season:
                score += 60
            ranked = (score, -index, candidate)
            if exact_douban:
                if best_id_match is None or (score, -index) > best_id_match[:2]:
                    best_id_match = ranked
            elif best_title_match is None or (score, -index) > best_title_match[:2]:
                best_title_match = ranked
        best = best_id_match or best_title_match
        return best[2] if best is not None else None

    def _parse_entries(self, text: str) -> List[Dict[str, str]]:
        entries = []
        seen = set()
        for matched in self._ENTRY_PATTERN.finditer(text):
            seed_id = str(matched.group("seed") or "").strip()
            if not seed_id or seed_id in seen:
                continue
            seen.add(seed_id)
            anchor = str(matched.group("a") or "")
            title_match = re.search(r'title="([^"]*)"', anchor, re.IGNORECASE)
            entries.append({
                "kind": "magnet",
                "seed_id": seed_id,
                "title": self._clean_text(title_match.group(1) if title_match else ""),
                "size": self._clean_text(matched.group("size")),
                "updated_at": self._clean_text(matched.group("updated")),
            })
        return entries

    @staticmethod
    def _parse_pan_entries(text: str) -> List[Dict[str, str]]:
        parser = _SeedHubLinkParser()
        parser.feed(text or "")
        parser.close()
        return parser.pan_entries

    def _resolve_magnet(self, seed_id: str) -> str:
        with self._cache_lock:
            cached = self._magnet_cache.get(seed_id)
        if cached:
            return str(cached)
        text = self._get_text(
            f"{self.base_url}/link_start/?seed_id={quote(seed_id)}&movie_title=seedhub"
        )
        matched = re.search(r'const\s+data\s*=\s*"([A-Za-z0-9+/=]+)"', text)
        if not matched:
            return ""
        try:
            magnet = base64.b64decode(matched.group(1)).decode(
                "utf-8", errors="ignore"
            ).strip()
        except (ValueError, TypeError):
            return ""
        if not magnet.lower().startswith("magnet:?"):
            return ""
        with self._cache_lock:
            self._magnet_cache.set(seed_id, magnet)
        return magnet

    def _resolve_pan_link(self, entry: Dict[str, str]) -> tuple[str, str]:
        href = str(entry.get("href") or "").strip()
        if not href:
            return "", ""
        host_hint = str(entry.get("host") or "").strip()
        if host_hint and not resource_type_from_url(f"https://{host_hint}"):
            return "", ""
        cache_key = f"pan:{href}"
        with self._cache_lock:
            cached = self._magnet_cache.get(cache_key)
        if cached:
            url = str(cached)
            return url, resource_type_from_url(url)
        parser = _SeedHubLinkParser()
        parser.feed(self._get_text(urljoin(f"{self.base_url}/", href)))
        parser.close()
        url = next((str(value).strip() for value in parser.direct_links if value), "")
        resource_type = resource_type_from_url(url)
        if not resource_type:
            return "", ""
        with self._cache_lock:
            self._magnet_cache.set(cache_key, url)
        return url, resource_type

    def _resolve_entry(self, item: Dict[str, str]) -> tuple[str, str]:
        if item.get("kind") == "pan":
            return self._resolve_pan_link(item)
        magnet = self._resolve_magnet(str(item.get("seed_id") or ""))
        return magnet, "magnet" if magnet else ""

    def resolve_resource(
            self,
            kind: str,
            resource_type: str,
            seed_id: str = "",
            path: str = "",
            host: str = "",
    ) -> Dict[str, str]:
        """解析测试列表中用户选中的单条 SeedHub 资源。"""
        kind = str(kind or "").strip().lower()
        expected_type = str(resource_type or "").strip().lower()
        if kind == "magnet":
            if expected_type != "magnet":
                raise SeedHubError("SeedHub Magnet 资源类型无效")
            seed_id = str(seed_id or "").strip()
            if not re.fullmatch(r"\d{1,32}", seed_id):
                raise SeedHubError("SeedHub 资源标识无效")
            item = {"kind": "magnet", "seed_id": seed_id}
        elif kind == "pan":
            path = str(path or "").strip()
            parsed = urlparse(path)
            redirect_to = parse_qs(parsed.query).get("redirect_to") or []
            if (
                    parsed.scheme or parsed.netloc
                    or parsed.path.rstrip("/") != "/link_start"
                    or len(redirect_to) != 1
                    or not re.fullmatch(r"pan_id_[A-Za-z0-9_-]{1,128}", redirect_to[0])
            ):
                raise SeedHubError("SeedHub 网盘资源标识无效")
            host = str(host or "").strip().lower()
            hinted_type = resource_type_from_url(f"https://{host}") if host else ""
            if not hinted_type or hinted_type != expected_type:
                raise SeedHubError("SeedHub 网盘资源类型无效")
            item = {"kind": "pan", "href": path, "host": host}
        else:
            raise SeedHubError("SeedHub 资源类型无效")

        url, actual_type = self._resolve_entry(item)
        if not url or actual_type != expected_type:
            raise SeedHubError("SeedHub 资源链接解析失败")
        return {"url": url, "resource_type": actual_type}

    def _pending_entries(
            self, movie_id: str, entries: List[Dict[str, str]], limit: int
    ) -> List[Dict[str, Any]]:
        """测试模式只返回可识别候选，实际链接由预览操作按需解析。"""
        results = []
        seen = set()
        for item in entries:
            kind = str(item.get("kind") or "").strip().lower()
            if kind == "magnet":
                seed_id = str(item.get("seed_id") or "").strip()
                if not re.fullmatch(r"\d{1,32}", seed_id):
                    continue
                identity = f"magnet:{seed_id}"
                resource_type = "magnet"
            elif kind == "pan":
                path = str(item.get("href") or "").strip()
                host = str(item.get("host") or "").strip().lower()
                resource_type = resource_type_from_url(f"https://{host}") if host else ""
                if not path or not resource_type:
                    continue
                identity = f"pan:{path}"
            else:
                continue
            if identity in seen:
                continue
            seen.add(identity)
            resource_id = item.get("seed_id") or item.get("href") or ""
            results.append({
                "url": "",
                "title": item.get("title") or f"SeedHub 资源 {resource_id}",
                "size": item.get("size") or 0,
                "update_time": item.get("updated_at") or "",
                "resource_type": resource_type,
                "pan_type": resource_type,
                "source": "seedhub",
                "source_service": "seedhub",
                "source_url": f"{self.base_url}/movies/{movie_id}/",
                "pending_resolution": True,
                "seedhub_kind": kind,
                "seedhub_seed_id": str(item.get("seed_id") or ""),
                "seedhub_path": str(item.get("href") or ""),
                "seedhub_host": str(item.get("host") or ""),
            })
            if len(results) >= limit:
                break
        return results

    def _resolve_entries(
            self, movie_id: str, entries: List[Dict[str, str]], limit: int
    ) -> List[Dict[str, Any]]:
        results = []
        seen = set()
        with ThreadPoolExecutor(
                max_workers=min(self._resolve_concurrency, len(entries) or 1),
                thread_name_prefix="seedhub",
        ) as executor:
            for offset in range(0, len(entries), self._resolve_concurrency):
                batch = entries[offset:offset + self._resolve_concurrency]
                futures = [
                    (item, executor.submit(self._resolve_entry, item))
                    for item in batch
                ]
                for item, future in futures:
                    try:
                        url, resource_type = future.result()
                    except SeedHubError:
                        continue
                    key = url.casefold()
                    if not url or not resource_type or key in seen:
                        continue
                    seen.add(key)
                    identity = item.get("seed_id") or item.get("href") or ""
                    title = item.get("title") or f"SeedHub 资源 {identity}"
                    results.append({
                        "url": url,
                        "title": title,
                        "size": item.get("size") or 0,
                        "update_time": item.get("updated_at") or "",
                        "resource_type": resource_type,
                        "pan_type": resource_type,
                        "source": "seedhub",
                        "source_service": "seedhub",
                        "source_url": f"{self.base_url}/movies/{movie_id}/",
                        "seed_id": item.get("seed_id") or "",
                    })
                    if len(results) >= limit:
                        break
                if len(results) >= limit:
                    break
        return results

    def search(
            self,
            keywords: Iterable[str],
            expected_titles: Iterable[str],
            expected_year: object,
            media_type: str,
            season: Optional[int] = None,
            douban_id: Optional[object] = None,
            limit: int = 20,
            test_mode: bool = False,
    ) -> List[Dict[str, Any]]:
        titles = [str(value).strip() for value in expected_titles if str(value or "").strip()]
        normalized_keywords = unique_texts(keywords)
        if not titles or not normalized_keywords:
            return []
        normalized_limit = max(1, min(int(limit or 20), 80))
        year = extract_year(expected_year)
        cache_key = (tuple(normalized_keywords), tuple(titles), year, media_type,
                     season, str(douban_id or ""), normalized_limit, bool(test_mode))
        cache_key = normalize_platform_cache_key(cache_key)
        lock = self._search_locks[hash(cache_key) % len(self._search_locks)]
        with lock:
            with self._cache_lock:
                cached = self._search_cache.get(cache_key)
            if cached is not None:
                return [dict(item) for item in cached]

            selected = None
            for keyword in normalized_keywords:
                text = self._get_text(f"{self.base_url}/s/{quote(keyword)}/")
                candidates = self._parse_search_candidates(text)
                selected = self._select_candidate(
                    candidates, titles, year, media_type, season, douban_id
                )
                if selected:
                    break
            if not selected:
                with self._cache_lock:
                    self._search_cache.set(cache_key, [])
                return []

            movie_id = selected["movie_id"]
            detail_text = self._get_text(f"{self.base_url}/movies/{movie_id}/")
            entries = self._parse_entries(detail_text)
            entries.extend({"kind": "pan", **item} for item in self._parse_pan_entries(detail_text))
            if not test_mode and media_type == "tv" and season:
                matching_entries = [
                    item for item in entries
                    if extract_season(item.get("title")) in (None, season)
                ]
                entries = matching_entries
            results = (
                self._pending_entries(movie_id, entries, normalized_limit)
                if test_mode else
                self._resolve_entries(movie_id, entries, normalized_limit)
            )
            selected_douban_id = str(selected.get("douban_id") or "").strip()
            if selected_douban_id:
                for result in results:
                    result["douban_id"] = selected_douban_id
            with self._cache_lock:
                self._search_cache.set(
                    cache_key, [dict(item) for item in results]
                )
            return results

    def clear_cache(self) -> Dict[str, int]:
        with self._cache_lock:
            counts = {
                "search": len(list(self._search_cache.items())),
                "links": len(list(self._magnet_cache.items())),
            }
            self._search_cache.clear()
            self._magnet_cache.clear()
        return counts
