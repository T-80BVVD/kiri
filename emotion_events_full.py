# -*- coding: utf-8 -*-
"""Kiri 情绪事件 — 完全体 (FULL, 2026-08-27 存档版, 当前不启用)
=====================================================================
背景: 阉割版 (emotion_events.py) 因 16GB 卡显存限制 (14B+小模型共存紧),
砍掉了双池/视角化评价/每轮 think 更新。本文档代码为完整设计实现,
未来更换硬件 (更大显存/更强 GPU) 后启用。

完全体特性 (相对阉割版新增):
  1. 双池: perspective=user (对话者心情) vs perspective=kiri (Kiri 内心)
     - user_msg 事件双写: user 视角记"用户心情" + kiri 视角记"Kiri 反应"
     - current_user() / current_kiri() 分开聚合 (不再混浆糊)
  2. 视角化小模型评价: small_appraise 按 perspective 换 prompt
     - user 视角: 问"说话者(雾弥)情绪如何" (valence 指用户的)
     - kiri 视角: 问"这件事对 AI 情绪影响" (valence 指 Kiri 的)
  3. 每轮 think 更新心情: agent 每解析出一轮 <think> → record("thought", fast)
     + 立即 current_kiri() 同步 (想的事改变心情, 心情影响下一个想法)
     - "说话不更新": <speak> 只是输出, 不产生情绪事件 (内心状态不变)
  4. 完整小模型评价: 3B 优先 (无显存压力时), 可切 1.5B; refine 补评可选关

启用步骤 (未来):
  1. kiri.py: import emotion_events_full as emotion_events_mod (替换阉割版)
  2. config: EMOTION_REFINE_AFTER_REPLY 可关 (完全体每轮 think 已实时更新)
  3. agent.py: _parse_think_speak 每轮解析后调 kiri._on_think_step(think_text)
  4. emotion_serve 用 3B adapter (D:\\models\\kiri-emotion-3b)

协议: 与阉割版共用 EMOTION_EVENT_PLAN.md 3.1/3.2/3.3 冻结协议,
仅新增 perspective 字段 (schema_version 升 2, 只加字段不删字段 — 风险#2 对策)
=====================================================================
"""
import json
import os
import time
import threading
import uuid
import urllib.request

import config as _cfg

SCHEMA_VERSION = 2

# ---- source 常量 (与阉割版一致) ----
SRC_USER = "user_msg"
SRC_THOUGHT = "thought"
SRC_TOOL = "tool_result"
SRC_GOAL = "goal"
SRC_PROACTIVE = "proactive_outcome"
SRC_TIME = "time_routine"

# ---- perspective (完全体新增) ----
P_USER = "user"     # 对话者 (雾弥) 的心情
P_KIRI = "kiri"     # Kiri 自己的心情

# user_msg 双写映射: user 视角 (用户心情) + kiri 视角 (Kiri 反应)
_DUAL_SOURCES = {SRC_USER}
# 纯 Kiri 内心来源: 只写 kiri 池
_KIRI_ONLY_SOURCES = {SRC_THOUGHT, SRC_TOOL, SRC_GOAL, SRC_PROACTIVE, SRC_TIME}

REL_WEIGHT = {"亲密": 1.0, "朋友": 0.6, "生疏": 0.3}
_DEFAULT_DECAY = {"user_msg": 3.0, "thought": 1.0, "tool_result": 1.0,
                  "goal": 24.0, "proactive_outcome": 6.0, "time_routine": 6.0}
_KW_POS = ["开心", "高兴", "喜欢", "谢谢", "好棒", "棒", "爱", "想你", "温柔", "记得", "夸", "厉害", "顺利"]
_KW_NEG = ["难过", "伤心", "累", "烦", "生气", "气死", "气人", "讨厌", "哭", "失眠", "生病", "病了", "骂", "委屈", "失望", "难受", "压力"]


class EmotionEventsFull:
    """完全体: 双池事件记录 / 视角化评价 / 双聚合 / 由头候选"""

    def __init__(self, filepath, enabled=True, retention_recent=500,
                 retention_high_rel=0.6, agg_min_decay=0.02):
        self.filepath = filepath
        self.enabled = enabled
        self.retention_recent = retention_recent
        self.retention_high_rel = retention_high_rel
        self.agg_min_decay = agg_min_decay
        self._events = []          # 每条含 perspective 字段
        self._lock = threading.Lock()
        self._load()

    # ---------------- 存储 ----------------
    def _load(self):
        if not os.path.exists(self.filepath):
            return
        try:
            with open(self.filepath, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._events.append(json.loads(line))
                    except Exception:
                        pass
            self._prune()
        except Exception:
            pass

    def _append_file(self, ev):
        try:
            with open(self.filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _rewrite_file(self):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                for e in self._events:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _prune(self):
        """按池分别保留: 最近 retention_recent 条 + relevance 高长留存"""
        self._events.sort(key=lambda e: e.get("ts", 0))
        seen = {}
        for pool in (P_USER, P_KIRI):
            pool_ev = [e for e in self._events if e.get("perspective") == pool]
            recent = pool_ev[-self.retention_recent:] if self.retention_recent else pool_ev
            high = [e for e in pool_ev
                    if e.get("appraisal", {}).get("relevance", 0) >= self.retention_high_rel]
            for e in recent + high:
                seen[e.get("id", uuid.uuid4().hex)] = e
        self._events = sorted(seen.values(), key=lambda e: e.get("ts", 0))

    # ---------------- 视角化规则评价 ----------------
    @classmethod
    def rule_appraise(cls, source, event_text, relationship="亲密", extra=None,
                      perspective=P_KIRI):
        extra = extra or {}
        if perspective == P_USER and source == SRC_USER:
            # user 视角: 判断"说话者情绪" — 关键词直读, 关系权重不衰减 (是他/她的情绪)
            kw = cls._kw_sentiment(event_text)
            if abs(kw) < 0.05:
                return None
            return {"valence": round(kw, 3), "arousal": round(min(0.7, abs(kw) + 0.1), 3),
                    "relevance": 1.0}
        # kiri 视角: 事件对 AI 的影响 (原阉割版逻辑)
        if source == SRC_USER:
            sentiment = extra.get("sentiment")
            kw = cls._kw_sentiment(event_text)
            if sentiment is None:
                sentiment = kw
            elif abs(sentiment) < 0.05 and abs(kw) > 0.05:
                sentiment = kw
            rel_w = REL_WEIGHT.get(relationship, 0.5)
            if abs(sentiment) < 0.05:
                return None
            return {"valence": round(sentiment * rel_w, 3),
                    "arousal": round(min(0.7, abs(sentiment) + 0.1), 3),
                    "relevance": round(rel_w, 3)}
        if source == SRC_THOUGHT:
            return {"valence": 0.15, "arousal": 0.2, "relevance": 0.4}
        if source == SRC_TOOL:
            ok = extra.get("ok", True)
            return {"valence": 0.1 if ok else -0.1, "arousal": 0.1, "relevance": 0.2}
        if source == SRC_GOAL:
            action = extra.get("action", "done")
            return {"valence": {"done": 0.3, "progress": 0.15, "drop": -0.2}.get(action, 0.0),
                    "arousal": 0.2, "relevance": 0.7}
        if source == SRC_PROACTIVE:
            replied = extra.get("replied")
            if replied is None:
                return None
            return {"valence": 0.25 if replied else -0.25, "arousal": 0.2, "relevance": 0.6}
        if source == SRC_TIME:
            return {"valence": -0.1, "arousal": 0.1, "relevance": 0.3}
        return None

    @staticmethod
    def _kw_sentiment(text):
        p = sum(1 for w in _KW_POS if w in (text or ""))
        n = sum(1 for w in _KW_NEG if w in (text or ""))
        if p + n == 0:
            return 0.0
        return max(-1.0, min(1.0, (p - n) / (p + n)))

    # ---------------- 视角化小模型评价 ----------------
    @staticmethod
    def small_appraise(event_text, speaker="user", relationship="亲密",
                       current_state=None, perspective=P_KIRI, timeout=3.0):
        """按视角调情绪小模型 (emotion_serve /appraise)
        完全体: emotion_serve 需支持 perspective 字段 (未来升级), 暂用 speaker 区分
        user 视角 → speaker=user 问说话者情绪; kiri 视角 → 原语义"""
        url = getattr(_cfg, "EMOTION_SERVE_URL", "http://127.0.0.1:8768") + "/appraise"
        body = json.dumps({
            "event_text": str(event_text)[:200],
            "speaker": speaker, "relationship": relationship,
            "current_state": current_state or {"valence": 0.0, "arousal": 0.0, "intensity": 0.0},
            "perspective": perspective,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                d = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None
        if not isinstance(d, dict) or "valence_delta" not in d:
            return None
        return d

    @classmethod
    def appraise(cls, source, event_text, relationship="亲密", extra=None,
                 perspective=P_KIRI):
        """视角化统一评价: 小模型优先 → 规则降级
        返回 (appraisal_dict, emotion_tags, appraisal_note)"""
        extra = extra or {}
        if getattr(_cfg, "EMOTION_EVENTS_ENABLED", True):
            try:
                sm = cls.small_appraise(
                    event_text,
                    speaker={"user_msg": "user", "thought": "self",
                             "memory_recall": "memory", "tool_result": "tool",
                             "goal": "self", "proactive_outcome": "self",
                             "time_routine": "system"}.get(source, "user"),
                    relationship=relationship,
                    current_state=extra.get("current_state"),
                    perspective=perspective,
                    timeout=getattr(_cfg, "EMOTION_SMALL_TIMEOUT", 3.0))
                if sm is not None:
                    ap = {"valence": round(float(sm["valence_delta"]), 3),
                          "arousal": round(float(sm["arousal_delta"]), 3),
                          "relevance": round(min(1.0, float(sm.get("intensity", 0.3)) + 0.3), 3)}
                    tags = [str(t).strip() for t in sm.get("emotion_tags", []) if str(t).strip()][:2]
                    note = str(sm.get("appraisal_note", "")).strip()[:60]
                    return ap, tags, note
            except Exception:
                pass
        ap = cls.rule_appraise(source, event_text, relationship, extra, perspective)
        if ap is None:
            return None, [], ""
        return ap, cls.rule_tags(source, ap, extra, perspective), ""

    @classmethod
    def rule_tags(cls, source, appraisal, extra=None, perspective=P_KIRI):
        extra = extra or {}
        if perspective == P_USER and source == SRC_USER:
            v = appraisal.get("valence", 0)
            if v >= 0.4:
                return ["开心"]
            if v <= -0.4:
                return ["难过"]
            if v > 0:
                return ["愉悦"]
            return ["低落"]
        if source == SRC_USER:
            v = appraisal.get("valence", 0)
            if v >= 0.4:
                return ["被在意"]
            if v <= -0.4:
                return ["被冷落"] if extra.get("silence_min", 0) > 30 else ["失落"]
            if v > 0:
                return ["温暖"]
            return ["低落"]
        if source == SRC_THOUGHT:
            return ["想起"]
        if source == SRC_TOOL:
            return ["有收获"] if extra.get("ok", True) else ["小挫败"]
        if source == SRC_GOAL:
            return {"done": ["满足"], "progress": ["有进展"], "drop": ["失落"]}.get(
                extra.get("action", "done"), [])
        if source == SRC_PROACTIVE:
            return ["被回应"] if extra.get("replied") else ["被冷落"]
        return []

    # ---------------- 记录 (双写) ----------------
    def record(self, source, event_text, cause_ref=None, appraisal=None,
               relationship="亲密", extra=None, emotion_tags=None, decay_hours=None,
               perspective=None, fast=False):
        """统一事件采集入口。perspective 默认按 source 推断:
          user_msg → 双写 (user + kiri 两条); 其余 → kiri 池"""
        if not self.enabled:
            return None
        extra = extra or {}
        if perspective:
            return self._record_one(source, event_text, cause_ref, appraisal,
                                    relationship, extra, emotion_tags, decay_hours,
                                    perspective, fast)
        if source in _DUAL_SOURCES:
            # 双写: 用户心情 + Kiri 反应
            ev_user = self._record_one(source, event_text, cause_ref, appraisal,
                                       relationship, extra, emotion_tags, decay_hours,
                                       P_USER, fast)
            ev_kiri = self._record_one(source, event_text, cause_ref, appraisal,
                                       relationship, extra, emotion_tags, decay_hours,
                                       P_KIRI, fast)
            return ev_kiri or ev_user
        return self._record_one(source, event_text, cause_ref, appraisal,
                                relationship, extra, emotion_tags, decay_hours,
                                P_KIRI, fast)

    def _record_one(self, source, event_text, cause_ref, appraisal,
                    relationship, extra, emotion_tags, decay_hours, perspective, fast):
        note = ""
        if appraisal is None:
            if fast:
                ap = self.rule_appraise(source, event_text, relationship, extra, perspective)
                if ap is None:
                    return None
                appraisal = ap
                emotion_tags = emotion_tags or self.rule_tags(source, ap, extra, perspective)
            else:
                appraisal, tags, note = self.appraise(source, event_text, relationship,
                                                      extra, perspective)
                if emotion_tags is None:
                    emotion_tags = tags
        if appraisal is None:
            return None
        intensity = float(extra.get("intensity", 0.5))
        tags = emotion_tags or self.rule_tags(source, appraisal, extra, perspective)
        ev = {
            "schema_version": SCHEMA_VERSION,
            "id": "ev_%s_%s" % (perspective, uuid.uuid4().hex[:10]),
            "ts": float(extra.get("ts", time.time())),
            "source": source,
            "perspective": perspective,          # ★ 完全体: user|kiri
            "event_text": str(event_text)[:200],
            "cause_ref": cause_ref,
            "appraisal": {"valence": round(float(appraisal.get("valence", 0)), 3),
                          "arousal": round(float(appraisal.get("arousal", 0)), 3),
                          "relevance": round(float(appraisal.get("relevance", 0.3)), 3)},
            "emotion_tags": tags[:2],
            "intensity": round(intensity, 3),
            "decay_hours": float(decay_hours or _DEFAULT_DECAY.get(source, 3.0)),
            "appraisal_note": note[:60],
        }
        with self._lock:
            self._events.append(ev)
            self._prune()
        self._append_file(ev)
        return ev

    def refine_by_ids(self, event_ids, timeout=8.0):
        """事后小模型补评 (与阉割版同)"""
        if not self.enabled or not event_ids:
            return 0
        fixed = 0
        with self._lock:
            targets = [e for e in self._events if e.get("id") in event_ids]
        for e in targets:
            try:
                sm = self.small_appraise(
                    e.get("event_text", ""),
                    speaker={"user_msg": "user", "thought": "self",
                             "memory_recall": "memory", "tool_result": "tool",
                             "goal": "self", "proactive_outcome": "self",
                             "time_routine": "system"}.get(e.get("source", "user_msg"), "user"),
                    relationship="亲密",
                    perspective=e.get("perspective", P_KIRI),
                    current_state={"valence": 0.0, "arousal": 0.0, "intensity": 0.0},
                    timeout=timeout)
                if sm is None:
                    continue
                ap = {"valence": round(float(sm["valence_delta"]), 3),
                      "arousal": round(float(sm["arousal_delta"]), 3),
                      "relevance": round(min(1.0, float(sm.get("intensity", 0.3)) + 0.3), 3)}
                tags = [str(t).strip() for t in sm.get("emotion_tags", []) if str(t).strip()][:2]
                note = str(sm.get("appraisal_note", "")).strip()[:60]
                e["appraisal"] = ap
                if tags:
                    e["emotion_tags"] = tags
                if note:
                    e["appraisal_note"] = note
                fixed += 1
            except Exception:
                continue
        if fixed:
            self._rewrite_file()
        return fixed

    # ---------------- 双池聚合 ----------------
    def _agg(self, perspective, now=None):
        now = now or time.time()
        total_w = 0.0
        mood_sum = 0.0
        aro_sum = 0.0
        contributing = []
        with self._lock:
            for e in self._events:
                if e.get("perspective") != perspective:
                    continue
                age_h = (now - e.get("ts", now)) / 3600.0
                decay = 2 ** (-age_h / max(0.1, e.get("decay_hours", 3.0)))
                if decay < self.agg_min_decay:
                    continue
                w = e.get("intensity", 0.5) * decay
                mood_sum += e["appraisal"]["valence"] * w
                aro_sum += e["appraisal"]["arousal"] * w
                total_w += w
                if w >= 0.15:
                    contributing.append({"event_text": e["event_text"],
                                         "intensity": round(w, 3),
                                         "ts": e.get("ts"), "tags": e.get("emotion_tags", [])})
        if total_w <= 0:
            return {"valence": 0.0, "arousal": 0.0, "mood": 0.0, "contributing": []}
        contributing.sort(key=lambda c: -c["intensity"])
        return {"valence": round(mood_sum / total_w, 3),
                "arousal": round(aro_sum / total_w, 3),
                "mood": round(mood_sum / total_w, 3),
                "contributing": contributing[:5]}

    def current_user(self, now=None):
        """对话者 (雾弥) 心情 — 陪伴策略/用户画像用"""
        return self._agg(P_USER, now)

    def current_kiri(self, now=None):
        """Kiri 自己的心情 — describe/行为驱动/主动性由头用"""
        return self._agg(P_KIRI, now)

    # 兼容旧调用: current() = Kiri 心情
    def current(self, now=None):
        return self.current_kiri(now)

    # ---------------- 由头候选 (只从 Kiri 池, 是她的心事) ----------------
    def top_candidates(self, n=3, min_intensity=0.4, now=None):
        now = now or time.time()
        out = []
        with self._lock:
            for e in self._events:
                if e.get("perspective") != P_KIRI:
                    continue
                age_h = (now - e.get("ts", now)) / 3600.0
                decay = 2 ** (-age_h / max(0.1, e.get("decay_hours", 3.0)))
                w = e.get("intensity", 0.5) * decay
                if w >= min_intensity and abs(e["appraisal"]["valence"]) >= 0.2:
                    out.append((w, e))
        out.sort(key=lambda x: -x[0])
        return [e for _, e in out[:n]]

    def all(self, limit=50, perspective=None):
        with self._lock:
            if perspective:
                return [e for e in self._events[-limit:] if e.get("perspective") == perspective]
            return list(self._events[-limit:])


# ---- 每轮 think 更新 (完全体特性 3) ----
def on_think_step(kiri, think_text, relationship="亲密"):
    """agent 每解析出一轮 <think> 后调用: 记录 thought 事件 (fast) + 立即同步 Kiri 心情
    调用点: agent.py _parse_think_speak 返回 {"action":"think"} 分支后
    speak 不调用本函数 — "说话不更新" (说话是输出, 内心状态不变)"""
    ee = getattr(kiri, "emotion_events", None)
    if not ee or not ee.enabled:
        return None
    text = str(think_text or "").strip()[:100]
    if not text:
        return None
    try:
        ev = ee.record(SRC_THOUGHT, text, relationship=relationship,
                       extra={"intensity": 0.4}, fast=True)
        cur = ee.current_kiri()
        if cur["contributing"]:
            kiri.state.emotion.state["deep_affect"]["current_mood"] = cur["mood"]
        return ev
    except Exception:
        return None
