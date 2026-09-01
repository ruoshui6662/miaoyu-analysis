"""OpenAI 兼容多后端 AI 客户端：本地 9router / DeepSeek / 通义千问(可开 enable_search)。

用法：
    from ai_client import AIClient
    ai = AIClient()
    text = ai.chat([{"role": "user", "content": "..."}], provider="deepseek")
"""
from __future__ import annotations

import json

import requests

from config import AI_ROUTER, DEEPSEEK, QWEN, HTTP_TIMEOUT, USER_AGENT, pick_provider

DEEPSEEK_BASE = "https://api.deepseek.com/v1"
QWEN_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"

_PROVIDERS = {
    # name -> (base_url, api_key, default_model, enable_search)
    "router": (AI_ROUTER["base_url"], AI_ROUTER["api_key"], AI_ROUTER["model"], False),
    "deepseek": (DEEPSEEK_BASE, DEEPSEEK["api_key"], DEEPSEEK["model"], False),
    "qwen": (QWEN_BASE, QWEN["api_key"], QWEN["model"], QWEN["enable_search"]),
}


class AIClientError(RuntimeError):
    pass


class AIClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def _resolve(self, provider: str | None) -> tuple[str, str, str, bool]:
        name = (provider or pick_provider()).lower()
        if name not in _PROVIDERS:
            raise AIClientError(f"未知 provider: {name}（可选 router/deepseek/qwen）")
        base, key, model, search = _PROVIDERS[name]
        if not base:
            raise AIClientError(f"provider '{name}' 未配置 base_url（请检查 .env）")
        if not key and name in ("deepseek", "qwen"):
            raise AIClientError(f"provider '{name}' 缺少 API key（请填写 .env）")
        if name == "router" and not key:
            key = "local"  # 本地网关常不需要 key
        return base, key, model or "default", search

    _RETRY_BACKOFF = (2, 5, 10)

    def _build_body(self, messages, model, temperature, max_tokens, stream, json_mode, enable_search, search):
        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if enable_search if enable_search is not None else search:
            body["enable_search"] = True
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def _request(self, url, headers, body_json, timeout, stream):
        """通用 POST，带 429 退避重试（返回最终响应，调用方负责 close/读取）。"""
        last_err = ""
        for attempt in range(3):
            resp = self.session.post(url, headers=headers, data=body_json, timeout=timeout, stream=stream)
            if resp.status_code == 429:
                last_err = resp.text[:200]
                resp.close()
                import time as _t
                _t.sleep(self._RETRY_BACKOFF[attempt])
                continue
            return resp
        raise AIClientError(f"API 持续限流(429): {last_err}")

    def _chat_nonstream(self, messages, provider=None, model=None, temperature=0.3,
                        max_tokens=6000, json_mode=False, enable_search=None,
                        timeout=(10, 300), **ignored):
        """非流式调用（流式空结果/网关不支持时的兜底路径）。"""
        base, key, model_, search = self._resolve(provider)
        url = f"{base}/chat/completions"
        body = self._build_body(messages, model or model_, temperature, max_tokens,
                                False, json_mode, enable_search, search)
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        resp = self._request(url, headers, json.dumps(body, ensure_ascii=False), timeout, stream=False)
        if resp.status_code != 200:
            raise AIClientError(f"API {resp.status_code}: {resp.text[:300]}")
        try:
            content = resp.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError):
            raise AIClientError(f"响应格式异常: {resp.text[:300]}")
        if not content.strip():
            # 网关对长请求偶发返回 200+空内容 → 显式报错供上层降级重试
            raise AIClientError("模型返回空内容（网关偶发行为，已触发降级重试）")
        return content

    def chat_stream(
        self,
        messages: list[dict],
        provider: str | None = None,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 6000,
        json_mode: bool = False,
        enable_search: bool | None = None,
        timeout: tuple | int = (10, 300),
        on_chunk: callable | None = None,
    ) -> str:
        """流式调用 chat/completions（SSE），逐句回调 on_chunk(text)，返回完整文本。

        - 429 限流：自动退避重试（最多 3 次）
        - 流式返回空（部分网关对 json+stream 组合偶发）：自动降级为非流式重发
        """
        base, key, model_, search = self._resolve(provider)
        url = f"{base}/chat/completions"
        body = self._build_body(messages, model or model_, temperature, max_tokens,
                                True, json_mode, enable_search, search)
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        resp = self._request(url, headers, json.dumps(body, ensure_ascii=False), timeout, stream=True)
        try:
            if resp.status_code != 200:
                raise AIClientError(f"API {resp.status_code}: {resp.text[:300]}")
            parts: list[str] = []
            for line in resp.iter_lines():
                if not line or line.startswith(b":"):
                    continue
                if line.startswith(b"data:"):
                    payload = line[5:].strip()
                    if payload in (b"[DONE]", b"[done]"):
                        break
                    try:
                        j = json.loads(payload)
                        delta = (j.get("choices") or [{}])[0].get("delta", {}).get("content")
                    except (ValueError, KeyError, IndexError):
                        continue
                    if delta:
                        parts.append(delta)
                        if on_chunk:
                            on_chunk(delta)
            text = "".join(parts)
        except requests.RequestException as e:
            raise AIClientError(f"流式请求失败: {e}") from e
        finally:
            resp.close()

        if not text.strip():
            # 网关对 json+stream 偶发返回空 → 非流式兜底
            return self._chat_nonstream(messages, provider=provider, model=model,
                                        temperature=temperature, max_tokens=max_tokens,
                                        json_mode=json_mode, enable_search=enable_search,
                                        timeout=timeout)
        return text

    def chat(self, messages: list[dict], **kw) -> str:
        """默认走非流式（实测本链路流式每章慢 2-3 倍且偶发空响应）。

        需要流式进度时显式调用 chat_stream(..., on_chunk=...)。
        """
        return self._chat_nonstream(messages, **kw)

    def chat_json(self, messages: list[dict], **kw) -> dict:
        """要求模型返回 JSON 对象并解析（分析流水线中间结果用）。"""
        text = self.chat(messages, json_mode=True, **kw)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 模型偶尔输出带 ```json 包裹，做一次剥离重试
            s = text.strip().removeprefix("```json").removesuffix("```").strip()
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                raise AIClientError(f"模型未返回合法 JSON: {text[:300]}")


def name_check(base: str) -> str:
    for name, (b, *_rest) in _PROVIDERS.items():
        if b and b.split("/")[-1] in base:
            return name
    return "AI"


if __name__ == "__main__":
    ai = AIClient()
    try:
        r = ai.chat([{"role": "user", "content": "用一句话解释什么是舆情分析"}], max_tokens=200)
        print("AI 回复:", r)
    except AIClientError as e:
        print("AIClient 未就绪:", e)