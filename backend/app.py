# -*- coding: utf-8 -*-
"""舆情分析系统 Web 后端（Flask，纯 Python 依赖，无 C 扩展）。

启动：python app.py  → 局域网访问 http://<本机IP>:5000
接口：
    POST /api/analyze          {topic, provider?, verify?} → {task_id}
    GET  /api/tasks            → 历史任务列表
    GET  /api/tasks/<id>       → 任务状态/进度/报告
    GET  /api/reports/<file>   → 下载报告文件（docx/xlsx/json）
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, Response

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
from pipeline import run_analysis
from source_catalog import CATALOG, CATEGORIES

# 启动时播种内置信源目录（表为空才写，幂等）
db_seed_sources(CATALOG)

app = Flask(
    __name__,
    static_folder=str(ROOT / "frontend"),
    static_url_path="",
)
app.config["JSON_AS_ASCII"] = False

FRONTEND = ROOT / "frontend"


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
            # 报告 JSON 由 pipeline 已落盘 data/reports/*.json（与 docx 同名）
            docx = rep.get("docx") or ""
            report_file = docx[:-5] + ".json" if docx.lower().endswith(".docx") else ""
            summary = {
                "title": rep.get("title"),
                "sections": len(rep.get("sections") or []),
                "docx": docx, "md": rep.get("md", ""),
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


@app.get("/api/reports/list")
def api_reports_list():
    """历史报告文件清单（docx/md，按修改时间倒序），B3 补充入口。"""
    files = []
    for p in sorted(DATA_DIR_REPORTS.glob("*.*"), key=lambda x: -x.stat().st_mtime):
        if p.suffix.lower() in (".docx", ".md") and not p.name.startswith("~"):
            files.append({"name": p.name, "kind": p.suffix.lstrip(".").upper(),
                          "url": f"/api/reports/{p.name}", "size": p.stat().st_size})
    return jsonify(files[:100])


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

    AI_PROVIDERS 为服务商注册表 JSON（含 API Key 明文，仅本机客户端面板使用，
    密钥存储在 password 输入框内，从不打印）。
    """
    import config as _cfg
    merged = {
        "SEARXNG_URL": _cfg.SEARXNG_URL,
        "AI_PRIMARY_PROVIDER": _cfg.AI_PRIMARY_PROVIDER,
        "ENABLED_GROUPS": ",".join(_cfg.ENABLED_GROUPS),
        "AI_PROVIDERS": json.dumps(_cfg.AI_PROVIDERS, ensure_ascii=False),
    }
    for k in SECRET_KEYS:
        merged[k] = "****已配置****" if os.getenv(k) else ""
    return jsonify({"settings": merged, "managed_keys": list(MANAGED_KEYS)})


@app.post("/api/settings")
def api_settings_save():
    body = request.get_json(silent=True) or {}
    items = body.get("settings") or {}
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


@app.errorhandler(500)
def _server_error(e):
    return jsonify({"error": f"服务器内部错误：{e}"}), 500


@app.get("/")
def index():
    return Response((FRONTEND / "index.html").read_text(encoding="utf-8"),
                    mimetype="text/html; charset=utf-8")


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "time": datetime.now().isoformat()})


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    print(f"舆情分析系统已启动: http://{args.host}:{args.port}（局域网设备访问请用本机IP）")
    print("提示: 首次启动若 Windows 防火墙弹窗请允许（专用网络）")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)