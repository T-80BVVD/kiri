# -*- coding: utf-8 -*-
"""知识页合成 (memory_knowledge.py) — 借鉴 Hindsight 机制: 记忆从RAG原文升级为综合画像
=====================================================================
为什么: chromadb RAG 检索的是"雾弥说: XX"原始对话, 散且旧。
知识页: 综合成"你了解的雾弥"画像 (偏好/习惯/关系/动态), 自动更新维护。
- 她记得的应该是"雾弥喜欢黑色、在学吉他、累时会找我"这种综合理解
- 每次夜间 consolidate 或主动合成时刷新, 旧理解合并/更新
存储: 独立 collection kiri_knowledge_<user>, 每用户几条综合画像条目
检索: respond 时知识页优先注入 (比原始对话准)

NEKO reflection 生命周期吸收 (2026-08-19):
- 每条画像有状态: pending(合成得出, 未证实) / confirmed(用户确认过) / promoted(反复确认, 固化为"确定了解")
- 证据分 evidence: 用户确认 +1.0, 否定 -1.0, 忽略 -0.2/次; >=1.0 升 confirmed, >=2.0 升 promoted, <=-1.0 删除
- 浮出标注: pending 渲染时加"(还不太确定)", promoted 不加 — 她"知道"哪些是确凿的
- 合成改为增量合并: 新洞察以 pending 加入, 已确认/已提升的条目保留不重写
- 自然淘汰: pending 且老(>7天)且证据低(<0.3) → 删除 (避免画像堆积过时信息)
=====================================================================
"""
import time
import uuid

import engine
import prompt as prompt_mod

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_PROMOTED = "promoted"

CONFIRM_EVIDENCE = 1.0      # 用户确认 +1.0 (NEKO: confirm +1.0)
DENY_EVIDENCE = 1.0         # 用户否定 -1.0 (NEKO: rebut +1.0 但反向)
IGNORE_EVIDENCE = 0.2       # 无反馈 -0.2 (NEKO: ignored -0.2)
CONFIRMED_AT = 1.0          # 升级阈值
PROMOTED_AT = 2.0
DELETE_AT = -1.5            # 强否定(两次否定或否定+多次忽略) → 删除; 单次否定先保留负证据
PENDING_MAX_AGE_DAYS = 7    # pending 保留上限
PENDING_MIN_EVIDENCE = 0.3  # pending 淘汰证据线
MAX_ENTRIES = 12            # 知识页条数上限

# 确认/否定信号 (关键词启发式, 零LLM成本; 复杂场景可升级为LLM判断)
CONFIRM_HINTS = ["对", "是的", "没错", "就是", "嗯嗯", "对对", "是这样", "确实", "说得对", "你真懂", "猜对了"]
DENY_HINTS = ["不是", "不对", "错了", "没有", "并不", "才不是", "别乱说", "哪里", "不是这样的",
              "并没有", "你猜错", "想多了"]


class KnowledgeBase:
    def __init__(self, memory):
        self.memory = memory          # memory_mod.Memory 实例 (复用 chromadb/embedding)
        self._cols = {}

    def _col(self, user):
        """知识页 collection (每用户独立)"""
        key = self.memory._user_key(user)
        if key in self._cols:
            return self._cols[key]
        name = "kiri_knowledge" if user == self.memory.DEFAULT_USER else f"kiri_knowledge_{key}"
        col = self.memory.client.get_or_create_collection(
            name, embedding_function=self.memory.emb,
            metadata={"hnsw:space": "cosine"})
        self._cols[key] = col
        return col

    # ---- 合成/更新知识页 (增量合并, NEKO reflection吸收) ----
    def synthesize(self, user=None):
        """综合某用户的记忆 → 生成新洞察, 以 pending 状态增量加入知识页
        已 confirmed/promoted 的条目保留 (不重写, 保住证据分)
        ★ speaker 分离: user(对方亲口说)=事实优先 / kiri(她回应)=推断降级"""
        user = user or self.memory.DEFAULT_USER
        col = self._col(user)
        old_knowledge = ""
        try:
            if col.count() > 0:
                data = col.get(limit=col.count())
                old_knowledge = "\n".join(data["documents"][:8])[:1500]
        except Exception:
            pass
        mems = self.memory.recent_events(hours=72, user=user)
        if not mems:
            return 0
        # ★ 按 speaker 分组: 对方说的=事实, 她说的=推断/回应
        facts = [m for m in mems if m.get("speaker", "user") == "user"]
        infers = [m for m in mems if m.get("speaker", "user") == "kiri"]
        # ★ P1-2 (2026-08-22 移植 NachoBot): 临时/不确定观察不进"亲口说的事实"
        #   ("今天心情不好" 是临时状态 → 不能固化成 "他忧郁") — 单独标出, 不进合成
        uncertain = [m for m in facts if m.get("uncertain")]
        facts = [m for m in facts if not m.get("uncertain")]
        fact_txt = "\n".join(f"- {m['text'][:70]}" for m in facts[-25:])[:1600]
        infer_txt = "\n".join(f"- {m['text'][:70]}" for m in infers[-15:])[:900]
        recent = (f"他亲口说过的事实:\n{fact_txt or '(无)'}\n\n"
                  f"她回应时提到的话(推断, 需谨慎):\n{infer_txt or '(无)'}")
        old_part = ("旧知识页(你的已有了解):\n" + old_knowledge + "\n\n") if old_knowledge else ""
        user_p = old_part + "最近的记忆:\n" + recent
        try:
            raw = engine.generate(prompt_mod.knowledge_system(), user_p,
                                  max_tokens=800, temperature=0.4)
            entries = [e.strip() for e in raw.split("\n") if e.strip().startswith(("-", "•"))]
            entries = [e.lstrip("-• ").strip() for e in entries if len(e.strip()) > 4]
            if not entries:
                return 0
            # 增量合并: 每条新洞察 → 与现有高度相似则强化现有, 否则以 pending 加入
            added = 0
            now = time.time()
            for e in entries[:8]:
                if self._merge_or_add(user, col, e, now):
                    added += 1
            self._prune(user, col, now)
            return added
        except Exception:
            return 0

    def _merge_or_add(self, user, col, text, now=None):
        """新洞察合并: 与现有条目语义相似(>=0.9) → 更新文本+证据+1(再次出现=更可靠);
        否则以 pending 新增 (条数封顶 MAX_ENTRIES)"""
        now = now or time.time()
        try:
            if col.count() > 0:
                res = col.query(query_texts=[text[:200]], n_results=min(3, col.count()))
                for id_, doc, meta, dist in zip(res["ids"][0], res["documents"][0],
                                                res["metadatas"][0], res["distances"][0]):
                    meta = meta or {}
                    if 1.0 - dist >= 0.90:
                        m = dict(meta)
                        m["text"] = text[:200]
                        m["updated"] = now
                        m["evidence"] = float(m.get("evidence", 0.0) or 0.0) + 0.5  # 再次合成到 = 半次确认
                        m["status"] = self._status_for(m["evidence"])
                        col.update(ids=[id_], documents=[text[:200]], metadatas=[m])
                        return True
            if col.count() >= MAX_ENTRIES:
                return False   # 已满, 不新增 (防膨胀)
            col.add(ids=[str(uuid.uuid4())], documents=[text[:200]],
                    metadatas=[{"user": user, "status": STATUS_PENDING,
                                "evidence": 0.0, "created": now, "updated": now,
                                "surfacings": 0}])
            return True
        except Exception:
            return False

    def _status_for(self, evidence):
        """证据分 → 状态 (NEKO: pending→confirmed(>=1)→promoted(>=2))"""
        if evidence >= PROMOTED_AT:
            return STATUS_PROMOTED
        if evidence >= CONFIRMED_AT:
            return STATUS_CONFIRMED
        return STATUS_PENDING

    def _prune(self, user, col, now=None):
        """自然淘汰: pending 且 老(>7天) 且 证据低(<0.3) → 删除; 强否定(<=-1) → 删除"""
        now = now or time.time()
        try:
            if col.count() == 0:
                return
            data = col.get(limit=col.count())
            drop = []
            for id_, meta in zip(data["ids"], data["metadatas"]):
                m = meta or {}
                ev = float(m.get("evidence", 0.0) or 0.0)
                if ev <= DELETE_AT:
                    drop.append(id_)
                    continue
                if m.get("status") == STATUS_PENDING:
                    created = float(m.get("created", 0) or 0)
                    age_days = (now - created) / 86400 if created else 999
                    if age_days > PENDING_MAX_AGE_DAYS and ev < PENDING_MIN_EVIDENCE:
                        drop.append(id_)
            if drop:
                col.delete(ids=drop)
        except Exception:
            pass

    # ---- 检索知识页 ----
    def retrieve(self, query_text="", n=3, user=None):
        """检索知识页 (综合画像, 优先于原始对话)
        返回 [{text, score, knowledge, id, status, evidence}] — id 供 feedback 回写"""
        user = user or self.memory.DEFAULT_USER
        col = self._col(user)
        if col.count() == 0:
            return []
        try:
            res = col.query(query_texts=[query_text or "关于你"],
                            n_results=min(n, col.count()))
            out = []
            for id_, doc, meta, dist in zip(res["ids"][0], res["documents"][0],
                                            res["metadatas"][0], res["distances"][0]):
                m = meta or {}
                status = m.get("status") or STATUS_PENDING
                # 浮出标注 (NEKO surfacing): pending 渲染加"还不太确定" (prompt层用)
                text = doc
                if status == STATUS_PENDING:
                    text = f"(还不太确定) {doc}"
                out.append({"text": text, "raw": doc, "score": 1.0 - dist,
                            "knowledge": True, "id": id_,
                            "status": status, "evidence": float(m.get("evidence", 0.0) or 0.0)})
            return out
        except Exception:
            return []

    # ---- 用户反馈回写 (NEKO reflection feedback 闭环) ----
    def feedback(self, user, user_text, knowledge_ids=None):
        """用户对刚注入的知识页的反应 → 调整证据分/状态
        confirm → +1.0; deny → -1.0; 无信号 → -0.2 (忽略衰减)
        返回 (确认数, 否定数, 忽略数)"""
        user = user or self.memory.DEFAULT_USER
        if not knowledge_ids:
            return 0, 0, 0
        col = self._col(user)
        text = str(user_text or "")
        if not text.strip():
            return 0, 0, 0
        if any(h in text for h in DENY_HINTS):
            delta, tag = -DENY_EVIDENCE, "deny"
        elif any(h in text for h in CONFIRM_HINTS):
            delta, tag = CONFIRM_EVIDENCE, "confirm"
        else:
            delta, tag = -IGNORE_EVIDENCE, "ignore"
        counts = {"confirm": 0, "deny": 0, "ignore": 0}
        try:
            data = col.get(ids=list(knowledge_ids))
            for id_, meta in zip(data["ids"], data["metadatas"]):
                m = dict(meta or {})
                ev = float(m.get("evidence", 0.0) or 0.0) + delta
                m["evidence"] = max(-2.0, min(4.0, ev))
                m["status"] = self._status_for(m["evidence"])
                m["surfacings"] = int(m.get("surfacings", 0) or 0) + 1
                m["updated"] = time.time()
                col.update(ids=[id_], metadatas=[m])
                counts[tag] += 1
        except Exception:
            pass
        # 强否定 → 删
        try:
            self._prune(user, col)
        except Exception:
            pass
        return counts["confirm"], counts["deny"], counts["ignore"]

    def count(self, user=None):
        return self._col(user).count()

    def stats(self, user=None):
        """知识页状态分布 (验收用)"""
        col = self._col(user)
        if col.count() == 0:
            return {}
        try:
            data = col.get(limit=col.count())
            out = {}
            for meta in data["metadatas"]:
                st = (meta or {}).get("status") or STATUS_PENDING
                out[st] = out.get(st, 0) + 1
            return out
        except Exception:
            return {}


if __name__ == "__main__":
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import memory as memory_mod
    m = memory_mod.Memory()
    kb = KnowledgeBase(m)
    n = kb.synthesize("雾弥")
    print(f"合成雾弥知识页: {n} 条, 状态分布: {kb.stats('雾弥')}")
    for q in ["他喜欢什么颜色", "他在学什么", "他最近怎么样"]:
        print(f"\n问[{q}]:")
        for r in kb.retrieve(q, user="雾弥"):
            print(f"  [{r['status']}] {r['text'][:50]}")
