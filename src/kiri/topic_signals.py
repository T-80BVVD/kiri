# -*- coding: utf-8 -*-
"""topic_signals.py — 慢速话题证据池 (吸收 N.E.K.O main_logic/topic/ 思想)
=====================================================================
为什么: Kiri 的 wander 是随机刷内容, 和"雾弥们聊过什么"脱节。
NEKO 做法 (topic/signals.py + pipeline.py):
  ① 慢速证据池: 只存对话文本+时间戳, 不判断 (判断交给LLM)
  ② 后台分析: LLM 从证据池提炼 interest/keywords/hook → 话头材料
  ③ 素材补充: 按关键词去外部搜相关内容
  ④ 投递: "自然借一个具体点开口, 别把联网结果讲成报告"
本模块 (Kiri 精简版):
  - TopicSignals.record(user, text): 记录对话 (过滤filler, 窗口裁剪)
  - maybe_analyze(): 证据够多且过期 → LLM 提炼话头 (1次调用, 幂等防重跑)
  - materials(): 当前话头 [{interest, keywords, hook, ts}]
  - 接入: reverie.world_wander 优先刷话头关键词
=====================================================================
"""
import os
import sys
import time
import json

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SIGNALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "topic_signals.json")

WINDOW_MAX = 80             # 证据窗口: 最近80条 (NEKO: 60轮)
WINDOW_HOURS = 24.0         # 且24h内
MIN_NEW_SIGNALS = 12        # 距上次分析至少新增12条才重分析
REANALYZE_HOURS = 6.0       # 距上次分析超过6h且证据变了才重分析
TOPICS_MAX = 3              # 每次提炼几个话头
FILLER = {"你好", "哈哈", "嗯嗯", "哦哦", "在吗", "测试", "test", "说句话", "在吗在吗",
          "嘿嘿", "哈哈哈", "笑死", "牛逼", "牛啊", "真的假的", "是啊", "对呀", "行吧"}
MIN_LEN = 3                 # 少于3字不记录 (短寒暄无话题价值)


class TopicSignals:
    def __init__(self, path=SIGNALS_FILE):
        self.path = path
        self._data = {"signals": [], "materials": [], "last_analyze": 0.0, "signal_count": 0}
        self.load()

    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                self._data["signals"] = d.get("signals") or []
                self._data["materials"] = d.get("materials") or []
                self._data["last_analyze"] = float(d.get("last_analyze", 0.0) or 0.0)
                self._data["signal_count"] = int(d.get("signal_count", 0) or 0)
        except Exception:
            pass

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
        except Exception:
            pass

    # ---- 记录对话 (filler过滤) ----
    def record(self, user, text, ts=None):
        """记录一条用户说的话 (过滤filler/过短/她自己以外的人说的话都算)
        只存文本, 不判断"""
        try:
            t = str(text or "").strip()
            if len(t) < MIN_LEN or t in FILLER:
                return False
            ts = ts or time.time()
            self._data["signals"].append({"user": str(user or "雾弥")[:20], "text": t[:120],
                                          "ts": ts})
            # 窗口裁剪
            now = time.time()
            self._data["signals"] = [s for s in self._data["signals"]
                                     if now - s.get("ts", 0) <= WINDOW_HOURS * 3600][-WINDOW_MAX:]
            self._data["signal_count"] += 1
            return True
        except Exception:
            return False

    def _fresh_signals(self):
        """距上次分析的未分析信号数 (决定要不要重分析)"""
        if not self._data["signals"]:
            return 0
        last = self._data["last_analyze"]
        fresh = [s for s in self._data["signals"] if s.get("ts", 0) > last]
        return len(fresh)

    # ---- LLM 提炼话头 ----
    def maybe_analyze(self, force=False):
        """证据够多且过期 → LLM 提炼话头 (幂等: 距上次不足/证据没变就不调)
        返回新话头数; 失败不抛异常"""
        try:
            if not force:
                fresh = self._fresh_signals()
                if fresh < MIN_NEW_SIGNALS:
                    return 0
                age = time.time() - self._data["last_analyze"]
                if self._data["last_analyze"] > 0 and age < REANALYZE_HOURS * 3600:
                    return 0
            return self.analyze()
        except Exception:
            # ★ 2026-08-30 修复: 失败也冷却 — 原异常冒泡时 last_analyze 不更新,
            #   LLM/解析持续失败时每 tick 都重试 → 无限烧 token
            try:
                self._data["last_analyze"] = time.time()
                self.save()
            except Exception:
                pass
            return 0

    def analyze(self):
        """LLM 提炼: 证据池 → 2-3个话头 {interest, keywords, hook}
        ★ 2026-08-30: 无论成败都更新 last_analyze (冷却) — 解析失败不再无限重试"""
        import engine
        import prompt as prompt_mod
        pool = self._data["signals"][-40:]
        pool_txt = "\n".join(f"- {s['user']}: {s['text'][:50]}" for s in pool)
        if len(pool) < MIN_NEW_SIGNALS and not self._data["materials"]:
            return 0
        raw = engine.generate(prompt_mod.topic_analyze_system(), pool_txt,
                              max_tokens=400, temperature=0.4)
        topics = prompt_mod.parse_topics(raw)
        now = time.time()
        self._data["last_analyze"] = now          # 无论成败都记 (冷却)
        if topics:
            self._data["materials"] = [{"interest": t.get("interest", "")[:20],
                                        "keywords": t.get("keywords", "")[:60],
                                        "hook": t.get("hook", "")[:60],
                                        "ts": now} for t in topics[:TOPICS_MAX]]
            self.save()
        return len(topics)

    def materials(self, max_age_hours=48.0):
        """当前话头 (过期的自动丢弃)"""
        mats = []
        now = time.time()
        for m in self._data.get("materials", []):
            if now - m.get("ts", 0) <= max_age_hours * 3600:
                mats.append(m)
        return mats

    def clear_materials(self):
        self._data["materials"] = []
        self.save()

    def stats(self):
        return {"signals": len(self._data["signals"]), "materials": len(self._data["materials"]),
                "last_analyze": self._data["last_analyze"],
                "signal_count": self._data["signal_count"]}


if __name__ == "__main__":
    # 自测 (纯逻辑, mock engine)
    import types
    fake = types.ModuleType("engine")
    fake.generate = lambda s, u, **kw: ('{"topics": ['
                                         '{"interest": "音游", "keywords": "osu 音游 练习", "hook": "你上次说在练音游，练得怎么样了？"}, '
                                         '{"interest": "白色", "keywords": "白色 穿搭", "hook": "说到白色你眼睛都亮了，到底多爱这色。"}]}')
    sys.modules["engine"] = fake
    p = TopicSignals(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_topics.json"))
    p._data = {"signals": [], "materials": [], "last_analyze": 0.0, "signal_count": 0}
    # 1. filler 过滤
    assert not p.record("雾弥", "哈哈")
    assert not p.record("雾弥", "嗯")
    assert p.record("雾弥", "我最近在练音游，手都酸了")
    assert p.record("雾弥", "你猜我最喜欢什么颜色")
    assert p.stats()["signals"] == 2
    print("A filler过滤 OK")
    # 2. 证据不够不分析
    assert p.maybe_analyze() == 0
    print("B 证据不足不分析 OK")
    # 3. 够12条 → 分析 → 话头
    for i in range(12):
        p.record("雾弥", f"我们聊点有意思的{i}")
    n = p.maybe_analyze()
    assert n == 2, n
    mats = p.materials()
    assert len(mats) == 2 and mats[0]["interest"] == "音游", mats
    print("C 提炼话头 OK:", [m["interest"] for m in mats])
    # 4. 幂等: 刚分析完不再调
    assert p.maybe_analyze() == 0
    print("D 幂等 OK")
    try:
        os.remove(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_topics.json"))
    except Exception:
        pass
    print("topic_signals 自测 PASS")
