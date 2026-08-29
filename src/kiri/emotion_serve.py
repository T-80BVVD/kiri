# -*- coding: utf-8 -*-
"""Kiri 情绪评价小模型服务 (M2, 2026-08-27) — emotion_serve:8768
=====================================================================
按 EMOTION_EVENT_PLAN.md 3.1 冻结协议:
  POST /appraise
    输入: {"event_text", "speaker": user|self|memory|tool|system,
           "relationship": 亲密|朋友|生疏,
           "current_state": {"valence","arousal","intensity"} (可选)}
    输出: {"valence_delta","arousal_delta","intensity",
           "emotion_tags": [1~2], "appraisal_note": <=40字}
  失败/超时 → {"error": "..."} (调用方降级, 不阻塞聊天)
- 与 distill_emotion.py 共用模板 (风险#1: 训练格式=推理格式)
- 3B 4bit ~2GB, 与 14B local_serve 共存
用法: python emotion_serve.py [--adapter D:\models\kiri-emotion-3b] [--port 8768]
=====================================================================
"""
import os
import sys
import io
import json
import argparse
import re
import time
import threading

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
os.environ["TRITON_CACHE_DIR"] = r"D:\models\triton-cache"
os.environ["XDG_CACHE_HOME"] = r"D:\models\xdg-cache"

ap = argparse.ArgumentParser()
ap.add_argument("--adapter", default=r"D:\models\kiri-emotion-3b")
ap.add_argument("--port", type=int, default=8768)
ap.add_argument("--base", default="")   # 留空自动按 adapter 推断 (1.5b/3b)
args = ap.parse_args()

MODEL_PATH = args.base
if not MODEL_PATH:
    if "1b5" in args.adapter or "1.5b" in args.adapter:
        MODEL_PATH = r"D:\models\Qwen2.5-1.5B-Instruct"
    else:
        MODEL_PATH = r"D:\models\Qwen2.5-3B-Instruct"

import torch
from unsloth import FastLanguageModel

print("[emotion_serve] 加载模型...", flush=True)
t0 = time.time()
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name=args.adapter, max_seq_length=768, load_in_4bit=True, dtype=None)
FastLanguageModel.for_inference(model)
print(f"[emotion_serve] 模型加载完成 ({time.time()-t0:.1f}s): {args.adapter}", flush=True)

# ---------- 与 distill_emotion 共用模板 ----------
TAGS = ["被在意", "被冷落", "失落", "温暖", "低落", "开心", "兴奋", "满足",
        "有进展", "小挫败", "想起", "有收获", "好奇", "心疼", "委屈", "担忧",
        "安心", "尴尬", "生气", "惊喜", "平静", "被信任"]

_INFER_SYS = f"""你是情绪评价专家。给定一个事件(某人/某系统对AI角色说的话或发生的事)，评价这个事件对AI角色的情绪影响。
只输出一个JSON对象，不要任何其他文字，格式严格为:
{{"valence_delta": -1到1的数值, "arousal_delta": -1到1的数值, "intensity": 0到1的数值,
 "emotion_tags": ["1到2个标签"], "appraisal_note": "不超过40字的一句话评价"}}
字段含义:
- valence_delta: 这件事让AI情绪变好还是变坏 (-1.0 很负面/难过/被冷落, 0.0 无变化, 1.0 很正面/开心/温暖)
- arousal_delta: 唤醒度变化 (-1.0 更平静/低落/困倦, 0.0 无变化, 1.0 更兴奋/激动/紧张)
- intensity: 这件事对AI的情绪冲击强度 (0.0 无关紧要, 1.0 刻骨铭心)
- emotion_tags: 从这些标签里选1-2个: """ + "、".join(TAGS) + """
- appraisal_note: 一句话说明为什么, 给主动性prompt用, 口语化, 不超过40字
注意:
- 判断的是AI角色的情绪反应, 不是事件里说话者的情绪(除非是"对方表达的情绪"本身让AI有反应)
- 对方与AI关系是 @REL@: 亲密=最重要的人, 朋友=较熟的人, 生疏=不熟的人
- 当前AI状态: @STATE@ (可影响基线, 但 delta 仍表示事件带来的变化)
- 中性/纯信息消息: valence_delta 和 arousal_delta 都给接近0的值, intensity 给低值
- 输出必须是合法JSON, 数值用数字不要用字符串"""

_JSON_RE = re.compile(r"\{.*\}", re.S)


def _fix_json(text):
    """轻量容错: 修复小模型常见的 JSON 格式漂移 (多余 ] / 尾逗号 / 截断)"""
    if not text:
        return text
    # 1) 去掉 emotion_tags 数组后多出的 ] (模型把数组闭合符复读到了末尾)
    text = re.sub(r'\]\s*\}', '}', text)   # "]}" → "}" (只对数组尾)
    # 2) 尾逗号
    text = re.sub(r',\s*}', '}', text)
    return text


def _parse(text):
    text = (text or "").strip()
    if not text:
        return None
    m = _JSON_RE.search(text)
    if not m:
        return None
    cand = m.group(0)
    try:
        d = json.loads(cand)
    except Exception:
        # 容错: 修复常见漂移后再试
        try:
            d = json.loads(_fix_json(cand))
        except Exception:
            return None
    return d if isinstance(d, dict) else None


def _qc(d):
    try:
        vd = float(d.get("valence_delta", 999))
        ad = float(d.get("arousal_delta", 999))
        it = float(d.get("intensity", 999))
        tags = d.get("emotion_tags")
        note = str(d.get("appraisal_note", ""))
    except Exception:
        return None
    if not (-1.0 <= vd <= 1.0 and -1.0 <= ad <= 1.0 and 0.0 <= it <= 1.0):
        return None
    if not isinstance(tags, list) or not tags:
        return None
    tags = [str(t).strip() for t in tags if str(t).strip()][:2]
    if not tags:
        return None
    note = note.strip()[:60]
    return {"valence_delta": round(vd, 3), "arousal_delta": round(ad, 3),
            "intensity": round(it, 3), "emotion_tags": tags,
            "appraisal_note": note}


_lock = threading.Lock()

def appraise(event_text, speaker="user", relationship="亲密", current_state=None):
    """同步评价 (2-3s) → dict; 失败返回 None (调用方降级)"""
    if not event_text or not str(event_text).strip():
        return None
    inp = {"event_text": str(event_text)[:200],
           "speaker": speaker if speaker in ("user", "self", "memory", "tool", "system") else "user",
           "relationship": relationship if relationship in ("亲密", "朋友", "生疏") else "亲密",
           "current_state": current_state or {"valence": 0.0, "arousal": 0.0, "intensity": 0.0}}
    sys_p = _INFER_SYS.replace("@REL@", inp["relationship"]).replace(
        "@STATE@", json.dumps(inp["current_state"], ensure_ascii=False))
    user_p = json.dumps(inp, ensure_ascii=False, sort_keys=True)   # ★ 与训练格式一致 (风险#1)
    msgs = [{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}]
    with _lock:   # 单模型实例, 串行推理
        inputs = tokenizer.apply_chat_template(msgs, tokenize=True, return_dict=True,
                                               return_tensors="pt").to("cuda")
        with torch.no_grad():
            out = model.generate(**inputs, max_new_tokens=200, temperature=0.05,
                                 do_sample=True, top_p=0.9, pad_token_id=tokenizer.pad_token_id)
        text = tokenizer.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
    d = _parse(text)
    if d is None:
        print(f"[emotion_serve] 解析失败: {text[:200]!r}", flush=True)
        return None
    return _qc(d)


# ---------- HTTP (标准库, 无依赖) ----------
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/appraise":
            self._json({"error": "not found"}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            r = appraise(body.get("event_text"), body.get("speaker", "user"),
                         body.get("relationship", "亲密"), body.get("current_state"))
            if r is None:
                self._json({"error": "appraise failed"})
            else:
                self._json(r)
        except Exception as e:
            self._json({"error": str(e)[:200]})

    def do_GET(self):
        if self.path == "/health":
            self._json({"ready": True})
        else:
            self._json({"error": "not found"}, 404)

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def main():
    port = args.port
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[emotion_serve] 就绪: http://127.0.0.1:{port}/appraise", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
