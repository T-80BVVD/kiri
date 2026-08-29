# -*- coding: utf-8 -*-
"""Kiri 引擎 — LLM 封装 (API + 本地双通道, 2026-08-16)
API 已验证(主动/幻觉研究); 本地待验证.
"""
import json
import os
import threading
import time
import urllib.request
import urllib.error
import config


# =====================================================================
# 每日 API 费用预算 (2026-08-29): 控制她"自主行动"的 API 花费
#   甜点区 =< SWEET_SPOT (默认25元): 正常自主
#   警戒区 SWEET_SPOT~HARD_LIMIT (25~40元): 自主降频 (只做高价值的事)
#   超上限 > HARD_LIMIT (40元): 自主完全静默 (你主动@她 不受限)
#   单价做成 config 可调 (价格会涨/有峰谷). 默认按 DeepSeek 调价后:
#     输入 1.5元 / 百万token, 输出 4.5元 / 百万token
# =====================================================================
class Budget:
    def __init__(self):
        self._cost_today = 0.0
        self._day = time.localtime().tm_yday
        self._lock = threading.Lock()

    def _rollover(self):
        """跨天重置"""
        now_day = time.localtime().tm_yday
        if now_day != self._day:
            self._day = now_day
            self._cost_today = 0.0

    def _account(self, prompt_tokens, completion_tokens):
        """token 数 → 元, 累加 (按 config 单价)"""
        self._rollover()
        price_in = getattr(config, "PRICE_INPUT_PER_M", 1.5)
        price_out = getattr(config, "PRICE_OUTPUT_PER_M", 4.5)
        cost = (prompt_tokens / 1e6 * price_in) + (completion_tokens / 1e6 * price_out)
        with self._lock:
            self._cost_today += cost
        return cost

    def spent(self):
        self._rollover()
        return self._cost_today

    def sweet_left(self):
        return max(0.0, getattr(config, "BUDGET_SWEET", 25.0) - self._cost_today)

    def hard_left(self):
        return max(0.0, getattr(config, "BUDGET_HARD", 40.0) - self._cost_today)

    # 自主是否可运行: 甜点内 true, 警戒区降频(返回"limited"), 超上限 false
    def autonomous_state(self):
        self._rollover()
        c = self._cost_today
        if c > getattr(config, "BUDGET_HARD", 40.0):
            return "blocked"      # 自主静默
        if c > getattr(config, "BUDGET_SWEET", 25.0):
            return "limited"      # 自主降频
        return "ok"


budget = Budget()


def _account_usage(raw):
    """从 API 响应 JSON 里提 token usage 计费"""
    try:
        u = raw.get("usage") or {}
        pt = u.get("prompt_tokens", 0)
        ct = u.get("completion_tokens", 0)
        budget._account(pt, ct)
    except Exception:
        pass


def _read_key():
    p = os.path.expanduser(r"~\.dsh\.credentials.yaml")
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f.read().splitlines():
                if line.strip().startswith("DEEPSEEK_API_KEY:"):
                    v = line.split(":", 1)[1].strip().strip("'\"")
                    if v:
                        return v
    except OSError:
        pass
    return os.environ.get("DEEPSEEK_API_KEY")


def _call_api(system, user, max_tokens, temperature):
    key = _read_key()
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY not found")
    body = json.dumps({
        "model": config.API_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST")
    try:
        # ★ 绕过系统代理直连 (2026-08-29): 系统 ProxyEnable=1 指向失效的 31181,
        #    urllib 默认读它 → 每次请求先连死代理 → 单次 API 拖到 10s+。直连 2s。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        # ★ 30s 超时 (原90s: 卡死会堵住 qq_bridge 单worker = 假掉线)
        with opener.open(req, timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
            _account_usage(raw)   # ★ 计费 (2026-08-29)
            return raw["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API HTTP {e.code}: {e.read()[:200]}") from e


def _call_local(system, user, max_tokens, temperature):
    """本地推理服务 (local_serve.py: 4bit + QLoRA adapter, ChatML)
    2026-08-22 替换原 Ollama 通道 (本地化部署)"""
    body = json.dumps({
        "system": system, "user": user,
        "max_tokens": max_tokens, "temperature": temperature,
    }).encode("utf-8")
    req = urllib.request.Request(
        config.LOCAL_SERVE_URL + "/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))["response"].strip()


def generate(system, user, max_tokens=500, temperature=0.85, _retry=0):
    """统一入口: 按配置选择引擎; ★防空回复: V4-flash偶发返回空内容 → 重试1次"""
    try:
        if config.ENGINE == "api":
            out = _call_api(system, user, max_tokens, temperature)
        else:
            out = _call_local(system, user, max_tokens, temperature)
    except Exception:
        if _retry < 1:
            return generate(system, user, max_tokens, temperature, _retry=_retry + 1)
        raise
    if not out or not out.strip():
        if _retry < 1:
            return generate(system, user, max_tokens, temperature, _retry=_retry + 1)
        return "(……)"
    return out


def generate_api(system, user, max_tokens=500, temperature=0.85, _retry=0):
    """★ 强制走云端 API (router/精挑用 — 不受 ENGINE=local 影响)
    轻量判断任务 (工具路由/参数提取) 用云端便宜又快, 保证准确率"""
    try:
        out = _call_api(system, user, max_tokens, temperature)
    except Exception:
        if _retry < 1:
            return generate_api(system, user, max_tokens, temperature, _retry=_retry + 1)
        raise
    if not out or not out.strip():
        if _retry < 1:
            return generate_api(system, user, max_tokens, temperature, _retry=_retry + 1)
        return "(……)"
    return out


def _call_local_stream(system, user, max_tokens, temperature):
    """本地流式 (local_serve SSE): 逐个 yield 文本 chunk"""
    body = json.dumps({
        "system": system, "user": user,
        "max_tokens": max_tokens, "temperature": temperature,
    }).encode("utf-8")
    req = urllib.request.Request(
        config.LOCAL_SERVE_URL + "/generate_stream",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line or not line.startswith("data: "):
                continue
            try:
                obj = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if "chunk" in obj:
                yield obj["chunk"]
            elif "done" in obj:
                break


def generate_stream(system, user, max_tokens=500, temperature=0.85):
    """流式生成 (SSE): 逐个产出文本 chunk (Neuro 式'边说边出')
    API 走 DeepSeek SSE; local 走 local_serve SSE"""
    if config.ENGINE != "api":
        try:
            yield from _call_local_stream(system, user, max_tokens, temperature)
            return
        except Exception:
            pass  # 本地流式失败 → 回退 API
    key = _read_key()
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY not found")
    body = json.dumps({
        "model": config.API_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST")
    try:
        # ★ 绕过系统代理直连 (2026-08-29): 系统代理指向失效的 31181, urllib 默认读它 → 拖慢
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=90) as resp:
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        delta = obj["choices"][0].get("delta", {}).get("content", "")
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API HTTP {e.code}: {e.read()[:200]}") from e
