# -*- coding: utf-8 -*-
"""user_mood.py — 用户情绪画像聚合层 (吸收 N.E.K.O master_emotion 思想, 仅聚合层)
=====================================================================
边界 (雾弥 2026-08-19 确认): 
- 这是"读雾弥的情绪", 不是"她的内心" — emotion_core 完全不动
- 输入: 已有的 _analyze_emotion 输出 (valence/arousal/salience) + confidence + external_intent
- 聚合: 滑动窗口(最近20条/24h), 按 新鲜度 x 重要性(salience) x 置信度(confidence) 加权
- 产出: 每用户 {主导情绪, 波动度} → ①describe注入(她"知道"雾弥最近心情) ②主动时机(低落时陪伴)
- 成本: 零额外LLM调用 (复用现有情绪解析), 零额外延迟; JSON持久化
=====================================================================
"""
import os
import sys
import time
import json
import math

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MOOD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_mood.json")

WINDOW_MAX = 20            # 滑动窗口: 最近20条读数
WINDOW_HOURS = 24.0        # 且 24h 内
MIN_READINGS = 3           # 最少几条才出画像 (太少不判断)
CONFIDENCE_FLOOR = 0.4     # 低置信读数不进入聚合 (防误判)
GENTLE_VALENCE = -0.25     # 主导情绪低于此 + 波动高 → 陪伴模式
GENTLE_VOLATILITY = 0.30   # 波动度阈值


def _mood_text(valence):
    """主导情绪值 → 文本 (给prompt/describe用)"""
    if valence <= -0.5:
        return "很低落"
    if valence <= -0.25:
        return "有点低落"
    if valence < 0.25:
        return "平静"
    if valence < 0.5:
        return "心情不错"
    return "很开心"


class UserMoodProfile:
    """每用户的情绪画像: {user: {readings: [{v,a,s,c,ts}], profile: {val, vol, n, at}}}"""

    def __init__(self, path=MOOD_FILE):
        self.path = path
        self._data = {}   # user -> {"readings": [...], "profile": {...}}
        self.load()

    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
        except Exception:
            self._data = {}

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
        except Exception:
            pass

    # ---- 记录一条情绪读数 (来自 _analyze_emotion) ----
    def record(self, user, reading, ts=None):
        """reading: {valence, arousal, salience, confidence?, external_intent?}
        confidence 低于下限的读数不入库 (防把玩笑当低落)"""
        try:
            if not reading:
                return
            v = float(reading.get("valence", 0.0) or 0.0)
            a = float(reading.get("arousal", 0.0) or 0.0)
            s = float(reading.get("salience", 0.0) or 0.0)
            c = float(reading.get("confidence", 1.0) or 1.0)
            if c < CONFIDENCE_FLOOR:
                return   # 低置信: 不采信
            ts = ts or time.time()
            u = str(user or "雾弥")
            entry = {"v": max(-1.0, min(1.0, v)), "a": max(-1.0, min(1.0, a)),
                     "s": max(0.0, min(1.0, s)), "c": max(0.0, min(1.0, c)), "ts": ts}
            rec = self._data.setdefault(u, {})
            rec.setdefault("readings", []).append(entry)
            # 窗口裁剪: 只留 最近WINDOW_MAX条 且 24h内
            now = time.time()
            rec["readings"] = [r for r in rec["readings"] if now - r["ts"] <= WINDOW_HOURS * 3600][-WINDOW_MAX:]
            rec["profile"] = self._aggregate(rec["readings"], now)
            # 瘦身持久化: profile 只留聚合结果, readings 太多就不写 (重启后从新读数重建)
        except Exception:
            pass

    @staticmethod
    def _aggregate(readings, now=None):
        """加权聚合: 权重 = 新鲜度(1-age/24h) x (0.5+salience) x confidence
        产出 {val, vol, n, at} — val=主导情绪, vol=波动度(平均绝对差)"""
        now = now or time.time()
        if not readings:
            return {"val": 0.0, "vol": 0.0, "n": 0, "at": 0.0}
        ws, vals = [], []
        for r in readings:
            age = max(0.0, (now - r["ts"]) / 3600.0)
            freshness = max(0.0, 1.0 - age / WINDOW_HOURS)
            w = freshness * (0.5 + r["s"]) * r["c"]
            ws.append(w)
            vals.append(r["v"])
        sw = sum(ws) or 1.0
        val = sum(w * v for w, v in zip(ws, vals)) / sw
        vol = sum(abs(v - val) for v in vals) / len(vals)
        return {"val": round(val, 3), "vol": round(vol, 3), "n": len(readings), "at": now}

    # ---- 读画像 ----
    def profile(self, user, now=None):
        """当前画像; 读数不够返回空 dict"""
        u = str(user or "雾弥")
        rec = self._data.get(u, {})
        prof = rec.get("profile") or {}
        if prof.get("n", 0) < MIN_READINGS:
            return {}
        return prof

    def should_be_gentle(self, user, now=None):
        """陪伴模式: 主导情绪低落 且 波动高 (他最近心情差又起伏 → 少闹多陪)"""
        prof = self.profile(user, now)
        if not prof:
            return False
        return prof["val"] < GENTLE_VALENCE and prof["vol"] >= GENTLE_VOLATILITY

    def describe_for(self, user, now=None):
        """给 prompt 的一句话: "雾弥最近心情有点低落" / "" (读数不够则空)"""
        prof = self.profile(user, now)
        if not prof:
            return ""
        who = user or "雾弥"
        txt = _mood_text(prof["val"])
        vol_txt = "、情绪起伏比较大" if prof["vol"] >= GENTLE_VOLATILITY else ""
        n = prof["n"]
        return f"{who}最近{txt}{vol_txt}(基于最近{n}次互动的感知)"

    def stats(self):
        """调试: 各用户画像概览"""
        out = {}
        for u, rec in self._data.items():
            prof = rec.get("profile") or {}
            if prof.get("n", 0) >= MIN_READINGS:
                out[u] = {"val": prof["val"], "vol": prof["vol"], "n": prof["n"]}
        return out


if __name__ == "__main__":
    # 自测 (纯逻辑)
    p = UserMoodProfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_mood.json"))
    p._data.clear()
    t = time.time()
    # 1. 读数不够 → 无画像
    assert p.profile("雾弥") == {}, "读数不够不应出画像"
    # 2. 持续低落+高波动 → gentle
    for i in range(5):
        p.record("雾弥", {"valence": -0.4 + (0.35 if i % 2 else -0.35), "arousal": 0.3,
                          "salience": 0.6, "confidence": 0.9}, ts=t + i * 60)
    prof = p.profile("雾弥", now=t + 360)
    assert prof and prof["val"] < -0.25, prof
    assert p.should_be_gentle("雾弥", now=t + 360), "低落+波动应陪伴模式"
    print(f"低落画像 OK: val={prof['val']} vol={prof['vol']} n={prof['n']}")
    d = p.describe_for("雾弥", now=t + 360)
    assert "低落" in d, d
    print("describe_for OK:", d)
    # 3. 低置信读数不入库
    p2 = UserMoodProfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_mood.json"))
    p2._data.clear()
    p2.record("雾弥", {"valence": -0.9, "arousal": 0.5, "salience": 0.3, "confidence": 0.1}, ts=t)
    assert p2.profile("雾弥", now=t) == {}, "低置信不应入库"
    print("低置信过滤 OK")
    # 4. 开心画像 → 非 gentle
    p3 = UserMoodProfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_mood.json"))
    p3._data.clear()
    for i in range(5):
        p3.record("雾弥", {"valence": 0.6, "arousal": 0.4, "salience": 0.5, "confidence": 0.9},
                  ts=t + i * 60)
    assert not p3.should_be_gentle("雾弥", now=t + 360), "开心不应陪伴模式"
    print("开心画像 OK: val=", p3.profile("雾弥", now=t + 360)["val"])
    print("user_mood 自测 PASS")
    try:
        os.remove(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_mood.json"))
    except Exception:
        pass
