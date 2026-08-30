# -*- coding: utf-8 -*-
"""Kiri 事件绑定情绪记录层 (M1, 2026-08-27)
=====================================================================
按 EMOTION_EVENT_PLAN.md 3.2/3.3/3.4 冻结协议:
  - EmotionEvent 落盘: emotion_events.jsonl (append-only, schema_version=1)
  - record(source, event_text, cause_ref, appraisal, relationship) -> EmotionEvent
  - current() -> {valence, arousal, mood, contributing}  心情聚合 (有因可查)
  - top_candidates(n, min_intensity) -> [EmotionEvent]   主动性由头候选
  - M1 评价 = 规则评价 (小模型 M2 接入, emotion_serve:8768 /appraise)
用法: kiri 实例持有 EmotionEvents(); 各事件源调用 .record()
=====================================================================
"""
import json
import os
import time
import threading
import uuid
import urllib.request

import config as _cfg

SCHEMA_VERSION = 1

SRC_USER = "user_msg"
SRC_THOUGHT = "thought"
SRC_TOOL = "tool_result"
SRC_GOAL = "goal"
SRC_PROACTIVE = "proactive_outcome"
SRC_TIME = "time_routine"

REL_WEIGHT = {"亲密": 1.0, "朋友": 0.6, "生疏": 0.3}
_DEFAULT_DECAY = {"user_msg": 3.0, "thought": 1.0, "tool_result": 1.0,
                  "goal": 24.0, "proactive_outcome": 6.0, "time_routine": 6.0}
_KW_POS = ["开心", "高兴", "喜欢", "谢谢", "好棒", "棒", "爱", "想你", "温柔", "记得", "夸", "厉害", "顺利"]
_KW_NEG = ["难过", "伤心", "累", "烦", "生气", "气死", "气人", "讨厌", "哭", "失眠", "生病", "病了", "骂", "委屈", "失望", "难受", "压力"]


class EmotionEvents:
    """事件绑定情绪: 记录 / 聚合 / 查询 / 由头候选"""

    def __init__(self, filepath, enabled=True, retention_recent=500,
                 retention_high_rel=0.6, agg_min_decay=0.02):
        self.filepath = filepath
        self.enabled = enabled
        self.retention_recent = retention_recent
        self.retention_high_rel = retention_high_rel
        self.agg_min_decay = agg_min_decay
        self._events = []        # 按 ts 升序 (旧→新)
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
            print(f"[emotion.record] 已写入 {ev['id']} → {self.filepath}", flush=True)
        except Exception as _e:
            print(f"[emotion.record] 写入失败: {_e}", flush=True)

    def _prune(self):
        """保留: 最近 retention_recent 条 + relevance≥阈值 的长留存 (按 id 去重)
        ★ 2026-08-30 修复: 高相关性条目原本无时间上限, 而 refine_by_ids 会把
          小模型事件 relevance 改写到 ≥0.6 → jsonl 无界增长。加 7 天上限。"""
        now = time.time()
        self._events.sort(key=lambda e: e.get("ts", 0))
        recent = self._events[-self.retention_recent:] if self.retention_recent else self._events
        high = [e for e in self._events
                if e.get("appraisal", {}).get("relevance", 0) >= self.retention_high_rel
                and now - float(e.get("ts", 0)) <= 7 * 86400]
        seen = {}
        for e in recent + high:
            seen[e.get("id", uuid.uuid4().hex)] = e
        self._events = sorted(seen.values(), key=lambda e: e.get("ts", 0))

    # ---------------- 规则评价 (M1) ----------------
    @classmethod
    def rule_appraise(cls, source, event_text, relationship="亲密", extra=None):
        """结构化事件的规则评价; 返回 appraisal dict 或 None(规则覆盖不到, M2 小模型接管)"""
        extra = extra or {}
        if source == SRC_USER:
            sentiment = extra.get("sentiment")
            kw = cls._kw_sentiment(event_text)
            if sentiment is None:
                sentiment = kw
            elif abs(sentiment) < 0.05 and abs(kw) > 0.05:
                sentiment = kw    # llm 中性但关键词有信号 → 用关键词 (本地模型情绪分析不可靠)
            rel_w = REL_WEIGHT.get(relationship, 0.5)
            if abs(sentiment) < 0.05:
                return None          # 中性消息不产生情绪 (防噪音)
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

    # ---------------- 小模型评价 (M2: emotion_serve /appraise) ----------------
    @staticmethod
    def small_appraise(event_text, speaker="user", relationship="亲密",
                       current_state=None, timeout=3.0):
        """调情绪小模型 (emotion_serve:8768) → 3.1 协议 dict; 失败/超时返回 None
        返回 {valence_delta, arousal_delta, intensity, emotion_tags, appraisal_note}"""
        url = getattr(_cfg, "EMOTION_SERVE_URL", "http://127.0.0.1:8768") + "/appraise"
        body = json.dumps({
            "event_text": str(event_text)[:200],
            "speaker": speaker, "relationship": relationship,
            "current_state": current_state or {"valence": 0.0, "arousal": 0.0, "intensity": 0.0},
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
    def appraise(cls, source, event_text, relationship="亲密", extra=None):
        """统一评价入口 (M2): 小模型优先, 失败/超时降级规则评价
        返回 (appraisal_dict, emotion_tags, appraisal_note):
          appraisal_dict = {valence, arousal, relevance} 或 None (规则也不覆盖)"""
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
        # 降级: 规则评价
        ap = cls.rule_appraise(source, event_text, relationship, extra)
        if ap is None:
            return None, [], ""
        return ap, cls.rule_tags(source, ap, extra), ""

    @classmethod
    def rule_tags(cls, source, appraisal, extra=None):
        extra = extra or {}
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

    # ---------------- 记录 ----------------
    def record(self, source, event_text, cause_ref=None, appraisal=None,
               relationship="亲密", extra=None, emotion_tags=None, decay_hours=None,
               fast=False):
        """统一事件采集入口 (PLAN 3.3)。appraisal=None → 走统一评价 (小模型优先+规则降级)
        fast=True (2026-08-27 阉割版): 跳过小模型, 纯规则即时评价 — 聊天关键路径用,
        事后由 refine_by_ids() 用小模型补评修正 (用户看回复时感知不到)"""
        if not self.enabled:
            return None
        extra = extra or {}
        note = ""
        if appraisal is None:
            if fast:
                ap = self.rule_appraise(source, event_text, relationship, extra)
                if ap is None:
                    return None
                appraisal = ap
                emotion_tags = emotion_tags or self.rule_tags(source, ap, extra)
            else:
                appraisal, tags, note = self.appraise(source, event_text, relationship, extra)
                if emotion_tags is None:
                    emotion_tags = tags
        if appraisal is None:
            return None
        intensity = float(extra.get("intensity", 0.5))
        tags = emotion_tags or self.rule_tags(source, appraisal, extra)
        ev = {
            "schema_version": SCHEMA_VERSION,
            "id": "ev_%s" % uuid.uuid4().hex[:10],
            "ts": float(extra.get("ts", time.time())),
            "source": source,
            "event_text": str(event_text)[:200],
            "cause_ref": cause_ref,
            "appraisal": {"valence": round(float(appraisal.get("valence", 0)), 3),
                          "arousal": round(float(appraisal.get("arousal", 0)), 3),
                          "relevance": round(float(appraisal.get("relevance", 0.3)), 3)},
            "emotion_tags": tags[:2],
            "intensity": round(intensity, 3),
            "decay_hours": float(decay_hours or _DEFAULT_DECAY.get(source, 3.0)),
            "appraisal_note": note[:60],
            "refined": False,          # ★ 阉割版: 是否已被小模型补评修正
        }
        with self._lock:
            self._events.append(ev)
            self._prune()
        self._append_file(ev)
        return ev

    # ---------------- 事后小模型补评 (阉割版, 2026-08-27) ----------------
    def refine_by_ids(self, event_ids, timeout=8.0):
        """生成完整回复后, 异步用小模型批量补评指定事件 (聊天关键路径不等待)
        成功修正的事件: 更新 appraisal/emotion_tags/appraisal_note + refined=True
        返回修正条数; 小模型失败 → 保持规则评价 (不丢数据)"""
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
                e["refined"] = True
                fixed += 1
            except Exception:
                continue
        if fixed:
            self._rewrite_file()
        return fixed

    def _rewrite_file(self):
        """把内存事件全量重写回文件 (补评修正后保持一致; 文件 ≤500 条, 代价可忽略)"""
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                for e in self._events:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ---------------- 聚合 (PLAN 3.4) ----------------
    def current(self, now=None):
        now = now or time.time()
        total_w = 0.0
        mood_sum = 0.0
        aro_sum = 0.0
        contributing = []
        with self._lock:
            for e in self._events:
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

    # ---------------- 由头候选 (PLAN 3.5) ----------------
    def top_candidates(self, n=3, min_intensity=0.4, now=None):
        now = now or time.time()
        out = []
        with self._lock:
            for e in self._events:
                age_h = (now - e.get("ts", now)) / 3600.0
                decay = 2 ** (-age_h / max(0.1, e.get("decay_hours", 3.0)))
                w = e.get("intensity", 0.5) * decay
                if w >= min_intensity and abs(e["appraisal"]["valence"]) >= 0.2:
                    out.append((w, e))
        out.sort(key=lambda x: -x[0])
        return [e for _, e in out[:n]]

    def all(self, limit=50):
        with self._lock:
            return list(self._events[-limit:])
