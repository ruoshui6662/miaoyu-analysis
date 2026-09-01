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
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, Response

from config import DATA_DIR_REPORTS, DATA_DIR_TASKS, ROOT
from pipeline import run_analysis

app = Flask(
    __name__,
    static_folder=str(ROOT / "frontend"),
    static_url_path="",
)
app.config["JSON_AS_ASCII"] = False

TASKS: dict[str, dict] = {}
_LOCK = threading.Lock()
FRONTEND = ROOT / "frontend"


def _new_task(topic: str, provider: str | None, verify: bool) -> str:
    tid = uuid.uuid4().hex[:12]
    task = {
        "id": tid,
        "topic": topic,
        "provider": provider or "auto",
        "verify": bool(verify),
        "status": "pending",       # pending|running|done|error
        "step": "",
        "detail": "",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "report": None,
        "error": "",
    }
    with _LOCK:
        TASKS[tid] = task

    def _progress(step: str, detail: str):
        with _LOCK:
            task["step"] = step
            task["detail"] = detail

    def _bg():
        with _LOCK:
            task["status"] = "running"
            task["step"] = "prepare"
            task["detail"] = "任务已开始"
        try:
            rep = run_analysis(
                topic,
                provider=provider,
                verify=verify,
                progress=_progress,
            )
            with _LOCK:
                task["status"] = "done"
                task["report"] = rep
                task["step"] = "done"
                task["detail"] = f"完成，耗时 {rep.get('elapsed_sec', '?')}s"
        except Exception as e:  # noqa: BLE001  Web 层兜底
            with _LOCK:
                task["status"] = "error"
                task["error"] = str(e)
                task["detail"] = f"任务失败: {e}"

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
    with _LOCK:
        items = [{
            "id": t["id"], "topic": t["topic"], "status": t["status"],
            "created_at": t["created_at"], "step": t["step"], "detail": t["detail"],
        } for t in TASKS.values()]
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return jsonify(items)


@app.get("/api/tasks/<tid>")
def api_task(tid: str):
    with _LOCK:
        t = TASKS.get(tid)
    if not t:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({
        "id": t["id"], "topic": t["topic"], "status": t["status"],
        "step": t["step"], "detail": t["detail"],
        "created_at": t["created_at"], "error": t["error"],
        "report": t["report"],
    })


@app.get("/api/reports/<path:filename>")
def api_report_file(filename: str):
    # 防目录穿越
    safe = Path(filename).name
    if (DATA_DIR_REPORTS / safe).exists():
        return send_from_directory(str(DATA_DIR_REPORTS), safe, as_attachment=False)
    return jsonify({"error": "文件不存在"}), 404


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