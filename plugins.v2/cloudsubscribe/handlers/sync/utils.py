import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote



def extract_ed2k_filename(url: str) -> str:
    """从 ED2K 链接中解析提取原始文件名。"""
    if not url or not str(url).strip().lower().startswith("ed2k://|file|"):
        return ""
    parts = str(url).strip().split("|")
    if len(parts) > 2 and parts[2]:
        return unquote(parts[2]).strip()
    return ""


def candidate_progress(processed: int, total: int, *, start: int, span: int) -> int:
    """候选校验进度：按全局已完成候选线性推进。"""
    processed = max(0, int(processed or 0))
    total = max(processed, int(total or 0))
    if total <= 0:
        return start
    return start + min(span, int(processed / total * span))


# 洗版旧文件备份命名：<原名>-<10 位任务短ID><后缀>
_UPGRADE_BACKUP_RE = re.compile(r"-[A-Za-z0-9]{10}\.[A-Za-z0-9]+$")


def is_upgrade_backup_name(file_name: str) -> bool:
    """判断文件名是否为洗版旧文件备份（<原名>-<10位短ID>.ext）。"""
    return bool(_UPGRADE_BACKUP_RE.search(str(file_name or "")))


def strip_upgrade_backup_suffix(file_name: str) -> str:
    """返回备份名去掉短ID后的原主干；非备份名返回空串。"""
    name = str(file_name or "")
    if not _UPGRADE_BACKUP_RE.search(name):
        return ""
    return re.sub(r"-[A-Za-z0-9]{10}$", "", Path(name).stem)


def normalize_season(value: Any, default: int = 1) -> int:
    """规范化季号：None 或空字符串回退默认季号；0 表示特别篇 S00。"""
    try:
        return max(0, int(default if value is None or value == "" else value))
    except (TypeError, ValueError):
        return default
