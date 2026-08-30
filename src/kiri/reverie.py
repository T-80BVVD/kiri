# -*- coding: utf-8 -*-
"""Kiri 联想引擎 (reverie) — 常驻运转的"走神"循环
=====================================================================
猜想来源: ARCHITECTURE.md 第十一章 (雾弥 2026-08-17 凌晨提出)
核心: 让 Kiri 不依赖用户说话, 自己持续产生念头 (血液循环)
机制 (四规则):
  ① 检索: 环境相似 + 加权评分(语义/深刻度/印象/时间/情绪) 取最高分
  ② 串行咀嚼: 每轮嚼 [环境+一条记忆+工作记忆] 产念头, 次轮取次高分
  ③ 工作记忆: 联想内累积, 联想结束全部清空, 高salience念头入长期库
  ④ 印象权重: 被嚼到的记忆 chewed_count+1 (检索时动态衰减)
诚实边界: 念头介质是LLM(假), 但调度是自主的(真) — 她不说话也在想
=====================================================================
"""
import time
import random
import re
import difflib

import engine
import prompt as prompt_mod
import config as config_mod

# 配置 (统一在 config.py, 2026-08-30 集中化)
REVERIE_INTERVAL_SECONDS = config_mod.REVERIE_INTERVAL_SECONDS
REVERIE_ROUNDS = config_mod.REVERIE_ROUNDS
REVERIE_SALIENCE_THRESHOLD = config_mod.REVERIE_SALIENCE_THRESHOLD
RECENT_CHEWED_MAX = config_mod.RECENT_CHEWED_MAX
REVERIE_MIN_SILENCE_MIN = config_mod.REVERIE_MIN_SILENCE_MIN
CURIOSITY_EVERY = config_mod.CURIOSITY_EVERY
CURIOSITY_SEARCH_N = config_mod.CURIOSITY_SEARCH_N
CURIOSITY_MAX_ROUNDS = config_mod.CURIOSITY_MAX_ROUNDS
GOAL_PUSH_COOLDOWN = config_mod.GOAL_PUSH_COOLDOWN
# (WANDER/DEEPEN/找事做 配置已随 2026-08-21 漫游取消 + 2026-08-30 死代码清理 一并移除)


class ReverieEngine:
    def __init__(self, kiri):
        self.kiri = kiri
        self.work_memory = []      # 本次联想的念头 [{text, salience}]
        self.recent_chewed = []    # 近期嚼过的记忆id (防重复)
        self.last_cycle = 0.0
        self.cycle_count = 0       # 联想次数 (好奇触发计数)
        self.last_curiosity = 0.0  # 上次好奇触发时间
        self._interest_topics = []       #  从念头提取的兴趣点 (供未来主动搜索用)
        self._last_goal_push_ts = 0.0    #  上次推进目标时间 (节流, 防每10分钟都启动)

    # ---- 环境快照 ----
    def _reverie_user(self):
        """联想用户: 雾弥70%权重(她独处时最常想起他), 朋友30% (朋友的记忆也进走神)
        只有雾弥有记忆时 → 雾弥"""
        k = self.kiri
        users = k.memory.users()
        others = [u for u in users if u != k.DEFAULT_USER]
        if not others:
            return k.DEFAULT_USER
        if random.random() < 0.7:
            return k.DEFAULT_USER
        return random.choice(others)

    def _env_text(self, user=None):
        k = self.kiri
        user = user or k.DEFAULT_USER
        st = k.state
        # 时间
        h = time.localtime().tm_hour
        if 5 <= h < 12: t = f"上午{h}点"
        elif 12 <= h < 14: t = f"中午{h}点"
        elif 14 <= h < 18: t = f"下午{h}点"
        elif 18 <= h < 23: t = f"晚上{h}点"
        else: t = f"深夜{h}点"
        # 情绪
        e = st.emotion.state
        mood = e["deep_affect"]["current_mood"]
        mood_txt = "很好" if mood > 0.3 else ("低落" if mood < -0.3 else "平静")
        silence = int((time.time() - st.last_interact) / 60)
        # 关系 (按联想用户)
        rel = st.social.relationships.get(user, {})
        intimacy = rel.get("intimacy", 0.0)
        # 最近对话 (最近30条, 联想用户的对话 — 让她看到自己跟谁说过什么)
        recent = ""
        dlg = k.get_dialog(user) if hasattr(k, "get_dialog") else k.dialog
        if dlg:
            parts = []
            for m in dlg[-30:]:
                who = user if m["role"] == "user" else "你"
                parts.append(f"{who}: {m['text'][:50]}")
            recent = "最近对话: " + " | ".join(parts[-20:])
        return (f"{t}。你的心情{mood_txt}。{user}已沉默约{silence}分钟。"
                f"你们的关系亲密程度约{intimacy:.2f}。{recent}")

    # ---- 单轮咀嚼 ----
    def _rel_time(self, ts):
        """时间戳 → 相对时间描述 (联想时标注记忆的'过去感', 防把旧记忆当现在)"""
        try:
            ts = float(ts or 0)
            if not ts:
                return ""
            dt = time.time() - ts
            if dt < 0:
                return "刚刚"
            if dt < 3600:
                return f"{int(dt//60)}分钟前"
            if dt < 86400:
                return f"{int(dt//3600)}小时前"
            if dt < 172800:
                return "昨天"
            if dt < 86400 * 7:
                return f"{int(dt//86400)}天前"
            return "很久以前"
        except Exception:
            return ""

    def _chew(self, env, memory_text, wm_lines, mem_ts=None):
        """LLM 自由联想: 环境+记忆+工作记忆 → 一个新念头
        mem_ts: 记忆的时间戳 → 标注相对时间, 让念头有'过去感'"""
        rel = self._rel_time(mem_ts)
        time_tag = f"[这是{rel}的记忆]" if rel else ""
        wm_txt = "\n".join(f"- {c['text']}" for c in wm_lines) if wm_lines else "(空)"
        user_p = (f"【此刻的环境】{env}\n\n"
                  f"【你想起的记忆】{time_tag} {memory_text}\n\n"
                  f"【刚才飘过的念头】{wm_txt}")
        raw = engine.generate(prompt_mod.reverie_chew_system(), user_p,
                              max_tokens=200, temperature=0.9)
        return prompt_mod.parse_reverie(raw)

    # ---- 情绪平衡: 判断已嚼的记忆里负面占比 ----
    _NEG_HINTS = ["难过", "不开心", "伤心", "低落", "哭", "疼", "痛", "累", "烦",
                  "讨厌", "生气", "失望", "害怕", "崩溃", "沉默", "沉重", "堵"]

    def _recent_chews_negative(self, chewed_ids, threshold, user=None):
        """最近嚼的记忆里负面占比是否过高 (用于强制换非负面记忆)"""
        if len(chewed_ids) < threshold:
            return False
        k = self.kiri
        try:
            col = k.memory.get_user_collection(user) if hasattr(k.memory, "get_user_collection") else k.memory.collection
            data = col.get(ids=list(chewed_ids))
            neg = 0
            for doc in data.get("documents", []):
                if any(w in doc for w in self._NEG_HINTS):
                    neg += 1
            return neg >= threshold
        except Exception:
            return False

    # ---- 一次完整联想循环 ----
    def run_cycle(self):
        # ★ 聊天优先 (2026-08-27): 用户正在聊天 → 联想让路 (不抢本地引擎)
        if config_mod.CHAT_ACTIVE.is_set() and time.time() - config_mod.CHAT_ACTIVE_TS[0] < 120:
            return 0, []
        """规则①②③④ 的完整执行。返回 (嚼过的记忆数, 产出的念头列表)
         多用户: 联想用户 = 雾弥为主(70%) + 朋友轮换(30%), 朋友的记忆也进走神"""
        k = self.kiri
        lu = self._reverie_user()          #  本次联想的用户
        env = self._env_text(lu)
        # ★ 目标召回 (2026-08-21 主动性完善): 联想时自然想起进行中的目标 (不强制)
        #   她有自己要做的事 → 走神时会想起来 → 可能产生"该去做了"的念头
        try:
            import goals
            recall = goals.recall_goal()
            if recall:
                env += "\n(心里惦记着: " + recall + ")"
        except Exception:
            pass
        # ★ 连续意识流 (2026-08-21 雾弥: "内部思维流本质上还是离散的" — 治本):
        #   念头不清空、跨周期/跨进程接续 — 走神是一条连续的线, 不是孤立的点
        prev = [c for c in self.work_memory if isinstance(c, dict) and c.get("text")][-8:]
        if not prev:
            try:
                import kiri_mind
                prev = [{"text": t, "salience": s} for t, s in kiri_mind.recent_thoughts(6)]
            except Exception:
                prev = []
        self.work_memory = prev
        chewed_ids = []
        thoughts = []

        for rnd in range(REVERIE_ROUNDS):
            # 规则①: 检索最高分记忆 (排除近期嚼过的)
            # query 用环境+当前工作记忆, 让联想随念头滚动
            wm_query = " ".join(c["text"] for c in self.work_memory[-2:])
            query = (env + " " + wm_query)[:300]

            #  情绪平衡: 若已连续嚼了2轮负面记忆, 这一轮强制检索非负面记忆
            #   (念头mood: 用salience无法直接判断正负, 用记忆文本里的负面词粗判)
            force_nonneg = self._recent_chews_negative(chewed_ids, 2, user=lu)
            mems = k.memory.reverie_retrieve(
                query,
                current_mood=k.state.emotion.state["deep_affect"]["current_mood"],
                n=1, exclude_ids=self.recent_chewed + chewed_ids,
                prefer_nonneg=force_nonneg, user=lu)
            if not mems:
                break  # 没新记忆可嚼, 提前结束
            mem = mems[0]
            chewed_ids.append(mem["id"])
            #  记忆时间戳 → 念头有'过去感' (防把旧记忆当现在)
            thought = self._chew(env, mem["text"], self.work_memory,
                                 mem_ts=mem.get("timestamp"))
            mem_label = mem["text"][:40]
            # 规则②③: 嚼 → 念头入工作记忆
            # ★ 2026-08-30 修复: 此处曾有一段完全重复的 if 块 (复制粘贴残留),
            #   导致同一念头被 append 两次进 work_memory/thoughts, 事件也记两遍。
            if thought and thought.get("text"):
                self.work_memory.append(thought)
                thoughts.append(thought)
                k._log_event("reverie", round=rnd, user=lu, memory=mem_label,
                             thought=thought["text"][:80],
                             salience=round(float(thought.get("salience", 0)), 3))
                #  心路日志: 所有联想念头 (带联想用户)
                try:
                    import kiri_mind
                    kiri_mind.thought(thought["text"], thought.get("salience", 0), mem_label, user=lu)
                except Exception:
                    pass
                #  念头入库 (供 respond 时"想起"检索) — 按联想用户
                try:
                    k.memory.encode_thought(thought["text"], thought.get("salience", 0), "reverie", user=lu)
                except Exception:
                    pass
                #  思考→行动: 念头提炼兴趣点 (下一次漫游搜索话题)
                #  ★ 修复 (2026-08-19): 原逻辑把整段念头文本当搜索词 → B站搜索必"没结果"
                #    (如"三小时前还在说'测试一下'，现在都能聊真"整句当关键词)
                #    改为: LLM提炼2-3个关键词, 失败回退规则(去括号+截断+去口语尾巴)
                try:
                    topic = self._extract_search_topic(thought["text"])
                    if topic:
                        self._interest_topics.append(topic)
                        if len(self._interest_topics) > 20:
                            self._interest_topics = self._interest_topics[-20:]
                except Exception:
                    pass

        # 规则④: 被嚼到的记忆印象+1 (按联想用户)
        if chewed_ids:
            k.memory.mark_chewed(chewed_ids, user=lu)
            self.recent_chewed.extend(chewed_ids)
            if len(self.recent_chewed) > RECENT_CHEWED_MAX:
                self.recent_chewed = self.recent_chewed[-RECENT_CHEWED_MAX:]

        # 规则③: 高salience念头写入长期记忆 (按联想用户)
        kept = 0
        for c in thoughts:
            if float(c.get("salience", 0) or 0) >= REVERIE_SALIENCE_THRESHOLD:
                k.memory.encode(f"[联想] {c['text']}", k.state.emotion.state, user=lu)
                kept += 1
            #  内心分享: 高salience念头小概率想说出来 (入队, 通道审查后外发)
            try:
                if (float(c.get("salience", 0) or 0) >= config_mod.SHARE_THOUGHT_SALIENCE
                        and config_mod.SHARE_THOUGHT_PROB >= random.random()):
                    k.offer_share(c["text"][:80], kind="thought")
            except Exception:
                pass
        if kept:
            logger = __import__("logging").getLogger("kiri")
            logger.info(f"联想[{lu}]: 嚼{len(chewed_ids)}条 → 产生{len(thoughts)}个念头, 沉淀{kept}条")

        # 联想结束: 保留最近念头 (连续意识流), 只截断上限
        self.work_memory = [c for c in self.work_memory if isinstance(c, dict) and c.get("text")][-8:]
        self.last_cycle = time.time()
        self.cycle_count += 1

        #  好奇触发 (每 CURIOSITY_EVERY 次联想): 念头→想查的问题→搜索→学进记忆
        if thoughts and self.cycle_count % CURIOSITY_EVERY == 0:
            self._curiosity(thoughts, user=lu)

        # ★ 念头→行动 (2026-08-21 雾弥: 治本 — 行动从她的念头流里长出来, 不是系统定时抛):
        #   念头里冒出"该去做了"的冲动 (高salience+行动词) → 启动后台真的去做 (节流)
        try:
            self._maybe_act_on_thoughts(thoughts)
        except Exception:
            pass

        # ★ 事件绑定情绪 (M1, 2026-08-27): 联想产出念头 → 情绪记录 (想起/回味, 有因可查)
        try:
            if thoughts and config_mod.EMOTION_EVENTS_ENABLED:
                top = max(thoughts, key=lambda c: float(c.get("salience", 0) or 0))
                k.emotion_events.record(
                    "thought", str(top.get("text", ""))[:100],
                    extra={"intensity": min(0.9, 0.4 + float(top.get("salience", 0) or 0) * 0.4)})
        except Exception:
            pass

        return len(chewed_ids), thoughts

    # ---- 记忆优先预检 (2026-08-19 雾弥发现: 会重复搜索已查过的问题) ----
    def _recall_before_search(self, question, user):
        """搜索前先翻记忆库: 是否已查过类似问题 (记忆里有 [外部] 我查了「...」)
        相关度高 → 返回记忆里的结果 (直接用, 不重复外搜); 否则 None (照常外搜)
        防: 同样的问题反复好奇反复搜索, 浪费API+重复劳动"""
        try:
            k = self.kiri
            mems = k.memory.retrieve(
                query_text=question, n=5, user=user,
                current_mood=k.state.emotion.state["deep_affect"]["current_mood"])
            if not mems:
                return None
            # 只看"她查过的问题"类记忆 ([外部] 前缀), 别把普通聊天记忆当搜索结果
            for m in mems:
                text = str(m.get("text", ""))
                if text.startswith("[外部] 我查了"):
                    # 相关度: chroma distance 已排序, 取第一个 [外部] 即最相关
                    result = text.replace("[外部] 我查了", "", 1).strip()
                    # 剥出问题部分做相关性二次校验 (防止"我查了别的"却答非所问)
                    q_part = result.split(":")[0].strip() if ":" in result else ""
                    if q_part and q_part and len(q_part) >= 2:
                        sim = difflib.SequenceMatcher(None, question[:20], q_part[:20]).ratio()
                        if sim < 0.25:
                            # 问题文字差异大 → 语义相关也不硬套 (保守: 宁可外搜)
                            continue
                    return result   # ★ 2026-08-20: 取消 [:400] 硬截断 — 记忆完整返回
            return None
        except Exception:
            return None

    # ---- 好奇→查→学 (agentic 多轮: 看结果→判断够不够→停/换角度继续/诚实放弃) ----
    def _curiosity(self, thoughts, user=None):
        """她有了念头 → 生成想查的问题 → MCP获取 → 评估够不够 → 循环最多CURIOSITY_MAX_ROUNDS轮
        每一轮都是一次工具调用; 满意才停, 换关键词再试, 查不到就诚实放弃
        user: 联想用户 (好奇学到的东西归该用户的库)"""
        k = self.kiri
        lu = user or k.DEFAULT_USER
        try:
            # 1. 基于最近念头生成问题 (初始决策)
            latest = thoughts[-1]["text"] if thoughts else ""
            raw = engine.generate(prompt_mod.curiosity_system(), f"你最近的念头: {latest}\n\n你的好奇是什么？",
                                  max_tokens=300, temperature=0.7)
            import re
            import json as _json
            m = re.search(r'\{[^{}]*\}', raw)
            if not m:
                return
            obj = _json.loads(m.group(0))
            q = str(obj.get("question", "")).strip()
            kw = str(obj.get("keywords", "")).strip()
            method = str(obj.get("method", "search")).strip().lower()
            if not q or len(q) < 4:
                return  # 没真正的好奇

            # ★ 记忆优先 (2026-08-19 雾弥发现): 联想后想搜索 → 先翻记忆库
            #   已查过的问题以 [外部] 我查了「...」 存过 → 相关度高直接用, 不再外搜 (防重复搜索)
            import mcp_client
            import kiri_mind
            mem_hit = self._recall_before_search(q, lu)
            if mem_hit:
                trail = [{"round": 1, "method": "记忆", "query": q,
                          "result": mem_hit[:150], "src": "记忆"}]
                verdict = "satisfied"
                final_result = mem_hit
                k._log_event("curiosity", question=q, keywords=q, method="memory",
                             round=1, verdict="satisfied", result=mem_hit[:120],
                             source="memory_hit")
                logger = __import__("logging").getLogger("kiri")
                logger.info(f"好奇: 记忆优先命中「{q}」→ 不重复外搜, 直接用了记忆 ({len(mem_hit)}字)")
                # 心路/数据日志 (与外部查到同格式)
                try:
                    kiri_mind.curiosity(q, kw, f"第1轮记忆「{q}」 | 判定:satisfied | {mem_hit[:160]}")
                    import data_log
                    data_log.curiosity(q, mem_hit[:200])
                except Exception:
                    pass
                # 分享: 想起自己查过的东西, 小概率想说出来
                try:
                    if config_mod.SHARE_CURIOSITY_PROB >= random.random():
                        share = self._share_line(q, mem_hit)
                        if share:
                            k.offer_share(share, kind="curiosity")
                except Exception:
                    pass
                return

            # 2. agentic 循环: 执行→评估→决定
            search_q = kw if kw and len(kw) >= 3 else q
            trail = []          # 每轮轨迹 (评估器能看到全部历史)
            verdict = "abandon"  # 默认: 异常/无结果 → 诚实放弃
            final_result = ""
            tried = []          # 已试过的关键词 (防原地打转)
            for rnd in range(1, CURIOSITY_MAX_ROUNDS + 1):
                # ★ 平台路由 (2026-08-19): LLM评估可换平台 — 不只 search/ask_ai 二选一
                #   search=Bing网页, ask_ai=问AI导师, bili_search=B站, zhihu=知乎日报
                if method == "ask_ai":
                    result = mcp_client.call_tool("ask_ai", {"question": q})
                    src = "问AI"
                elif method == "bili_search":
                    result = mcp_client.call_tool("bili_search", {"keyword": search_q, "n": 3})
                    src = "B站搜索"
                elif method == "zhihu":
                    result = mcp_client.call_tool("zhihu_daily", {"n": 3})
                    src = "知乎日报"
                else:
                    result = mcp_client.call_tool("search", {"query": search_q, "n": CURIOSITY_SEARCH_N})
                    src = "搜索"
                # ★ 空回复标记 (engine.generate 对空响应的兜底 "(……)" ) 视为无结果,
                #   不拿去评估 (否则一轮白烧, 还误判 abandon)
                if result and str(result).strip().startswith("(……"):
                    result = None
                if not result:
                    verdict = "abandon"
                    break        # 工具失败/无结果 → 停止 (不烧评估)
                final_result = result
                tried.append(search_q)
                trail.append({"round": rnd, "method": method, "query": search_q,
                              "result": result[:150], "src": src})
                k._log_event("curiosity", question=q, keywords=search_q, method=method,
                             round=rnd, verdict="", result=result[:120])

                if rnd >= CURIOSITY_MAX_ROUNDS:
                    verdict = "satisfied" if len(final_result) > 30 else "abandon"
                    break        # 到顶了, 强制收尾

                # 评估: 这些信息够不够?
                ctx = (f"你想搞明白的问题: {q}\n\n已查到的信息:\n" + "\n".join(
                    f"第{t['round']}轮({t['src']}) 查「{t['query']}」: {t['result']}"
                    for t in trail) + "\n\n你判断: 够不够? (satisfied/continue/abandon)")
                raw2 = engine.generate(prompt_mod.curiosity_eval_system(), ctx,
                                       max_tokens=300, temperature=0.4)
                m2 = re.search(r'\{[^{}]*\}', raw2)
                if not m2:
                    verdict = "abandon"
                    break
                ev = _json.loads(m2.group(0))
                verdict = str(ev.get("verdict", "abandon")).strip().lower()
                if verdict in ("satisfied", "abandon"):
                    break
                # continue: 必须给出新角度的关键词, 且不能重复已试过的
                new_kw = str(ev.get("new_keywords", "")).strip()
                new_method = str(ev.get("new_method", "search")).strip().lower()
                if not new_kw or len(new_kw) < 3 or new_kw in tried:
                    verdict = "abandon"
                    break        # 防死循环: 没新角度就诚实收手
                search_q = new_kw
                method = new_method if new_method in ("search", "ask_ai", "bili_search", "zhihu") else "search"

            # 3. 心路日志: 完整轨迹 (她能"看到"自己查了几轮、怎么调整的)
            try:
                trail_txt = " → ".join(
                    f"第{t['round']}轮{t['src']}「{t['query']}」" for t in trail)
                note = f"{trail_txt} | 判定:{verdict}"
                kiri_mind.curiosity(q, kw, f"{note} | {final_result[:160]}")
            except Exception:
                pass
            #  数据日志: 好奇样本 (分析用)
            try:
                import data_log
                data_log.curiosity(q, final_result[:200])
            except Exception:
                pass

            # 4. 结果入长期记忆: 查到了才沉淀 (abandon/空结果不污染记忆库) — 按联想用户
            if verdict != "abandon" and final_result and len(final_result) > 30:
                summary = f"[外部] 我查了「{q}」: {final_result[:250]}"
                k.memory.encode(summary, k.state.emotion.state, user=lu)
                self.last_curiosity = time.time()
                logger = __import__("logging").getLogger("kiri")
                logger.info(f"好奇: {len(trail)}轮查「{q}」→ 判定{verdict} → 学到{len(final_result)}字")
                #  内心分享: 查到了想告诉雾弥/群里 (入队, 通道审查后外发)
                try:
                    if config_mod.SHARE_CURIOSITY_PROB >= random.random():
                        share = self._share_line(q, final_result)
                        if share:
                            k.offer_share(share, kind="curiosity")
                except Exception:
                    pass
            else:
                self.last_curiosity = time.time()
                logger = __import__("logging").getLogger("kiri")
                logger.info(f"好奇: {len(trail)}轮查「{q}」→ 判定{verdict} (没查到, 没入记忆)")
        except Exception:
            pass

    # ---- 内心分享句: 好奇结果 → 她想分享的话 (轻量生成) ----
    def _share_line(self, question, result):
        """把好奇+结果包装成她想说的话 (自然口语, 像人聊天时分享刚查到的东西)
        ★ max_tokens=400 + 断句重试: V4-flash思考占token, 150会断成半句"""
        try:
            share_sys = (
                "你是Kiri。你刚才好奇一件事, 查到了资料, 现在想分享给群里/雾弥。"
                "把这件事用一两句口语说出来, 像人聊天时随口分享: "
                "'我刚查了……原来……' '突然好奇……结果发现……'"
                "要带你的语气(傲娇/好奇/惊讶), 不要用引号, 不要提'我搜索了'这种工具词。")
            user_p = f"你好奇的问题: {question}\n查到的内容: {result[:200]}"
            raw = engine.generate(share_sys, user_p, max_tokens=400, temperature=0.8)
            share = raw.strip().split("\n")[0].strip()[:300]   # ★ 2026-08-20: 100→300, 分享话不再砍碎
            if not share:
                return None
            # ★ 断句截断检测: 半句话不分享 → 重生成一次
            if prompt_mod.looks_truncated(share):
                raw2 = engine.generate(share_sys, user_p, max_tokens=400, temperature=0.9)
                share2 = raw2.strip().split("\n")[0].strip()[:300]
                if share2 and not prompt_mod.looks_truncated(share2):
                    share = share2
                else:
                    return None   # 仍断句 → 不分享 (半句话发出去更糟)
            return share or None
        except Exception:
            return None

    # ---- 世界漫游 (行动为主: 刷B站/知乎日报/少数派/天气/搜索) ----
    def _extract_search_topic(self, thought_text):
        """念头 → 搜索关键词 (思考→行动的提炼环节)
        ★ 修复 (2026-08-19): 原实现把念头原文当前缀 → B站搜索"没结果"
        新逻辑 (按雾弥指示): 用 LLM 提炼关键词 (本质=ask_ai 的精简版);
        失败 → 引号/书名号内的专有名词 (如"虎门抽烟");
        仍找不到 → 返回空, 漫游走默认话题 (绝不拿整句/半截垃圾去搜)"""
        t = str(thought_text or "").strip()
        if not t or len(t) < 4:
            return ""
        # 主路径: LLM 提炼 1-2 个搜索关键词 (像人把想法转成搜索词)
        try:
            sys_p = ("把下面这句Kiri的内心念头，提炼出1-2个最相关的搜索关键词（空格分隔），"
                     "只留可检索的实词/专有名词，去掉口语情绪。只输出关键词：\n")
            kw = engine.generate(sys_p, t, max_tokens=50, temperature=0.3)
            kw = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9 ]", " ", kw or "").strip()
            kw = " ".join(kw.split())[:40]
            if len(kw) >= 2:
                return kw
        except Exception:
            pass
        # 兜底: 引号/书名号里的专有名词 (如"虎门抽烟"、'测试一下')
        try:
            m = re.search(r"[\"'「」『』“”《》]([^\"'「」『』“”《》]{2,8})[\"'「」『』“”《》]", t)
            if m:
                return m.group(1).strip()[:15]
        except Exception:
            pass
        # 提炼不出可检索词 → 返回空 (让漫游用默认话题, 不硬搜)
        return ""

    # ★ 念头→行动 (2026-08-21 雾弥: "内部思维流离散" 治本 — 行动从念头流里长出来):
    #   她走神时冒出"该去做了"的冲动 (高salience+行动意图词) → 启动后台真的去做
    #   不是系统定时抛任务, 是她自己的念头流里长出来的决定; 节流防频繁
    _ACTION_HINTS = ("该去", "该动手", "去试试", "去写", "去修", "去研究", "去做",
                     "动手", "试试写", "去翻", "去弄", "去逛", "该写", "该修")
    _ACTION_SALIENCE = 0.7

    def _maybe_act_on_thoughts(self, thoughts):
        """念头涌现行动: 高salience + 行动意图 → 后台真做 (复用目标推进节流)"""
        if not thoughts:
            return
        for c in thoughts:
            text = str(c.get("text", ""))
            sal = float(c.get("salience", 0) or 0)
            if sal < self._ACTION_SALIENCE:
                continue
            if not any(h in text for h in self._ACTION_HINTS):
                continue
            now = time.time()
            if now - self._last_goal_push_ts < GOAL_PUSH_COOLDOWN:
                return  # 节流: 20分钟内只触发一次
            self._last_goal_push_ts = now
            k = self.kiri
            try:
                import diagnose_agent
                mgr = diagnose_agent.get_manager()
                tid = mgr.start(k, f"去做: {text[:60]}", max_rounds=40, mode="explore")
                k._log_event("wander", source="thought_action", label="念头行动",
                             result=text[:60], task_id=tid)
                logger = __import__("logging").getLogger("kiri")
                logger.info(f"念头→行动: {text[:50]}")
            except Exception:
                pass
            return

    # ---- 调度检查 (daemon 每 tick 调用) ----
    def should_run(self):
        """间隔到了 + 雾弥沉默够久 → 才走神 (聊天时不走神, 省成本也符合"专注对话")"""
        if time.time() - self.last_cycle < REVERIE_INTERVAL_SECONDS:
            return False
        silence = (time.time() - self.kiri.state.last_interact) / 60
        return silence >= REVERIE_MIN_SILENCE_MIN


