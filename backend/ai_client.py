"""OpenAI 兼容多后端 AI 客户端：一套通用服务商注册表（内置品牌 + 自定义，见 config.AI_PROVIDERS）。

用法：
    from ai_client import AIClient
    ai = AIClient()
    text = ai.chat([{"role": "user", "content": "..."}], provider="deepseek")
"""
from __future__ import annotations

import json
import time

import requests

from config import USER_AGENT, pick_provider
import config as _cfg


def _providers() -> dict:
    """从 config 注册表动态读取（设置页保存后 reload() 即生效，无需重启）。

    注意必须用 _cfg 模块点访问：config.reload() 会整体替换模块级变量，
    `from config import X` 的快照不会随之更新。
    返回 name -> (base_url, api_key, default_model, enable_search)
    """
    ps: dict = {}
    for pid, p in _cfg.AI_PROVIDERS.items():
        ps[pid] = (
            p.get("endpoint") or "",
            p.get("apiKey") or "",
            p.get("model") or "",
            bool(p.get("enable_search")),
        )
    return ps


class AIClientError(RuntimeError):
    pass


class AIClient:
    # ---------- 通道容灾（借鉴 LiteLLM router：retry + model/provider fallback + cooldown） ----------
    _RETRY_BACKOFF_429 = (2, 5, 10)   # 限流：3 次，2s/5s/10s（README 沉淀结论，不改）
    _RETRY_BACKOFF_5XX = (5,)          # 5xx/连接：2 次、单次 5s 间隔（长请求失败每次要等网关超时，少试一次省 ~1 分钟）
    _COOLDOWN_AUTH_SEC = 3600          # 鉴权/配置错误：任务期内不会自愈，冷却 60 分钟
    _COOLDOWN_FLAKY_SEC = 300          # 抖动类（5xx/超时/429/空响应）：连续 2 次失败后冷却 5 分钟
    _ALLOWED_FLAKY_FAILS = 2
    _cooldowns: dict = {}    # provider_id -> 冷却到期时间戳（进程级共享）
    _fail_counts: dict = {}  # provider_id -> 连续抖动失败次数（成功后清零）
    # 通道级错误特征（可换模型/换服务商救活）；业务级错误（格式/内容）不触发换源
    _CHANNEL_MARKS = ("API 401", "API 403", "API 408", "API 429", "API 5",
                      "持续不可用", "持续限流", "连接持续失败", "流式请求失败", "空内容",
                      "未配置", "缺少 API key", "response_format", "json_object")
    _AUTH_MARKS = ("API 401", "API 403", "缺少 API key", "未配置")

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.last_provider = ""  # 最近一次成功调用的 provider（预检固定用）

    def _provider_chain(self, provider: str | None) -> list[str]:
        """故障转移链：显式指定（别名归一）→ auto 选择 → 其余注册服务商。
        冷却期内的服务商被跳过（全部冷却时保留链头再试一次，保留原始报错）。"""
        from config import AI_PROVIDERS as _AP
        ids = list(_AP.keys())
        want = (provider or "").lower().strip()
        first = None
        if want in ("", "auto"):
            first = pick_provider() or None
        elif want in ("router", "local", "custom"):
            customs = [pid for pid in ids
                       if _AP.get(pid, {}).get("custom") and _AP[pid].get("endpoint") and _AP[pid].get("model")]
            first = customs[0] if customs else None
        elif want in ids:
            first = want
        chain = ([first] if first else []) + [p for p in ids if p != first]
        now = time.time()
        live = [p for p in chain if self._cooldowns.get(p, 0) <= now]
        return live or chain[:1]

    def _is_channel_error(self, msg: str) -> bool:
        return any(m in msg for m in self._CHANNEL_MARKS)

    def _is_auth_error(self, msg: str) -> bool:
        return any(m in msg for m in self._AUTH_MARKS)

    def _model_candidates(self, pid: str) -> list[str]:
        """该服务商的模型候选：配置主模型 + 后备模型表（fallback_models，≤5）。
        同一网关下不同模型对应不同上游，主模型上游故障时轮替后备。"""
        p = _cfg.AI_PROVIDERS.get(pid) or {}
        cands: list[str] = []
        if p.get("model"):
            cands.append(p["model"])
        for m in (p.get("fallback_models") or []):
            if isinstance(m, str) and m.strip() and m.strip() not in cands:
                cands.append(m.strip())
            if len(cands) >= 5:
                break
        return cands or [""]

    def _resolve(self, provider: str | None) -> tuple[str, str, str, bool]:
        from config import AI_PROVIDERS as _AP, AI_PRIMARY_PROVIDER as _APP
        name = (provider or "").lower().strip()
        if name in ("", "auto"):
            name = pick_provider()
        elif name in ("router", "local", "custom"):
            # 旧别名 → 注册表中可用自定义服务商（9router，id 可能随迁移变化）
            customs = [pid for pid, p in _AP.items()
                       if p.get("custom") and p.get("endpoint") and p.get("model")]
            name = customs[0] if len(customs) == 1 else (_APP or (customs[0] if customs else pick_provider()))
        ps = _providers()
        if name not in ps:
            raise AIClientError(f"未知 provider: {name}（请在设置页配置服务商）")
        base, key, model, search = ps[name]
        entry = _cfg.AI_PROVIDERS.get(name, {})
        if not base:
            raise AIClientError(f"provider '{name}' 未配置接口地址（Base URL）")
        if not model:
            raise AIClientError(f"provider '{name}' 未配置模型名称")
        if not key and entry.get("custom") is not True:
            raise AIClientError(f"provider '{name}' 缺少 API key（请填写 .env）")
        return base, key, model, search

    def _build_body(self, messages, model, temperature, max_tokens, stream, json_mode, enable_search, search,
                    enable_thinking=False):
        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if enable_search if enable_search is not None else search:
            body["enable_search"] = True
        # 关闭推理模式：混合推理模型的 reasoning_tokens 计入 max_tokens 预算，
        # 实测会把 JSON 分析类请求的内容额度吃光（返回 200+空内容），且拖慢生成。
        # 网关不识别此参数时会忽略（OpenAI 兼容惯例）。
        body["enable_thinking"] = bool(enable_thinking)
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def _request(self, url, headers, body_json, timeout, stream):
        """通用 POST：429 退避重试 3 次（2s/5s/10s）；5xx/连接异常重试 2 次（间隔 5s）。"""
        err_429, n_429 = "", 0
        err_5xx, n_5xx = "", 0
        while True:
            try:
                resp = self.session.post(url, headers=headers, data=body_json, timeout=timeout, stream=stream)
            except (requests.ConnectionError, requests.Timeout) as e:
                n_5xx += 1
                err_5xx = f"连接失败: {type(e).__name__}"
                if n_5xx > len(self._RETRY_BACKOFF_5XX):
                    raise AIClientError(f"连接持续失败: {err_5xx}") from e
                time.sleep(self._RETRY_BACKOFF_5XX[n_5xx - 1])
                continue
            if resp.status_code == 429:
                n_429 += 1
                err_429 = resp.text[:160]
                resp.close()
                if n_429 > len(self._RETRY_BACKOFF_429):
                    raise AIClientError(f"API 持续限流(429): {err_429}")
                time.sleep(self._RETRY_BACKOFF_429[n_429 - 1])
                continue
            if resp.status_code in (500, 502, 503, 504):
                n_5xx += 1
                err_5xx = f"HTTP {resp.status_code}: {resp.text[:160]}"
                resp.close()
                if n_5xx > len(self._RETRY_BACKOFF_5XX):
                    raise AIClientError(f"API 持续不可用({resp.status_code}): {err_5xx}")
                time.sleep(self._RETRY_BACKOFF_5XX[n_5xx - 1])
                continue
            return resp

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
            # 网关对长请求偶发/频发返回 200+空内容：自动退避重试（3s/6s），仍空才报错
            for _delay in (3, 6):
                time.sleep(_delay)
                resp2 = self._request(url, headers, json.dumps(body, ensure_ascii=False),
                                      timeout, stream=False)
                if resp2.status_code == 200:
                    try:
                        content = resp2.json()["choices"][0]["message"]["content"] or ""
                    except (KeyError, IndexError, ValueError):
                        content = ""
                    if content.strip():
                        return content
            raise AIClientError("模型连续返回空内容（网关不稳定，非配置错误）")
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

    def chat(self, messages: list[dict], provider: str | None = None, **kw) -> str:
        """非流式调用 + 双层故障转移：服务商链 × 每服务商模型候选（主模型→后备模型）。

        通道级失败（5xx/超时/连接失败/限流耗尽/密钥无效/空响应/网关不支持 json 模式）
        先换同服务商的后备模型，再换下一个服务商；业务级错误（响应格式/内容）不换源直接上抛。
        鉴权类错误立即长冷却；抖动类连续失败 2 次才短冷却（给小请求/下一章节留重试机会）。
        设计参照 LiteLLM router：retry + fallbacks + cooldown。
        需要流式进度时显式调用 chat_stream(..., on_chunk=...)。
        """
        chain = self._provider_chain(provider)
        errs = []
        for pid in chain:
            models = self._model_candidates(pid)
            auth_dead = False
            for m in models:
                try:
                    text = self._chat_nonstream(messages, provider=pid, model=m, **kw)
                    AIClient._fail_counts.pop(pid, None)
                    self.last_provider = pid
                    if m != models[0]:
                        print(f"       [AI 故障转移] {pid} 主模型不可用，本轮改用后备模型：{m}")
                    elif pid != chain[0]:
                        print(f"       [AI 故障转移] {chain[0]} 不可用，本轮改用服务商：{pid}")
                    return text
                except AIClientError as e:
                    msg = str(e)
                    errs.append(f"{pid}/{m}: {msg[:90]}")
                    if not self._is_channel_error(msg):
                        raise  # 业务级错误（格式/内容），不是通道问题
                    if self._is_auth_error(msg):
                        AIClient._cooldowns[pid] = time.time() + self._COOLDOWN_AUTH_SEC
                        print(f"       [AI 故障转移] {pid} 鉴权/配置错误（冷却 60 分钟）：{msg[:90]}")
                        auth_dead = True
                        break
                    print(f"       [AI 故障转移] {pid}/{m} 通道异常：{msg[:90]}")
            if auth_dead:
                continue
            # 该服务商全部模型候选均抖动失败 → 计次，达阈值才冷却
            cnt = AIClient._fail_counts.get(pid, 0) + 1
            AIClient._fail_counts[pid] = cnt
            if cnt >= self._ALLOWED_FLAKY_FAILS:
                AIClient._cooldowns[pid] = time.time() + self._COOLDOWN_FLAKY_SEC
                print(f"       [AI 故障转移] {pid} 连续 {cnt} 次失败，冷却 {self._COOLDOWN_FLAKY_SEC // 60} 分钟")
        raise AIClientError("所有 AI 服务商均不可用：" + " ｜ ".join(errs[:4]))

    def preflight(self, provider: str | None = None) -> str:
        """连通性冒烟测试（单 token 请求，走完整故障转移链）。

        返回实际可用的 provider id（供任务全程固定使用，跳过死通道）；
        全链失败抛 AIClientError —— 任务开始前快速失败，
        避免采集跑完后产出四个"待分析"空章节。
        """
        # 注意：该系模型带推理开销（reasoning_tokens 计入 max_tokens 预算），
        # 预检预算给 128，避免推理吃光额度返回空内容误判为通道故障。
        self.chat([{"role": "user", "content": "只回复一个字：常"}],
                  provider=provider, temperature=0, max_tokens=128, timeout=(8, 30))
        return self.last_provider or (provider or "")

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
    for name, (b, *_rest) in _providers().items():
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