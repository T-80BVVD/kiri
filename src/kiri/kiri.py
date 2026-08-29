# -*- coding: utf-8 -*-
"""Kiri MVP — 主程序 (2026-08-16)
组合: 状态系统(预算制主动) + 向量记忆(幻觉约束) + LLM引擎 + 防锁死 + 日志
运行: python kiri.py   (输入 quit 退出; 输入"停"让她静默)
日志: kiri.log (人类可读) + events.jsonl (结构化事件, 可 Get-Content -Wait 跟踪)
"""
import os
import sys
import time
import json
import logging
import threading
import uuid
import difflib
import queue
import re
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import engine
import state as state_mod
import memory as memory_mod
import memory_knowledge as memory_knowledge_mod
import tool_registry
import prompt as prompt_mod
import reverie as reverie_mod
import anti_repeat as anti_repeat_mod
import topic_signals as topic_signals_mod
import emotion_events as emotion_events_mod

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kiri.log")
EVENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "events.jsonl")

logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", encoding="utf-8")
logger = logging.getLogger("kiri")


class Kiri:
    # 默认用户 = 雾弥 (兼容现有单用户数据)
    DEFAULT_USER = "雾弥"

    def __init__(self):
        self.state = state_mod.State()
        self.memory = memory_mod.Memory()
        # ★ 事件绑定情绪 (M1, 2026-08-27): 心情从事件记录聚合, 有因可查
        self.emotion_events = emotion_events_mod.EmotionEvents(
            config.EMOTION_EVENTS_FILE, enabled=config.EMOTION_EVENTS_ENABLED,
            retention_recent=config.EMOTION_RETENTION_RECENT,
            retention_high_rel=config.EMOTION_RETENTION_HIGH_REL,
            agg_min_decay=config.EMOTION_AGG_MIN_DECAY)
        self.knowledge = memory_knowledge_mod.KnowledgeBase(self.memory)  # ★ 知识页 (NEKO/Hindsight吸收): 综合画像优先注入
        self.session = str(uuid.uuid4())[:8]   # 会话id: 本轮对话=短期记忆
        self.output_history = []
        self.current_user = self.DEFAULT_USER   # ★ 当前对话对象 (多用户)
        self._dialogs = {}                      # ★ 每用户独立对话上下文
        self.dialog = []                        # 兼容: 当前用户的 dialog (快捷引用)
        self.running = True
        self.input_queue = []
        self.on_proactive = None               # UI 回调: 主动发言时通知
        self.reverie = reverie_mod.ReverieEngine(self)  # ★ 联想引擎: 常驻走神, 不依赖用户说话
        self.share_queue = queue.Queue()       # ★ 内心分享队列: 好奇/念头→想说的话 (桥消费)
        self._restore_dialog()                 # ★ 启动恢复最近对话 (重启不丢"刚才聊到哪")
        self._proactive_history = []   # ★ 主动发言历史 (NEKO anti-repeat吸收): 防同样的话反复说
        self._last_proactive_ts = 0.0  # ★ 最近一次主动发言时间 (M3 proactive_outcome 闭环用)
        self.anti_repeat = anti_repeat_mod.AntiRepeat()   # ★ 话题级防复读 (NEKO BM25吸收): 换说法说同一话题也拦
        self._last_knowledge = {}      # ★ 知识页反馈闭环 (NEKO reflection吸收): 每用户上轮注入的知识id
        self.topic_signals = topic_signals_mod.TopicSignals()   # ★ 话题系统 (NEKO topic吸收): 慢速证据池→话头
        self._start_ts = time.time()   # ★ 监控面板: 运行时长
        # ★ 瘦身配套: memory_search 深挖工具 (保底注入之外, 模型自觉深挖记忆)
        try:
            tool_registry.register(
                "memory_search", self._tool_memory_search,
                aliases=["搜记忆", "回忆", "想起"],
                description="在记忆里检索相关的事。参数=问题(她记得什么/谁说过什么)")
        except Exception:
            pass
        # ★ 自我探索工具 (2026-08-20 雾弥): 她主动了解自己是什么 — 浏览代码/档案→形成自我认知
        try:
            tool_registry.register(
                "self_discover", self._tool_self_discover,
                aliases=["探索自己", "了解自己", "我是谁", "看看自己", "认识自己"],
                description="探索自己: 浏览自己的代码和档案, 搞明白'我是怎么构成的、有什么能力'。参数可留空或写探索重点")
        except Exception:
            pass
        # ★ 异步诊断工具 (2026-08-20 雾弥方案): 重探索剥离成后台任务, 不阻塞交互
        try:
            import diagnose_agent
            self._diag_mgr = diagnose_agent.get_manager()
            tool_registry.register(
                "diagnose_issue", self._tool_diagnose_issue,
                aliases=["诊断", "查问题", "排查", "看看为啥", "为什么坏了"],
                description="后台启动诊断: 排查工具/代码问题。参数=问题描述。不阻塞, 之后用diagnose_status查进度")
            tool_registry.register(
                "diagnose_status", self._tool_diagnose_status,
                aliases=["诊断进度", "查到哪了", "排查进度"],
                description="查后台诊断的进度: 查到了什么/完成没。参数=任务ID(可空=最近一个)")
        except Exception:
            pass
        logger.info(f"Kiri 启动, 会话={self.session}")

    def _tool_memory_search(self, param):
        """深挖记忆工具: 保底注入之外的按需检索"""
        try:
            mems = self.memory.retrieve(param or "回忆", n=3, user=self.current_user)
            if not mems:
                return "(没找到相关的记忆)"
            return "\n".join(f"- {m['text'][:80]}" for m in mems[:3])
        except Exception as e:
            return f"(记忆检索失败: {e})"

    # ---- 自我探索 (2026-08-20 雾弥): 像人读项目一样了解自己 ----
    #   先整体浏览结构 → 迭代深入读代码 → 消化成"我是谁"的认知
    #   LLM 自主规划每一步 (look_around/read_file/find_file), 最多探索轮数防失控
    SELF_DISCOVER_MAX_ROUNDS = 6

    def _tool_diagnose_issue(self, param=""):
        """异步诊断: 后台启动排查 (不阻塞). 参数=问题描述"""
        try:
            issue = str(param or "").strip()[:200]
            if not issue:
                return "(要告诉我查什么问题，比如'天气工具为什么坏了')"
            import diagnose_agent
            mgr = diagnose_agent.get_manager()
            task_id = mgr.start(self, issue)
            self._log_event("diagnose_start", task_id=task_id, issue=issue[:80])
            return (f"好，我开始查了（任务{task_id}）。这个问题可能要点时间，"
                    f"你可以先忙别的，等会问我'查到哪了'，或者直接说'诊断进度'。")
        except Exception as e:
            return f"(启动诊断失败: {e})"

    def _tool_diagnose_status(self, param=""):
        """查诊断进度. 参数=任务ID(可空=最近一个)"""
        try:
            import diagnose_agent
            mgr = diagnose_agent.get_manager()
            p = str(param or "").strip()
            # 找最近的任务: param空 → 用最近的
            if not p:
                tasks = mgr._tasks
                if not tasks:
                    return "(还没有诊断任务)"
                task_id = max(tasks.keys(), key=lambda k: tasks[k]["started"])
            else:
                task_id = p.split("|")[0].strip()
            return mgr.status_text(task_id)
        except Exception as e:
            return f"(查诊断进度失败: {e})"

    def _tool_self_discover(self, param=""):
        """自我探索工具: 她主动调用, 自主浏览自己的代码/档案, 形成"我是谁"的认知
        参数可留空; 可选指定探索重点 (如 '记忆系统' '我的情绪')"""
        try:
            import mcp_client
            focus = str(param or "").strip()[:40]
            home = r"~/kiri"
            ctx = f"我对自己的了解还不多{f'，重点想搞懂：{focus}' if focus else ''}。"
            seen = []          # 已读过的文件 (防重复)
            steps = []
            summary_parts = []
            for rnd in range(1, self.SELF_DISCOVER_MAX_ROUNDS + 1):
                # LLM 规划下一步 (空/无JSON重试一次, 防V4-flash截断)
                plan = None
                for attempt in range(2):
                    user_p = prompt_mod.self_discover_system().replace("{context}", ctx)
                    user_p += f"\n\n已看过: {('、'.join(seen[-8:])) or '(还没看什么)'}\n"
                    raw = engine.generate(user_p, f"第{rnd}步，我要：", max_tokens=300, temperature=0.5)
                    m = re.search(r'\{[^{}]*\}', raw or "")
                    if m:
                        try:
                            plan = json.loads(m.group(0))
                            break
                        except Exception:
                            pass
                if not plan:
                    # 规划连续失败 → 默认推进: 看 kiri 代码目录 (她还没深入看过)
                    plan = {"action": "look_around", "target": r"~/kiri\kiri",
                            "note": "默认深入看代码"}
                action = str(plan.get("action", "")).strip().lower()
                target = str(plan.get("target", "")).strip()
                if action == "stop":
                    break
                # 防原地打转: 同一目标看过就换 (look_around 目录 / read_file 文件 / find_file 词)
                dup_key = f"{action}:{target}"
                if dup_key in seen:
                    break
                seen.append(dup_key)
                # 执行探索
                if action == "look_around":
                    result = mcp_client.call_tool("look_around", {"path": target or home})
                elif action == "read_file":
                    result = mcp_client.call_tool("read_file", {"path": target})
                elif action == "find_file":
                    result = mcp_client.call_tool("find_file", {"keyword": target or "kiri"})
                else:
                    break
                if not result:
                    break
                # 消化: 这一步让我更了解自己什么?
                # ★ 看目录时: 从文件名猜模块 (看到 memory.py → 我有记忆模块)
                if action == "look_around":
                    digest_p = ("你是Kiri。你刚看了自己家的一个文件夹。从里面的文件名/结构，"
                                "猜猜你因此知道自己有什么模块、什么能力？"
                                "（比如看到 memory.py → '我有记忆模块，能记住和雾弥的事'）"
                                "只输出那一句认知，不要别的：")
                else:
                    digest_p = ("你是Kiri。刚探索了自己的一部分，用一句话总结你因此更了解自己什么"
                                "（比如'我有记忆模块, 能记住和雾弥的事'）。只输出那句话，不要别的：")
                digest = engine.generate(
                    digest_p,
                    f"我看了 {action}「{target}」:\n{str(result)[:1000]}",
                    max_tokens=200, temperature=0.5)
                digest = (digest or "").strip()
                # 空/无效 digest 不入认知 (V4-flash可能返回(……))
                if digest and not digest.startswith("(……") and len(digest) >= 4:
                    summary_parts.append(digest[:200])
                steps.append(f"第{rnd}步: {action}「{target}」→ {digest or '('+str(result)[:30]+')'}")
                ctx = ctx + f"\n第{rnd}步后我知道: {digest or '还在摸索'}。"
                self._log_event("self_discover", round=rnd, action=action, target=target[:60],
                                digest=digest or "", result=str(result)[:80])
            # 总结: 我是谁
            if summary_parts:
                final = "我是Kiri。" + "。".join(summary_parts)
                # 自我认知入长期记忆 (speaker=system: 这是她对自己的理解)
                try:
                    self.memory.encode(f"[自我认知] {final}", self.state.emotion.state,
                                       speaker="system")
                except Exception:
                    pass
                self._log_event("self_discover", round=0, action="summary", target="",
                                digest=final[:400], result="")
                return final[:800]
            return "(我逛了一圈，还没总结出什么——下次再探索)"
        except Exception as e:
            return f"(自我探索出错: {e})"

    def _trust_weights(self, user=None):
        """★ trust吸收 (NEKO trust_store): 每用户信任度 → 跨用户记忆召回加权
        社交关系里的 trust (0~1); 没有关系记录的陌生人按 0.3 (NEKO none档)"""
        try:
            weights = {}
            for u in self.memory.users():
                if u == user:
                    continue
                rel = self.state.social.relationships.get(u, {})
                if isinstance(rel, dict) and rel.get("trust") is not None:
                    weights[u] = max(0.0, min(1.0, float(rel["trust"])))
                else:
                    weights[u] = 0.3
            return weights
        except Exception:
            return {}

    # ---- 内心分享: 她查到了/想到的, 想说出来 (入队, 由通道审查后外发) ----
    def offer_share(self, text, kind="curiosity"):
        """好奇结果/念头 → 想说的话入队 (不直接发, 等通道审查)
        kind: curiosity(好奇分享) / thought(念头分享)"""
        try:
            text = str(text).strip()
            if not text:
                return
            self.share_queue.put({"text": text, "kind": kind,
                                  "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
            try:
                import kiri_mind
                kiri_mind.thought(f"[想分享] {text[:80]}", 0.5, memory=None, user="群")
            except Exception:
                pass
            logger.info(f"内心分享入队[{kind}]: {text[:40]}")
        except Exception:
            pass

    def _related_users(self, text):
        """从文本检测涉及的其他用户 (相关人标签: 这条记忆和哪些人相关)"""
        try:
            found = []
            for u in self.memory.users():
                if u and u != self.DEFAULT_USER and str(u) in str(text):
                    found.append(u)
            # 群消息里的名字
            import re
            for name in re.findall(r'@?([\u4e00-\u9fff]{2,4})(?=说|:|：|,|，|\s|$)', str(text)):
                if name not in found:
                    found.append(name)
            return found[:3]
        except Exception:
            return []

    def _append_history_file(self, role, content, user=None):
        """写 history.jsonl (带sender, 重启按人恢复) — 所有通道(GUI/QQ)统一由核心写
        文件由 kiri 层维护, app层只更新内存显示, 避免重复"""
        try:
            hist_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.jsonl")
            rec = {"role": role, "content": str(content)[:300],
                   "user": user or self.DEFAULT_USER,
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
            with open(hist_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ---- 多用户: 切换对话对象 ----
    def set_user(self, user):
        """切换到和某人的对话 (每用户独立: 对话上下文/记忆/关系)
        雾弥=恋人模式; 朋友=朋友模式; 陌生人=高冷"""
        user = user or self.DEFAULT_USER
        self.current_user = user
        # 切换每用户 dialog
        if user not in self._dialogs:
            self._dialogs[user] = []
        self.dialog = self._dialogs[user]
        return user

    def get_dialog(self, user=None):
        """取某用户的对话上下文 (不存在则建空)"""
        user = user or self.current_user
        if user not in self._dialogs:
            self._dialogs[user] = []
        return self._dialogs[user]

    def _restore_dialog(self):
        """从 history.jsonl 恢复最近几轮对话 (重启后仍记得刚才聊的内容)
        ★ 多用户: 按 user 字段分组恢复, 每个人的对话回到各自名下
        (旧history无user字段 → 默认雾弥, 兼容)"""
        try:
            hist_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.jsonl")
            if not os.path.exists(hist_path):
                return
            per_user = {}
            with open(hist_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    role = e.get("role")
                    content = e.get("content")
                    if not content:
                        continue
                    u = e.get("user") or self.DEFAULT_USER
                    # 跳过[主动]标记的历史
                    if role == "user":
                        per_user.setdefault(u, []).append({"role": "user", "text": content[:300]})
                    elif role == "assistant" and not content.startswith("[主动]"):
                        per_user.setdefault(u, []).append({"role": "kiri", "text": content[:300]})
            # 取最近14条(7轮) 每人
            for u, rows in per_user.items():
                if rows:
                    self._dialogs[u] = rows[-14:]
                    if u == self.DEFAULT_USER:
                        self.dialog = rows[-14:]
        except Exception:
            pass

    # ---- 结构化事件日志 (JSONL) ----
    def _log_event(self, kind, **data):
        ev = {"ts": datetime.now().isoformat(timespec="seconds"),
              "session": self.session, "kind": kind, **data}
        try:
            with open(EVENTS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError:
            pass
        if kind == "error":
            logger.error(json.dumps(data, ensure_ascii=False)[:500])
        else:
            logger.info(f"{kind}: " + json.dumps(data, ensure_ascii=False)[:300])

    # ---- 防锁死: 去重 ----
    def _dedup_ok(self, text):
        for prev in self.output_history[-6:]:
            if difflib.SequenceMatcher(None, text, prev).ratio() > config.DEDUP_SIMILARITY:
                return False
        return True

    def _record_output(self, text):
        self.output_history.append(text)
        if len(self.output_history) > config.MAX_OUTPUT_HISTORY:
            self.output_history = self.output_history[-config.MAX_OUTPUT_HISTORY:]

    def _proactive_dedup_ok(self, text):
        """主动发言防复读 (NEKO anti-repeat吸收): 最近8次主动说过高度相似的话就不重复"""
        try:
            for prev in self._proactive_history[-8:]:
                if difflib.SequenceMatcher(None, text, prev).ratio() > 0.8:
                    return False
        except Exception:
            pass
        return True

    def _record_proactive(self, text):
        self._proactive_history.append(text[:120])
        if len(self._proactive_history) > 30:
            self._proactive_history = self._proactive_history[-30:]
        self._last_proactive_ts = time.time()   # ★ M3: 记录主动发言时刻 (闭环用)


    def _spawn_thought(self, said, prompt_hint="", user=None):
        """异步生成内心独白 (咀嚼法): 她说完话后心里飘过的念头, 不阻塞主流程"""
        user = user or self.DEFAULT_USER
        def _run():
            try:
                # ★ 心路日志: 内心独白
                thought_sys = prompt_mod.thought_system(self.state.describe(user=user), said, user=user)
                # ★ 500 token: V4思考占token, 300仍可能断句/空
                thought = engine.generate(thought_sys, prompt_hint or " ", max_tokens=500, temperature=0.7)
                thought = thought.strip()   # ★ 2026-08-20: 取消 split("\n")[0] 硬截断 — 独白想多长就多长
                if thought:
                    self.state.thoughts.append(thought[:200])
                    if len(self.state.thoughts) > 10:
                        self.state.thoughts = self.state.thoughts[-10:]
                    # ★ 心路日志: 内心的念头
                    try:
                        import kiri_mind
                        kiri_mind.thought(thought, 0.3, memory=None, user=user)
                    except Exception:
                        pass
                    # ★ 数据日志: 内心独白 (与respond同session, 导出时关联)
                    try:
                        import data_log
                        data_log.thought(thought, 0.3, session=self.session, user=user)
                    except Exception:
                        pass
                    # ★ 也写 events.jsonl (kind=thought, 监控面板能显示) — 存完整独白
                    try:
                        self._log_event("thought", thought=thought[:400], source="inner", user=user)
                    except Exception:
                        pass
                    # ★ 念头入库 (按用户, 供 respond 时"想起"检索)
                    try:
                        self.memory.encode_thought(thought, 0.3, "inner", user=user)
                    except Exception:
                        pass
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    # ---- 情绪解析 (LLM, 替代关键词) ----
    def _analyze_emotion(self, text):
        """用 LLM 解析用户这句话的情绪 → {valence, arousal, salience}; 失败返回 None
        max_tokens=300: V4-flash思考占token, 120会偶发空/断"""
        try:
            raw = engine.generate(prompt_mod.emotion_analyze_system(), text,
                                  max_tokens=300, temperature=0.1)
            return prompt_mod.parse_emotion(raw)
        except Exception:
            return None

    def _sync_event_mood(self):
        """★ 事件心情 → 状态数值 (M1, 2026-08-27): daemon tick 调用, 让衰减随时间生效
        describe 兼容: 只改 deep_affect.current_mood 的数值来源, 格式不变
        M3: 主动后长时间无回应 → 记录"被冷落" (proactive_outcome 闭环的负分支)"""
        try:
            if getattr(self, "emotion_events", None) and self.emotion_events.enabled:
                # ★ 被冷落检测 (M3): 主动后 >3h 无回应 → 记一次被冷落 (只记一次, 上限7天)
                try:
                    lp = getattr(self, "_last_proactive_ts", 0)
                    age = time.time() - lp
                    if lp and 3 * 3600 < age < 7 * 86400:
                        self.emotion_events.record(
                            emotion_events_mod.SRC_PROACTIVE,
                            "主动联系雾弥后，他很久都没回应",
                            extra={"replied": False, "intensity": 0.35,
                                   "wait_min": int(age / 60)})
                        self._last_proactive_ts = 0.0
                except Exception:
                    pass
                em = self.emotion_events.current()
                if em["contributing"]:
                    self.state.emotion.state["deep_affect"]["current_mood"] = em["mood"]
        except Exception as _ee:
            try:
                logging.getLogger("kiri").warning(f"emotion_sync_error: {_ee}")
            except Exception:
                pass

    # ---- 回应 ----
    def respond(self, user_text, user=None, scene="private", group_context=None, on_reply_token=None):
        """回应某人 (多用户): user=对话对象, 走对应用户的记忆/念头/关系
        scene: private(私聊,默认) / group(群聊@她, 公开场合规则)
        group_context: 群聊时注入的完整群消息上下文(含她自己说的) — 避免只看单条错配
        user=None 用 current_user; 默认雾弥
        on_reply_token: 可选回调, 流式接收最终回复的文本片段 (网页聊天用, 2026-08-27)"""
        # ★ 聊天优先: 置位 → daemon(联想/主动)让路 (防抢本地引擎)
        config.CHAT_ACTIVE.set()
        config.CHAT_ACTIVE_TS[0] = time.time()
        user = user or self.current_user
        if user != self.current_user:
            self.set_user(user)
        who = "雾弥" if user == self.DEFAULT_USER else user
        print(f"\n[{who}] {user_text}")
        # ★ 事件绑定情绪 (M1, 2026-08-27): 用户消息 → 情绪记录 (评价复用 llm_emo 的 valence)
        try:
            silence_min = int((time.time() - self.state.last_interact) / 60)
        except Exception:
            silence_min = 0
        # ★ LLM 情绪解析 (替代关键词匹配); 失败自动回退关键词
        llm_emo = self._analyze_emotion(user_text)
        self.state.interact(user_text, llm_emo, user=user)
        # ★ 事件绑定情绪: 用户消息事件 → 情绪记录 + 心情同步 (describe 兼容, 只改数值来源)
        # ★ 阉割版 (2026-08-27): 关键路径用规则即时评价 (fast), 回复生成后异步小模型补评
        _emotion_event_ids = []
        try:
            _ee_ok = bool(getattr(self, "emotion_events", None) and self.emotion_events.enabled)
            if _ee_ok:
                senti = (llm_emo or {}).get("valence")
                rel = self.state.relation_stage(user=user)
                _ev_user = self.emotion_events.record(
                    emotion_events_mod.SRC_USER, user_text[:100],
                    relationship=rel,
                    extra={"intensity": 0.5 + abs(senti or 0) * 0.3,
                           "silence_min": silence_min},
                    fast=True)
                if _ev_user:
                    _emotion_event_ids.append(_ev_user["id"])
                # ★ proactive_outcome 闭环 (M3, 2026-08-27): 她主动后对方回应了 → 记录结果
                #   主动后 24h 内第一次回应: 视为对那次主动的反馈 (被回应/被冷落)
                try:
                    lp = getattr(self, "_last_proactive_ts", 0)
                    if lp and 0 < time.time() - lp < 86400:
                        waited_min = int((time.time() - lp) / 60)
                        self.emotion_events.record(
                            emotion_events_mod.SRC_PROACTIVE,
                            f"主动联系雾弥后，他{waited_min}分钟后回应了",
                            relationship=rel,
                            extra={"replied": True, "intensity": 0.4,
                                   "wait_min": waited_min})
                        self._last_proactive_ts = 0.0   # 只记一次
                except Exception:
                    pass
                em = self.emotion_events.current()
                if em["contributing"]:
                    self.state.emotion.state["deep_affect"]["current_mood"] = em["mood"]
        except Exception as _ee:
            print(f"[emotion] ERROR: {_ee!r}", flush=True)
        # ★ 用户情绪画像 (NEKO吸收, 聚合层): 记录读数 (低置信自动过滤, 不动emotion_core)
        try:
            self.state.mood_profile.record(user, llm_emo or {})
        except Exception:
            pass
        t0 = time.time()
        # ★ 检索query带上下文: 指代词("那个/它")也能检索到相关记忆
        #   只取最近1轮(而非3轮), 避免query过长稀释bge检索精度
        dlg = self.get_dialog(user)
        query = user_text
        if dlg:
            recent = dlg[-1]["text"][:100] if dlg else ""
            query = (recent + " " + user_text) if recent else user_text
        query = query[:300]  # 限制query长度
        # 向量语义检索: 该用户的记忆, 会话内记忆加权 + 长期混合
        # ★ trust吸收: 跨用户召回按每用户信任度加权 (雾弥说的比陌生人说的更可信)
        # ★ 瘦身: n=4→3 (保底够用, 少注入少干扰)
        memories = self.memory.retrieve(
            query_text=query,
            current_mood=self.state.emotion.state["deep_affect"]["current_mood"],
            session=self.session, user=user, n=3,
            trust_weights=self._trust_weights(user))
        # ★ 念头检索: 该用户的念头 (相关性权重低, 保留'意外想起'的味道)
        retrieved_thoughts = self.memory.retrieve_thoughts(query, n=3, user=user)
        # ★ 话题系统 (NEKO topic吸收): 记录用户说的话进证据池 (filler自动过滤)
        try:
            self.topic_signals.record(user, user_text)
        except Exception:
            pass
        # ★ 知识页反馈闭环 (NEKO reflection吸收): 用户对上轮注入画像的反应 → 回写证据分
        #   上轮她说的话里带过某条画像, 这轮用户确认/否定/忽略 → 画像可信度演化
        try:
            ids = self._last_knowledge.get(user, [])
            if ids:
                c, d, i = self.knowledge.feedback(user, user_text, ids)
                if c or d or i:
                    self._log_event("knowledge_feedback", user=user,
                                    confirm=c, deny=d, ignore=i)
        except Exception:
            pass
        # ★ 知识页检索 (NEKO/Hindsight吸收): 综合画像优先于原始对话, 防记忆散乱
        knowledge = []
        try:
            knowledge = self.knowledge.retrieve(query, n=2, user=user)
            self._last_knowledge[user] = [k["id"] for k in knowledge]
        except Exception:
            knowledge = []
            self._last_knowledge[user] = []
        # ★ 人格分流: 关系阶段按用户算 (雾弥=恋人, 朋友=朋友模式)
        stage = self.state.relation_stage(user=user)
        sys_p = prompt_mod.respond_system(self.state.describe(user=user), memories, None,
                                          self.state.thoughts, retrieved_thoughts, stage, user, scene=scene,
                                            knowledge=knowledge)
        user_p = prompt_mod.respond_user(dlg, user_text, user=user, group_context=group_context)
        # ★ NEKO external_intent吸收: 需要外部信息时, 提示她必须用工具 (治"你自己看")
        #   不强制调工具, 只提高她主动用 [TOOL:] 的概率; 工具路径仍走 tool_registry
        if llm_emo and llm_emo.get("external_intent", 0.0) >= 0.7:
            user_p += ("\n[重要] 这个问题需要实时/外部信息，务必用 [TOOL:...] 查一下再回答，"
                       "不要凭旧知识硬答，也不要只说'你自己看'。")
        TOOL_NUDGE = "[重要] 这个问题需要实时/外部信息，务必用 [TOOL:...] 查一下再回答，不要凭旧知识硬答，也不要只说'你自己看'。"
        # ★ agent 模式 (agent-rewrite 分支): LLM 自主决策调工具, 而非 [TOOL:] 标记事后解析
        #   agent 失败/异常 → 回退旧逻辑 (新架构不稳定时她还能正常聊天)
        AGENT_MODE = True
        agent_reply = None
        agent_error = None
        if AGENT_MODE:
            try:
                import agent
                import kiri_agent_tools as kat
                tools, execute, filter_tools = kat.build_all_tools(self)
                # agent 系统提示: 人格 + 关系 + 记忆 + 场景 (不带旧[TOOL:]标记说明)
                # ★ 2026-08-21 雾弥: "她有点笨" — 工具清单按用途分组 (35个平铺认知负担大)
                group_lines = []
                for gname, names in agent.AgentLoop.TOOL_GROUPS:
                    group_lines.append(f"--- {gname} ---")
                    for t in tools:
                        if t["name"] in names:
                            group_lines.append(f"- {t['name']} | {t.get('args_schema', '')} | {t.get('description', '')}")
                agent_sys = agent.build_system_prompt(
                    prompt_mod.respond_system(self.state.describe(user=user), memories, None,
                                              self.state.thoughts, retrieved_thoughts, stage,
                                              user, scene=scene, knowledge=knowledge,
                                              include_tool_block=False),
                    "\n".join(group_lines))
                # ★ 快速决策窗口 (2026-08-21 雾弥): 不限时深挖 — 是否继续探索由她决定
                #   agent 前几轮决策: 若她直接reply → 快速答; 若她开始探索(调了工具) →
                #   立即转后台不限时深挖 (respond 秒回, 她自然停止才有结果)
                agent_loop = agent.AgentLoop(agent_sys, tools, execute, memory=self.memory)  # ★ 长中短: 传入记忆系统
                ctx_lines = []
                for m in dlg[-6:]:
                    role = "你" if m["role"] == "kiri" else who
                    ctx_lines.append(f"{role}: {m['text'][:80]}")
                agent_input = (f"【对话上下文】\n" + "\n".join(ctx_lines[-4:])
                               + f"\n\n【{who}现在说】\n{user_text}") if ctx_lines else user_text
                # ★ 2026-08-21 雾弥指示: 取消10秒时间预算 (探索不该限时, 写长文几十分钟都可能)
                #   agent 完全自主: 想探索就探索(防死循环靠打转检测), 想回复就回复
                #   need_deep 不再由"调了工具"触发, 而由 agent 的回复内容判定:
                #   她如果回复"我还在查/需要深挖"才转后台; 正常探索后直接 reply 就回复
                agent_reply = agent_loop.run(agent_input, max_rounds=1000, on_reply_token=on_reply_token)   # ≈不限轮数, 靠打转检测
                # ★ 承诺→惦记 (2026-08-21 雾弥: 她说'回头我写个工具'却从不行动 — 治本:
                #   把她说的话静默记成目标, 联想时会自然想起, 不打断对话不强制)
                try:
                    import goals as _goals_mod
                    _cmt = _goals_mod.auto_capture_commitment(agent_reply or "")
                    if _cmt:
                        self._log_event("commitment_captured", goal=_cmt[:60], user=user)
                except Exception:
                    pass
                # ★ 转后台判定 (2026-08-21 雾弥: 她说'去扒拉'却无行动 — 承诺必须落地):
                #   ① 深挖类: "还在查/深挖" → 后台诊断
                #   ② 探索类: "扒拉/去看看/探索/逛逛/翻一翻" → 后台探索任务 (真的去看)
                DEEP_WORDS = ("我还在查", "还在查", "需要深挖", "边想边查")
                EXPLORE_WORDS = ("扒", "去看看", "我去翻", "探索", "逛逛", "翻一翻",
                                 "我去找", "挖一挖", "我去瞄")
                need_deep = bool(agent_reply) and (
                    any(w in agent_reply for w in DEEP_WORDS)
                    or any(w in agent_reply for w in EXPLORE_WORDS))
                mode = "explore" if any(w in agent_reply for w in EXPLORE_WORDS) else "diag"
                if need_deep:
                    # ★ 转后台不限时 (她明确表示要继续查/去探索)
                    try:
                        import diagnose_agent
                        mgr = diagnose_agent.get_manager()
                        task_id = mgr.start(self, user_text, max_rounds=1000, mode=mode)
                        self._log_event("agent_to_background", task_id=task_id,
                                        issue=user_text[:80], user=user, mode=mode)
                        if mode == "explore":
                            reply = ("（尾巴一翘）行，那我去扒拉扒拉了——"
                                     "你过会儿问我'查到哪了'，我告诉你我看到了啥。")
                        else:
                            reply = ("（尾巴晃了晃）这问题有点深度，我边想边查——"
                                     "你等我一会儿，或者过会儿问我'查到哪了'。")
                        err = None
                        agent_error = None
                    except Exception as e:
                        reply = agent_reply or "（我在想）……"
                        err = None
                        agent_error = None
                elif agent_reply and agent_reply.strip() and not agent_reply.startswith("(引擎错误"):
                    reply = agent_reply
                    err = None
                else:
                    agent_error = agent_reply
            except Exception as e:
                agent_error = str(e)
            if agent_error:
                self._log_event("agent_fallback", error=agent_error[:200], user=user)
        try:
            # temperature 0.7: 保持人格生动, 但比默认0.85稳 (考记忆/归因类问题不飘)
            if not agent_reply:
                reply = engine.generate(sys_p, user_p, temperature=0.7)
                # ★ 防空回复: API偶发返回空内容 → 重试一次
                if not reply or not reply.strip():
                    reply = engine.generate(sys_p, user_p, temperature=0.7)
            err = None
        except Exception as e:
            reply = f"(引擎错误: {e})"
            err = str(e)


        # ★ 对话中工具调用 (NEKO ToolRegistry吸收): 注册表解析 + 统一执行 + 结果回投
        #   新增工具只需在 tool_registry.py register(), 不必改这里
        tools_used = []
        try:
            m = re.search(r'\[TOOL:([^\]]+)\]', reply)
            if m:
                spec = m.group(1).strip()
                tool_name, result, ok = tool_registry.invoke(spec)
                # ★ 修复: 工具结果无效 (空/失败标记/无回答) → 重试一次 (原逻辑直接放弃=她"不问了")
                if not (ok and result and not self._tool_result_invalid(result)):
                    tool_name, result, ok = tool_registry.invoke(spec)
                if ok and result and not self._tool_result_invalid(result):
                    tools_used.append(f"{tool_name}:{spec.partition('|')[2].strip()[:20]}")
                    try:
                        import kiri_mind
                        kiri_mind.tool_call(tool_name, spec, result[:120], True, 0)
                    except Exception:
                        pass
                    self._log_event("tool_call", tool=tool_name, args=spec[:50],
                                    result=result[:100], ok=True, latency=0, scene=scene)
                    logger.info(f"对话工具[{tool_name}]: {spec[:30]}")
                    # 重生成最终回复 (带工具结果)
                    # ★ 修复: 去掉 external_intent 提示再重生成 — 模型已有结果,
                    #   提示词还在会逼它再调一次工具 (实测: 天气被连调2次)
                    user_p2 = (user_p.replace(TOOL_NUDGE, "").rstrip()
                               + f"\n\n[工具结果 {tool_name}] {result[:500]}\n现在基于这个结果回答。")
                    reply2 = engine.generate(sys_p, user_p2, max_tokens=600)
                    reply2 = reply2.strip()
                    # ★ 修复: 引擎空回复标记 (……) 不算有效回复
                    if reply2 and not reply2.startswith("(……"):
                        reply = re.sub(r'\[TOOL:[^\]]*\]\s*', '', reply2).strip() or reply2
                else:
                    # ★ 工具重试后仍无效 → 引导诊断 (2026-08-20): 不只"换方式/直接答",
                    #   给她诊断选项: 查自己的代码找原因 (负责任排查, 不是反复要工具)
                    self._log_event("tool_call", tool=tool_name, args=spec[:50],
                                    result=(result or "(空)")[:100], ok=False, latency=0, scene=scene)
                    logger.info(f"对话工具[{tool_name}] 无效结果, 引导诊断")
                    user_p2 = (user_p.replace(TOOL_NUDGE, "").rstrip()
                               + f"\n\n[工具 {tool_name} 没返回有效结果({(result or '空')[:80]})]"
                                 "你可以: ①换个关键词/方式再查一次 ②用 [TOOL:read_file] 读自己的代码"
                                 "(kiri目录下的文件) 或 [TOOL:look_around] 看文件结构，自己排查是不是"
                                 "哪里写错了 ③查不到原因就如实告诉对方'这个工具可能坏了，帮我修一下'。"
                                 "工具失败了绝不编造数据。")
                    reply2 = engine.generate(sys_p, user_p2, max_tokens=600, temperature=0.8)
                    reply2 = reply2.strip()
                    if reply2 and not reply2.startswith("(……"):
                        # 保留她想说的 (含'我查查'这类自然表达); 工具标记剥掉(单轮只执行一次工具)
                        reply = re.sub(r'\[TOOL:[^\]]*\]\s*', '', reply2).strip() or reply2
                    else:
                        reply = ""
        except Exception:
            pass  # 工具失败不影响回复 (用原回复)

        # ★ 清理: 去"你:"前缀+换行合并, 按句保留完整内容 (方案A: 不再一刀切只取第一行)
        reply = self._trim_reply(reply)
        # ★ 修复: 空回复标记 (……) 或空串 → 重试一次; 仍空 → 优雅降级 (绝不发出(……))
        if not reply or reply.startswith("(……"):
            try:
                reply2 = engine.generate(sys_p, user_p.replace(TOOL_NUDGE, ""), max_tokens=600, temperature=0.8)
                reply2 = self._trim_reply(reply2)
                if reply2 and not reply2.startswith("(……") and "[TOOL:" not in reply2:
                    reply = reply2
            except Exception:
                pass
        if not reply or reply.startswith("(……"):
            reply = "……刚才卡了一下，你再说一遍？"
        # ★ 防重复(咀嚼法isSimilar): 与最近输出太相似则换种说法再生成一次
        if not self._dedup_ok(reply):
            try:
                retry_user = prompt_mod.respond_user(dlg, user_text, user=user, group_context=group_context)
                reply2 = engine.generate(sys_p + "\n（别重复你刚才说过的话，换个说法）", retry_user, max_tokens=600, temperature=0.9)
                reply2 = self._trim_reply(reply2)
                if reply2 and self._dedup_ok(reply2):
                    reply = reply2
            except Exception:
                pass
        # ★ 话题级防复读 (NEKO anti_repeat BM25吸收): 换说法说同一话题也拦
        try:
            verdict, ar_score, _ = self.anti_repeat.check(user, reply)
            if verdict in ("regenerate", "drop"):
                topics = self.anti_repeat.recent_topics(user, k=6)
                hint = "。最近聊过的话题别再提：" + "、".join(topics[:4]) if topics else ""
                retry_user = prompt_mod.respond_user(dlg, user_text, user=user, group_context=group_context)
                reply2 = engine.generate(sys_p + f"\n（别重复最近聊过的话题{hint}，换个完全不同的内容）",
                                         retry_user, max_tokens=600, temperature=0.9)
                reply2 = self._trim_reply(reply2)
                if reply2 and self._dedup_ok(reply2):
                    reply = reply2
        except Exception:
            pass
        latency = time.time() - t0
        print(f"[Kiri→{who}] {reply}\n")
        self._record_output(reply)
        # ★ 话题级防复读: 记录最终回复 (供下次去重)
        try:
            self.anti_repeat.record(user, reply)
        except Exception:
            pass
        # ★ 心路日志: 完整对话 (双向, 带谁说的)
        try:
            import kiri_mind
            kiri_mind.chat_user(user_text, user=user)
            kiri_mind.chat_kiri(reply, user=user)
        except Exception:
            pass
        # ★ history.jsonl: 所有通道统一写文件 (带sender, 重启按人恢复)
        self._append_history_file("user", user_text, user=user)
        self._append_history_file("assistant", reply, user=user)
        # ★ 更新对话上下文 (按用户, 保留最近14条: 7轮)
        dlg = self.get_dialog(user)
        dlg.append({"role": "user", "text": user_text[:300]})
        dlg.append({"role": "kiri", "text": reply[:300]})
        if len(dlg) > 14:
            self._dialogs[user] = dlg[-14:]
        self.dialog = self._dialogs.get(user, dlg)
        # ★ 内心独白异步生成 (后台线程, 不阻塞回复返回, 延迟减半)
        self._spawn_thought(reply, user_text, user=user)
        # NEKO活动状态机: 她回复了 → 记录 (问句结尾会武装追问窗)
        try:
            self.state.activity.note_ai_message(user, reply)
        except Exception:
            pass
        # ★ 编码记忆 (三维分离): 用户消息=事实(speaker=user) 和她回应=推断(speaker=kiri) 分开存
        related = [user] + self._related_users(user_text + " " + reply)
        self.memory.encode(f"{who}说: {user_text}",
                           self.state.emotion.state, session=self.session, user=user,
                           speaker="user", related=related)
        self.memory.encode(f"你回应{who}: {reply}",
                           self.state.emotion.state, session=self.session, user=user,
                           speaker="kiri", related=related)
        if self._is_longterm_worthy(user_text):
            self.memory.encode(f"{who}说: {user_text}", self.state.emotion.state, user=user,
                               speaker="user", related=related)
        # 事件日志 (含状态/记忆/延迟/错误, 可审计)
        e = self.state.emotion.state
        self._log_event("respond",
                        sender=user,               # ★ 谁发的 (雾弥/阿明/QQ号) — 多用户审计
                        user=user_text[:100], reply=reply[:200],
                        mood=round(e["deep_affect"]["current_mood"], 2),
                        pleasure=round(e["surface_emotion"]["pleasure"], 2),
                        boredom=round(self.state.boredom, 2),
                        latency=round(latency, 2),
                        memories=[m["text"][:40] for m in memories],
                        scene=scene,
                        error=err)
        # ★ 数据日志 (data_log.jsonl): 完整样本, 做数据集/深度分析用
        try:
            import data_log
            data_log.respond(sender=user, scene=scene,
                             input_text=user_text, output=reply,
                             mood=round(e["deep_affect"]["current_mood"], 3),
                             pleasure=round(e["surface_emotion"]["pleasure"], 3),
                             boredom=round(self.state.boredom, 3),
                             latency=round(latency, 2),
                             memories=[m["text"][:80] for m in memories],
                             tools=tools_used, error=err)
        except Exception:
            pass
        # ★ 阉割版补评 (2026-08-27): 回复已生成, 用户在看回复/打字 — 异步用小模型补评本轮情绪事件
        #   聊天关键路径零延迟 (入口 fast 规则评价), 精度仍达小模型水平 (事后修正)
        try:
            if _emotion_event_ids and getattr(self, "emotion_events", None) \
                    and getattr(self, "emotion_events", None).enabled \
                    and getattr(config, "EMOTION_REFINE_AFTER_REPLY", True):
                ids = list(_emotion_event_ids)

                def _refine():
                    try:
                        n = self.emotion_events.refine_by_ids(ids)
                        if n:
                            em = self.emotion_events.current()
                            if em["contributing"]:
                                self.state.emotion.state["deep_affect"]["current_mood"] = em["mood"]
                    except Exception:
                        pass
                threading.Thread(target=_refine, daemon=True).start()
        except Exception:
            pass
        return reply

    @staticmethod
    def _trim_reply(text):
        """回复清理 (2026-08-20 雾弥: 取消硬截断 — 长度限制是拟人化的阻碍):
        - 去掉续写带回的 '你:' 前缀
        - 换行合并为空格 (跨行内容不丢)
        - 不做任何长度裁剪: 她想说多长就说多长, 完整保留
        背景: 之前有 600字/4句 等硬截断 → 完整回复被削成碎片, 内心独白比说出口的话完整;
              现在彻底取消, 她说什么就是什么"""
        t = str(text or "").strip()
        if not t:
            return ""
        # 去掉续写带回的"你:"/"Kiri:"前缀
        t = re.sub(r"^(?:你|Kiri|kiri|Kiri酱)[:：]\s*", "", t)
        # 换行合并为空格
        t = re.sub(r"\s*\n+\s*", " ", t).strip()
        return t

    @staticmethod
    def _tool_result_invalid(result):
        """工具结果有效性: 空/失败标记/无回答 → 无效 (需重试)
        注意: '没找到/没结果' 是诚实的空结果(工具工作正常), 不算无效"""
        t = str(result or "").strip()
        if not t:
            return True
        bad = ("(……", "(失败", "工具执行失败", "(工具执行失败", "(没有这个工具",
               "(AI导师没有给出回答)", "(引擎错误", "搜索失败:", "获取失败:", "查询失败",
               "(记忆检索失败", "(工具", "天气获取失败", "失败:", "错误:", "timed out",
               "timeout", "超时")
        # 兼容: 失败消息可能以工具名开头 (如"天气获取失败")
        if t.startswith(bad):
            return True
        # 更宽松: 含"获取失败/执行失败/查询失败/无结果/没找到"等失败信号
        for sig in ("获取失败", "执行失败", "查询失败", "搜索失败", "请求失败",
                    "连接失败", "无法访问", "获取不到", "没有搜到", "没结果"):
            if sig in t:
                return True
        return False

    def _is_longterm_worthy(self, text):
        """重要内容同时存入长期记忆 (用深刻度评分, 统一 _salience 的 poignancy 逻辑)"""
        return self.memory._salience(text) >= 0.45

    # ---- 睡眠期记忆巩固 ----
    def consolidate_memory(self):
        """睡眠期: 回放最近的对话, 提炼成长期事实记忆 (像睡眠中的记忆巩固)
        多用户: 每个用户的记忆分别巩固"""
        if not self.state.should_consolidate():
            return False
        total_facts = 0
        for user in self.memory.users():
            total_facts += self._consolidate_user(user)
            # ★ 知识页同步刷新 (NEKO/Hindsight吸收): 睡眠巩固后把零散记忆合成综合画像
            try:
                self.knowledge.synthesize(user)
            except Exception:
                pass
        self.state.last_consolidate = time.time()
        return total_facts > 0

    def _consolidate_user(self, user):
        """巩固单个用户的记忆"""
        events = self.memory.recent_events(hours=24, user=user)
        # 去掉主动发言记录, 只保留 XX↔Kiri 对话
        conv = [e["text"] for e in events if "你主动联系了" not in e["text"]]
        if len(conv) < config.CONSOLIDATE_MIN_EVENTS:
            return 0
        # 组装回放材料 (最近30条, 避免过长)
        replay_text = "\n".join(conv[-30:])
        sys_p = prompt_mod.consolidate_system()
        t0 = time.time()
        try:
            raw = engine.generate(sys_p, replay_text, max_tokens=500, temperature=0.4)
            err = None
        except Exception as e:
            raw = ""
            err = str(e)
        latency = time.time() - t0
        # ★ 解析 {qa} 分隔的 Reflection 记忆 (Neuro 式)
        facts = []
        for seg in raw.split("{qa}"):
            seg = seg.strip()
            seg = seg.replace("关于雾弥，有什么值得长期记住的事实？", "").strip()
            if seg and seg not in ("无", "没有") and len(seg) >= 2:
                facts.append(seg)
        # 存入长期记忆 (按用户)
        # ★ speaker="kiri": 巩固提炼的是"她的洞见"(推断), 需用户印证才可信
        for f in facts[:5]:
            self.memory.encode(f, self.state.emotion.state, user=user, speaker="kiri")
        # ★ 自然遗忘 (按用户)
        forgotten = self.memory.forget(user=user)
        self._log_event("consolidate", user=user, facts=len(facts), forgotten=forgotten,
                        source_events=len(conv), latency=round(latency, 2), error=err)
        logger.info(f"记忆巩固[{user}]: 回放{len(conv)}条 → 提炼{len(facts)}条长期记忆, 遗忘{forgotten}条琐事")
        return len(facts)

    # ---- 主动 ----
    def proactive(self):
        # ★ 聊天优先 (2026-08-27): 用户正在聊天(respond进行中) → 主动让路, 不抢引擎
        if config.CHAT_ACTIVE.is_set() and time.time() - config.CHAT_ACTIVE_TS[0] < 120:
            return
        # ★ 睡眠期硬闸: 23-7点完全禁止主动发言 (用户要求更严格)
        # ★ 2026-08-20 放宽: 23-2点允许记忆涌现/强烈意愿时主动 (去掉重复限制),
        #   2-7点仍完全静默 (深度睡眠不打扰)
        if config.NIGHT_SHARE_BLOCK and self.state.is_deep_sleeping():
            return
        # ★ 事件由头预取 (M3, 2026-08-27): 有高情绪强度事件 → 提升开口意愿 + prompt 注入
        event_cands = []
        try:
            if getattr(self, "emotion_events", None):
                event_cands = self.emotion_events.top_candidates(n=3, min_intensity=0.4)
        except Exception:
            event_cands = []
        trigger, reason = self.state.should_proactive(has_event=bool(event_cands))
        # ★ 决策样本记录 (阶段B: SNN习惯层训练数据)
        #   记录所有"处于可主动时段"的检查(沉默≥20min, 未停止), 限频5分钟一条
        #   与开口预算/间隔解耦: 预算用完也记(考虑开口但没开), 负样本才全
        now = time.time()
        silence = (now - self.state.last_interact) / 60
        if (silence >= 20 and not self.state.stopped
                and now - getattr(self, "_last_decision_log", 0) >= 300):
            self._last_decision_log = now
            try:
                mood = self.state.emotion.state["deep_affect"]["current_mood"]
            except Exception:
                mood = 0.0
            self._log_event("proactive_decision",
                            decided=bool(trigger), reason=reason,
                            boredom=round(float(self.state.boredom), 3),
                            mood=round(float(mood), 3),
                            silence_min=round(float(silence), 1),
                            is_night=int(time.localtime().tm_hour >= config.NIGHT_HOUR
                                         or time.localtime().tm_hour < 7),
                            glow=round(float(self.state.memory_glow), 3))
        if not trigger:
            return
        # NEKO活动状态机: 欢迎窗/追问窗消费 (无论说不说, 窗口只给一次机会)
        try:
            snap = self.state.activity.snapshot(user=self.DEFAULT_USER)
            if snap.get("greeting_window"):
                self.state.activity.greeting_armed.pop(self.DEFAULT_USER, None)
            if snap.get("followup_active"):
                self.state.activity.mark_followup_used(self.DEFAULT_USER)
        except Exception:
            pass
        memories = self.memory.retrieve(
            query_text=self.state.describe() + " 主动联系",
            current_mood=self.state.emotion.state["deep_affect"]["current_mood"],
            session=self.session)
        # ★ 主动找雾弥: 用雾弥的对话上下文和关系阶段
        wu = self.DEFAULT_USER
        wdlg = self.get_dialog(wu)
        # NEKO追问窗: 追问的prompt提示 (她上轮问过, 轻轻追问, 别重复原话)
        followup_hint = ""
        if reason == "followup":
            followup_hint = "\n你上轮问了他一个问题但他还没回。你可以轻轻追问一句, 别重复原话, 也别催。"
        # ★ 纯无聊降级 (M3): 无聊时 prompt 里明确"日常问候即可, 不许编具体动机"
        if reason == "boredom":
            followup_hint += ("\n你现在只是想打个招呼。**只说一句简单的日常问候**（比如问吃了没/在干嘛/今天怎么样），"
                              "**不要编造任何具体事件、理由或动机**——没有真实的事可提就只说问候。")
        sys_p = prompt_mod.proactive_system(self.state.describe(user=wu), memories, reason,
                                            wdlg, self.state.thoughts,
                                            self.state.relation_stage(user=wu), wu,
                                            event_candidates=event_cands)
        t0 = time.time()
        try:
            raw = engine.generate(sys_p, "现在这一刻到了。" + followup_hint, max_tokens=600)
            err = None
        except Exception as e:
            raw = ""
            err = str(e)
            print(f"[Kiri·主动] (引擎错误: {e})")
        latency = time.time() - t0
        mono, speech, say = prompt_mod.parse_proactive(raw)
        # ★ 断句截断检测 (V4-flash思考占token): 半句话不主动发 → 重生成一次, 还断就咽回去
        if say and prompt_mod.looks_truncated(say):
            try:
                raw2 = engine.generate(sys_p, "现在这一刻到了。" + followup_hint,
                                       max_tokens=600, temperature=0.9)
                mono2, speech2, say2 = prompt_mod.parse_proactive(raw2)
                if say2 and not prompt_mod.looks_truncated(say2):
                    mono, speech, say = mono2, speech2, say2
                else:
                    say = None   # 仍截断 → 宁缺毋滥
            except Exception:
                say = None
        # ★ 话题级防复读 (NEKO BM25吸收): 主动话和最近聊过的高度重复 → 咽回去
        if say:
            try:
                verdict, _, _ = self.anti_repeat.check(self.DEFAULT_USER, say)
                if verdict == "drop":
                    say = None
            except Exception:
                pass
        if speech == "YES" and say and self._dedup_ok(say) and self._proactive_dedup_ok(say):
            print(f"\n[Kiri·主动] {say}\n")
            self.state.consume_budget()
            self._record_output(say)
            self._record_proactive(say)
            # ★ 话题级防复读: 记录主动发言
            try:
                self.anti_repeat.record(self.DEFAULT_USER, say)
            except Exception:
                pass
            # ★ 心路日志: 主动发言
            try:
                import kiri_mind
                kiri_mind.proactive(say, reason)
                # ★ 数据日志: 主动发言样本
                try:
                    import data_log
                    data_log.proactive(say, reason)
                except Exception:
                    pass
            except Exception:
                pass
            # ★ history.jsonl: 主动发言也入历史 (带sender=雾弥)
            self._append_history_file("assistant", f"[主动] {say}", user=self.DEFAULT_USER)
            # ★ 主动说的话也进对话上下文 (雾弥的)
            wdlg = self.get_dialog(self.DEFAULT_USER)
            wdlg.append({"role": "kiri", "text": say[:300]})
            if len(wdlg) > 14:
                self._dialogs[self.DEFAULT_USER] = wdlg[-14:]
            self.dialog = self._dialogs.get(self.current_user, wdlg)
            self.memory.encode(f"你主动联系了雾弥: {say}",
                               self.state.emotion.state, session=self.session,
                               user=self.DEFAULT_USER, speaker="kiri")
            # ★ 主动发言后也生成内心独白 (异步, 保持意识连续性)
            self._spawn_thought(say, "主动说话后", user=self.DEFAULT_USER)
            self._log_event("proactive", trigger=reason, said=True,
                            say=say[:200], latency=round(latency, 2), error=err)
            if self.on_proactive:
                try:
                    self.on_proactive(say, reason)
                except Exception:
                    pass
        else:
            self.state.consume_budget()  # 决定不说也更新间隔 (宁缺毋滥)
            # ★ passive 模式 (NEKO吸纳): 话到嘴边咽回去 → 入念头库, 不投递
            #   傲娇的"想说没说出口"被保留, 之后"想起"检索能浮出来
            if say and len(say.strip()) > 4:
                try:
                    self.memory.encode_thought(f"[想说没说出口] {say.strip()[:80]}",
                                               0.2, "passive", user=self.DEFAULT_USER)
                except Exception:
                    pass
            self._log_event("proactive", trigger=reason, said=False,
                            say=say[:200] if say else "", latency=round(latency, 2), error=err)

    def proactive_event(self, event_type, say, reason="event", min_gap_ratio=0.5):
        """★ 事件驱动主动 (NEKO吸纳): 任务完成等事件直接投递 (不走意愿分)
        如任务完成主动聊; 仍受 睡眠硬禁 + 缩短的间隔(0.5×MIN_INTERVAL) + 活动门 约束
        2026-08-20: 2-7点深睡才禁, 23-2点允许 (事件=有理由开口)"""
        if config.NIGHT_SHARE_BLOCK and self.state.is_deep_sleeping():
            return False
        # NEKO活动门: 生成中时不投事件主动
        try:
            snap = self.state.activity.snapshot(user=self.DEFAULT_USER)
            if snap["state"] in ("busy",):
                return False
        except Exception:
            pass
        if time.time() - self.state.last_proactive < config.MIN_INTERVAL_MINUTES * 60 * min_gap_ratio:
            return False
        say = (say or "").strip()   # ★ 2026-08-20: 取消 [:200] 硬截断 — 主动发言想多长就多长
        if not say or not self._dedup_ok(say) or not self._proactive_dedup_ok(say):
            return False
        # ★ 话题级防复读 (NEKO BM25吸收): 事件主动话与最近聊过重复 → 不发
        try:
            verdict, _, _ = self.anti_repeat.check(self.DEFAULT_USER, say)
            if verdict == "drop":
                return False
        except Exception:
            pass
        self.state.consume_budget()
        print(f"\n[Kiri·主动事件:{event_type}] {say}\n")
        self._record_output(say)
        self._record_proactive(say)
        # ★ 话题级防复读: 记录
        try:
            self.anti_repeat.record(self.DEFAULT_USER, say)
        except Exception:
            pass
        try:
            import kiri_mind
            kiri_mind.proactive(say, reason)
            try:
                import data_log
                data_log.proactive(say, reason)
            except Exception:
                pass
        except Exception:
            pass
        self._append_history_file("assistant", f"[主动] {say}", user=self.DEFAULT_USER)
        wdlg = self.get_dialog(self.DEFAULT_USER)
        wdlg.append({"role": "kiri", "text": say[:300]})
        if len(wdlg) > 14:
            self._dialogs[self.DEFAULT_USER] = wdlg[-14:]
        self.dialog = self._dialogs.get(self.current_user, wdlg)
        self.memory.encode(f"你主动联系了雾弥: {say}",
                           self.state.emotion.state, session=self.session,
                           user=self.DEFAULT_USER, speaker="kiri")
        self._spawn_thought(say, "主动说话后", user=self.DEFAULT_USER)
        self._log_event("proactive", trigger=reason, said=True, say=say[:200], error=None)
        if self.on_proactive:
            try:
                self.on_proactive(say, reason)
            except Exception:
                pass
        return True

    # ---- 主循环 ----
    def run(self):
        print("=" * 60)
        print("Kiri 醒了。输入消息与她说话 (quit 退出)")
        print("=" * 60)
        # 输入线程
        def input_loop():
            while self.running:
                try:
                    line = input()
                    self.input_queue.append(line)
                except (EOFError, KeyboardInterrupt):
                    self.running = False
        threading.Thread(target=input_loop, daemon=True).start()

        last_tick = time.time()
        while self.running:
            # 处理输入
            while self.input_queue:
                line = self.input_queue.pop(0)
                if line.strip().lower() in ("quit", "exit"):
                    self.running = False
                    break
                self.respond(line)
            # 状态演化 + 记忆巩固 + 联想 + 主动检查
            now = time.time()
            if now - last_tick >= config.TICK_SECONDS:
                self.state.tick()
                self.consolidate_memory()   # ★ 睡眠期记忆巩固
                if self.reverie.should_run():   # ★ 联想引擎
                    self.reverie.run_cycle()
                self.proactive()
                # NEKO活动状态机: 周期落盘 (跨重启保留最近时间/追问/charge)
                try:
                    self.state.activity.save()
                except Exception:
                    pass
                last_tick = now
            time.sleep(0.5)

        print("\nKiri 睡下了。她的记忆已保存。")


if __name__ == "__main__":
    Kiri().run()

