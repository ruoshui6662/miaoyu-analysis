# -*- coding: utf-8 -*-
"""四角色 AI 分析 prompt 模板：事实整理 → 归因 → 风险 → 对策（对齐《参考格式.docx》四段式）。

所有角色均为 JSON 输出（json_mode），输出结构直接可写入 report.json。
"""
from __future__ import annotations

import json

SYSTEM_BASE = (
    "你是资深舆情分析师，擅长从原始网络素材中提炼事实、归纳原因、识别风险、给出对策。"
    "你只依据素材中真实存在的信息作答；素材中没有的依据一带而过或明确标注“（素材缺失）”。"
    "禁止编造事件、数字、引文或来源。输出必须是合法 JSON。"
    "\n写作规范：所有事件、数字、观点均须在行文中注明来源媒体（如“据人民日报报道”“澎湃新闻指出”）；"
    "素材提供了 source_name 的用其媒体名，未提供时写“据公开报道”，绝不虚构媒体名。"
    "\n事实约束：时间、地点、数字、事件等事实性信息只能取自高可信度（官方/权威媒体）素材；"
    "若某事实仅存在于中低可信度素材（自媒体、网友、短视频等），必须明确标注其来源与局限，"
    "如“据微信公众号文章（未经权威媒体证实）”“据网友爆料”，"
    "严禁把自媒体信息表述为权威媒体报道，严禁把传闻写成既定事实。"
)


def expand_keywords_prompt(topic: str) -> list[dict]:
    return [
        {"role": "system", "content": (
            "你是舆情监测策划师。根据给定主题，输出覆盖性搜索关键词清单，用于搜索引擎采集。"
            "关键词应覆盖：核心词、同义/别称、关联人物或机构、事件发展（起因/过程/回应/争议/结果）、观点词（质疑/回应/看法）。"
            '只输出 JSON：{"keywords": ["..."至少8个关键词]}'
        )},
        {"role": "user", "content": f"舆情主题：{topic}"},
    ]


def _materials_block(items: list[dict], limit: int = 20, body_chars: int = 400) -> str:
    """把素材压缩成文本块给模型，含媒体名与可信度。"""
    lines = []
    for i, it in enumerate(items[:limit], 1):
        body = (it.get("body") or it.get("snippet") or "")[:body_chars].replace("\n", " ")
        media = it.get("source_name") or "（未知媒体）"
        lines.append(
            f"[{i}] 标题：{it['title']}\n    媒体：{media}　来源：{it.get('url', '')}（可信度：{it.get('credibility', '')}，"
            f"发布时间：{it.get('published', '') or '未知'}）\n    内容：{body}"
        )
    return "\n\n".join(lines)


def facts_prompt(topic: str, items: list[dict]) -> list[dict]:
    return [
        {"role": "system", "content": (
            SYSTEM_BASE
            + "\n任务：整理事件梗概（引言）、时间线，并输出情感统计与代表性观点。"
            "每条 timeline 需判断该事件是否被 ≥2 个独立来源/媒体提及："
            "是则 cross_checked=true 并列出涉及媒体到 sources，否则 cross_checked=false 且 sources 留空列表。"
            '只输出 JSON：{"intro": "一段事件背景概括（100-200字，交代发展脉络与当前状态）",'
            ' "timeline": [{"date": "2025年7月29日", "event": "（据某媒体/署名报道，谁/何时/何地/发生了什么，80-150字，行文须带媒体名与事实细节，注明观点分歧及各方立场）", "cross_checked": true, "sources": ["媒体A", "媒体B"]}],'
            ' "emotion": {"positive": 40, "negative": 25, "neutral": 35}（百分比整数，三项合计=100，基于素材整体舆论倾向评估）,'
            ' "quotes": [{"stance": "positive或negative或neutral", "text": "代表性观点（20-60字，压缩转述但保留原意与语气）", "source": "素材媒体名", "url": "素材原文链接"}]（3-6条，尽量覆盖正/负/中立三种立场，优先取自 high/mid 可信度素材，禁止编造）}'
            "时间线按时间正序，3-8条；日期不确定的用“近期/近日”；同一日期的合并。"
        )},
        {"role": "user", "content": f"舆情主题：{topic}\n\n以下是采集到的素材：\n\n{_materials_block(items)}"},
    ]


def chapter_prompt(topic: str, chapter: str, items: list[dict], facts_summary: str,
                   risk_points: list[dict] | None = None) -> list[dict]:
    """归因 / 风险 / 对策共用模板。chapter: causes|risks|advice

    risk_points: 仅对策（advice）传入，为 risks 输出的风险点（带 id），
    对策须逐条对应这些风险，禁止提出清单之外的新议题。
    """
    spec = {
        "causes": (
            '任务：分析“{t}”现象形成的原因。输出 JSON：{"points": [{"title": "（一）四字短语小标题。", "body": "200-300字论述"}]}'
            "2-4个分论点，每个论点从社会背景、经济/制度/心理等多角度展开，论点之间不重复。"
        ),
        "risks": (
            '任务：分析“{t}”存在的风险。输出 JSON：{"points": [{"id": "r1", "title": "（一）四字短语小标题。", "body": "200-300字论述"}]}'
            "2-4个风险点，id 从 r1 依次编号；从法律合规、社会影响、商业经济、公众信任等维度展开，"
            "每条风险需说明其成因与后果，并注明依据媒体（源自素材事实，不得编造）。"
        ),
        "advice": (
            '任务：针对“{t}”提出对策建议。输出 JSON：{"points": [{"title": "两字短语。", "body": "200-300字论述", "for_id": "r1"}]}'
            "2-4条建议，覆盖政府监管/平台自律/社会引导/个体应对等主体。"
            "硬性要求：对策必须逐条对应你收到的《风险清单》，每条对策的 for_id 指向其针对的风险点 id，"
            "每个风险点至少有一条对策；body 开头必须点明所针对的风险（如“针对上述‘虚构合同关系’风险：……”）。"
            "严禁提出风险清单之外的新议题或泛泛之谈；若素材不足以支撑某对策，说明依据媒体即可，不得编造做法与案例。"
        ),
    }[chapter].replace("{t}", topic)

    if chapter == "advice" and risk_points:
        spec += (
            "\n\n《风险清单》（你必须逐条回应的风险）："
            + json.dumps([{"id": p.get("id"), "title": p.get("title", ""), "body": (p.get("body") or "")[:120]}
                          for p in risk_points], ensure_ascii=False)
        )

    return [
        {"role": "system", "content": SYSTEM_BASE + "\n" + spec},
        {
            "role": "user",
            "content": (
                f"舆情主题：{topic}\n\n事件概况参考：\n{facts_summary}\n\n素材：\n\n{_materials_block(items)}"
                "\n\n（若素材不足支撑某论点，请基于素材内出现的事实展开，不要虚构。）"
            ),
        },
    ]


def combined_prompt(topic: str, items: list[dict], facts_summary: str = "") -> list[dict]:
    """原因+风险 合并一次生成（减少调用次数；对策仍单独生成以保持对应关系）。

    输出结构：{"causes": [{"title": "...", "body": "..."}], "risks": [{"id": "r1", "title": "...", "body": "..."}]}
    """
    spec = (
        '任务：一次完成“{t}”的两个分析，输出 JSON：\n'
        '{"causes": [{"title": "（一）四字短语小标题。", "body": "200-300字论述"}],\n'
        ' "risks": [{"id": "r1", "title": "（一）四字短语小标题。", "body": "200-300字论述"}]}\n'
        "causes 2-4个原因点（社会背景/经济/制度/心理等多角度，不重复）；"
        "risks 2-4个风险点，id 从 r1 依次编号（法律合规/社会影响/商业经济/公众信任等维度，说明成因后果并注明依据媒体）。"
    ).replace("{t}", topic)
    return [
        {"role": "system", "content": SYSTEM_BASE + "\n" + spec},
        {"role": "user", "content": (
            f"舆情主题：{topic}\n\n事件概况参考：\n{facts_summary}\n\n素材：\n\n{_materials_block(items)}"
            "\n\n（若素材不足支撑某论点，请基于素材内出现的事实展开，不要虚构。）"
        )},
    ]


def verify_prompt(topic: str, report_text: str, items: list[dict]) -> list[dict]:
    return [
        {"role": "system", "content": (
            SYSTEM_BASE
            + "\n任务：事实核查。逐条检查报告中每个具体论断（事件、日期、数字、人物、机构、引文）是否能在素材中找到依据。"
            '输出 JSON：{"issues": [{"claim": "报告中的原句片段", "ok": true/false, "note": "有依据/无依据说明"}]}'
            "无依据的标 ok=false。不要修改文本，只检查。"
        )},
        {"role": "user", "content": f"舆情主题：{topic}\n\n报告全文：\n{report_text}\n\n素材：\n\n{_materials_block(items, limit=30, body_chars=200)}"},
    ]