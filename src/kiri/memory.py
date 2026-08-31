# -*- coding: utf-8 -*-
"""Kiri 向量记忆系统 — chromadb + bge-small-zh 语义检索 (2026-08-16)
替代 JSON 文本库: 记忆=向量 (语义相关才是'记得'), 保留 session/情感/遗忘机制
接口: encode / retrieve (kiri.py 调用, retrieve 需传 query 文本)
"""
import json
import os
import time
import uuid
import threading
import numpy as np
import torch
import chromadb
from transformers import AutoModel, AutoTokenizer
import config

BGE_PATH = config.BGE_MODEL_PATH   # ★ 2026-08-30 开源适配: 改由 config 读取 (环境变量 KIRI_BGE_PATH 可覆盖)
DB_PATH = os.path.join(os.path.dirname(__file__), "mem_db")


class BgeEmbedding:
    """bge-small-zh embedding (transformers, CLS pooling, 归一化)
    chromadb 0.5x 接口: embed_documents / embed_query
    """
    def __init__(self):
        self.model = AutoModel.from_pretrained(BGE_PATH, trust_remote_code=True)
        self.tokenizer = AutoTokenizer.from_pretrained(BGE_PATH, trust_remote_code=True)
        self.model.eval()

    def name(self):
        return "bge-small-zh-v1.5"

    def _encode(self, input):
        if isinstance(input, str):
            input = [input]
        with torch.no_grad():
            enc = self.tokenizer(input, padding=True, truncation=True,
                                 max_length=512, return_tensors="pt")
            out = self.model(**enc)
            emb = out.last_hidden_state[:, 0].numpy()  # CLS
        norm = np.linalg.norm(emb, axis=1, keepdims=True)
        return (emb / np.maximum(norm, 1e-9)).tolist()

    def embed_documents(self, input):
        return self._encode(input)

    def embed_query(self, input):
        if isinstance(input, list):
            return self._encode(input)
        return self._encode([input])[0]

    def __call__(self, input):
        return self._encode(input)


class Memory:
    # 默认用户 = 雾弥 (兼容现有数据, collection 名不变)
    DEFAULT_USER = "雾弥"

    def __init__(self, path=None):
        os.makedirs(DB_PATH, exist_ok=True)
        self.emb = BgeEmbedding()
        self.client = chromadb.PersistentClient(path=DB_PATH)
        self._collections = {}      # user -> 记忆 collection
        self._thought_cols = {}     # user -> 念头 collection
        # ★ P1-4 (2026-08-22 移植 NachoBot execute_request_with_dedup):
        #   写锁 (并发写 chromadb 防冲突) + 检索缓存 (同query 5s去重)
        self._write_lock = threading.Lock()
        self._retrieve_cache = {}   # (user, query) -> (ts, result)
        # 雾弥(默认)的库: 保持原名不迁移, 现有数据不丢
        self.collection = self.get_user_collection(self.DEFAULT_USER)
        self.thought_col = self.get_user_thoughts(self.DEFAULT_USER)

    # ---- 多用户: 每用户独立记忆库 ----
    def _user_key(self, user):
        """用户标识 → 安全 collection 后缀 (chromadb只允许字母数字._-)
        中文用户名 → 哈希, 避免非法字符"""
        import hashlib
        import re
        u = str(user or self.DEFAULT_USER)
        safe = re.sub(r"[^0-9a-zA-Z]", "_", u)
        if safe and safe[0].isalnum() and len(safe) <= 40:
            return safe
        # 中文/特殊字符 → 用哈希
        h = hashlib.md5(u.encode("utf-8")).hexdigest()[:12]
        return f"u{h}"

    def get_user_collection(self, user=None):
        """获取某用户的记忆 collection (雾弥用原名, 其他用户带前缀)"""
        user = user or self.DEFAULT_USER
        key = self._user_key(user)
        # 缓存以原始用户名为键 (users() 需要还原)
        for orig, col in self._collections.items():
            if orig == user:
                return col
        name = "kiri_memory" if user == self.DEFAULT_USER else f"kiri_memory_{key}"
        col = self.client.get_or_create_collection(
            name, embedding_function=self.emb,
            metadata={"hnsw:space": "cosine"})
        self._collections[user] = col
        return col

    def get_user_thoughts(self, user=None):
        """获取某用户的念头 collection"""
        user = user or self.DEFAULT_USER
        key = self._user_key(user)
        for orig, col in self._thought_cols.items():
            if orig == user:
                return col
        name = "kiri_thoughts" if user == self.DEFAULT_USER else f"kiri_thoughts_{key}"
        col = self.client.get_or_create_collection(
            name, embedding_function=self.emb,
            metadata={"hnsw:space": "cosine"})
        self._thought_cols[user] = col
        return col

    def count(self, user=None):
        """某用户的记忆条数"""
        try:
            return self.get_user_collection(user).count()
        except Exception:
            return 0

    def users(self):
        """列出所有有记忆的用户 (雾弥 + 各用户库)
        ★ 修复: 扫描chromadb全部collection还原用户名 (不依赖内存缓存, 重启后朋友库不丢)
        数字QQ号等安全名: 后缀即用户名; 中文哈希(u开头): 用哈希标识(仍可对应同一库)"""
        names = [self.DEFAULT_USER]
        try:
            # 先加缓存里已知的用户
            for u in list(self._collections.keys()):
                if u not in names:
                    names.append(u)
            # 扫描库里的 collection, 补漏
            cols = self.client.list_collections()
            for c in cols:
                n = c.name
                if n.startswith("kiri_memory_"):
                    suffix = n[len("kiri_memory_"):]
                    if suffix and suffix not in names:
                        names.append(suffix)
        except Exception:
            pass
        return list(dict.fromkeys(names))

    # ---- 念头库 ----
    def encode_thought(self, text, salience=0.0, source="reverie", user=None):
        """念头入库 (联想/内心独白都存, 供'想起'检索); 按用户分库"""
        try:
            col = self.get_user_thoughts(user)
            meta = {"timestamp": time.time(), "salience": float(salience or 0.0),
                    "source": source}
            col.add(ids=[str(uuid.uuid4())],
                    documents=[str(text)[:200]], metadatas=[meta])
        except Exception:
            pass

    def retrieve_thoughts(self, query_text, n=4, user=None):
        """按当前对话检索相关念头 (相关性权重低, 偏新鲜+重要 — 保留'意外想起'的味道)
        只检索对应用户的念头库"""
        try:
            col = self.get_user_thoughts(user)
            if col.count() == 0:
                return []
            res = col.query(query_texts=[query_text or "回忆"],
                            n_results=min(max(n * 4, 8), col.count()))
        except Exception:
            return []
        now = time.time()
        scored = []
        for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
            meta = meta or {}
            sim = 1.0 - dist
            age_h = (now - float(meta.get("timestamp", now))) / 3600
            freshness = max(0.1, 1.0 - age_h * 0.08)   # 新鲜度 (近期念头更易浮现)
            sal = float(meta.get("salience", 0.0) or 0.0)
            # ★ 相关性权重低 (0.25), 新鲜度(0.45)+重要(0.30)主导
            score = sim * 0.25 + freshness * 0.45 + sal * 0.30
            scored.append((score, doc))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in scored[:n]]

    # ---- 编码 ----
    # ★ P1-2 (2026-08-22 移植 NachoBot _looks_uncertain_or_temporary):
    #   临时/不确定标记 — "可能/好像/今天/打算/心情"等 → 不进长期画像 (防'今天心情不好'固化'他忧郁')
    _UNCERTAIN_HINTS = ["可能", "似乎", "好像", "大概", "也许", "暂时", "今天", "现在", "刚刚",
                        "最近", "计划", "打算", "准备", "心情", "感觉", "有点", "觉得", "以为",
                        "听说", "希望", "期待", "不太", "说不定"]

    @staticmethod
    def _is_uncertain(text):
        t = str(text or "")
        return any(h in t for h in Memory._UNCERTAIN_HINTS)

    def encode(self, event, emotion_state=None, session=None, user=None, speaker="user", related=None):
        """编码记忆 (★三维分离 + ★来源可信度):
          user:   属于谁的库 (雾弥/朋友分库)
          speaker: 谁说的事实 — "user"(对方亲口说,事实) / "kiri"(她回应/推断) / "system"(系统/联想)
          related: 相关的人列表 (这条记忆涉及谁, 如 ["雾弥","阿明"]) — 跨用户合成用
        ★ source 可信度 (NEKO 机制): user_observation=用户亲口说的事实(可信) /
          ai_disclosure=AI自我披露或推断(不可信, 需用户印证才升级) — 防"喜欢黑色"类bug
        ★ P1-2: 临时/不确定标记 (uncertain); P1-3: 幂等去重 (同文本hash跳过)"""
        # ★ P1-3 幂等去重 (2026-08-22 移植 NachoBot external_id): 完全相同文本跳过
        #   (QQ消息重复投递/同轮多次编码 → 同一条不存两遍)
        # ★ 2026-08-30 修复: 去重只拦"同层"重复 — 短期(session)/长期(global) 是同一事实的
        #   两个分层 (短期可遗忘, 长期保留), 互不挡。修复 kiri.py 长期记忆分支被
        #   hash 去重静默挡掉、重要事实永远进不了长期库的问题。
        # ★ P1-4 写锁 (并发写 chromadb 防冲突)
        with self._write_lock:
            try:
                import hashlib as _hl
                col0 = self.get_user_collection(user or self.DEFAULT_USER)
                event_hash = _hl.md5(str(event)[:300].encode("utf-8", errors="ignore")).hexdigest()
                dup = col0.get(where={"hash": event_hash}, limit=1)
                if dup and dup.get("ids"):
                    ex_m = (dup.get("metadatas") or [{}])[0] or {}
                    ex_sess = ex_m.get("session")
                    new_sess = session or "global"
                    if ex_sess == new_sess:
                        return                      # 同层重复 → 跳过
                    if ex_sess in (None, "global") and new_sess == "global":
                        return                      # 都是长期 → 跳过
                    # 短期 vs 长期 → 允许各存一份 (不 return, 继续写入)
            except Exception:
                event_hash = ""
        mood = 0.0
        if emotion_state and isinstance(emotion_state, dict):
            deep = emotion_state.get("deep_affect", {})
            mood = deep.get("current_mood", 0.0) if isinstance(deep, dict) else 0.0
        source = self._source(speaker)
        meta = {
            "session": session or "global",
            "timestamp": time.time(),
            "salience": self._salience(event),
            "mood": float(mood),
            "memory_type": self._type(event),
            "speaker": str(speaker or "user"),
            "source": source,   # ★ 可信度: user_observation / ai_disclosure / system
            "related": json.dumps([str(r) for r in (related or [user or self.DEFAULT_USER])],
                                  ensure_ascii=False),
            # ★ 联想引擎: 印象权重 (被嚼次数 + 最近被嚼时间, 检索时动态衰减)
            "chewed_count": 0.0,
            "last_chewed": 0.0,
            # ★ 证据计数器 (NEKO reinforcement/disputation吸收): 
            #   rein=被用户再次确认的次数 / disp=被否定的次数 (读时按半衰期衰减)
            "rein": 0.0,
            "disp": 0.0,
            "rein_ts": 0.0,
            "disp_ts": 0.0,
            # ★ P1-2/P1-3 (2026-08-22): 不确定标记 + 幂等hash
            "uncertain": self._is_uncertain(event) if event_hash else False,
            "hash": event_hash,
        }
        col = self.get_user_collection(user)
        # ★ 纠正检测 (NEKO corrections): 用户否定/纠正某认知 → 先纠正相关记忆
        if source == "user_observation" and self._is_correction(event):
            self.negate(event, user)
        # ★ 升级规则: 用户亲口说的事实, 若已有同文本的 ai_disclosure (Kiri推断) → 升级为 user_observation
        #   (NEKO monotonic upgrade: ai_disclosure→user_observation, 绝不回退)
        if source == "user_observation":
            self._upgrade_disclosure(event, col)
            # ★ 证据强化 (NEKO reinforcement): 用户再次确认相似事实 → 强化已有记忆而非重复存
            if self._reinforce_similar(event, col):
                return
        # ★ P1-4 写锁保护 add (并发写 chromadb 防冲突)
        with self._write_lock:
            col.add(ids=[str(uuid.uuid4())], documents=[event[:500]], metadatas=[meta])
        # 限制规模: 超过上限删最旧 (chromadb get不支持sort, Python侧按timestamp排)
        count = col.count()
        if count > config.MAX_MEMORIES:
            try:
                data = col.get(limit=count)
                ids_ts = []
                metas = data.get("metadatas") or []
                for i, mid in enumerate(data["ids"]):
                    m = metas[i] if i < len(metas) else {}
                    m = m or {}
                    ids_ts.append((mid, float(m.get("timestamp", 0) or 0)))
                ids_ts.sort(key=lambda x: x[1])
                old_ids = [mid for mid, _ in ids_ts[:count - config.MAX_MEMORIES]]
                if old_ids:
                    col.delete(ids=old_ids)
            except Exception:
                pass  # 裁剪失败不致命 (下次再试)

    # ---- 检索 (语义 + 会话加权 + 时间衰减 + 情感 + 深刻度) ----
    # ★ P0-2 (2026-08-22 移植 NachoBot dual_path): BM25 稀疏路 + weighted RRF 融合
    #   解决 bge 对专名/数字/精确词召回差的问题 (0.7 向量 / 0.3 BM25, k=60)
    def _bm25_search(self, query, col, n):
        """稀疏路: jieba 分词 + BM25 打分 (专名/数字召回补偿)
        返回 [(doc, meta, sim)] top n (sim = BM25 分归一化近似)"""
        try:
            import jieba
            import math
            q_tokens = [t for t in jieba.cut_for_search(query or "") if len(t.strip()) > 1]
            if not q_tokens:
                return []
            data = col.get()   # 该用户库全量 (128条级, 可接受)
            docs = data.get("documents") or []
            metas = data.get("metadatas") or []
            N = max(len(docs), 1)
            df = {}
            doc_tokens = []
            for d in docs:
                toks = [t for t in jieba.cut_for_search(d or "") if len(t.strip()) > 1]
                doc_tokens.append(toks)
                for t in set(toks):
                    df[t] = df.get(t, 0) + 1
            scored = []
            for i, toks in enumerate(doc_tokens):
                tf = {}
                for t in toks:
                    tf[t] = tf.get(t, 0) + 1
                dl = max(len(toks), 1)
                s = 0.0
                for t in q_tokens:
                    if t in tf:
                        idf = math.log((N - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5) + 1)
                        s += idf * (tf[t] * 2.5) / (tf[t] + 1.5 * (0.25 + 0.75 * (dl / 40.0)))
                if s > 0:
                    scored.append((i, s))
            scored.sort(key=lambda x: -x[1])
            top = max(scored[0][1], 1e-9) if scored else 1.0
            out = []
            for i, s in scored[:n]:
                meta = metas[i] or {}
                if meta.get("corrected"):
                    continue
                out.append((docs[i], meta, 1.0 - min(s / top, 1.0)))  # sim 近似
            return out
        except Exception:
            return []

    def _rrf_merge(self, vec_res, bm25_hits, k=60, w_vec=0.7, w_bm=0.3):
        """weighted RRF 融合: 向量路 + BM25路 → 伪 chromadb res (按融合分排序)
        RRF: score = Σ w/(k+rank); 尾部弱命中因排名靠前拿分的缺陷由 BM25 分过滤兜底"""
        merged = {}
        for rank, (doc, meta, _d) in enumerate(zip(
                vec_res["documents"][0], vec_res["metadatas"][0], vec_res["distances"][0])):
            merged.setdefault(doc, {"meta": meta, "rrf": 0.0})
            merged[doc]["rrf"] += w_vec / (k + rank + 1)
        for rank, (doc, meta, _s) in enumerate(bm25_hits):
            merged.setdefault(doc, {"meta": meta, "rrf": 0.0})
            merged[doc]["rrf"] += w_bm / (k + rank + 1)
        if not merged:
            return vec_res
        items = sorted(merged.items(), key=lambda x: -x[1]["rrf"])
        best = max(items[0][1]["rrf"], 1e-9)
        docs = [d for d, _ in items]
        metas = [v["meta"] for _, v in items]
        dists = [1.0 - v["rrf"] / best for _, v in items]
        return {"documents": [docs], "metadatas": [metas], "distances": [dists]}

    def retrieve(self, query_text="", current_mood=0.0, n=4, session=None, user=None,
                 trust_weights=None):
        """★ trust吸收 (NEKO trust_store): trust_weights = {用户: 信任度0~1}
        跨用户召回时, 低信任用户的记忆权重更低 (雾弥说的比陌生人说的更可信)
        缺省: 未提供信任度的其他用户按 0.5
        ★ P0-2: BM25 稀疏路 + RRF 融合 (专名/数字召回补偿)"""
        col = self.get_user_collection(user)
        if col.count() == 0:
            return []
        # ★ P1-4 检索缓存 (同 query 5s 内去重, 并发重复查询合并)
        # ★ 2026-08-30: 缓存键加入 n — 原 (user, query) 相同时, n=4 缓存会让
        #   5 秒内的 n=8 调用只返回 4 条。
        cache_key = (user, query_text, n)
        cached = self._retrieve_cache.get(cache_key)
        if cached and time.time() - cached[0] < 5:
            return cached[1]
        # 语义检索 (冗余取3倍, 后处理重排)
        try:
            res = col.query(query_texts=[query_text or "回忆"],
                            n_results=min(n * 3, col.count()))
        except Exception:
            return []
        # ★ P0-2 BM25 路 + RRF 融合
        try:
            bm25_hits = self._bm25_search(query_text, col, n * 3)
            if bm25_hits:
                res = self._rrf_merge(res, bm25_hits)
        except Exception:
            pass
        now = time.time()
        scored = self._score_results(res, query_text, current_mood, session, now)
        # ★ 跨用户记忆: 她记得跟其他人聊过什么 (雾弥问"有人说过XX"时能想起来)
        #   其他用户库低权重合并, 相关性主导 (当前用户记忆优先, 高度相关的其他库记忆也能召回)
        trust_weights = trust_weights or {}
        try:
            for u in self.users():
                if u == user:
                    continue
                col2 = self.get_user_collection(u)
                if col2.count() == 0:
                    continue
                res2 = col2.query(query_texts=[query_text or "回忆"],
                                  n_results=min(3, col2.count()))
                trust = float(trust_weights.get(u, 0.5))   # ★ 该用户的信任度 (缺省0.5)
                scored += self._score_results(res2, query_text, current_mood, session, now,
                                              cross_user=True, trust=trust)
        except Exception:
            pass
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:n]
        # ★ 保底: 其他用户库有高相关记忆时, 至少留1个位置 (她记得跟别人聊过什么)
        #   用户问句型记忆(如'雾弥说: 你记得...吗')常霸占结果, 挤掉真正信息量大的跨用户记忆
        others = [x for x in scored if x[1].get("cross_user")]
        if others:
            best_o = max(others, key=lambda x: x[0])
            if best_o[0] > 0.35 and not any(x[1].get("cross_user") for x in top):
                top[-1] = best_o
        result = [m for _, m in top]
        # ★ P1-4 缓存写入 (5s TTL, 上限防膨胀)
        try:
            if len(self._retrieve_cache) > 64:
                now2 = time.time()
                self._retrieve_cache = {k: v for k, v in self._retrieve_cache.items()
                                        if now2 - v[0] < 30}
            self._retrieve_cache[cache_key] = (time.time(), result)
        except Exception:
            pass
        return result

    def _score_results(self, res, query_text, current_mood, session, now, cross_user=False, trust=0.5):
        """chromadb query结果 → 评分列表 [(score, m)]; cross_user=True时其他库记忆降权
        trust: 该用户信任度 0~1 (NEKO trust吸收): 信任越高跨用户记忆越被采信
        权重映射: factor = 0.3 + 0.7*trust → 信任1.0=满权, 信任0.0=0.3 (低信任仍可召回, 只是弱)"""
        scored = []
        for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
            meta = meta or {}
            if meta.get("corrected"):
                continue   # ★ 已纠正的记忆不召回 (NEKO corrections)
            sim = 1.0 - dist                       # 余弦相似度
            age_h = (now - float(meta.get("timestamp", now))) / 3600
            decay = max(0.1, 1.0 - age_h * 0.05)   # 遗忘曲线
            salience = float(meta.get("salience", 0.0) or 0.0)   # 深刻度 0~1
            evidence = self._evidence_score(meta, now)   # ★ 证据分 (rein-disp, 读时衰减)
            score = sim * 0.45 + evidence * 0.30 + salience * 0.15 + decay * 0.10
            # 情绪一致性
            mem_mood = float(meta.get("mood", 0.0) or 0.0)
            mood_sim = 1.0 - min(abs(mem_mood - current_mood) / 2.0, 1.0)
            score += mood_sim * 0.05
            m_session = meta.get("session")
            if cross_user:
                # ★ 跨用户记忆 = 她的长期经历(记得别人说过的话), 不受当前会话衰减影响
                pass
            elif m_session == session:
                score += 0.15                      # 当前会话记忆加权(短期优先)
            elif m_session not in (None, "global"):
                score *= 0.15                      # 其他会话短期记忆基本淘汰
            if cross_user:
                # ★ 信任加权 (NEKO trust吸收): 雾弥(信任高)说的比陌生人(信任低)更可信
                trust_factor = 0.3 + 0.7 * max(0.0, min(1.0, float(trust)))
                score *= trust_factor
            # ★ 来源可信度惩罚 (防幻觉吸收): 她自己说过的话(未证实)/系统生成内容
            #   不能和用户亲口说的事实同权重 — 否则编造一次就会被当证据, 自我强化成"假记忆"
            src = meta.get("source", "user_observation")
            if src == "ai_disclosure":
                score *= 0.55      # 她自己的推断/回应: 明显降权
            elif src == "system":
                score *= 0.45      # 系统/联想/外部内容: 更低 (不是关于TA的事实)
            m = {"text": doc, "session": m_session, "timestamp": meta.get("timestamp"),
                 "salience": salience, "mood": mem_mood,
                 "speaker": meta.get("speaker", "user"),      # ★ 谁说的 (user事实/kiri回应/system)
                 "source": meta.get("source", "user_observation"),   # ★ 可信度来源
                 "related": meta.get("related", ""),
                 "evidence": evidence}   # ★ 证据分 (prompt可标注)
            if cross_user:
                m["cross_user"] = True
            scored.append((score, m))
        return scored

    # ---- 联想引擎检索 (含印象权重, reverie 专用) ----
    def reverie_retrieve(self, query_text, current_mood=0.0, n=1, exclude_ids=None, prefer_nonneg=False, user=None):
        """给联想引擎检索记忆: 语义+深刻度+印象(动态衰减)+时间+情绪 加权
        取评分最高的 n 条; exclude_ids 为近期嚼过的记忆(防死循环)
        prefer_nonneg=True: 强制偏向非负面记忆 (情绪平衡, 防联想一直压抑)
        返回含 id 的记忆列表 (mark_chewed 需要 id)"""
        col = self.get_user_collection(user)
        if col.count() == 0:
            return []
        exclude = set(exclude_ids or [])
        try:
            res = col.query(query_texts=[query_text],
                            n_results=min(max(n * 5, 10), col.count()))
        except Exception:
            return []
        now = time.time()
        NEG_HINTS = ["难过", "不开心", "伤心", "低落", "哭", "疼", "痛", "累", "烦",
                     "讨厌", "生气", "失望", "害怕", "崩溃", "沉默", "沉重", "堵"]
        scored = []
        for id_, doc, meta, dist in zip(res["ids"][0], res["documents"][0],
                                        res["metadatas"][0], res["distances"][0]):
            if id_ in exclude:
                continue
            meta = meta or {}
            sim = 1.0 - dist
            age_h = (now - float(meta.get("timestamp", now))) / 3600
            decay = max(0.1, 1.0 - age_h * 0.05)
            salience = float(meta.get("salience", 0.0) or 0.0)
            # ★ 印象权重 (规则④): 被嚼次数 × 距上次被嚼的指数衰减 (τ=6h)
            chewed = float(meta.get("chewed_count", 0.0) or 0.0)
            last_chewed = float(meta.get("last_chewed", 0.0) or 0.0)
            if chewed > 0 and last_chewed > 0:
                impression = chewed * max(0.0, 2.0 - (now - last_chewed) / 21600.0)
            else:
                impression = 0.0
            impression_norm = min(impression / 6.0, 1.0)  # 归一化
            evidence = self._evidence_score(meta, now)    # ★ 证据分 (rein-disp, 读时衰减)
            score = sim * 0.40 + evidence * 0.25 + salience * 0.15 + impression_norm * 0.10 + decay * 0.10
            mem_mood = float(meta.get("mood", 0.0) or 0.0)
            mood_sim = 1.0 - min(abs(mem_mood - current_mood) / 2.0, 1.0)
            score += mood_sim * 0.05
            # ★ 情绪平衡: prefer_nonneg 时负面记忆降权 (只影响排序, 不是硬排除)
            if prefer_nonneg and any(w in doc for w in NEG_HINTS):
                score -= 0.30
            # ★ 来源可信度惩罚 (同 _score_results): 联想优先嚼"用户说的事实", 不嚼她自己的话
            src = meta.get("source", "user_observation")
            if src == "ai_disclosure":
                score *= 0.55
            elif src == "system":
                score *= 0.45
            m = {"id": id_, "text": doc, "salience": salience,
                 "timestamp": meta.get("timestamp"), "mood": mem_mood,
                 "score": score}
            scored.append((score, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:n]]

    def mark_chewed(self, memory_ids, user=None):
        """联想结束后: 被嚼到的记忆印象权重 +1 (规则④)
        ★ P0-3 (2026-08-22 移植 NachoBot sdk_memory_kernel 半衰期衰减):
          权重不无限膨胀 — 加1前先按距上次被嚼时间做半衰期衰减 (τ=24h)
          很久没嚼的记忆权重自然衰减到近0, 再被嚼才重新拉起 (不被历史刷屏永久霸占)"""
        if not memory_ids:
            return
        try:
            col = self.get_user_collection(user)
            data = col.get(ids=list(memory_ids))
        except Exception:
            return
        now = time.time()
        for id_, meta in zip(data["ids"], data["metadatas"]):
            meta = dict(meta or {})
            chewed = float(meta.get("chewed_count", 0.0) or 0.0)
            last = float(meta.get("last_chewed", 0.0) or 0.0)
            # 半衰期衰减: 距上次被嚼越久, 权重越接近 0 (τ=24h)
            if last > 0 and chewed > 0:
                age_h = (now - last) / 3600.0
                chewed = chewed * (0.5 ** (age_h / 24.0))
            meta["chewed_count"] = chewed + 1.0
            meta["last_chewed"] = now
            try:
                col.update(ids=[id_], metadatas=[meta])
            except Exception:
                pass

    # ---- 睡眠期回放: 取最近N小时的事件 (供记忆巩固用) ----
    def recent_events(self, hours=24, max_n=80, user=None, since_ts=None):
        """按时间取最近 hours 小时(或 since_ts 之后)的记忆 (回放原材料); 按用户
        ★ 2026-08-31: 新增 since_ts — 增量回放: 只取"上次巩固以来"的新事件,
          支持白天多次增量巩固 (借鉴 Operit 持续记忆, 防重复提炼旧内容)
        ★ chromadb get 不保证时间序 → 取全量后 Python 排序取最近"""
        try:
            col = self.get_user_collection(user)
            data = col.get(limit=col.count() or 1)
        except Exception:
            return []
        now = time.time()
        out = []
        for doc, meta in zip(data["documents"], data["metadatas"]):
            meta = meta or {}
            if meta.get("corrected"):
                continue   # ★ 已纠正的记忆不进入回放 (NEKO corrections)
            ts = float(meta.get("timestamp", 0) or 0)
            if since_ts is not None:
                if ts < since_ts:
                    continue
            elif now - ts > hours * 3600:
                continue
            out.append({"text": doc, "timestamp": ts, "session": meta.get("session"),
                        "speaker": meta.get("speaker", "user"),
                        "uncertain": bool(meta.get("uncertain", False)),
                        "source": meta.get("source", "user_observation"),
                        "related": meta.get("related", "")})
        out.sort(key=lambda x: x["timestamp"])
        return out[-max_n:]

    # ---- 自然遗忘 (睡眠期: 又旧又低深刻的短期记忆淡出) ----
    def forget(self, older_than_hours=48, salience_below=0.4, session_not_global=True, user=None):
        """遗忘: 删除 超过older_than_hours 且 深刻度低于salience_below 的短期记忆
        模拟'不重要的琐事随睡眠淡忘'; 长期记忆(global)不受影响; 按用户"""
        try:
            col = self.get_user_collection(user)
            data = col.get()
        except Exception:
            return 0
        now = time.time()
        drop_ids = []
        for id_, meta in zip(data["ids"], data["metadatas"]):
            meta = meta or {}
            ts = float(meta.get("timestamp", 0) or 0)
            sal = float(meta.get("salience", 0.0) or 0.0)
            sess = meta.get("session")
            if session_not_global and sess in (None, "global"):
                continue  # 长期记忆不遗忘
            if now - ts > older_than_hours * 3600 and sal < salience_below:
                drop_ids.append(id_)
        if drop_ids:
            col.delete(ids=drop_ids)
        return len(drop_ids)

    # ---- 兼容: 旧接口(save/load 由 chromadb 持久化) ----
    def save(self):
        pass

    def load(self):
        pass

    @staticmethod
    def _source(speaker):
        """speaker → 可信度来源 (NEKO机制): user=用户事实, kiri=AI自我披露, system=系统"""
        if speaker == "user":
            return "user_observation"
        if speaker == "system":
            return "system"
        return "ai_disclosure"

    def _upgrade_disclosure(self, event, col):
        """★ 升级规则: 用户亲口说的事实, 若已有同文本的 ai_disclosure (Kiri推断) → 升级
        (NEKO monotonic upgrade: ai_disclosure→user_observation 单向, 绝不回退)"""
        try:
            data = col.get(limit=col.count() or 1)
            ids = data.get("ids") or []
            metas = data.get("metadatas") or []
            docs = data.get("documents") or []
            for i, mid in enumerate(ids):
                m = metas[i] if i < len(metas) else {}
                m = m or {}
                if m.get("source") == "ai_disclosure":
                    doc = docs[i] if i < len(docs) else ""
                    if (doc or "").strip() == event.strip():
                        m = dict(m)
                        m["source"] = "user_observation"   # ★ 用户印证 → 升级为事实
                        col.update(ids=[mid], metadatas=[m])
        except Exception:
            pass

    def _reinforce_similar(self, event, col):
        """★ 证据强化 (NEKO reinforcement吸收): 用户再次确认高度相似的事实 →
        已有记忆 rein+1 + 刷新时间, 不再重复存 (防重复 + 重要的事越来越重要)
        返回 True=已强化(不新增) / False=无相似(新增)"""
        try:
            if col.count() == 0:
                return False
            res = col.query(query_texts=[event[:200]], n_results=min(3, col.count()))
            for id_, doc, meta, dist in zip(res["ids"][0], res["documents"][0],
                                            res["metadatas"][0], res["distances"][0]):
                meta = meta or {}
                if meta.get("corrected"):
                    continue
                if 1.0 - dist >= 0.90:      # 高度相似 = 同一件事被再次确认
                    m = dict(meta)
                    m["rein"] = float(m.get("rein", 0.0) or 0.0) + 1.0
                    m["rein_ts"] = time.time()
                    m["timestamp"] = time.time()   # 刷新: 近期再次确认 = 还重要
                    col.update(ids=[id_], metadatas=[m])
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _evidence_score(meta, now=None):
        """★ 证据分 (NEKO reinforcement/disputation吸收, 读时计算):
        rein 半衰期7天 (近期确认才有效), disp 半衰期30天 (否定记得更久)
        归一化到 0~1: e/(e+1)"""
        now = now or time.time()
        try:
            rein = float(meta.get("rein", 0.0) or 0.0)
            disp = float(meta.get("disp", 0.0) or 0.0)
            rein_ts = float(meta.get("rein_ts", 0.0) or 0.0)
            disp_ts = float(meta.get("disp_ts", 0.0) or 0.0)
            r = rein * (0.5 ** ((now - rein_ts) / (7 * 86400))) if rein_ts else 0.0
            d = disp * (0.5 ** ((now - disp_ts) / (30 * 86400))) if disp_ts else 0.0
            e = r - d
            return e / (abs(e) + 1.0) if e else 0.0
        except Exception:
            return 0.0

    @staticmethod
    def _is_correction(event):
        """检测纠正意图: 用户否定/纠正之前的认知 (NEKO corrections)
        命中 → 先纠正相关记忆, 避免错误认知残留"""
        hints = ["不喜欢", "不是这样", "不是那样的", "错了", "记错", "误会", "并没有",
                 "才没有", "别乱说", "没这回事", "纠正", "其实我", "其实不是", "并不"]
        return any(h in event for h in hints)

    def negate(self, event, user=None):
        """★ 纠正 (NEKO corrections + LLM仲裁吸收): 用户否定某认知 →
        1. 检索语义相关候选; 2. LLM 仲裁哪些真的被否定 (防过度标记:
           用户说"不是黑色"不该把"喜欢蓝色"也标错); 3. 命中的标 corrected + disp+1
        LLM 不可用/超时 → 降级全部标记 (旧行为, 不阻塞)"""
        try:
            col = self.get_user_collection(user)
            if col.count() == 0:
                return 0
            res = col.query(query_texts=[event or "纠正"],
                            n_results=min(6, col.count()))
        except Exception:
            return 0
        cands = []
        for id_, doc, meta in zip(res["ids"][0], res["documents"][0], res["metadatas"][0]):
            m = meta or {}
            if m.get("corrected"):
                continue
            cands.append({"id": id_, "doc": doc, "meta": m})
        if not cands:
            return 0
        marked = self._arbitrate_corrections(event, cands)
        # ★ 2026-08-30 修复: 原降级是 marked = cands (全部标记 corrected),
        #   触发场景: _is_correction 命中日常高频词("其实我/并不/才没有") +
        #   LLM 仲裁失败/超时 → 最多6条相似记忆被静默标 corrected, 从此不召回。
        #   降级策略改为"标记空集" — 宁可不纠正, 不可误杀记忆。
        if marked is None:
            marked = []
        n = 0
        for c in marked:
            m = dict(c["meta"])
            m["corrected"] = True
            m["corrected_at"] = time.time()
            m["disp"] = float(m.get("disp", 0.0) or 0.0) + 1.0   # ★ 否定计数 (证据分下降)
            m["disp_ts"] = time.time()
            try:
                col.update(ids=[c["id"]], metadatas=[m])
                n += 1
            except Exception:
                pass
        return n

    def _arbitrate_corrections(self, event, cands):
        """LLM 仲裁: 返回被否定真正命中的候选子集; 失败返回 None (调用方降级)
        NEKO: 矛盾不静默覆盖, 攒批交 LLM 裁决 (Kiri: 单条纠正即时仲裁, 候选≤6)"""
        try:
            import engine
            cand_txt = "\n".join(f"{i}. {c['doc'][:80]}" for i, c in enumerate(cands))
            sys_p = ("你是记忆纠错仲裁器。用户说了一句话, 可能否定/纠正了 Kiri 记忆里的某些认知。"
                     "判断每条候选记忆是否**真的**被这句话否定: "
                     "只标记明确被否定的 (如用户说'不喜欢黑色', 则'喜欢黑色'被否定); "
                     "无关的、或只是话题相近但没被否定的, 不标记。")
            user_p = (f"用户的话: {event}\n\n候选记忆:\n{cand_txt}\n\n"
                      f"输出JSON(只输出JSON): {{\"corrected\": [true或false, 按序号一一对应]}}")
            raw = engine.generate(sys_p, user_p, max_tokens=150, temperature=0.1)
            import re
            import json as _json
            m = re.search(r"\{[^{}]*\}", raw)
            if not m:
                return None
            verdicts = _json.loads(m.group(0)).get("corrected")
            if not isinstance(verdicts, list) or len(verdicts) != len(cands):
                return None
            return [c for c, v in zip(cands, verdicts) if v]
        except Exception:
            return None

    @staticmethod
    def _salience(event):
        """深刻度评分 0~1 (generative_agents poignancy 的启发式版, 零API成本)
        情感/关系/个人信息 > 普通内容"""
        s = 0.2  # 基础分
        # 长度: 内容越具体越可能有价值
        s += min(len(event) / 200, 0.2)
        # 强情感词 (高深刻度)
        strong = ["喜欢", "爱", "我爱你", "喜欢你", "讨厌", "恨", "难过", "伤心", "崩溃",
                  "害怕", "担心", "想你了", "想你", "在乎", "心疼"]
        if any(w in event for w in strong):
            s += 0.4
        # 个人信息/关系进展
        mid = ["生日", "家人", "爸妈", "父母", "妈妈", "爸爸", "朋友", "工作", "梦想", "害怕", "秘密", "重要",
               "答应", "约定", "记得", "第一次", "永远", "我们", "学", "练", "打算", "计划", "想学", "在学"]
        if any(w in event for w in mid):
            s += 0.25
        # 一般情感/状态
        weak = ["开心", "高兴", "累", "忙", "烦", "无聊", "困", "饿"]
        if any(w in event for w in weak):
            s += 0.15
        return min(s, 1.0)

    @staticmethod
    def _type(event):
        if any(k in event for k in ["知道", "学习", "理解", "发现"]):
            return "semantic"
        if any(k in event for k in ["感觉", "难过", "开心", "伤心", "喜欢"]):
            return "emotional"
        if any(k in event for k in ["我", "我的"]):
            return "episodic"
        return "procedural"
