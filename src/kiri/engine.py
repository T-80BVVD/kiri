# -*- coding: utf-8 -*-
"""Kiri 引擎 — LLM 封装 (API + 本地双通道, 2026-08-16)
API 已验证(主动/幻觉研究); 本地待验证.
"""
import json
import os
import urllib.request
import urllib.error
import config


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
        # ★ 30s 超时 (原90s: 卡死会堵住 qq_bridge 单worker = 假掉线)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"].strip()
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
        with urllib.request.urlopen(req, timeout=90) as resp:
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
