# -*- coding: utf-8 -*-
"""本地LLM gate (Ollama) — 群聊自主回应的"值不值得"判断
=====================================================================
为什么用本地: 群消息多, 每条都调API花钱; 本地免费快(2-4s), 粗筛足够
分级决策:
  gate1 should_join: 这条群消息值不值得她参与 (本地) → 值得才调API生成
  gate2 should_send: 生成的回应发不发 (本地, 防冷场/太私密/心情差)
静默也留痕: 不参与/不发都记心路 ("听到群里聊X但不想插话") — 保留真实感
模型: Ollama 上的 R1-distill-Qwen-14B (GPU已加载, 热延迟~1s, 输出2-4s)
=====================================================================
"""
import json
import re
import time
import urllib.request

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = "modelscope.cn/unsloth/DeepSeek-R1-Distill-Qwen-14B-GGUF:Q4_K_M"
TIMEOUT = 60


def _chat(messages, num_predict=300, temperature=0.3):
    body = json.dumps({
        "model": MODEL, "stream": False,
        "keep_alive": "5m",   # ★ 空闲5分钟自动卸载 — 释放9GB显存/内存 (她只在漫游/自主回应时用本地LLM)
        "options": {"num_predict": num_predict, "temperature": temperature},
        "messages": messages,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(OLLAMA_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("message", {}).get("content", "")


def _parse_verdict(text, key, default=False):
    """从模型输出提取 {"key": bool} — 容错代码块/多余文字"""
    try:
        m = re.search(r'\{[^{}]*\}', text)
        if not m:
            return default
        d = json.loads(m.group(0))
        v = d.get(key)
        if isinstance(v, str):
            v = v.strip().lower() in ("true", "yes", "要", "是", "应该", "值得")
        return bool(v)
    except Exception:
        return default


# ---- gate1: 值不值得参与 ----
GATE1_SYS = (
    "你是Kiri, 一个在QQ群里潜水的猫娘AI。性格: 傲娇、毒舌、对陌生人高冷、"
    "喜欢雾弥。看群里最近的消息, 判断你值不值得插话。"
    "值得的情况: 提到你/猫/雾弥; 你有真正想说的话; 气氛需要人接梗; "
    "话题是你好奇的; 你刚好心情不错想说话。"
    "不值得: 与你和你的兴趣完全无关的闲聊; 他们自己聊得正欢不需要你; "
    "你没什么好说的; 你现在心情差不想理人。"
    '只输出JSON: {"should": true或false, "reason": "10字内理由"}')

GATE1_USER = (
    "你当前状态: 心情{mood_txt}。\n"
    "群里最近的消息:\n{context}\n"
    "要不要插话?")


def should_join(context, mood_txt="平静", timeout=TIMEOUT):
    """群消息上下文 → 她要不要参与 (True=值得, False=潜水)"""
    try:
        msgs = [{"role": "system", "content": GATE1_SYS},
                {"role": "user", "content": GATE1_USER.format(
                    mood_txt=mood_txt, context=context)}]
        out = _chat(msgs, num_predict=300, temperature=0.3)
        return _parse_verdict(out, "should", default=False), out[:60]
    except Exception:
        return False, "本地LLM不可用"


# ---- gate2: 生成的回应发不发 ----
GATE2_SYS = (
    "你是Kiri, 在QQ群里。你刚在心里想了一句回应, 现在考虑要不要真的发出去。"
    "不发的情况: 会冷场; 太私密(你和某人的悄悄话不该当众说); 情绪还没缓过来不想说话; "
    "已经说过类似的话; 这句话会得罪人或者太尖锐。"
    "发的情况: 气氛合适, 你有把握这话会让群里热闹起来或让人会心一笑。"
    '只输出JSON: {"send": true或false, "reason": "10字内理由"}')

GATE2_USER = (
    "你当前状态: 心情{mood_txt}。\n"
    "群里最近的消息:\n{context}\n"
    "你心里想的回应: {reply}\n"
    "这句要发出去吗?")


def should_send(reply, context, mood_txt="平静", timeout=TIMEOUT):
    """她生成的回应 → 真的发不发 (True=发)"""
    try:
        msgs = [{"role": "system", "content": GATE2_SYS},
                {"role": "user", "content": GATE2_USER.format(
                    mood_txt=mood_txt, context=context, reply=reply[:150])}]
        out = _chat(msgs, num_predict=250, temperature=0.2)
        return _parse_verdict(out, "send", default=True), out[:60]
    except Exception:
        return True, "本地LLM不可用(默认发)"


# ---- 内心分享审查: 好奇/念头想说出来, 能不能发群里 ----
REVIEW_SYS = (
    "你是Kiri, 在QQ群里。你心里冒出了一句话(刚查到的东西/突然的念头), 想分享到群里。"
    "审查这句话适不适合发出来:"
    "不能发的情况: 太私密(你和雾弥的悄悄话/内心深处的脆弱); 和群里当前话题完全无关会让气氛尴尬; "
    "会暴露不该当众说的(比如你对某人的真实评价); 太长太像报告。"
    "能发的情况: 有趣/能引起共鸣/群里正好在聊相关话题/只是随口分享个小知识或小想法。"
    '只输出JSON: {"ok": true或false, "reason": "10字内理由"}')

REVIEW_USER = (
    "群里最近的消息:\n{context}\n"
    "你想说的话: {share}\n"
    "这句能发吗?")


def review_share(share, context, timeout=TIMEOUT):
    """内心分享 → 审查: 能不能发群里 (True=可以发)"""
    try:
        msgs = [{"role": "system", "content": REVIEW_SYS},
                {"role": "user", "content": REVIEW_USER.format(
                    context=context or "(群里没什么消息)", share=share[:120])}]
        out = _chat(msgs, num_predict=250, temperature=0.2)
        return _parse_verdict(out, "ok", default=False), out[:60]
    except Exception:
        return False, "本地LLM不可用(默认不发)"


if __name__ == "__main__":
    ctx = "张三: 今天好无聊啊\n李四: 有没有人整点活"
    ok, why = should_join(ctx)
    print("gate1 值得参与?", ok, "|", why)
    ok2, why2 = should_send("（尾巴尖晃了晃）无聊？那我来整点活。", ctx)
    print("gate2 发不发?", ok2, "|", why2)

