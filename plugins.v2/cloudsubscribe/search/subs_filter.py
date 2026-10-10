"""动漫资源公共处理模块。

集中处理 Mikan、AnimeGarden 等动漫 BT 资源站点的集数解析、合集判定、
字幕组优先级评分、中文字幕过滤以及各渠道独立的排除规则。
"""

import re
from typing import Any, Dict, Iterable, List, Optional

from .matching import extract_season, title_matches, title_without_season

DEFAULT_FANSUB_ORDER = (
    r"LoliHouse", r"VCB-Studio", r"喵萌奶茶|Nekomoe", r"Nix-Raws", r"\bANI\b|ANi",
    r"SweetSub", r"千夏",
    r"动漫国|動漫國|DMG", r"极影|極影|KTXP", r"桜都|樱都|櫻都|Sakurato", r"诸神|諸神|Kamigami",
    r"北宇治|Kitauji", r"悠哈璃羽|UHA-WINGS", r"爱恋字幕|愛戀字幕|KissSub", r"拨雪寻春|撥雪尋春",
    r"\bHaru[ &]*Hana\b", r"澄空|Sumisora", r"华盟|華盟|CASO", r"霜庭云花|霜庭雲花|STYH",
    r"豌豆|Dymy", r"Airota", r"Lilith-Raws", r"DBD制作组|DBD-Raws",
    r"\bNC-Raws\b", r"雪飘|FLsnow", r"幻樱|HYSub",
)

# 默认无字幕/生肉排除正则
DEFAULT_NO_SUBS_RE = r"无字幕|無字幕|无字版|無字版|生肉|\b(?:unsubbed|no[ ._-]*subs?|subtitle[ ._-]*free)\b"

# 默认中文字幕匹配正则
DEFAULT_CHINESE_RE = r"简[体體繁中]|簡[体體繁中]|繁[体體简簡中]|中[日英双雙文]|[简簡繁]日|\b(?:CHS|CHT|BIG5|GB|SC|TC|ZH|CHI|ZHO)(?:\b|_)"

# 默认动漫排除规则（过滤 720p、480p、特别篇、集数跨度等低质量或不需要的内容）
DEFAULT_ANIME_EXCLUDE_RE = r"720[pP]|480[pP]|特别篇|特別篇|\b(?:SP|OVA|OAD)\d*|\b\d+\s*-\s*\d+\b"

_TECHNICAL_TAG_RE = re.compile(
    r"^(?:4K|2160[pP]|1080[pP]|720[pP]|480[pP]|WEB(?:-?DL|-?Rip)?|BD(?:-?Rip)?|BDRip|MKV|MP4|HEVC.*|AVC.*|x264.*|x265.*|H\.?264.*|H\.?265.*|\d+bit.*|AAC.*|FLAC.*|RAW.*|\d{1,4}|\d{4}|S\d+|EP?\d+)$",
    re.I,
)


def extract_fansub_from_title(title: str) -> Optional[str]:
    """从动漫资源标题前缀方括号中提取字幕组名称（公共方法）。"""
    if not title:
        return None
    brackets = re.findall(r"\[([^\]]+)\]|【([^】]+)】", str(title).strip())
    candidates = [b[0] or b[1] for b in brackets]
    for name in candidates:
        cleaned = name.strip()
        if not cleaned:
            continue
        if _TECHNICAL_TAG_RE.match(cleaned):
            continue
        return cleaned
    return None


def is_special_release(title: str) -> bool:
    """判定是否为特别篇、SP、OVA 等非正片季度资源。"""
    return bool(
        re.search(
            r"(?<![A-Za-z0-9])(?:OVA|OAD|SP|EX)(?=[\W_\d]|$)|S00E|特别篇|特別篇",
            str(title or ""),
            re.I,
        )
    )


def special_episodes(title: str) -> List[int]:
    """提取 S00 特别篇集数。"""
    found = set()
    for match in re.finditer(
            r"(?<![A-Za-z0-9])(?:S00E|OVA|OAD|SP|EX)[ ._-]*0*(\d{1,3})|(?:特别篇|特別篇)[ ._-]*0*(\d{1,3})",
            str(title or ""),
            re.I,
    ):
        ep = match.group(1) or match.group(2)
        if ep:
            found.add(int(ep))
    return sorted(found)


def release_season_matches(title: str, season: Optional[int]) -> bool:
    """判断资源标题是否属于当前目标季度（严防正片与特别篇互相误选）。"""
    if season == 0:
        return bool(special_episodes(title))
    return not is_special_release(title)


def anime_is_excluded(
        title: str,
        config: Optional[Dict[str, Any]] = None,
        prefix: str = "mikan",
        exclude_re: Optional[Any] = None,
) -> bool:
    """检查标题是否匹配渠道独立的动漫排除规则。

    :param title: 资源标题
    :param config: 渠道配置字典（兼容旧格式）
    :param prefix: 渠道配置项前缀，如 'mikan' 或 'animegarden'
    :param exclude_re: 直接传入的排除正则（优先于 config）
    """
    if not title:
        return False
    cfg = config or {}
    target_season = cfg.get("_target_season")
    if target_season is not None and target_season != 0 and is_special_release(title):
        return True
    pattern = (
        exclude_re
        if exclude_re is not None
        else (
                cfg.get(f"{prefix}_exclude_re")
                or cfg.get(f"{prefix}_filter_exclude")
                or cfg.get("anime_exclude_re")
                or DEFAULT_ANIME_EXCLUDE_RE
        )
    )
    if target_season == 0 and pattern == DEFAULT_ANIME_EXCLUDE_RE:
        pattern = r"720[pP]|480[pP]"
    # 若是合法完结合集/打包资源，剥离其自身的合集范围标签（如 [01-12 合集]），避免被 \d-\d 等跨度规则误排除
    check_title = title
    if is_pack_release(title):
        check_title = re.sub(
            r"\[(?:EP|E)?\s*0*\d{1,3}\s*[-~～–—至到]\s*(?:EP|E)?\s*0*\d{1,3}(?:v\d)?"
            r"(?:\s*(?:合集|全集|Fin|End|完|话|話|集|\+SP|\+OVA))?[^\]]*\]",
            "",
            check_title,
            flags=re.I,
        )

    if isinstance(pattern, (list, tuple)):
        for item in pattern:
            s = str(item or "").strip()
            if s and re.search(s, check_title, re.I):
                return True
        return False
    pattern_str = str(pattern or "").strip()
    if pattern_str and re.search(pattern_str, check_title, re.I):
        return True
    return False


def fansub_priority(
        title: str,
        config: Optional[Dict[str, Any]] = None,
        prefix: str = "mikan",
        fansub_order: Optional[Any] = None,
        exclude_re: Optional[Any] = None,
        no_subs_re: Optional[str] = None,
        chinese_re: Optional[str] = None,
) -> Optional[int]:
    """计算资源的字幕组优先级分数。

    返回 None 表示拒绝（无字幕/生肉/非中文字幕/命中排除规则）；整数值越大优先级越高。
    选择的顺序即优先级：排在前面的字幕组分数更高，且支持各渠道单独配置过滤。
    """
    title = str(title or "")
    cfg = config or {}

    # 1. 优先检查渠道排除规则（如 720p、特别篇等）
    if anime_is_excluded(title, config=cfg, prefix=prefix, exclude_re=exclude_re):
        return None

    # 2. 检查生肉与无字幕
    exclude_pattern = (
        no_subs_re
        if no_subs_re is not None
        else (
                cfg.get(f"{prefix}_no_subs_re")
                or cfg.get(f"{prefix}_fansub_exclude")
                or cfg.get("mikan_no_subs_re")
                or DEFAULT_NO_SUBS_RE
        )
    )
    chinese_pattern = (
        chinese_re
        if chinese_re is not None
        else (
                cfg.get(f"{prefix}_chinese_re")
                or cfg.get("mikan_chinese_re")
                or DEFAULT_CHINESE_RE
        )
    )
    if (exclude_pattern and re.search(exclude_pattern, title, re.I)) or (
            chinese_pattern and not re.search(chinese_pattern, title, re.I)
    ):
        return None

    tags = " ".join(
        left or right
        for left, right in re.findall(r"\[([^\]]+)\]|【([^】]+)】", title)
    )

    # 3. 优先读取渠道配置的优先级顺序列表（选择的顺序就是优先级）
    raw_order = (
        fansub_order
        if fansub_order is not None
        else (
                cfg.get(f"{prefix}_fansub_order")
                or cfg.get(f"{prefix}_fansub_priority")
                or cfg.get(f"{prefix}_fansubs")
                or cfg.get("mikan_fansub_order")
        )
    )
    if raw_order and isinstance(raw_order, (list, tuple)):
        fansub_list = []
        for item in raw_order:
            if isinstance(item, dict):
                val = str(item.get("value") or item.get("title") or "").strip()
            else:
                val = str(item or "").strip()
            if val:
                fansub_list.append(val)
        for index, pattern in enumerate(fansub_list):
            if re.search(pattern, tags, re.I) or re.search(pattern, title, re.I):
                return max(100, 1000 - index * 10)

    # 5. 默认常用字幕组顺序匹配
    for index, pattern in enumerate(DEFAULT_FANSUB_ORDER):
        if re.search(pattern, tags, re.I) or re.search(pattern, title, re.I):
            return max(100, 1000 - index * 10)

    # 6. 其他具名中文字幕组兜底
    if re.search(r"[^\s\[\]]{2,}(?:字幕组|字幕組|字幕社|字幕屋)", tags):
        return 50
    return None


def filter_fansubs(
        resources: List[Dict[str, Any]],
        config: Optional[Dict[str, Any]] = None,
        prefix: str = "mikan",
        strict: bool = True,
        fansub_order: Optional[Any] = None,
        exclude_re: Optional[Any] = None,
        no_subs_re: Optional[str] = None,
        chinese_re: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """按字幕组偏好策略与渠道排除规则过滤候选资源，并附加 fansub_priority 权重。

    :param strict: 为 True 时（自动下载），严格过滤排除项与生肉；为 False 时（资源列表/测试），不丢弃资源，仅标记权重排序。
    """
    accepted = []
    for resource in resources:
        priority = fansub_priority(
            resource.get("title") or resource.get("name"),
            config=config,
            prefix=prefix,
            fansub_order=fansub_order,
            exclude_re=exclude_re,
            no_subs_re=no_subs_re,
            chinese_re=chinese_re,
        )
        if priority is not None:
            accepted.append({**resource, "fansub_priority": priority})
        elif not strict:
            accepted.append({**resource, "fansub_priority": 10})
    return sorted(accepted, key=lambda item: -item["fansub_priority"])


def release_titles(title: str) -> List[str]:
    text = str(title or "").strip()
    clean = re.sub(r"^(?:\s*\[[^\]]*\]\s*)+", "", text)
    clean = re.sub(r"\.(?:mkv|mp4|avi|ts|m2ts)$", "", clean, flags=re.I)
    clean = re.split(r"\s*\[|\s+-\s+\d|\s+\(\d{2}", clean, maxsplit=1)[0]
    if clean.strip():
        return [part.strip() for part in clean.split(" / ") if part.strip()]
    names = []
    for tag in re.findall(r"\[([^\]]+)\]", text)[1:]:
        tag = tag.strip()
        if not tag or _TECHNICAL_TAG_RE.fullmatch(tag):
            continue
        if re.search(r"新番|字幕|汉化|漢化|简中|繁中|简体|繁体|內嵌|内嵌|内封|內封", tag):
            continue
        if re.fullmatch(r"(?:CHS|CHT|BIG5|GB|SC|TC|ZH|CHI|ZHO|JPN|ENG)(?:[&+ /].*)?|\d+(?:v\d+)?|\d+[-~～]\d+.*", tag,
                        re.I):
            continue
        names.extend(part.strip() for part in tag.split(" / ") if part.strip())
    return names


def release_matches(
        title: str,
        expected: List[str],
        season: Optional[int],
        fuzzy: bool = True,
) -> bool:
    """验证资源标题是否与预期媒体标题及季度匹配。"""
    explicit_season = extract_season(title)
    if explicit_season and season and explicit_season != season:
        return False
    if season and season > 1 and not explicit_season:
        return False
    parts = release_titles(title)
    if any(title_matches(part, expected) for part in parts):
        return True

    # 弹性别名与子串匹配（解决前缀如 Re:、篇章副标题如 冰结之绊/袭来篇、或者用户输入核心词匹配完整片名）
    if fuzzy and expected:
        for exp in expected:
            exp_clean = title_without_season(exp)
            if not exp_clean or len(exp_clean) < 2:
                continue
            for part in parts:
                part_clean = title_without_season(part)
                if exp_clean in part_clean or part_clean in exp_clean:
                    return True
    return False


def release_episodes(title: str, season: Optional[int] = None) -> List[int]:
    """从标题中提取显式集数，严格排除分辨率、年份等伪集数标签，支持完结合集与范围。"""
    title_str = str(title or "")
    if season == 0:
        return special_episodes(title_str)
    if is_special_release(title_str):
        return []
    range_matches = re.findall(
        r"\[(?:EP|E)?\s*0*(\d{1,3})\s*[-~～–—至到]\s*(?:EP|E)?\s*0*(\d{1,3})(?:v\d)?"
        r"(?:\s*(?:合集|全集|Fin|End|完|话|話|集|\+SP|\+OVA))?[^\]]*\]",
        title_str,
        re.I,
    )
    episodes = set()
    for start, end in range_matches:
        first = int(start)
        last = int(end or start)
        if 0 < first <= last <= 999 and (last - first) <= 200:
            episodes.update(range(first, last + 1))

    # 2. 匹配 [全12话], - 全12话, (全12集) 形式
    if not episodes:
        all_matches = re.findall(
            r"(?:\[|[\s\-_(（])全\s*0*(\d{1,3})\s*[话話集](?:\]|[\s\-_)）]|$)",
            title_str,
            re.I,
        )
        for total in all_matches:
            val = int(total)
            if 0 < val <= 200:
                episodes.update(range(1, val + 1))

    # 3. 匹配普通单集/双集标签如 [01], [01-02], [EP01]
    if not episodes:
        matches = re.findall(r"\[(?:EP|E)?\s*0*(\d{1,2})(?:\s*[-~]\s*0*(\d{1,2}))?(?:v\d)?\]", title_str, re.I)
        if not matches:
            matches = re.findall(r"\s-\s(\d{1,2})(?:v\d)?(?=\s|\[|$)", title_str, re.I)
            matches = [(number, "") for number in matches]
        for start, end in matches:
            first = int(start)
            last = int(end or start)
            if 0 < first <= last <= 99:
                episodes.update(range(first, last + 1))
    return sorted(episodes)


def is_pack_release(title: str, episodes: Optional[List[int]] = None) -> bool:
    """判定是否为动漫完结合集或批量资源包。"""
    if episodes and len(episodes) > 1:
        return True
    title_str = str(title or "")
    if re.search(r"合集|全集|全\s*\d+\s*[话話集]|\bComplete\b|\bPack\b|\[0*\d+\s*[-~～–—至到]\s*0*\d+[^\]]*\]",
                 title_str, re.I):
        return True
    return False


def anime_file_candidates(
        files: List[Dict[str, Any]],
        release_title: str,
        season: int,
        targets: Iterable[int],
        config: Optional[Dict[str, Any]] = None,
        prefix: str = "mikan",
) -> Dict[int, List[Dict[str, Any]]]:
    """利用已匹配发布的双语名称精确匹配文件候选。"""
    aliases = release_titles(release_title)
    aliases += [
        re.sub(r"\s+The Animation$", "", name, flags=re.I)
        for name in list(aliases)
        if re.search(r"\s+The Animation$", name, re.I)
    ]
    candidates: Dict[int, List[Dict[str, Any]]] = {
        int(episode): [] for episode in targets if int(episode) > 0
    }
    for file in files:
        name = str(file.get("name") or "")
        episodes = release_episodes(name)
        if (
                len(episodes) != 1
                or episodes[0] not in candidates
                or fansub_priority(name, config=config, prefix=prefix) is None
                or not release_matches(name, aliases, season)
        ):
            continue
        candidates[episodes[0]].append(file)
    return candidates


def build_anime_candidate(
        title: str,
        magnet: str,
        size: int,
        source: str,
        season: int = 1,
        fansub: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """统一构建动漫磁力候选资源字典。"""
    title_str = str(title or "").strip()
    magnet_str = str(magnet or "").strip()
    episodes = release_episodes(title_str)
    candidate = {
        "title": title_str,
        "name": title_str,
        "url": magnet_str,
        "share_url": magnet_str,
        "magnet": magnet_str,
        "size": int(size or 0),
        "resource_type": "magnet",
        "source": str(source or "").strip(),
        "fansub": fansub,
        "season": int(season or 1),
        "episodes": episodes,
        "preview_episodes": {str(season or 1): episodes},
        "is_pack": is_pack_release(title_str, episodes),
    }
    if extra:
        candidate.update(extra)
    return candidate
