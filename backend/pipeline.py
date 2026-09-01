# -*- coding: utf-8 -*-
"""舆情分析流水线编排：关键词扩展 → 多信源组采集 → AI 四角色分析 → report.json → docx。

报告严格对齐《参考格式.docx》四段式结构（事件概况/原因分析/风险分析/对策建议）。

用法：
    python pipeline.py "假装上班"                       # 采集+分析+生成 docx
    python pipeline.py "假装上班" --provider qwen       # 指定 provider（deepseek/qwen/router）
    python pipeline.py "假装上班" --verify              # 开启校验轮（成本更高）
    python pipeline.py "假装上班" --collect-only        # 只做采集，不调 AI
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

from ai_client import AIClient, AIClientError
from ai_prompts import chapter_prompt, combined_prompt, expand_keywords_prompt, facts_prompt
from collector import collect_topic
from config import DATA_DIR_REPORTS, DATA_DIR_TASKS, ROOT


# ---------- 关键词扩展 ----------

def rule_keywords(topic: str) -> list[str]:
    """无 AI 时的规则降级关键词。"""
    kws = [topic, f"{topic}舆情", f"{topic}事件", f"{topic}最新", f"{topic}争议",
           f"{topic}回应", f"{topic}为什么", f"{topic}处理", f"{topic}评论"]
    seen, out = set(), []
    for k in kws:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def expand_keywords(topic: str, ai: AIClient | None, provider: str | None) -> list[str]:
    if ai is None:
        return rule_keywords(topic)
    try:
        data = ai.chat_json(expand_keywords_prompt(topic), provider=provider, temperature=0.4, max_tokens=1200)
        kws = [str(k).strip() for k in data.get("keywords", []) if str(k).strip()]
        if len(kws) >= 3:
            # 主题词前置 + 去重
            return list(dict.fromkeys([topic, *kws]))[:12]
    except AIClientError as e:
        print(f"[警告] AI 关键词扩展失败，使用规则关键词: {e}")
    return rule_keywords(topic)


# ---------- AI 分析（四角色） ----------

def _facts_section(topic: str, materials: list[dict], ai: AIClient, provider: str | None,
                   on_chunk: callable | None = None):
    """事实整理（事件概况）。失败/空响应时降级重试：素材减半 + 输出预算降档。"""
    last_err: Exception | None = None
    for idx, (items, mt) in enumerate(((materials, 8000), (materials[:10], 5000)), 1):
        try:
            data = ai.chat_json(facts_prompt(topic, items), provider=provider, temperature=0.3,
                                max_tokens=mt, on_chunk=on_chunk)
            intro = str(data.get("intro", "")).strip()
            timeline = data.get("timeline") or []
            paragraphs = []
            for tt in timeline:
                date = str(tt.get("date", "")).strip()
                event = str(tt.get("event", "")).strip()
                if not event:
                    continue
                # D2：多源交叉验证标记（docx/md/前端通吃）
                cross = tt.get("cross_checked")
                if cross:
                    mark = "（▲多源交叉验证）"
                else:
                    mark = "（◐单源，待核）"
                text = f"{date}，{event}{mark}" if date else f"{event}{mark}"
                paragraphs.append(text)
            if intro or paragraphs:
                return intro, paragraphs, {
                    "emotion": data.get("emotion") or {},
                    "viewpoints": {
                        "media": data.get("media_quotes") or [],
                        "netizen": data.get("netizen_quotes") or [],
                    },
                    "quotes": data.get("quotes") or [],  # 兜底：无 media/netizen 时的通用观点
                }
            last_err = AIClientError("AI 未返回有效时间线内容")
        except AIClientError as e:
            last_err = e
            if "缺少 API key" in str(e) or "未配置" in str(e):
                raise  # 配置类错误不重试
            print(f"       ⚠ 事实整理第{idx}档失败: {e}")
    raise last_err or AIClientError("事实整理失败")


def _chapter_section(topic, chapter, materials, facts_summary, ai, provider,
                     on_chunk: callable | None = None, risk_points: list[dict] | None = None):
    data = ai.chat_json(chapter_prompt(topic, chapter, materials, facts_summary, risk_points=risk_points),
                        provider=provider, temperature=0.4, max_tokens=6000, on_chunk=on_chunk)
    points = data.get("points") or []
    paragraphs = []
    for p in points:
        title = str(p.get("title", "")).strip()
        body = str(p.get("body", "")).strip()
        if not title or not body:
            continue
        # 标题需以句号收尾（模板风格"（一）四字短语。"），没有则补
        if not title.endswith(("。", "！", "？")):
            title += "。"
        paragraphs.append({"lead": title, "body": body})
    return paragraphs


def _placeholder_section(reason: str) -> list[dict]:
    return [{"lead": "待分析。", "body": f"本环节暂未生成（原因：{reason}）。请配置 AI 接口后重跑。"}]


# ---------- docx 生成 ----------

def render_markdown(report: dict) -> str:
    """把报告渲染为 Markdown（标题/引言/章节/段落，{lead,body} 段落句首加粗）。"""
    lines = [f"# {report.get('title', '')}", ""]
    if report.get("intro"):
        lines += [report["intro"], ""]
    for sec in report.get("sections", []):
        lines += [f"## {sec.get('heading', '')}", ""]
        for p in sec.get("paragraphs", []):
            if isinstance(p, str):
                lines.append(p)
            elif isinstance(p, dict):
                lines.append(f"**{p.get('lead', '')}**{p.get('body', '')}")
            lines.append("")
    st = report.get("stats") or {}
    lines += ["---", "## 数据附录", ""]
    lines += [
        f"- 搜索引擎原始结果：{st.get('total_raw', '-')} 条",
        f"- 去重后保留：{st.get('total_after_dedupe', '-')} 条",
        f"- 抓取到正文：{st.get('body_fetched', '-')} 条",
    ]
    dist = st.get("credibility_dist") or {}
    lines.append(f"- 可信度分布：高 {dist.get('high', 0)} / 中 {dist.get('mid', 0)} / 低 {dist.get('low', 0)}")
    if report.get("elapsed_sec"):
        lines.append(f"- 分析耗时：{report['elapsed_sec']} s")
    if st.get("keywords"):
        lines.append(f"- 关键词：{'、'.join(st['keywords'])}")
    return "\n".join(lines)


def gen_markdown(report: dict, out_path: str) -> bool:
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(render_markdown(report))
        return True
    except OSError as e:
        print("[错误] markdown 生成失败:", e)
        return False

def gen_docx(report: dict, out_path: str) -> bool:
    gen = ROOT / "backend" / "scripts" / "gen_docx.mjs"
    tmp_json = DATA_DIR_TASKS / f"_tmp_{int(time.time())}.json"
    with open(tmp_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False)
    try:
        r = subprocess.run(
            ["node", str(gen), str(tmp_json), out_path],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            print("[错误] docx 生成失败:", r.stderr[-500:])
            return False
        return True
    except FileNotFoundError:
        print("[错误] 未找到 node，无法生成 docx（报告 JSON 已保存）")
        return False
    finally:
        try:
            os.remove(tmp_json)
        except OSError:
            pass


def _validate_risk_advice(risk_points: list[dict], advice_points: list[dict],
                          allow_position_fallback: bool = True) -> tuple[list[dict], list[str]]:
    """硬校验对策-风险对应：advice.for_id 必须指向存在的风险 id；每个风险至少一条对策。

    当模型漏填 for_id 时（实测常见），若 allow_position_fallback，按
    "对策输出顺序 ↔ 风险清单顺序"自动回填（保留告警说明），避免整批对策被误杀。
    返回 (清洗后的对策点, 告警列表)。
    """
    risk_ids = [str(p.get("id", "")).strip() for p in risk_points if p.get("id")]
    risk_ids_set = set(risk_ids)
    warnings: list[str] = []
    kept: list[dict] = []

    # 第一轮：按显式 for_id 匹配
    explicit_used = set()
    for adv in advice_points:
        for_id = str(adv.get("for_id", "")).strip()
        if for_id in risk_ids_set:
            kept.append({**adv, "for_id": for_id})
            explicit_used.add(for_id)
        else:
            kept.append({**adv, "for_id": None})  # 待回填
            if for_id:
                warnings.append(f"对策“{adv.get('title', '')[:20]}”引用不存在的风险 id “{for_id}”，按顺序回填处理")

    if allow_position_fallback:
        unused = [rid for rid in risk_ids if rid not in explicit_used]
        no_id = [a for a in kept if not a.get("for_id")]
        for a, rid in zip(no_id, unused):
            a["for_id"] = rid
            warnings.append(f"对策“{a.get('title', '')[:20]}”未标注对应风险，已按输出顺序推定对应风险“{rid}”")
        # 未被对应到任何风险的对策 → 剔除
        kept = [a for a in kept if a.get("for_id")]

    used = {str(a.get("for_id", "")) for a in kept}
    for rid in risk_ids:
        if rid not in used:
            warnings.append(f"风险点 “{rid}” 没有对应的对策，存在遗漏")
    return kept, warnings


def _points_to_paragraphs(points: list[dict]) -> list[dict]:
    """原始 points（含 id/title/body）→ 报告段落（{lead, body}）。"""
    paragraphs = []
    for p in points or []:
        title = str(p.get("title", "")).strip()
        body = str(p.get("body", "")).strip()
        if not title or not body:
            continue
        if not title.endswith(("。", "！", "？")):
            title += "。"
        paragraphs.append({"lead": title, "body": body})
    return paragraphs


def _run_ai_stage(topic: str, items: list[dict], provider: str | None,
                  emit: callable) -> dict:
    """AI 四角色阶段（串行基线 + 流式进度 + 输出瘦身）。

    实测校准（第一性原理）：后端模型生成速率 ~18-20 token/s 是物理瓶颈；
    并发会触发 burst 限流 429 并被重试惩罚（实测 3 路并发 220s > 串行 83s），
    因此采用串行 4 次调用，仅在流式回调与素材瘦身上做优化，不改变调用拓扑。
    对策严格串行于风险之后，逐条 for_id 对应，程序硬校验防凭空编造。
    """
    results: dict = {"facts": None, "causes": [], "risks": [], "risk_points": [], "advice": []}
    errors: dict = {}

    def _new_ai() -> AIClient:
        a = AIClient()
        a._resolve(provider)
        return a

    def _chunk_progress(label: str):
        state = {"n": 0, "last": 0}

        def cb(text: str) -> None:
            state["n"] += len(text)
            if state["n"] - state["last"] >= 40:
                state["last"] = state["n"]
                emit("ai", f"AI {label}…已生成 {state['n']} 字")
        return cb

    # 素材瘦身：正文截断辅助（facts 保持全量以保留时间线细节）
    def slim(full: bool = False) -> list[dict]:
        items_ = items[:12] if not full else items
        return [{**it, "body": (it.get("body") or "")[:400 if full else 250]} for it in items_]

    # 1) 事实整理（全量素材）
    emit("ai", "AI 事实整理（事件概况）…")
    try:
        ai = _new_ai()
        results["facts"] = _facts_section(topic, slim(full=True), ai, provider,
                                          on_chunk=_chunk_progress("事实整理"))
    except Exception as e:
        errors["facts"] = str(e)
        print(f"       ⚠ 事实整理失败: {e}")
        emit("ai", f"AI 事实整理失败：{str(e)[:80]}")
        emit("ai", "事件概况章节缺失，继续后续分析…")

    facts = results["facts"]
    intro, facts_paras, facts_overview = facts if facts else ("", [], {})
    facts_summary = "；".join(facts_paras[:3])[:600] if facts_paras else ""

    # 2) 原因分析
    emit("ai", "AI 原因分析…")
    try:
        ai = _new_ai()
        data = ai.chat_json(chapter_prompt(topic, "causes", slim(), facts_summary),
                            provider=provider, temperature=0.4, max_tokens=6000,
                            on_chunk=_chunk_progress("原因分析"))
        results["causes"] = _points_to_paragraphs(data.get("points") or [])
    except Exception as e:
        errors["causes"] = str(e)
        print(f"       ⚠ 原因分析失败: {e}")
        emit("ai", f"AI 原因分析失败：{str(e)[:80]}")

    # 3) 风险分析（保留 id 供对策引用）
    emit("ai", "AI 风险分析…")
    try:
        ai = _new_ai()
        data = ai.chat_json(chapter_prompt(topic, "risks", slim(), facts_summary),
                            provider=provider, temperature=0.4, max_tokens=6000,
                            on_chunk=_chunk_progress("风险分析"))
        risk_points = data.get("points") or []
        results["risk_points"] = risk_points                       # 原始（带 id）
        results["risks"] = _points_to_paragraphs(risk_points)      # 报告段落
    except Exception as e:
        errors["risks"] = str(e)
        print(f"       ⚠ 风险分析失败: {e}")
        emit("ai", f"AI 风险分析失败：{str(e)[:80]}")

    risk_ids_map = {str(p.get("id", "")).strip(): (p.get("title") or "")
                    for p in results["risk_points"] if p.get("id")}

    # 4) 对策建议（严格串行于风险之后，逐条对应风险清单）
    advice_points: list[dict] = []
    advice_warnings: list[str] = []
    if risk_ids_map:
        emit("ai", f"AI 对策建议…（逐条对应 {len(risk_ids_map)} 个风险点）")
        try:
            ai = _new_ai()
            risk_brief = [{"id": k, "title": v} for k, v in risk_ids_map.items()]
            data = ai.chat_json(
                chapter_prompt(topic, "advice", slim(), facts_summary, risk_points=risk_brief),
                provider=provider, temperature=0.4, max_tokens=6000,
                on_chunk=_chunk_progress("对策建议"),
            )
            advice_points, advice_warnings = _validate_risk_advice(risk_brief, data.get("points") or [])
        except Exception as e:
            errors["advice"] = str(e)
            print(f"       ⚠ 对策建议失败: {e}")
            emit("ai", f"AI 对策建议失败：{str(e)[:80]}")
    else:
        advice_warnings.append("风险环节未产出风险点，对策无法对应生成")

    return {
        "intro": intro, "facts_paras": facts_paras,
        "causes": results["causes"], "risks": results["risks"],
        "advice": advice_points, "errors": errors,
        "overview": facts_overview,
        "risk_advice_check": {
            "risk_count": len(risk_ids_map),
            "advice_count": len(advice_points),
            "warnings": advice_warnings,
        },
    }


# ---------- 主编排 ----------

def run_analysis(topic: str, provider: str | None = None, verify: bool = False,
                 collect_only: bool = False, save: bool = True,
                 progress: callable | None = None) -> dict:
    """progress: callable(step: str, detail: str) 供 Web 页面轮询进度。"""
    t0 = time.time()

    def emit(step, detail=""):
        print(f"[step] {step} {detail}".rstrip())
        if progress:
            progress(step, detail)

    # 0) 配置热重载（A4：设置页保存后无需重启即生效）
    from config import reload as reload_config
    reload_config()

    # 1) AI 客户端（可能无 key）
    ai: AIClient | None = None
    if not collect_only:
        try:
            ai = AIClient()
            ai._resolve(provider)  # 预检配置
        except AIClientError as e:
            print("[提示] 未配置可用 AI 接口:", e)
            print("       → 请编辑 .env 填写 DEEPSEEK_API_KEY / QWEN_API_KEY，"
                  "或配置 AI_ROUTER_BASE_URL（本地 9router）后重跑。")
            ai = None

    # 2) 关键词扩展
    emit("keywords", f"关键词扩展：{topic}")
    keywords = expand_keywords(topic, ai, provider)
    emit("keywords", f"生成 {len(keywords)} 个关键词: {'、'.join(keywords[:8])}")

    # 3) 多信源采集
    emit("collect", "多信源采集（news/微信/主流/视频 4 组）...")
    materials = collect_topic(topic, keywords)
    n = len(materials["items"])
    print(f"       → 原始 {materials['total_raw']} 条 / 去重后 {n} 条 / "
          f"正文 {materials.get('body_fetched', 0)} 条 / 分布 {materials.get('credibility_dist')}")
    emit("collect", f"原始 {materials['total_raw']} 条 → 去重 {n} 条，正文 {materials.get('body_fetched', 0)} 条")
    if materials.get("warning"):
        print("       ⚠", materials["warning"])
    stats = {
        "total_raw": materials["total_raw"],
        "total_after_dedupe": materials["total_after_dedupe"],
        "body_fetched": materials.get("body_fetched", 0),
        "credibility_dist": materials.get("credibility_dist"),
        "query_log": materials.get("query_log"),
    }

    if save:
        artifact = DATA_DIR_TASKS / f"collect_{datetime.now():%Y%m%d_%H%M%S}.json"
        with open(artifact, "w", encoding="utf-8") as f:
            json.dump({"topic": topic, "stats": stats, "items": materials["items"]},
                      f, ensure_ascii=False)
        print(f"       素材已存: {artifact}")

    if collect_only or ai is None:
        return {"title": f"“{topic}”舆情存在问题风险分析及对策建议", "intro": "",
                "sections": [], "stats": stats, "ai_ready": ai is not None,
                "ai_warning": "" if ai is not None else "未配置可用 AI 接口：请在 .env 填写 DEEPSEEK_API_KEY / QWEN_API_KEY，或配置 AI_ROUTER_BASE_URL（本地 9router）后重试。"}

    # 4) AI 四角色生成（阶段A 三路并行：事实整理‖原因‖风险 → 阶段B 对策串行）
    ai_stage = _run_ai_stage(topic, materials["items"], provider, emit)

    report: dict = {
        "title": f"“{topic}”舆情存在问题风险分析及对策建议",
        "intro": ai_stage["intro"],
        "sections": [],
        "stats": stats,
        "overview": ai_stage.get("overview") or {},
    }
    # B1 图表数据（规则统计，零 AI 成本）：信源分布 + 时间趋势
    from collections import Counter
    src_cnt: Counter = Counter()
    time_cnt: Counter = Counter()
    for it in materials["items"]:
        src = it.get("source_name") or "其他平台"
        src_cnt[src] += 1
        pub = (it.get("published") or "")[:10]
        if pub:
            time_cnt[pub] += 1
    report["stats"]["charts"] = {
        "source_dist": [{"name": n, "value": c} for n, c in src_cnt.most_common(8)],
        "time_dist": [{"date": d, "count": c} for d, c in sorted(time_cnt.items())][-14:],
    }

    # D1：来源 URL 可达性核查（并发 + 当日缓存，不显著增加耗时）
    try:
        from url_check import check_urls, summarize
        urls = [it.get("url") for it in materials["items"] if it.get("url")]
        check = check_urls(urls)
        report["source_check"] = {"detail": check, "summary": summarize(check)}
    except Exception:  # noqa: BLE001 核查失败不影响报告产出
        report["source_check"] = None
    if ai_stage["facts_paras"]:
        report["sections"].append({"heading": f"一、“{topic}”事件概况梳理", "paragraphs": ai_stage["facts_paras"]})

    causes = ai_stage["causes"]
    report["sections"].append({
        "heading": f"二、“{topic}”的原因分析",
        "paragraphs": causes or _placeholder_section(ai_stage["errors"].get("causes", "AI 未返回有效分论点")),
    })
    risks = ai_stage["risks"]
    report["sections"].append({
        "heading": f"三、“{topic}”的风险分析",
        "paragraphs": risks or _placeholder_section(ai_stage["errors"].get("risks", "AI 未返回有效分论点")),
    })
    advice = ai_stage["advice"]
    if advice:
        report["sections"].append({"heading": "四、对策建议", "paragraphs": advice})
    elif ai_stage["errors"].get("advice"):
        report["sections"].append({"heading": "四、对策建议",
                                   "paragraphs": _placeholder_section(ai_stage["errors"]["advice"])})
    else:
        report["sections"].append({"heading": "四、对策建议",
                                   "paragraphs": _placeholder_section("AI 未返回有效对策，或全部未通过“对策-风险对应”校验被剔除")})

    check = ai_stage["risk_advice_check"]
    report["risk_advice_check"] = check
    report["ai_errors"] = ai_stage["errors"]
    if check["warnings"]:
        print(f"       ⚠ 对策-风险对应校验（{len(check['warnings'])} 条告警）:")
        for w in check["warnings"]:
            print(f"          - {w}")

    ai_ok = not ai_stage["errors"]

    # 校验轮（默认关闭，--verify 开启）
    if verify and ai_ok:
        print("[4.5] 校验轮（事实核查）...")
        from ai_prompts import verify_prompt
        try:
            plain = json.dumps(report, ensure_ascii=False)
            vd = ai.chat_json(verify_prompt(topic, plain, materials["items"]), provider=provider,
                              temperature=0.2, max_tokens=2000)
            issues = [i for i in vd.get("issues", []) if i.get("ok") is False]
            if issues:
                print(f"       ⚠ 发现 {len(issues)} 处存疑论断（未自动修正，供人工复核）:")
                for i in issues[:5]:
                    print("       -", str(i.get("claim", ""))[:60])
                report["verify_issues"] = issues
        except AIClientError as e:
            print(f"       ⚠ 校验失败: {e}")

    emit("report", "生成 docx ...")
    ts = datetime.now().strftime("%m%d_%H%M")
    out_docx = DATA_DIR_REPORTS / f"{topic}舆情分析报告-{ts}.docx"
    ok = gen_docx(report, str(out_docx))
    report["docx"] = str(out_docx) if ok else ""
    out_md = out_docx.with_suffix(".md")
    ok_md = gen_markdown(report, str(out_md))
    report["md"] = str(out_md) if ok_md else ""
    report["ai_ready"] = ai_ok
    report["elapsed_sec"] = round(time.time() - t0, 1)

    if save:
        out_json = out_docx.with_suffix(".json")  # 与 docx/md 同一时间戳，避免跨分钟不同步
        report["json"] = str(out_json)
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False)
        print(f"报告 JSON: {out_json}")
    if ok:
        print(f"报告 DOCX: {out_docx}")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="舆情分析流水线")
    ap.add_argument("topic", help="舆情主题")
    ap.add_argument("--provider", default=None, help="deepseek/qwen/router")
    ap.add_argument("--verify", action="store_true", help="开启事实校验轮")
    ap.add_argument("--collect-only", action="store_true", help="仅采集，不调用 AI")
    args = ap.parse_args()

    start = time.time()
    rep = run_analysis(args.topic, provider=args.provider, verify=args.verify, collect_only=args.collect_only)
    print(f"\n总耗时 {round(time.time()-start, 1)}s | 章节数 {len(rep.get('sections', []))} | AI 可用: {rep.get('ai_ready')}")