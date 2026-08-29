# -*- coding: utf-8 -*-
"""Kiri 本地推理服务 — 优化版 (2026-08-22 雾弥: 速度优化)
=====================================================================
路线: transformers 4bit + QLoRA (v2 adapter) — ExLlamaV2 在 Blackwell 上
kernel 崩溃(0xC0000005), 回退此路线并做三项优化:

① sdpa attention: attn_implementation="sdpa" (torch 2.11 原生,
   Blackwell 上比 eager 快 1.3-1.5x, 显存更省)
② KV 前缀缓存: system prompt (决策循环每轮不变) 只 prefill 一次,
   之后每轮只 prefill user 部分 — 决策循环省 2-4s/轮
③ 流式输出: SSE /generate_stream — agent 决策边流边解析 JSON,
   不等完整输出 (长回复体验大幅提升)

接口:
  POST /generate         {system, user, max_tokens, temperature} -> {response}
  POST /generate_stream  {system, user, max_tokens, temperature} -> SSE 流
  GET  /health           {ready}

用法: python kiri/local_serve.py   (后台常驻, 端口 8767)
切换: config.ENGINE = "local" + LOCAL_SERVE_URL 指向 8767
=====================================================================
"""
import os
import sys
import json
import io
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
os.environ["TRITON_CACHE_DIR"] = r"D:\models\triton-cache"
os.environ["XDG_CACHE_HOME"] = r"D:\models\xdg-cache"
os.makedirs(r"D:\models\triton-cache", exist_ok=True)

BASE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = r"D:\models\Qwen2.5-14B-Instruct"
LORA_PATH = r"D:\models\kiri-14b-lora_v22"   # ★ v2.2 正式版 (2026-08-27: 工具行为0/6→5/6, 护城河全守住)
PORT = 8767

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from peft import PeftModel

# ---- 全局模型 (懒加载, 首次请求时加载) ----
_model, _tokenizer = None, None
_load_lock = None
_ready = [False]
# system 前缀 KV 缓存: {system 文本 -> (input_ids, past_key_values)}
# 决策循环 system 不变 → 每轮只 prefill user 部分
_prefix = {"system": None, "ids": None, "past": None}


def get_model():
    global _model, _tokenizer, _load_lock
    if _ready[0]:
        return _model, _tokenizer
    if _load_lock is None:
        _load_lock = __import__("threading").Lock()
    with _load_lock:
        if _ready[0]:
            return _model, _tokenizer
        t0 = time.time()
        print(f"[local_serve] 加载模型 4bit + adapter (sdpa)... ({time.time():.0f})", flush=True)
        bnb = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16, bnb_4bit_use_double_quant=True)
        model = AutoModelForCausalLM.from_pretrained(
            MODEL_PATH, quantization_config=bnb, device_map="auto",
            trust_remote_code=True, attn_implementation="sdpa")
        model = PeftModel.from_pretrained(model, LORA_PATH)
        model.eval()
        tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, trust_remote_code=True)
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token
        _model, _tokenizer = model, tokenizer
        _ready[0] = True
        print(f"[local_serve] 模型就绪 ({time.time()-t0:.0f}s)", flush=True)
        return model, tokenizer


def _split_prompt(tokenizer, system, user):
    """拆成 [system 段, user+assistant 段], 让 system KV 可缓存
    ChatML: <|im_start|>system\nX<|im_end|>\n<|im_start|>user\nY<|im_end|>\n<|im_start|>assistant\n
    → 前缀: <|im_start|>system\nX<|im_end|>\n   (可缓存)
      后缀: <|im_start|>user\nY<|im_end|>\n<|im_start|>assistant\n
    """
    if system:
        pre = f"<|im_start|>system\n{system}<|im_end|>\n"
    else:
        pre = ""
    suf = f"<|im_start|>user\n{user}<|im_end|>\n<|im_start|>assistant\n"
    return pre, suf


def _get_prefix_kv(model, tokenizer, system):
    """获取 system 前缀的 KV (缓存; 变了才重算)"""
    if not system:
        return None, None
    if _prefix["system"] == system and _prefix["past"] is not None:
        return _prefix["ids"], _prefix["past"]
    pre, _ = _split_prompt(tokenizer, system, "")
    ids = tokenizer(pre, return_tensors="pt").to(model.device)
    with torch.no_grad():
        out = model(input_ids=ids["input_ids"], use_cache=True)
    _prefix["system"] = system
    _prefix["ids"] = ids
    _prefix["past"] = out.past_key_values
    print(f"[local_serve] system 前缀 KV 已缓存 ({ids['input_ids'].shape[1]} tokens)", flush=True)
    return ids, out.past_key_values


def _generate(model, tokenizer, system, user, max_tokens, temperature):
    """非流式生成 (带 system KV 复用)"""
    pre, suf = _split_prompt(tokenizer, system, user)
    past_ids, past_kv = _get_prefix_kv(model, tokenizer, system)
    suf_ids = tokenizer(suf, return_tensors="pt").to(model.device)
    past_len = past_ids["input_ids"].shape[1] if past_ids is not None else 0
    # attention_mask 必须覆盖 past+当前 (generate 靠它定位 past 长度)
    attn_mask = torch.ones((1, past_len + suf_ids["input_ids"].shape[1]),
                           dtype=torch.long, device=model.device)
    with torch.no_grad():
        out = model.generate(
            input_ids=suf_ids["input_ids"],
            attention_mask=attn_mask,
            past_key_values=past_kv,
            max_new_tokens=max_tokens,
            temperature=temperature, do_sample=True,
            pad_token_id=tokenizer.eos_token_id,
            repetition_penalty=1.05,
            use_cache=True)
    # out 不含 past: [suf + 生成] — 从 suf 长度处截取生成部分
    return tokenizer.decode(out[0][suf_ids["input_ids"].shape[1]:],
                            skip_special_tokens=True).strip()


def generate(system, user, max_tokens=500, temperature=0.85):
    model, tokenizer = get_model()
    return _generate(model, tokenizer, system, user, max_tokens, temperature)


def generate_stream(system, user, max_tokens=500, temperature=0.85):
    """流式生成: yield 文本块 (供 SSE / 直接调用)"""
    model, tokenizer = get_model()
    pre, suf = _split_prompt(tokenizer, system, user)
    past_ids, past_kv = _get_prefix_kv(model, tokenizer, system)
    suf_ids = tokenizer(suf, return_tensors="pt").to(model.device)
    past_len = past_ids["input_ids"].shape[1] if past_ids is not None else 0
    attn_mask = torch.ones((1, past_len + suf_ids["input_ids"].shape[1]),
                           dtype=torch.long, device=model.device)

    from transformers import TextIteratorStreamer
    streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
    gen_kwargs = dict(
        input_ids=suf_ids["input_ids"], attention_mask=attn_mask, past_key_values=past_kv,
        max_new_tokens=max_tokens, temperature=temperature, do_sample=True,
        pad_token_id=tokenizer.eos_token_id, repetition_penalty=1.05,
        use_cache=True, streamer=streamer)
    import threading
    t = threading.Thread(target=lambda: model.generate(**gen_kwargs), daemon=True)
    t.start()
    for chunk in streamer:
        yield chunk


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            t0 = time.time()
            if self.path == "/generate_stream":
                self._send_stream(body, t0)
            else:
                resp = generate(
                    body.get("system", ""), body.get("user", ""),
                    max_tokens=int(body.get("max_tokens", 500)),
                    temperature=float(body.get("temperature", 0.85)))
                self._send({"response": resp, "latency": round(time.time() - t0, 2)})
        except Exception as e:
            try:
                self._send({"response": "", "error": str(e)}, code=500)
            except Exception:
                pass

    def _send_stream(self, body, t0):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for chunk in generate_stream(
                    body.get("system", ""), body.get("user", ""),
                    max_tokens=int(body.get("max_tokens", 500)),
                    temperature=float(body.get("temperature", 0.85))):
                if chunk:
                    data = json.dumps({"chunk": chunk}, ensure_ascii=False)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
            done = json.dumps({"done": True, "latency": round(time.time() - t0, 2)})
            self.wfile.write(f"data: {done}\n\n".encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                err = json.dumps({"error": str(e)}, ensure_ascii=False)
                self.wfile.write(f"data: {err}\n\n".encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

    def do_GET(self):
        if self.path == "/health":
            self._send({"ready": _ready[0]})
        else:
            self._send({"service": "kiri-local-serve", "ready": _ready[0]})

    def _send(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"[local_serve] 启动 http://127.0.0.1:{PORT} (优化版: sdpa+KV前缀缓存+流式)", flush=True)
    import threading
    threading.Thread(target=get_model, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.serve_forever()
