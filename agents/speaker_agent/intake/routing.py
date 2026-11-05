"""项目名称匹配；只在发现的项目中选择唯一目标，不由模型生成目标 ID。"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from shared.feishu.catalog import TableReference


def normalized_name(value: str) -> str:
    return re.sub(r"[\s_-]+", "", unicodedata.normalize("NFKC", value).casefold())


def match_projects(text: str, refs: list[TableReference], *, exact: bool = False) -> list[TableReference]:
    normalized = normalized_name(text)
    matches = [ref for ref in refs if normalized_name(ref.title) and
               (normalized_name(ref.title) == normalized if exact else normalized_name(ref.title) in normalized)]
    if matches or exact:
        return matches
    # 只容忍较长英文项目名的小幅拼写错误；候选接近或重复时继续追问。
    words = [normalized_name(word) for word in re.findall(r"[A-Za-z][A-Za-z0-9 _-]*", text)]
    scores = []
    for ref in refs:
        name = normalized_name(ref.title)
        if len(name) < 8 or not name.isascii():
            continue
        score = max((SequenceMatcher(None, name, word).ratio() for word in words
                     if abs(len(name) - len(word)) <= 2), default=0)
        if score >= 0.85:
            scores.append((score, ref))
    scores.sort(key=lambda entry: entry[0], reverse=True)
    if not scores:
        return []
    return [ref for score, ref in scores if scores[0][0] - score < 0.08]
