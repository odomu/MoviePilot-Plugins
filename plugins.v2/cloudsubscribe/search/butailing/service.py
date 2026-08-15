"""不太灵作品匹配与 Magnet 候选构造。"""

import hashlib
import re
from typing import Any, Dict, Iterable, List, Optional

from app.schemas.types import MediaType

from .client import ButailingClient
from ..magnet import clear_cache, media_titles, normalize_magnets
from ..matching import extract_season, extract_year, title_matches, unique_texts
from ...core.search import SearchQuery


class ButailingSearchService:
    _TITLE_SEPARATOR = re.compile(r"[：:～~]")

    def __init__(self, client: ButailingClient, result_limit: int):
        self._client = client
        self._result_limit = result_limit

    @classmethod
    def _keywords(cls, titles: List[str]) -> List[str]:
        keywords = []
        for title in titles:
            text = str(title or "").strip()
            if not text:
                continue
            short_title = cls._TITLE_SEPARATOR.split(text, maxsplit=1)[0].strip()
            if len(short_title) >= 2 and short_title != text:
                keywords.append(short_title)
            keywords.append(text)
        return unique_texts(keywords)

    @staticmethod
    def _candidate_titles(row: Dict[str, Any]) -> List[str]:
        values = [row.get("title"), row.get("otitle")]
        aliases = str(row.get("alias") or "")
        values.extend(part.strip() for part in aliases.replace("/", ",").split(","))
        return [str(value).strip() for value in values if str(value or "").strip()]

    @classmethod
    def _select_row(
            cls,
            rows: Iterable[Dict[str, Any]],
            expected_titles: List[str],
            expected_year: str,
            media_type: str,
            season: Optional[int],
            douban_id: Optional[object],
            imdb_id: Optional[object] = None,
    ) -> Optional[Dict[str, Any]]:
        target_type = 2 if media_type == "tv" else 1
        expected_douban_id = str(douban_id or "").strip()
        expected_imdb_id = str(imdb_id or "").strip().casefold()
        best_title_match = None
        best_external_id_match = None
        for index, row in enumerate(rows):
            try:
                row_type = int(row.get("type") or 0)
            except (TypeError, ValueError):
                continue
            if row_type != target_type:
                continue
            candidate_titles = cls._candidate_titles(row)
            exact_external_id = bool(
                expected_douban_id
                and expected_douban_id in {
                    str(row.get("doub_id") or "").strip(),
                    str(row.get("idcode") or "").strip(),
                }
            ) or bool(
                expected_imdb_id
                and str(row.get("IMDB_number") or "").strip().casefold()
                == expected_imdb_id
            )
            matched_title = any(
                title_matches(value, expected_titles) for value in candidate_titles
            )
            if not exact_external_id and not matched_title:
                continue
            candidate_season = next(
                (value for value in map(extract_season, candidate_titles) if value),
                None,
            )
            if season and candidate_season and candidate_season != season:
                continue
            if season and season > 1 and candidate_season is None:
                continue
            candidate_year = extract_year(row.get("years") or row.get("release"))
            if media_type == "movie" and expected_year and (
                    not candidate_year or candidate_year != expected_year
            ):
                continue
            score = 1000 if exact_external_id else 100
            if candidate_year and candidate_year == expected_year:
                score += 40
            if season and candidate_season == season:
                score += 100
            elif season and candidate_season is None:
                score += 10
            ranked = (score, -index, row)
            if exact_external_id:
                if (
                        best_external_id_match is None
                        or (score, -index) > best_external_id_match[:2]
                ):
                    best_external_id_match = ranked
            elif best_title_match is None or (score, -index) > best_title_match[:2]:
                best_title_match = ranked
        best = best_external_id_match or best_title_match
        return best[2] if best else None

    def _search(
            self,
            keywords: Iterable[str],
            expected_titles: List[str],
            expected_year: object,
            media_type: str,
            season: Optional[int],
            douban_id: Optional[object],
            imdb_id: Optional[object],
            limit: int,
    ) -> List[Dict[str, Any]]:
        normalized_keywords = unique_texts(keywords)
        expected_douban_id = str(douban_id or "").strip()
        if not expected_douban_id.isdigit() or int(expected_douban_id) <= 0:
            expected_douban_id = ""
        if not expected_douban_id and (
                not expected_titles or not normalized_keywords
        ):
            return []
        year = extract_year(expected_year)
        identity_verified = False
        if expected_douban_id:
            detail = self._client.detail(int(expected_douban_id))
            expected_type = 2 if media_type == "tv" else 1
            try:
                detail_type = int(detail.get("type") or 0)
            except (TypeError, ValueError):
                detail_type = 0
            if not detail or detail_type != expected_type:
                return []
            identity_verified = True
            selected_douban_id = detail.get("doub_id") or expected_douban_id
            selected_title = detail.get("title") or (
                expected_titles[0] if expected_titles else ""
            )
        else:
            selected = None
            for keyword in normalized_keywords:
                selected = self._select_row(
                    self._client.search_rows(keyword), expected_titles, year,
                    media_type, season, None, imdb_id,
                )
                if selected:
                    break
            if not selected or not selected.get("doub_id"):
                return []
            selected_douban_id = selected["doub_id"]
            selected_title = selected.get("title") or expected_titles[0]
            detail = self._client.detail(int(selected_douban_id))
        if not detail:
            return []
        results = []
        seen = set()
        normalized_limit = max(1, min(int(limit or 20), 80))
        for index, seed in enumerate(detail.get("all_seeds") or []):
            if not isinstance(seed, dict):
                continue
            magnet = str(seed.get("zlink") or "").strip()
            key = magnet.casefold()
            if not magnet.lower().startswith("magnet:?") or key in seen:
                continue
            seen.add(key)
            title = str(seed.get("zname") or "").strip()
            results.append({
                "id": "btl-" + hashlib.sha1(
                    magnet.encode("utf-8")
                ).hexdigest()[:16],
                "url": magnet,
                "title": title or f"{selected_title} - 磁力资源 #{index + 1}",
                "size": str(seed.get("zsize") or "").strip() or 0,
                "quality": str(seed.get("zqxd") or "").strip(),
                "resource_type": "magnet",
                "source_url": f"https://web5.mukaku.com/mv/{selected_douban_id}",
                "provider_data": {"douban_id": int(selected_douban_id)},
                "identity_verified": identity_verified,
                "target_season": (
                    int(season) if identity_verified and season else None
                ),
            })
            if len(results) >= normalized_limit:
                break
        return results

    def search(self, query: SearchQuery):
        mediainfo = query.mediainfo
        subscribe = query.subscribe
        titles = media_titles(mediainfo)
        resources = self._search(
            keywords=self._keywords(titles),
            expected_titles=titles,
            expected_year=getattr(mediainfo, "year", None),
            media_type=(
                "tv" if query.media_type == MediaType.TV else "movie"
            ),
            season=query.season,
            douban_id=(
                    getattr(mediainfo, "douban_id", None)
                    or getattr(subscribe, "doubanid", None)
            ),
            imdb_id=(
                    getattr(mediainfo, "imdb_id", None)
                    or getattr(subscribe, "imdbid", None)
            ),
            limit=(
                query.result_limit
                if query.result_limit is not None else self._result_limit
            ),
        )
        return normalize_magnets(resources, "butailing")

    def clear_cache(self) -> int:
        return clear_cache(self._client)
