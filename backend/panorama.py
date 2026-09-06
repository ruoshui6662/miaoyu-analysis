"""单次分析的检索范围判定（PANORAMA-1）。

这一层只回答“是否需要扩展检索视角”，不执行搜索，也不改变现有 Provider
顺序。这样可以先用纯规则和离线测试校准误触发/漏触发，再接入查询规划与补漏。
"""

from __future__ import annotations

import re
from typing import Iterable


SEARCH_MODES = frozenset({"auto", "domestic", "panorama"})

_STRONG_FOREIGN = {
    "美国", "英国", "法国", "德国", "日本", "韩国", "俄罗斯", "乌克兰", "印度",
    "澳大利亚", "加拿大", "欧盟", "北约", "联合国", "世卫组织", "世界卫生组织",
    "美国政府", "英国政府", "日本政府", "美国监管", "海外监管", "境外监管",
    "openai", "google", "microsoft", "apple", "tesla", "meta", "nvidia",
}
_CROSS_BORDER = {
    "跨境", "国际", "外交", "制裁", "关税", "出口管制", "进口限制", "供应链",
    "国际贸易", "外贸", "海外市场", "境外市场", "海外业务", "外资", "汇率",
    "地缘政治", "国际组织", "境外回应", "国际传播", "跨国", "全球市场",
}
_PROFESSIONAL_HIGH_IMPACT = {
    "战争", "冲突", "制裁", "外交", "核", "导弹", "出口管制", "金融制裁",
    "跨境安全", "全球供应链", "国际公共卫生",
}
_DOMESTIC_HINTS = {
    "中国", "国内", "国内企业", "中国企业", "本地", "地方", "县", "区政府", "市政府", "省政府", "街道", "社区",
    "校园", "小区", "物业", "本土消费",
}


def _text(topic: str, keywords: Iterable[str] | None = None) -> str:
    values = [topic or ""]
    values.extend(str(item or "") for item in (keywords or []))
    return " ".join(values).strip().casefold()


def _hits(text: str, terms: set[str]) -> list[str]:
    return sorted(term for term in terms if term.casefold() in text)


def _has_latin_entity(text: str) -> bool:
    return bool(re.search(r"(?<![a-z])[a-z][a-z0-9.-]{2,}(?![a-z])", text))


def classify_scope(
    topic: str,
    requested_mode: str = "auto",
    keywords: Iterable[str] | None = None,
) -> dict:
    """返回可解释的检索范围决定，不执行检索。"""
    mode = str(requested_mode or "auto").strip().casefold()
    if mode not in SEARCH_MODES:
        mode = "auto"
    value = _text(topic, keywords)
    foreign = _hits(value, _STRONG_FOREIGN)
    cross = _hits(value, _CROSS_BORDER)
    domestic = _hits(value, _DOMESTIC_HINTS)
    high_impact = _hits(value, _PROFESSIONAL_HIGH_IMPACT)
    latin_entity = _has_latin_entity(value)

    score = 0.0
    reasons: list[dict] = []
    if foreign:
        score += 0.55
        reasons.append({"code": "foreign_entity", "label": "出现境外国家、机构或实体", "signals": foreign})
    if cross:
        score += 0.45
        reasons.append({"code": "cross_border_topic", "label": "出现跨境或国际议题信号", "signals": cross})
    if latin_entity:
        score += 0.20
        reasons.append({"code": "foreign_name_signal", "label": "出现外文实体线索", "signals": []})
    if high_impact:
        score += 0.15
        reasons.append({"code": "high_impact", "label": "涉及需要提高覆盖优先级的高影响议题", "signals": high_impact})
    score = round(min(score, 0.99), 2)

    mixed_domestic = bool(domestic) and bool(foreign or cross)
    if foreign and mixed_domestic:
        inferred = "cross_border"
    elif foreign or cross:
        inferred = "international"
    elif score >= 0.40:
        inferred = "unknown"
    else:
        inferred = "domestic"

    if mode == "domestic":
        decision, trigger = "domestic", "user_override"
    elif mode == "panorama":
        decision, trigger = "panorama", "user_override"
    elif score >= 0.75:
        decision, trigger = "panorama", "strong_signal"
    elif score >= 0.40:
        decision, trigger = "first_pass_then_gap_check", "medium_signal"
    else:
        decision, trigger = "domestic", "low_signal"

    if not reasons:
        reasons.append({"code": "no_foreign_signal", "label": "未发现明确跨境信号", "signals": []})
    return {
        "requested_mode": mode,
        "scope_type": inferred,
        "decision": decision,
        "confidence": score,
        "trigger": trigger,
        "reasons": reasons,
        "requires_gap_check": decision == "first_pass_then_gap_check",
        "high_impact": bool(high_impact),
        "version": "PANORAMA-1-rule-v1",
    }
