# -*- coding: utf-8 -*-
"""妙舆 Web 后端（Flask，纯 Python 依赖，无 C 扩展）。

启动：python app.py  → 局域网访问 http://<本机IP>:5000
接口：
    POST /api/analyze          {topic, provider?, verify?} → {task_id}
    GET  /api/tasks            → 历史任务列表
    GET  /api/tasks/<id>       → 任务状态/进度/报告
    GET  /api/reports/<file>   → 下载报告文件（docx/xlsx/json）
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, Response
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
from security import (MIN_TOKEN_LENGTH, SESSION_COOKIE, attach_session_cookie,
                      authorized, client_key, limiter, verify_admin_token)


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
    monitor_service.start()


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
                  "rank": it.get("rank"), "provider": it.get("provider") or b.get("provider", ""),
                  "heat": it.get("heat", {})}
                 for it in visible_items]
        for item in items:
            delta = rank_data["items"].get(content_hash(item["title"], item["url"]), {})
            item.update({"previous_rank": delta.get("previous_rank"),
                         "rank_change": delta.get("rank_change"),
                         "is_new": delta.get("is_new", False)})
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


@app.get("/api/reports/<path:filename>")
def api_report_file(filename: str):
    # 防目录穿越
    safe = Path(filename).name
    if (DATA_DIR_REPORTS / safe).exists():
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
        "AI_PRIMARY_PROVIDER": _cfg.AI_PRIMARY_PROVIDER,
        "ENABLED_GROUPS": ",".join(_cfg.ENABLED_GROUPS),
        "AI_PROVIDERS": json.dumps(public_providers, ensure_ascii=False),
    }
    for k in SECRET_KEYS:
        merged[k] = "****已配置****" if any(
            str(provider.get("apiKey") or "") for provider in _cfg.AI_PROVIDERS.values()
        ) else ""
    return jsonify({"settings": merged, "managed_keys": list(MANAGED_KEYS)})


@app.get("/api/auth/status")
def api_auth_status():
    """返回当前会话是否已认证，不返回令牌或令牌配置内容。"""
    return jsonify({"authenticated": authorized()})


@app.post("/api/auth/login")
def api_auth_login():
    """验证管理员令牌并建立 HttpOnly 会话 Cookie。"""
    body = request.get_json(silent=True) or {}
    token = str(body.get("token") or "").strip()
    if not verify_admin_token(token):
        return jsonify({"ok": False,
                        "error": f"管理员令牌无效（至少需要{MIN_TOKEN_LENGTH}个字符）"}), 401
    response = jsonify({"ok": True})
    response.set_cookie(
        SESSION_COOKIE, token, max_age=86400,
        httponly=True, samesite="Lax", secure=bool(request.is_secure),
    )
    return response


@app.post("/api/auth/logout")
def api_auth_logout():
    response = jsonify({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
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
