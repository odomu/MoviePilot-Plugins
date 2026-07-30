"""SeedHub Magnet 搜索客户端。"""

import base64
import html
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote, unquote, urlparse

from cachetools import TTLCache

from ..http_client import RequestGate, gated_request, normalize_proxies, requests
from ..matching import extract_season, extract_year, title_matches, unique_texts


class SeedHubError(RuntimeError):
    """SeedHub 请求或页面解析失败。"""


class SeedHubClient:
    """搜索作品页并解析其中的 Magnet 链接。"""

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
        r'(?P<title>.*?)</h2></li>\s*<li>(?P<meta>.*?)</li>',
        re.IGNORECASE | re.DOTALL,
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
    ):
        self.base_url = str(base_url or "https://www.seedhub.cc").rstrip("/")
        self._proxies = normalize_proxies(proxy)
        self._browser_proxy = self._normalize_browser_proxy(proxy)
        self._request_timeout = max(5, min(int(request_timeout or 20), 60))
        self._resolve_concurrency = max(1, min(int(resolve_concurrency or 6), 8))
        self._search_cache = TTLCache(maxsize=128, ttl=15 * 60)
        self._magnet_cache = TTLCache(maxsize=1024, ttl=60 * 60)
        self._cache_lock = threading.RLock()
        self._search_locks = tuple(threading.Lock() for _ in range(16))
        self._browser_lock = threading.RLock()
        self._browser_state_version = 0
        self._browser_cookie_header = ""
        self._browser_user_agent = ""
        self._request_gate = RequestGate(
            "SeedHub",
            request_interval=0.3,
            minimum_interval=0.2,
            challenge_detector=self._is_challenge_response,
            serial_requests=False,
        )

    @staticmethod
    def _normalize_browser_proxy(proxy: Any) -> Optional[Dict[str, str]]:
        if isinstance(proxy, dict):
            proxy = proxy.get("https") or proxy.get("http")
        if not proxy:
            return None
        parsed = urlparse(str(proxy))
        if not parsed.scheme or not parsed.hostname:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        result = {"server": f"{parsed.scheme}://{parsed.hostname}:{port}"}
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

            result = PlaywrightHelper().action(
                url=url,
                callback=snapshot,
                proxies=self._browser_proxy,
                headless=True,
                timeout=max(30, self._request_timeout),
            )
            if not isinstance(result, dict):
                return ""
            text = str(result.get("text") or "")
            if not text or self._is_cloudflare_challenge(text):
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
            candidates.append({
                "movie_id": movie_id,
                "title": self._clean_text(matched.group("title")),
                "anchor_title": self._clean_text(matched.group("anchor_title")),
                "year": extract_year(meta),
                "media_type": meta_parts[1] if len(meta_parts) >= 2 else "",
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
    ) -> Optional[Dict[str, str]]:
        ranked = []
        for index, candidate in enumerate(candidates):
            if not self._type_matches(candidate.get("media_type"), media_type):
                continue
            candidate_titles = [candidate.get("title"), candidate.get("anchor_title")]
            if not any(title_matches(value, expected_titles) for value in candidate_titles):
                continue
            candidate_year = str(candidate.get("year") or "")
            if expected_year and candidate_year and candidate_year != expected_year:
                continue
            if media_type == "movie" and expected_year and not candidate_year:
                continue
            candidate_season = next(
                (value for value in map(extract_season, candidate_titles) if value),
                None,
            )
            if season and candidate_season and candidate_season != season:
                continue
            score = 100
            if expected_year and candidate_year == expected_year:
                score += 40
            if season and candidate_season == season:
                score += 60
            ranked.append((score, -index, candidate))
        ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
        return ranked[0][2] if ranked else None

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
                "seed_id": seed_id,
                "title": self._clean_text(title_match.group(1) if title_match else ""),
                "size": self._clean_text(matched.group("size")),
                "updated_at": self._clean_text(matched.group("updated")),
            })
        return entries

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
            self._magnet_cache[seed_id] = magnet
        return magnet

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
                    (item, executor.submit(self._resolve_magnet, item["seed_id"]))
                    for item in batch
                ]
                for item, future in futures:
                    try:
                        magnet = future.result()
                    except SeedHubError:
                        continue
                    key = magnet.casefold()
                    if not magnet or key in seen:
                        continue
                    seen.add(key)
                    title = item.get("title") or f"SeedHub 资源 #{item['seed_id']}"
                    results.append({
                        "url": magnet,
                        "title": title,
                        "size": item.get("size") or 0,
                        "update_time": item.get("updated_at") or "",
                        "resource_type": "magnet",
                        "pan_type": "magnet",
                        "source": "seedhub",
                        "source_service": "seedhub",
                        "source_url": f"{self.base_url}/movies/{movie_id}/",
                        "seed_id": item["seed_id"],
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
                     season, normalized_limit, bool(test_mode))
        lock = self._search_locks[hash(cache_key) % len(self._search_locks)]
        with lock:
            with self._cache_lock:
                cached = self._search_cache.get(cache_key)
            if cached is not None:
                return [dict(item) for item in cached]

            selected = None
            for keyword in normalized_keywords:
                text = self._get_text(f"{self.base_url}/s/{quote(keyword)}/")
                selected = self._select_candidate(
                    self._parse_search_candidates(text),
                    titles,
                    "" if test_mode else year,
                    media_type,
                    None if test_mode else season,
                )
                if selected:
                    break
            if not selected:
                with self._cache_lock:
                    self._search_cache[cache_key] = []
                return []

            movie_id = selected["movie_id"]
            entries = self._parse_entries(
                self._get_text(f"{self.base_url}/movies/{movie_id}/")
            )
            if not test_mode and media_type == "tv" and season:
                matching_entries = [
                    item for item in entries
                    if extract_season(item.get("title")) in (None, season)
                ]
                entries = matching_entries
            results = self._resolve_entries(movie_id, entries, normalized_limit)
            with self._cache_lock:
                self._search_cache[cache_key] = [dict(item) for item in results]
            return results

    def clear_cache(self) -> Dict[str, int]:
        with self._cache_lock:
            counts = {
                "search": len(self._search_cache),
                "magnet": len(self._magnet_cache),
            }
            self._search_cache.clear()
            self._magnet_cache.clear()
        return counts
