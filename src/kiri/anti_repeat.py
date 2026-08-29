# -*- coding: utf-8 -*-
"""anti_repeat.py — 话题级防复读 (吸收 N.E.K.O anti_repeat.py BM25机制)
=====================================================================
为什么: Kiri 现有防复读是 SequenceMatcher 类字面避重 (difflib),
  换个说法说同一件事就漏了 ("你喜欢黑色吗" vs "黑色是你最喜欢的吧").
NEKO 做法 (改造BM25):
  - BG = 最近100条 (算 DF/IDF, 只按条数封顶不过滤时间)
  - FG = 最近5条且在600s TTL内 (算 TF)
  - draft分 = Σ IDF_bg × TF_fg (话题词低频→IDF高→强信号; 高频词今天/哈哈→IDF≈0不扣分)
  - 主动搭话 分>8 强制重生成一次, 仍>16 放弃投递; 普通回复注入 top-K ngram 提示
本模块:
  - CJK 2/3-gram 抽取
  - record(user, text) 记录她的输出 (每用户独立语料)
  - score(user, draft) 话题重复分 (0~∞)
  - recent_topics(user, k) 最近聊过的 top ngram (注入 prompt 用)
  - check(user, draft) -> "ok" / "regenerate" / "drop"
=====================================================================
"""
import os
import sys
import time
import json
import re

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CORPUS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anti_repeat_corpus.json")

BG_MAX = 100            # 背景窗口: 最近100条 (IDF)
FG_MAX = 5              # 前景窗口: 最近5条 (TF)
FG_TTL_SECONDS = 600    # 前景 TTL: 600s 内才算 (防空闲死锁: 空闲期FG冻结→永远高分)
SCORE_REGENERATE = 4.0  # 超过 → 重生成一次 (话题重复信号)
SCORE_DROP = 8.0        # 超过 → 放弃投递 (主动发言)
TOP_K = 6               # 注入提示的 ngram 数


def _ngrams(text):
    """CJK 2/3-gram: 取文本中的中文字/字母数字, 生成连续2字和3字片段
    过滤掉纯高频噪音 (只保留长度>=2的片段)"""
    chars = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", str(text or ""))
    out = set()
    for k in (2, 3):
        for i in range(len(chars) - k + 1):
            out.add("".join(chars[i:i + k]))
    return out


class AntiRepeat:
    def __init__(self, path=CORPUS_FILE):
        self.path = path
        self._corpus = {}   # user -> [{ts, ngrams:[...], text}]
        self.load()

    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    self._corpus = json.load(f)
            for u in self._corpus:
                self._corpus[u] = self._corpus[u][-BG_MAX:]
        except Exception:
            self._corpus = {}

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._corpus, f, ensure_ascii=False)
        except Exception:
            pass

    def record(self, user, text, ts=None):
        """记录她的输出 (回复/主动发言都记)"""
        u = str(user or "雾弥")
        grams = sorted(_ngrams(text))
        if not grams:
            return
        self._corpus.setdefault(u, []).append(
            {"ts": ts or time.time(), "ngrams": grams, "text": str(text)[:200]})
        self._corpus[u] = self._corpus[u][-BG_MAX:]

    def _bg_stats(self, user, now=None):
        """背景窗口 (最近BG_MAX条, 不过滤时间) → df"""
        recs = self._corpus.get(str(user or "雾弥"), [])
        df = {}
        for r in recs:
            for g in set(r.get("ngrams", [])):
                df[g] = df.get(g, 0) + 1
        return recs, df

    def _fg_counts(self, user, now=None):
        """前景窗口 (最近FG_MAX条且在FG_TTL内) → tf"""
        now = now or time.time()
        recs = self._corpus.get(str(user or "雾弥"), [])
        fg = [r for r in recs if now - r.get("ts", 0) <= FG_TTL_SECONDS][-FG_MAX:]
        tf = {}
        for r in fg:
            for g in r.get("ngrams", []):
                tf[g] = tf.get(g, 0) + 1
        return fg, tf

    def score(self, user, draft, now=None):
        """话题重复分: Σ idf_bg × tf_fg
        idf = log(1 + (N - df + 0.5)/(df + 0.5)) — 高频词≈0, 话题词高
        返回 (score, 命中的ngram列表)"""
        now = now or time.time()
        u = str(user or "雾弥")
        recs, df = self._bg_stats(u, now)
        fg, tf = self._fg_counts(u, now)
        if not fg or not recs:
            return 0.0, []
        N = len(recs)
        dgrams = _ngrams(draft)
        score = 0.0
        hits = []
        for g in dgrams:
            f = tf.get(g, 0)
            if f <= 0:
                continue
            d = df.get(g, 0)
            idf = 0.0 if d <= 0 else max(0.0, __import__("math").log(1 + (N - d + 0.5) / (d + 0.5)))
            contrib = idf * min(f, 3)   # 单ngram计数封顶3 (防刷屏伪信号)
            score += contrib
            hits.append((g, round(contrib, 2)))
        return round(score, 2), hits

    def recent_topics(self, user, k=TOP_K, now=None):
        """最近聊过的话题 ngram (按 tf×idf 排序) — 注入 prompt 用"""
        now = now or time.time()
        u = str(user or "雾弥")
        recs, df = self._bg_stats(u, now)
        fg, tf = self._fg_counts(u, now)
        if not fg or not recs:
            return []
        N = len(recs)
        scored = []
        for g, f in tf.items():
            d = df.get(g, 0)
            idf = 0.0 if d <= 0 else __import__("math").log(1 + (N - d + 0.5) / (d + 0.5))
            scored.append((idf * min(f, 3), g))
        scored.sort(reverse=True)
        return [g for _, g in scored[:k]]

    def check(self, user, draft, now=None):
        """判断草稿: "ok" 可发 / "regenerate" 话题重复需换说法 / "drop" 放弃"""
        score, hits = self.score(user, draft, now)
        if score >= SCORE_DROP:
            return "drop", score, hits
        if score >= SCORE_REGENERATE:
            return "regenerate", score, hits
        return "ok", score, hits


if __name__ == "__main__":
    # 自测 (纯逻辑)
    a = AntiRepeat(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_corpus.json"))
    a._corpus.clear()
    now = time.time()
    # 1. 普通回复不误伤
    a.record("雾弥", "今天天气不错，出去走走。", ts=now - 10)
    s, h = a.score("雾弥", "嗯嗯，是个好天气呢。", now)
    assert s < SCORE_REGENERATE, f"高频词(今天/天气)不应触发, score={s}"
    # 2. 同一话题重复 → 触发 regenerate/drop
    a.record("雾弥", "我最近在学量子计算，很有意思。", ts=now - 20)
    s, h = a.score("雾弥", "量子计算真的很难，我在学。", now)
    assert s >= SCORE_REGENERATE, f"量子计算话题应触发, score={s}"
    print(f"话题重复分: {s} (命中共计{len(h)}个ngram)")
    # 3. TTL 过期 → 分数归零 (防空闲死锁)
    s2, _ = a.score("雾弥", "量子计算真的很难。", now + 700)
    assert s2 < SCORE_REGENERATE, f"TTL过期后应放行, score={s2}"
    # 4. check 返回档位
    c, sc, hh = a.check("雾弥", "量子计算真的很难，我在学。", now)
    assert c in ("regenerate", "drop"), c
    print(f"check: {c} (score={sc})")
    # 5. recent_topics
    tops = a.recent_topics("雾弥", now=now)
    assert tops, "应有话题ngram"
    print("recent_topics:", tops[:4])
    print("anti_repeat 自测 PASS")
    try:
        os.remove(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_corpus.json"))
    except Exception:
        pass
