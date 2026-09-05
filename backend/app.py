# -*- coding: utf-8 -*-
"""妙舆 Web 后端（Flask，纯 Python 依赖，无 C 扩展）。

启动：python app.py  → 局域网访问 http://<本机IP>:5000
接口：
    POST /api/analyze          {topic, provider?, verify?} → {task_id}
    GET  /api/tasks            → 历史任务列表
    GET  /api/history          → 按主题组织的研判记录与报告导出
    GET  /api/tasks/<id>       → 任务状态/进度/报告
    GET  /api/reports/<file>   → 下载报告文件（docx/xlsx/json）
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import quote
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory, Response
from werkzeug.middleware.proxy_fix import ProxyFix

from config import (DATA_DIR_REPORTS, DATA_DIR_TASKS, MANAGED_KEYS, ROOT,
                    SECRET_KEYS, reload as reload_config)
from db import (
    add_source as db_add_source,
    delete_source as db_delete_source,
    get_all as db_get_all,
    list_sources as db_list_sources,
    save as db_save,
    seed_sources as db_seed_sources,
    set_enabled as db_set_enabled,
    task_create as db_task_create,
    task_get as db_task_get,
    task_list as db_task_list,
    task_update as db_task_update,
)
from pipeline import gen_docx as pipeline_gen_docx, render_markdown, run_analysis
from source_catalog import CATALOG, CATEGORIES
from observability import JsonFormatter, begin_request, finish_request
from search_providers import SearchRouter
from security import (DEFAULT_ADMIN_USERNAME, MIN_PASSWORD_LENGTH,
                      MIN_TOKEN_LENGTH, SESSION_COOKIE, attach_session_cookie,
                      authenticated_username, authorized, client_key,
                      create_session, limiter, password_must_change,
                      revoke_session, set_admin_password, verify_admin_password,
                      verify_admin_token)


_root_logger = logging.getLogger()
if not _root_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(JsonFormatter())
    _root_logger.addHandler(_handler)
_root_logger.setLevel(logging.INFO)

# 启动时播种内置信源目录（表为空才写，幂等）
db_seed_sources(CATALOG)

app = Flask(
    __name__,
    static_folder=str(ROOT / "frontend"),
    static_url_path="",
)
app.config["JSON_AS_ASCII"] = False
if os.getenv("MIAOYU_TRUST_PROXY", "0").strip().lower() in {"1", "true", "yes"}:
    # 仅在应用明确位于 Cloudflare/Nginx 等单层受信反向代理后时启用。
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
try:
    _max_body_mb = max(1, min(50, int(os.getenv("MIAOYU_MAX_BODY_MB", "10"))))
except ValueError:
    _max_body_mb = 10
app.config["MAX_CONTENT_LENGTH"] = _max_body_mb * 1024 * 1024

FRONTEND = ROOT / "frontend"


_PUBLIC_API_PATHS = {
    "/api/hot/boards", "/api/hot/history",
    "/api/auth/status", "/api/auth/login", "/api/auth/logout",
}


@app.before_request
def _security_and_scheduler():
    begin_request()
    allowed, retry_after = limiter.allow(f"{client_key()}:{request.path}")
    if not allowed:
        response = jsonify({"error": "请求过于频繁，请稍后重试", "retry_after": retry_after})
        response.status_code = 429
        response.headers["Retry-After"] = str(retry_after)
        return response
    if request.path.startswith("/api/") and request.path not in _PUBLIC_API_PATHS:
        if not authorized():
            response = jsonify({"error": "需要管理员令牌", "auth": "Bearer token required"})
            response.status_code = 401
            return response
    from monitor import monitor_service
    from radar import radar_service
    monitor_service.start()
    radar_service.start()


@app.after_request
def _security_headers(response):
    response = attach_session_cookie(response)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )
    origin = request.headers.get("Origin", "")
    configured_origin = os.getenv("MIAOYU_ALLOWED_ORIGIN", "").strip()
    if configured_origin and origin == configured_origin:
        response.headers["Access-Control-Allow-Origin"] = configured_origin
        response.headers["Vary"] = "Origin"
    return finish_request(response)


def _new_task(topic: str, provider: str | None, verify: bool) -> str:
    tid = uuid.uuid4().hex[:12]
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db_task_create(tid, topic, provider or "auto", bool(verify), created_at)

    def _progress(step: str, detail: str):
        db_task_update(tid, step=step, detail=detail)

    def _bg():
        db_task_update(tid, status="running", step="prepare", detail="任务已开始")
        try:
            rep = run_analysis(
                topic,
                provider=provider,
                verify=verify,
                progress=_progress,
            )
            # 报告 JSON 由 pipeline 已落盘 data/reports/*.json（优先用报告内的 json 字段，避免时间戳推导错位）
            rep_json = rep.get("json") or ""
            report_file = rep_json if rep_json and Path(rep_json).exists() else ""
            if not report_file and rep.get("docx"):
                pj = Path(rep["docx"]).with_suffix(".json")
                report_file = str(pj) if pj.exists() else ""
            summary = {
                "title": rep.get("title"),
                "sections": len(rep.get("sections") or []),
                "docx": rep.get("docx") or "", "md": rep.get("md", ""),
                "elapsed": rep.get("elapsed_sec"),
                "ai_ready": rep.get("ai_ready"),
            }
            db_task_update(
                tid, status="done", step="done",
                detail=f"完成，耗时 {rep.get('elapsed_sec', '?')}s",
                report_file=report_file, report_summary=json.dumps(summary, ensure_ascii=False),
                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
        except Exception as e:  # noqa: BLE001  Web 层兜底
            db_task_update(tid, status="error", error=str(e), detail=f"任务失败: {e}",
                           finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    threading.Thread(target=_bg, daemon=True).start()
    return tid


@app.post("/api/analyze")
def api_analyze():
    body = request.get_json(silent=True) or {}
    topic = (body.get("topic") or "").strip()
    if not topic:
        return jsonify({"error": "请填写舆情主题"}), 400
    tid = _new_task(topic, body.get("provider"), body.get("verify", False))
    return jsonify({"task_id": tid})


@app.get("/api/tasks")
def api_tasks():
    """历史任务列表（支持分页）：?page=1&per=10 → {items, total, page, per_page}"""
    page = max(1, int(request.args.get("page", 1) or 1))
    per = min(100, max(1, int(request.args.get("per", 10) or 10)))
    all_items = [{
        "id": t["id"], "topic": t["topic"], "status": t["status"],
        "created_at": t["created_at"], "step": t["step"], "detail": t["detail"],
    } for t in db_task_list(500)]
    total = len(all_items)
    items = all_items[(page - 1) * per: page * per]
    return jsonify({"items": items, "total": total, "page": page, "per_page": per})


def _history_report_summary(value) -> dict:
    """把任务摘要收敛为历史页所需的报告元数据，不暴露服务器文件路径。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    if not isinstance(value, dict):
        value = {}

    exports = []
    for field, kind, label in (("docx", "DOCX", "Word"), ("md", "MD", "Markdown")):
        raw_path = str(value.get(field) or "").strip()
        if not raw_path:
            continue
        name = Path(raw_path).name
        if not name:
            continue
        exports.append({
            "kind": kind,
            "label": label,
            "name": name,
            "url": f"/api/reports/{name}",
        })

    try:
        sections = max(0, int(value.get("sections") or 0))
    except (TypeError, ValueError):
        sections = 0
    return {
        "title": str(value.get("title") or "").strip(),
        "sections": sections,
        "elapsed_sec": value.get("elapsed_sec"),
        "exports": exports,
    }


def _history_failure_message(error: str) -> str:
    """历史页只给出用户可行动的原因；完整异常仍留在日志与任务详情中。"""
    detail = (error or "").lower()
    if "codec can't encode" in detail or "unicodeencodeerror" in detail:
        return "报告生成时遇到字符兼容问题，可以重新研判。"
    if "timeout" in detail or "timed out" in detail:
        return "采集或分析服务响应超时，可以稍后重试。"
    if "unauthorized" in detail or "api key" in detail or "401" in detail:
        return "分析服务认证失败，请检查接口设置后重试。"
    return "本次研判未完成，可以查看设置后重新发起。"


def _history_run(task: dict) -> dict:
    status = task.get("status") or "pending"
    report = _history_report_summary(task.get("report_summary"))
    if status == "error":
        detail = _history_failure_message(task.get("error") or task.get("detail") or "")
    else:
        detail = str(task.get("detail") or "").strip()
    return {
        "id": task.get("id") or "",
        "status": status,
        "step": task.get("step") or "",
        "detail": detail,
        "created_at": task.get("created_at") or "",
        "finished_at": task.get("finished_at") or "",
        "provider": task.get("provider") or "auto",
        "verify": bool(task.get("verify")),
        "report": report,
        "can_open": status in {"pending", "running", "done"},
        "can_retry": status == "error",
    }


@app.get("/api/history")
def api_history():
    """按主题组织历史研判；报告格式是任务结果的子资源，不再与任务平级。"""
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
        per = min(50, max(1, int(request.args.get("per", 8) or 8)))
        days = max(0, min(3650, int(request.args.get("days", 0) or 0)))
    except (TypeError, ValueError):
        return jsonify({"error": "分页或时间范围参数无效"}), 400

    query = " ".join((request.args.get("q") or "").split()).casefold()
    status_filter = (request.args.get("status") or "all").strip().lower()
    if status_filter not in {"all", "active", "done", "error"}:
        return jsonify({"error": "状态筛选参数无效"}), 400
    sort_order = (request.args.get("sort") or "newest").strip().lower()
    if sort_order not in {"newest", "oldest"}:
        return jsonify({"error": "排序参数无效"}), 400

    cutoff = datetime.now() - timedelta(days=days) if days else None
    grouped: dict[str, dict] = {}
    for task in db_task_list(500):
        topic = " ".join(str(task.get("topic") or "").split()) or "未命名主题"
        if query and query not in topic.casefold():
            continue
        if cutoff:
            try:
                created = datetime.strptime(task.get("created_at") or "", "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                created = None
            if created and created < cutoff:
                continue

        key = topic.casefold()
        if key not in grouped:
            grouped[key] = {"id": task.get("id") or key, "topic": topic, "runs": []}
        grouped[key]["runs"].append(_history_run(task))

    groups = []
    for group in grouped.values():
        group["runs"].sort(key=lambda item: item["created_at"], reverse=True)
        latest = group["runs"][0]
        group["latest"] = latest
        group["status"] = latest["status"]
        group["run_count"] = len(group["runs"])
        group["completed_count"] = sum(run["status"] == "done" for run in group["runs"])
        if status_filter == "active" and latest["status"] not in {"pending", "running"}:
            continue
        if status_filter in {"done", "error"} and latest["status"] != status_filter:
            continue
        groups.append(group)

    groups.sort(key=lambda item: item["latest"]["created_at"], reverse=sort_order == "newest")
    total_groups = len(groups)
    total_runs = sum(group["run_count"] for group in groups)
    start = (page - 1) * per
    return jsonify({
        "items": groups[start:start + per],
        "total": total_groups,
        "total_runs": total_runs,
        "page": page,
        "per_page": per,
    })


@app.get("/api/tasks/<tid>")
def api_task(tid: str):
    t = db_task_get(tid)
    if not t:
        return jsonify({"error": "任务不存在"}), 404
    report = None
    # 优先读落盘的完整报告 JSON；否则用摘要
    rf = t.get("report_file") or ""
    if rf and Path(rf).exists():
        try:
            report = json.loads(Path(rf).read_text(encoding="utf-8"))
        except (ValueError, OSError):
            report = None
    if report is None and t.get("report_summary"):
        report = t["report_summary"]
    return jsonify({
        "id": t["id"], "topic": t["topic"], "status": t["status"],
        "step": t["step"], "detail": t["detail"],
        "created_at": t["created_at"], "finished_at": t.get("finished_at", ""),
        "error": t.get("error", ""), "report": report,
    })


@app.get("/api/hot/boards")
def api_hot_boards():
    """首页热点榜：公开源主链路，按榜单保留 provider 与健康快照。"""
    from hotlists import annotate_hot_items, fetch_aggregated, quota_state, source_health
    from db import hot_rank_changes
    from evidence import content_hash, record_hot_boards
    try:
        boards = fetch_aggregated()
        provider = quota_state()
    except Exception as e:  # noqa: BLE001
        return jsonify({"boards": [], "provider": {"error": str(e)[:80]}, "source_health": source_health()})
    # 单榜条目限 15；为首页聚合去重（多榜标记由前端按标题聚合）
    # G1：请求命中聚合缓存后才会进入这里，按当前快照写入证据库；入库
    # 失败不能阻断热榜展示，便于后续继续使用外部源降级链路。
    try:
        record_hot_boards(boards)
    except Exception as e:  # noqa: BLE001
        app.logger.warning("热榜证据入库失败: %s", str(e)[:160])
    out = []
    for b in boards:
        board_id = b.get("source_id", "")
        rank_data = hot_rank_changes(board_id) if board_id else {"items": {}}
        visible_items = annotate_hot_items((b.get("items") or [])[:15])
        items = [{"title": it.get("title", ""), "url": it.get("url", ""),
                  "hot": it.get("hot", ""), "published": it.get("published", ""),
                  "captured_at": it.get("captured_at", ""),
                  "rank": it.get("rank"), "provider": it.get("provider") or b.get("provider", ""),
                  "heat": it.get("heat", {})}
                 for it in visible_items]
        for item in items:
            delta = rank_data["items"].get(content_hash(item["title"], item["url"]), {})
            item.update({"previous_rank": delta.get("previous_rank"),
                         "rank_change": delta.get("rank_change"),
                         "is_new": delta.get("is_new", False),
                         "captured_at": delta.get("captured_at") or item.get("captured_at", ""),
                         "first_seen_at": delta.get("first_seen_at", "")})
        out.append({"name": b.get("name", "热榜"), "source_id": b.get("source_id", ""),
                    "provider": b.get("provider", "public"), "count": len(items), "items": items})
    return jsonify({"boards": out, "provider": provider,
                    "source_health": source_health(), "updated": None})


@app.get("/api/hot/history")
def api_hot_history():
    """G1.5：读取指定榜单的历史快照，不触发外部采集。"""
    from db import hot_history
    board_id = (request.args.get("board_id") or "").strip()
    if not board_id:
        return jsonify({"error": "缺少 board_id"}), 400
    try:
        hours = max(1, min(168, int(request.args.get("hours", 24))))
        limit = max(1, min(5000, int(request.args.get("limit", 1000))))
    except ValueError:
        return jsonify({"error": "hours/limit 必须是整数"}), 400
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return jsonify({"board_id": board_id, "hours": hours,
                    "items": hot_history(board_id, since=since, limit=limit)})


def _list_input(value, *, default=None) -> list[str]:
    if isinstance(value, list):
        values = value
    elif isinstance(value, str):
        values = value.replace("，", ",").replace("\n", ",").split(",")
    else:
        values = default or []
    return list(dict.fromkeys(str(v).strip() for v in values if str(v).strip()))


@app.get("/api/monitor/topics")
def api_monitor_topics():
    """G2：查看持久化监测主题及订阅状态。"""
    from db import subscription_get_by_topic, topic_list
    out = []
    for topic in topic_list():
        item = dict(topic)
        subscriptions = [s for s in (subscription_get_by_topic(topic["id"]),) if s]
        item["subscription"] = subscriptions[0] if subscriptions else None
        out.append(item)
    return jsonify({"items": out})


@app.post("/api/monitor/topics")
def api_monitor_topic_create():
    """G2：创建主题并自动创建一条订阅。"""
    from db import subscription_upsert, topic_create
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or body.get("topic") or "").strip()
    keywords = _list_input(body.get("keywords"), default=[name])
    exclude = _list_input(body.get("exclude_keywords") or body.get("excludeKeywords"))
    if not name or not keywords:
        return jsonify({"error": "主题名称和关键词不能为空"}), 400
    try:
        interval = max(60, min(86400, int(body.get("interval_seconds", 600))))
    except (TypeError, ValueError):
        return jsonify({"error": "interval_seconds 必须是整数"}), 400
    enabled = bool(body.get("enabled", True))
    topic_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()
    topic_create(topic_id, name, keywords, exclude, now, enabled=enabled)
    subscription_id = subscription_upsert(topic_id, interval, enabled, now)
    return jsonify({"topic_id": topic_id, "subscription_id": subscription_id,
                    "name": name, "keywords": keywords,
                    "exclude_keywords": exclude, "interval_seconds": interval}), 201


# ---------- 雷达（关键词 → 公开信源 → 时间线，不做分析） ----------

@app.get("/api/radar/topics")
def api_radar_topics():
    from db import radar_stats, radar_topics, subscription_get_by_topic
    items = []
    for topic in radar_topics():
        item = dict(topic)
        item["subscription"] = subscription_get_by_topic(topic["id"])
        item["stats"] = radar_stats(topic["id"])
        items.append(item)
    return jsonify({"items": items})


@app.post("/api/radar/topics")
def api_radar_topic_create():
    from db import subscription_upsert, topic_create
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()
    keywords = _list_input(body.get("keywords"), default=[name])[:20]
    exclude = _list_input(body.get("exclude_keywords") or body.get("excludeKeywords"))[:20]
    if not name or not keywords:
        return jsonify({"error": "名称和至少一个关键词不能为空"}), 400
    try:
        interval = max(60, min(86400, int(body.get("interval_seconds", 900))))
    except (TypeError, ValueError):
        return jsonify({"error": "interval_seconds 必须是整数"}), 400
    scope = [str(x).strip().upper() for x in (body.get("source_scope") or ["L1", "L2", "L3"])
             if str(x).strip().upper() in {"L1", "L2", "L3", "L4", "L5", "L6"}]
    now = datetime.now(timezone.utc).isoformat()
    topic_id = uuid.uuid4().hex[:12]
    topic_create(topic_id, name, keywords, exclude, now, enabled=True,
                 kind="radar", source_scope=scope or ["L1", "L2", "L3"])
    subscription_id = subscription_upsert(topic_id, interval, True, now)
    return jsonify({"topic_id": topic_id, "subscription_id": subscription_id,
                    "name": name, "keywords": keywords, "exclude_keywords": exclude,
                    "interval_seconds": interval}), 201


@app.get("/api/radar/timeline")
def api_radar_timeline():
    from db import radar_stats, radar_timeline, topic_get
    topic_id = (request.args.get("topic_id") or "").strip()
    topic = topic_get(topic_id)
    if not topic or topic.get("kind") != "radar":
        return jsonify({"error": "雷达主题不存在"}), 404
    try:
        limit = max(1, min(500, int(request.args.get("limit", 100))))
    except ValueError:
        return jsonify({"error": "limit 必须是整数"}), 400
    return jsonify({"topic": topic, "items": radar_timeline(
        topic_id, limit=limit, before=(request.args.get("before") or "").strip(),
        source_id=(request.args.get("source_id") or "").strip()),
        "stats": radar_stats(topic_id)})


@app.post("/api/radar/topics/<topic_id>/refresh")
def api_radar_topic_refresh(topic_id: str):
    from db import topic_get
    from radar import radar_service
    topic = topic_get(topic_id)
    if not topic or topic.get("kind") != "radar":
        return jsonify({"error": "雷达主题不存在"}), 404
    threading.Thread(target=radar_service.run_topic, args=(topic_id,), daemon=True).start()
    return jsonify({"accepted": True, "topic_id": topic_id}), 202


@app.post("/api/radar/topics/<topic_id>/read")
def api_radar_topic_read(topic_id: str):
    from db import radar_mark_read, topic_get
    topic = topic_get(topic_id)
    if not topic or topic.get("kind") != "radar":
        return jsonify({"error": "雷达主题不存在"}), 404
    now = datetime.now(timezone.utc).isoformat()
    radar_mark_read(topic_id, now)
    return jsonify({"ok": True, "topic_id": topic_id, "last_read_at": now})


@app.post("/api/radar/topics/<topic_id>/toggle")
def api_radar_topic_toggle(topic_id: str):
    from db import subscription_get_by_topic, subscription_set_enabled, topic_get, topic_set_enabled
    body = request.get_json(silent=True) or {}
    topic = topic_get(topic_id)
    if not topic or topic.get("kind") != "radar":
        return jsonify({"error": "雷达主题不存在"}), 404
    enabled = bool(body.get("enabled"))
    now = datetime.now(timezone.utc).isoformat()
    topic_set_enabled(topic_id, enabled, now)
    sub = subscription_get_by_topic(topic_id)
    if sub:
        subscription_set_enabled(sub["id"], enabled, now)
    return jsonify({"ok": True, "topic_id": topic_id, "enabled": enabled})


@app.delete("/api/radar/topics/<topic_id>")
def api_radar_topic_delete(topic_id: str):
    from db import radar_delete_topic
    if not radar_delete_topic(topic_id):
        return jsonify({"error": "雷达主题不存在"}), 404
    return jsonify({"ok": True, "topic_id": topic_id})


@app.get("/api/radar/source-health")
def api_radar_source_health():
    from db import radar_source_states
    return jsonify({"items": radar_source_states()})


@app.post("/api/monitor/topics/<topic_id>/run")
def api_monitor_topic_run(topic_id: str):
    """G2：手动触发一次监测；实际采集在后台线程运行。"""
    from db import subscription_get_by_topic, topic_get
    from monitor import monitor_service
    if not topic_get(topic_id):
        return jsonify({"error": "主题不存在"}), 404
    sub = subscription_get_by_topic(topic_id)
    if not sub:
        return jsonify({"error": "主题没有订阅"}), 409
    threading.Thread(target=monitor_service.run_subscription,
                     args=(sub["id"],), daemon=True).start()
    return jsonify({"accepted": True, "subscription_id": sub["id"]}), 202


@app.post("/api/monitor/topics/<topic_id>/toggle")
def api_monitor_topic_toggle(topic_id: str):
    from db import subscription_get_by_topic, subscription_set_enabled, topic_set_enabled
    body = request.get_json(silent=True) or {}
    enabled = bool(body.get("enabled"))
    now = datetime.now(timezone.utc).isoformat()
    if not topic_set_enabled(topic_id, enabled, now):
        return jsonify({"error": "主题不存在"}), 404
    sub = subscription_get_by_topic(topic_id)
    if sub:
        subscription_set_enabled(sub["id"], enabled, now)
    return jsonify({"topic_id": topic_id, "enabled": enabled})


@app.get("/api/monitor/runs")
def api_monitor_runs():
    from db import monitor_runs
    topic_id = (request.args.get("topic_id") or "").strip()
    try:
        limit = max(1, min(500, int(request.args.get("limit", 50))))
    except ValueError:
        return jsonify({"error": "limit 必须是整数"}), 400
    return jsonify({"items": monitor_runs(topic_id, limit)})


@app.get("/api/monitor/events")
def api_monitor_events():
    """G2.5：返回按可解释热度排序的主题事件。"""
    from db import event_list, topic_get
    topic_id = (request.args.get("topic_id") or "").strip()
    if topic_id and not topic_get(topic_id):
        return jsonify({"error": "主题不存在"}), 404
    try:
        limit = max(1, min(500, int(request.args.get("limit", 50))))
    except ValueError:
        return jsonify({"error": "limit 必须是整数"}), 400
    return jsonify({"items": event_list(topic_id, limit)})


@app.get("/api/monitor/signals")
def api_monitor_signals():
    """G2.6：返回去重后的跨平台、突增和负面占比信号。"""
    from db import signal_list, topic_get
    topic_id = (request.args.get("topic_id") or "").strip()
    if topic_id and not topic_get(topic_id):
        return jsonify({"error": "主题不存在"}), 404
    try:
        limit = max(1, min(500, int(request.args.get("limit", 50))))
    except ValueError:
        return jsonify({"error": "limit 必须是整数"}), 400
    return jsonify({"items": signal_list(topic_id, limit)})


@app.get("/api/metrics")
def api_metrics():
    """G3：受保护的运行摘要；只返回计数，不返回设置和密钥。"""
    from db import metrics_snapshot
    from observability import metrics
    return jsonify({"database": metrics_snapshot(), "http": metrics.snapshot()})


@app.post("/api/monitor/topics/<topic_id>/report")
def api_monitor_topic_report(topic_id: str):
    """G2.6：按已落库监测数据生成一个可下载周期报告。"""
    from monitor_report import generate_periodic_report
    body = request.get_json(silent=True) or {}
    try:
        report = generate_periodic_report(
            topic_id,
            start=body.get("start"), end=body.get("end"),
            hours=body.get("hours", 24),
        )
    except ValueError as exc:
        status = 404 if str(exc) == "主题不存在" else 400
        return jsonify({"error": str(exc)}), status
    return jsonify({
        "ok": True, "title": report["title"],
        "json_name": Path(report["json"]).name,
        "md_name": Path(report["md"]).name if report.get("md") else "",
        "docx_name": Path(report["docx"]).name if report.get("docx") else "",
        "summary": report["monitor"]["summary"],
    }), 201


@app.get("/api/reports/list")
def api_reports_list():
    """历史报告文件清单（docx/md，按修改时间倒序）+ 关联 JSON（用于页面内预览）。"""
    files = []
    for p in sorted(DATA_DIR_REPORTS.glob("*.*"), key=lambda x: -x.stat().st_mtime):
        if p.suffix.lower() in (".docx", ".md") and not p.name.startswith("~"):
            j = p.with_suffix(".json")
            files.append({
                "name": p.name, "kind": p.suffix.lstrip(".").upper(),
                "url": f"/api/reports/{p.name}", "size": p.stat().st_size,
                "json_name": j.name if j.exists() else "",
            })
    return jsonify(files[:100])


@app.post("/api/report/edit")
def api_report_edit():
    """B4 报告在线编辑：保存修改后的报告结构，就地更新 JSON 并重生成 docx/md。
    保存前把上一版备份为 .bak.json，供「还原」回滚。"""
    body = request.get_json(silent=True) or {}
    json_name = (body.get("json_name") or "").strip()
    rep = body.get("report")
    if not json_name or not isinstance(rep, dict):
        return jsonify({"error": "参数缺失"}), 400
    safe = Path(json_name).name
    jpath = DATA_DIR_REPORTS / safe
    if not jpath.exists() or jpath.suffix.lower() != ".json":
        return jsonify({"error": "报告不存在"}), 404
    if not rep.get("title") or not isinstance(rep.get("sections"), list):
        return jsonify({"error": "报告结构不完整（缺少标题或章节）"}), 400
    # 保存前备份上一版（仅当当前文件是有效报告才备份，避免把损坏内容存为备份）
    try:
        prev = json.loads(jpath.read_text(encoding="utf-8"))
        if prev.get("title") and isinstance(prev.get("sections"), list):
            jpath.with_name(jpath.stem + ".bak.json").write_text(
                json.dumps(prev, ensure_ascii=False), encoding="utf-8")
    except (ValueError, OSError):
        pass
    rep["edited"] = True
    jpath.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
    # 重新生成 md（必做）与 docx（尽力而为）
    md_path = jpath.with_suffix(".md")
    md_path.write_text(render_markdown(rep), encoding="utf-8")
    docx_path = jpath.with_suffix(".docx")
    try:
        ok_docx = pipeline_gen_docx(rep, str(docx_path))
    except Exception:  # noqa: BLE001
        ok_docx = False
    return jsonify({"ok": True, "edited": True, "docx_ok": ok_docx,
                    "has_backup": (jpath.with_name(jpath.stem + ".bak.json")).exists(),
                    "docx": str(docx_path), "md": str(md_path)})


@app.post("/api/report/restore")
def api_report_restore():
    """B4 还原：把 .bak.json 覆盖回主 JSON 并重生成 docx/md（回滚上一次编辑）。"""
    body = request.get_json(silent=True) or {}
    json_name = (body.get("json_name") or "").strip()
    if not json_name:
        return jsonify({"error": "参数缺失"}), 400
    safe = Path(json_name).name
    jpath = DATA_DIR_REPORTS / safe
    if not jpath.exists() or jpath.suffix.lower() != ".json":
        return jsonify({"error": "报告不存在"}), 404
    bak = jpath.with_name(jpath.stem + ".bak.json")
    if not bak.exists():
        return jsonify({"error": "没有可还原的备份（仅编辑过且保存过的报告可还原）"}), 404
    rep = json.loads(bak.read_text(encoding="utf-8"))
    if not rep.get("title") or not isinstance(rep.get("sections"), list):
        return jsonify({"error": "备份内容损坏"}), 500
    jpath.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
    md_path = jpath.with_suffix(".md")
    md_path.write_text(render_markdown(rep), encoding="utf-8")
    docx_path = jpath.with_suffix(".docx")
    try:
        ok_docx = pipeline_gen_docx(rep, str(docx_path))
    except Exception:  # noqa: BLE001
        ok_docx = False
    return jsonify({"ok": True, "restored": True, "docx_ok": ok_docx,
                    "docx": str(docx_path), "md": str(md_path)})


@app.post("/api/report/export-docx")
def api_report_export_docx():
    """P1-c：导出含图表的 Word。前端把图表 PNG dataURL 传回，解码为临时图片
    注入报告 JSON（_chart_images），由 gen_docx 嵌入后返回 docx 文件。"""
    import base64
    body = request.get_json(silent=True) or {}
    json_name = (body.get("json_name") or "").strip()
    charts = body.get("charts") or {}   # {kind: dataURL}
    if not json_name:
        return jsonify({"error": "参数缺失"}), 400
    safe = Path(json_name).name
    jpath = DATA_DIR_REPORTS / safe
    if not jpath.exists() or jpath.suffix.lower() != ".json":
        return jsonify({"error": "报告不存在"}), 404
    rep = json.loads(jpath.read_text(encoding="utf-8"))
    # 解码图表 PNG → 临时文件
    imgs = []
    try:
        for kind, dataurl in charts.items():
            if not dataurl or "," not in dataurl:
                continue
            b64 = dataurl.split(",", 1)[1]
            # 客户端可并发导出，图表临时文件必须独占，且不能把外部 kind 当文件名。
            with tempfile.NamedTemporaryFile("wb", suffix=".png", prefix="_chart_",
                                             dir=DATA_DIR_REPORTS, delete=False) as f:
                f.write(base64.b64decode(b64))
                tmp = f.name
            imgs.append({"kind": kind, "path": tmp})
        if imgs:
            rep["_chart_images"] = imgs
        docx_path = jpath.with_suffix(".docx")
        ok = pipeline_gen_docx(rep, str(docx_path))
        if not ok:
            return jsonify({"error": "Word 生成失败（含图表）"}), 500
        return send_from_directory(str(DATA_DIR_REPORTS), docx_path.name,
                                   as_attachment=True, download_name=docx_path.name)
    finally:
        for im in imgs:
            try:
                Path(im["path"]).unlink()
            except OSError:
                pass


@app.post("/api/report/export-pdf")
def api_report_export_pdf():
    """使用 Node Playwright + Chromium 的原生打印链路导出 PDF。

    前端只提交已完成排版的、无脚本报告 HTML；后端不重新拼装第二套报告模板。
    临时输入/输出文件使用独立目录，导出失败不返回空 PDF。
    """
    body = request.get_json(silent=True) or {}
    html = body.get("html")
    filename = str(body.get("filename") or "舆情分析报告.pdf").strip()
    if not isinstance(html, str) or not html.strip():
        return jsonify({"error": "缺少报告渲染内容"}), 400
    if len(html.encode("utf-8")) > 8 * 1024 * 1024:
        return jsonify({"error": "报告渲染内容过大"}), 413
    safe_stem = Path(filename).stem or "舆情分析报告"
    safe_stem = "".join("_" if c in '\\/:*?\"<>|' else c for c in safe_stem).strip() or "舆情分析报告"
    download_name = safe_stem + ".pdf"
    script = ROOT / "backend" / "scripts" / "render_pdf.mjs"
    node = os.getenv("MIAOYU_NODE", "node")
    if not script.is_file():
        return jsonify({"error": "PDF 渲染器未安装：缺少 Playwright 脚本"}), 503

    with tempfile.TemporaryDirectory(prefix="_pdf_render_", dir=str(DATA_DIR_REPORTS)) as work:
        input_path = Path(work) / "input.json"
        output_path = Path(work) / "report.pdf"
        input_path.write_text(json.dumps({"html": html}, ensure_ascii=False), encoding="utf-8")
        try:
            result = subprocess.run(
                [node, str(script), str(input_path), str(output_path)],
                cwd=str(script.parent), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=90, check=False,
            )
        except FileNotFoundError:
            return jsonify({"error": "未找到 Node.js，无法启动 Playwright 渲染器"}), 503
        except subprocess.TimeoutExpired:
            return jsonify({"error": "PDF 渲染超时，请稍后重试"}), 503
        if result.returncode != 0 or not output_path.is_file():
            detail = (result.stderr or result.stdout or "Playwright 未生成 PDF").strip()[-500:]
            app.logger.error("Playwright PDF 渲染失败: %s", detail)
            return jsonify({"error": "PDF 渲染失败，请检查 Playwright/Chromium 安装"}), 503
        pdf = output_path.read_bytes()
        # 只接受结构完整的 PDF；避免把 html2canvas 式的空白产物伪装成成功。
        if len(pdf) < 1024 or not pdf.startswith(b"%PDF-") or b"/Type /Page" not in pdf:
            app.logger.error("Playwright 返回无效 PDF: %d bytes", len(pdf))
            return jsonify({"error": "PDF 渲染结果无效，未返回空白文件"}), 503
        response = send_file(
            BytesIO(pdf), mimetype="application/pdf",
            as_attachment=True, download_name="miaoyu-report.pdf", max_age=0,
        )
        response.headers["Content-Disposition"] = (
            'attachment; filename="miaoyu-report.pdf"; '
            f"filename*=UTF-8''{quote(download_name)}"
        )
        return response


@app.get("/api/reports/<path:filename>")
def api_report_file(filename: str):
    # 防目录穿越
    safe = Path(filename).name
    target = DATA_DIR_REPORTS / safe
    # Markdown 不直接返回历史静态文件：旧报告可能仍是“数据附录”版本，
    # 这里按当前渲染器即时重建，保证下载内容与网页/Word/PDF 使用同一参考资料尾部。
    if target.suffix.lower() == ".md":
        json_path = target.with_suffix(".json")
        if json_path.exists():
            try:
                rep = json.loads(json_path.read_text(encoding="utf-8"))
                content = render_markdown(rep)
                response = Response(content, mimetype="text/markdown; charset=utf-8")
                response.headers["Content-Disposition"] = (
                    'attachment; filename="report.md"; '
                    f"filename*=UTF-8''{quote(safe)}"
                )
                return response
            except (ValueError, OSError):
                pass
    if target.exists():
        return send_from_directory(str(DATA_DIR_REPORTS), safe, as_attachment=False)
    return jsonify({"error": "文件不存在"}), 404


@app.get("/api/settings")
def api_settings_get():
    """返回当前生效配置（.env + 数据库合并）；密钥一律打码。

    AI_PROVIDERS 只返回服务商元数据和 apiKeyConfigured，绝不把 API Key 发给浏览器。
    """
    import config as _cfg
    public_providers = {}
    for pid, provider in _cfg.AI_PROVIDERS.items():
        public_providers[pid] = {
            key: value for key, value in provider.items() if key != "apiKey"
        }
        public_providers[pid]["apiKeyConfigured"] = bool(provider.get("apiKey"))
    merged = {
        "SEARXNG_URL": _cfg.SEARXNG_URL,
        "SEARXNG_CONFIGURED": bool(_cfg.SEARXNG_URL.strip()),
        "SEARCH_PROVIDER_ORDER": "searxng,brave,tavily",
        "SEARCH_PROVIDER_MODE": _cfg.SEARCH_PROVIDER_MODE,
        "SEARCH_TIMEOUT": str(_cfg.SEARCH_TIMEOUT),
        "BRAVE_SEARCH_ENDPOINT": _cfg.BRAVE_SEARCH_ENDPOINT,
        "BRAVE_SEARCH_LANG": _cfg.BRAVE_SEARCH_LANG,
        "BRAVE_COUNTRY": _cfg.BRAVE_COUNTRY,
        "BRAVE_API_KEY": "****已配置****" if _cfg.BRAVE_API_KEY else "",
        "BRAVE_API_KEY_CONFIGURED": bool(_cfg.BRAVE_API_KEY),
        "TAVILY_SEARCH_ENDPOINT": _cfg.TAVILY_SEARCH_ENDPOINT,
        "TAVILY_SEARCH_DEPTH": _cfg.TAVILY_SEARCH_DEPTH,
        "TAVILY_API_KEY": "****已配置****" if _cfg.TAVILY_API_KEY else "",
        "TAVILY_API_KEY_CONFIGURED": bool(_cfg.TAVILY_API_KEY),
        "AI_PRIMARY_PROVIDER": _cfg.AI_PRIMARY_PROVIDER,
        "ENABLED_GROUPS": ",".join(_cfg.ENABLED_GROUPS),
        "AI_PROVIDERS": json.dumps(public_providers, ensure_ascii=False),
    }
    for k in SECRET_KEYS:
        if k in {"BRAVE_API_KEY", "TAVILY_API_KEY"}:
            continue
        merged[k] = "****已配置****" if any(
            str(provider.get("apiKey") or "") for provider in _cfg.AI_PROVIDERS.values()
        ) else ""
    return jsonify({"settings": merged, "managed_keys": list(MANAGED_KEYS)})


@app.post("/api/search/test")
def api_search_test():
    """用当前已保存配置测试一个搜索 Provider；结果只返回前三条摘要。"""
    body = request.get_json(silent=True) or {}
    provider = str(body.get("provider") or "").strip().lower()
    query = str(body.get("query") or "中国 舆情 新闻").strip()[:200]
    return jsonify(SearchRouter().test(provider, query))


@app.get("/api/auth/status")
def api_auth_status():
    """返回当前会话状态，不返回密码、令牌或密码哈希。"""
    authenticated = authorized()
    return jsonify({
        "authenticated": authenticated,
        "username": authenticated_username() if authenticated else "",
        "must_change_password": password_must_change() if authenticated else False,
    })


@app.post("/api/auth/login")
def api_auth_login():
    """验证站内账号密码并建立随机 HttpOnly 会话 Cookie。"""
    body = request.get_json(silent=True) or {}
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if username or password:
        if username != DEFAULT_ADMIN_USERNAME:
            return jsonify({"ok": False, "error": "用户名必须为 admin"}), 401
        if not verify_admin_password(username, password):
            return jsonify({"ok": False, "error": "用户名或密码错误"}), 401
        sid = create_session(username)
        response = jsonify({
            "ok": True,
            "username": username,
            "must_change_password": password_must_change(),
        })
        response.set_cookie(
            SESSION_COOKIE, sid, max_age=86400,
            httponly=True, samesite="Lax", secure=bool(request.is_secure),
        )
        return response

    # 兼容旧版脚本/部署：Bearer 令牌仍可用于 API，但不作为网页登录方式。
    token = str(body.get("token") or "").strip()
    if not verify_admin_token(token):
        return jsonify({"ok": False,
                        "error": f"账号密码无效；旧版令牌至少需要{MIN_TOKEN_LENGTH}个字符"}), 401
    sid = create_session()
    response = jsonify({"ok": True})
    response.set_cookie(
        SESSION_COOKIE, sid, max_age=86400,
        httponly=True, samesite="Lax", secure=bool(request.is_secure),
    )
    return response


@app.post("/api/auth/logout")
def api_auth_logout():
    revoke_session(request.cookies.get(SESSION_COOKIE, ""))
    response = jsonify({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.post("/api/auth/password")
def api_auth_password():
    """修改站内管理员密码，成功后旧会话全部失效并续发当前会话。"""
    body = request.get_json(silent=True) or {}
    current_password = str(body.get("current_password") or "")
    new_password = str(body.get("new_password") or "")
    confirm_password = str(body.get("confirm_password") or "")
    if len(new_password) < MIN_PASSWORD_LENGTH:
        return jsonify({"ok": False, "error": f"新密码至少需要 {MIN_PASSWORD_LENGTH} 个字符"}), 400
    if new_password != confirm_password:
        return jsonify({"ok": False, "error": "两次输入的新密码不一致"}), 400
    if not set_admin_password(current_password, new_password):
        return jsonify({"ok": False, "error": "当前密码错误，未修改密码"}), 400
    sid = create_session(DEFAULT_ADMIN_USERNAME)
    response = jsonify({"ok": True, "username": DEFAULT_ADMIN_USERNAME,
                        "must_change_password": False})
    response.set_cookie(
        SESSION_COOKIE, sid, max_age=86400,
        httponly=True, samesite="Lax", secure=bool(request.is_secure),
    )
    return response


@app.post("/api/settings")
def api_settings_save():
    import config as _cfg
    body = request.get_json(silent=True) or {}
    items = dict(body.get("settings") or {})
    # 浏览器拿到的 AI_PROVIDERS 已脱敏；空值或掩码值表示“保持原 Key”，
    # 防止保存其它设置时把数据库中的真实 Key 清空。
    if "AI_PROVIDERS" in items:
        try:
            incoming = json.loads(str(items["AI_PROVIDERS"]))
            current = _cfg.AI_PROVIDERS
            if isinstance(incoming, dict):
                for pid, provider in incoming.items():
                    if not isinstance(provider, dict):
                        continue
                    key = str(provider.get("apiKey") or "")
                    if not key or key.startswith("****"):
                        old = current.get(pid, {})
                        if old.get("apiKey"):
                            provider["apiKey"] = old["apiKey"]
                items["AI_PROVIDERS"] = json.dumps(incoming, ensure_ascii=False)
        except (TypeError, ValueError):
            return jsonify({"error": "AI_PROVIDERS 必须是合法 JSON"}), 400
    # API Key 通过设置页脱敏回传；空值或掩码值均表示保持已有密钥。
    current_db = _cfg.db_settings()
    for secret_key in ("BRAVE_API_KEY", "TAVILY_API_KEY"):
        if secret_key not in items:
            continue
        incoming = str(items.get(secret_key) or "").strip()
        if not incoming or incoming.startswith("****"):
            items[secret_key] = current_db.get(secret_key) or getattr(_cfg, secret_key, "")
    n = db_save(items)
    reload_config()  # 保存即生效
    return jsonify({"saved": n, "ok": True})


@app.get("/api/ai/providers")
def api_ai_providers():
    """服务商注册表状态：名称/配置与否/当前生效（供设置页左列表与首页提示渲染）。"""
    import config as _cfg
    providers = []
    for pid, p in _cfg.AI_PROVIDERS.items():
        providers.append({
            "id": pid,
            "name": p.get("name") or pid,
            "custom": bool(p.get("custom")),
            "configured": bool(p.get("endpoint") and p.get("model")),
            "endpoint": p.get("endpoint") or "",
            "model": p.get("model") or "",
        })
    return jsonify({
        "providers": providers,
        "active": _cfg.AI_PRIMARY_PROVIDER or "auto",
        "auto": _cfg.pick_provider(),
    })


@app.post("/api/ai/test")
def api_ai_test():
    """测试指定服务商连通性：发一个最小请求，返回模型回显与耗时。"""
    import config as _cfg
    from ai_client import AIClient, AIClientError
    body = request.get_json(silent=True) or {}
    provider = (body.get("provider") or "").strip().lower()
    if provider not in _cfg.AI_PROVIDERS:
        return jsonify({"error": "未知服务商"}), 400
    try:
        reload_config()  # 保存即生效：测试最新配置
        ai = AIClient()
        t0 = datetime.now()
        out = ai.chat(
            [{"role": "user", "content": "请只回复两个字：正常"}],
            provider=provider, temperature=0, max_tokens=20, timeout=(8, 30),
        )
        model = ai._resolve(provider)[2]
        elapsed = round((datetime.now() - t0).total_seconds(), 1)
        return jsonify({"ok": True, "model": model, "reply": (out.strip() or "")[:50], "elapsed": elapsed})
    except AIClientError as e:
        return jsonify({"ok": False, "error": str(e)})


def _parse_models_payload(payload: object) -> list:
    """容错解析 /models 响应：兼容 {data:[{id}]}、{models:[{id}]}、根级 [{id}|"str"] 等变体。"""
    def to_ids(items: object) -> list:
        out = []
        if isinstance(items, list):
            for it in items:
                if isinstance(it, str) and it.strip():
                    out.append(it.strip())
                elif isinstance(it, dict):
                    for k in ("id", "model", "name"):
                        if isinstance(it.get(k), str) and it[k].strip():
                            out.append(it[k].strip())
                            break
        return out
    if isinstance(payload, list):
        return to_ids(payload)
    if isinstance(payload, dict):
        from_data = to_ids(payload.get("data"))
        if from_data:
            return from_data
        from_models = to_ids(payload.get("models"))
        if from_models:
            return from_models
    return []


@app.post("/api/ai/models")
def api_ai_models():
    """拉取指定服务商的可用模型列表（GET {base_url}/models，OpenAI 兼容）。"""
    import config as _cfg
    import requests as rq
    body = request.get_json(silent=True) or {}
    provider = (body.get("provider") or "").strip().lower()
    entry = _cfg.AI_PROVIDERS.get(provider)
    if not entry:
        return jsonify({"error": "未知服务商"}), 400
    endpoint = (entry.get("endpoint") or "").rstrip("/")
    if not endpoint:
        return jsonify({"ok": False, "error": "请先填写接口地址"})
    key = entry.get("apiKey") or ""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        resp = rq.get(f"{endpoint}/models", headers=headers, timeout=12)
        if resp.status_code in (401, 404, 405):
            return jsonify({"ok": False, "error": f"该接口不提供模型列表（HTTP {resp.status_code}）"})
        if resp.status_code != 200:
            return jsonify({"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:120]}"})
        models = _parse_models_payload(resp.json())
        return jsonify({"ok": True, "models": models[:100]})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:200]})


@app.get("/api/sources")
def api_sources_get():
    """信源目录（C1b）：按类别分组返回，含勾选状态。"""
    srcs = db_list_sources()
    groups = []
    for key, meta in CATEGORIES.items():
        items = [
            {k: s[k] for k in ("id", "name", "host", "level", "stype", "enabled", "manual")}
            for s in srcs if s["category"] == key
        ]
        if not items:
            continue
        groups.append({
            "key": key, "label": meta["label"], "icon": meta["icon"],
            "enabled_count": sum(1 for it in items if it["enabled"]),
            "items": items,
        })
    return jsonify({"groups": groups, "total": len(srcs),
                    "enabled_total": sum(1 for s in srcs if s["enabled"])})


@app.post("/api/sources/toggle")
def api_sources_toggle():
    body = request.get_json(silent=True) or {}
    ids = [int(i) for i in (body.get("ids") or [])]
    if not ids or body.get("enabled") is None:
        return jsonify({"error": "参数缺失"}), 400
    n = db_set_enabled(ids, bool(body["enabled"]))
    return jsonify({"ok": True, "updated": n})


@app.post("/api/sources/add")
def api_sources_add():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    host = (body.get("host") or "").strip().lower().replace("http://", "").replace("https://", "").split("/")[0]
    category = (body.get("category") or "portal").strip()
    level = (body.get("level") or "C").strip().upper()
    if not name or not host:
        return jsonify({"error": "名称和域名必填"}), 400
    if category not in CATEGORIES or level not in ("S", "A", "B", "C", "D"):
        return jsonify({"error": "类别或等级无效"}), 400
    sid = db_add_source(name, host, category, level)
    return jsonify({"ok": True, "id": sid})


@app.post("/api/sources/delete")
def api_sources_delete():
    body = request.get_json(silent=True) or {}
    sid = int(body.get("id") or 0)
    ok = db_delete_source(sid)
    return jsonify({"ok": ok, "deleted": ok})


@app.errorhandler(404)
def _not_found(e):
    """统一 JSON 404：前端 fetch 的都是接口，返回 HTML 会让 JSON 解析报 'Unexpected token <'。"""
    return jsonify({"error": "接口不存在（404）。请确认后端已更新并重启（Ctrl+C 停掉后重新 python app.py）。"}), 404


@app.errorhandler(405)
def _method_not_allowed(e):
    return jsonify({"error": "请求方法不允许（405）。请确认后端已更新并重启。"}), 405


@app.errorhandler(413)
def _request_too_large(e):
    return jsonify({"error": "请求体过大，已超过服务器限制"}), 413


@app.errorhandler(500)
def _server_error(e):
    return jsonify({"error": f"服务器内部错误：{e}"}), 500


@app.get("/")
def index():
    # 开发期禁用缓存，避免浏览器持有旧前端导致"改了看不到"
    return Response((FRONTEND / "index.html").read_text(encoding="utf-8"),
                    mimetype="text/html; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "time": datetime.now().isoformat()})


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    print(f"妙舆已启动: http://{args.host}:{args.port}（局域网设备访问请用本机IP）")
    print("提示: 首次启动若 Windows 防火墙弹窗请允许（专用网络）")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
