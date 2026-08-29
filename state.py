# -*- coding: utf-8 -*-
"""Kiri 状态系统 — 情绪(复用修好的 emotion_core) + 无聊/沉默 + 预算制主动 (2026-08-16)
预算制: 每日主动额度, 意愿分(无聊/沉默/深夜/记忆)最高时消耗
"""
import os
import sys
import json
import time
import random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "reuse", "emotion"))
from emotion_core import EmotionStateMachine, MotivationSystem, SocialIntelligenceSystem, BiorhythmSystem

import config
import user_activity as user_activity_mod
import user_mood as user_mood_mod


class State:
    DEFAULT_USER = "雾弥"   # 主动发言/活动状态机的默认对象 (与 kiri.Kiri 对齐)

    def __init__(self):
        cfg_path = os.path.join(os.path.dirname(__file__), "..", "src", "reuse", "emotion", "config.yaml")
        self.emotion = EmotionStateMachine(cfg_path)
        self.motivation = MotivationSystem()   # ★ 动机系统: 让Kiri有"此刻想做什么"的内驱力
        self.motivation.last_motivation_update = 0.0  # ★ 修复: 允许首次立即更新(原emotion_core初始化为now导致首次被跳过)
        self.social = SocialIntelligenceSystem()  # ★ 社交智能: 追踪和雾弥的关系(熟悉度/信任/亲密)
        self.biorhythm = BiorhythmSystem()        # ★ 生理节律: 精力/注意力昼夜波动
        self.boredom = 0.1
        self.last_interact = time.time()
        self.last_proactive = -9999.0
        self.used_today = 0
        self.stopped = False
        self.memory_glow = 0.0
        self.last_consolidate = 0.0       # ★ 上次记忆巩固时间戳 (睡眠期回放用)
        self.thoughts = []                # ★ 内心独白流 (咀嚼法: 持续的意识, 持久化)
        self.activity = user_activity_mod.UserActivity()   # NEKO活动状态机吸收: 用户活动/欢迎窗/追问窗/PASS门
        self.mood_profile = user_mood_mod.UserMoodProfile()   # NEKO用户情绪画像吸收(聚合层): 读雾弥心情, 不动emotion_core
        self.rng = random.Random()
        self._day = time.localtime().tm_yday
        self._save_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "state_save.json")
        self.load()   # ★ 退出存储: 启动恢复上次状态

    # ---- 退出存储 (情绪/无聊/沉默/预算/停止标志) ----
    def save(self):
        """把当前内心状态存盘 (退出时/定期调用)"""
        try:
            # ★ 社交关系 (多用户): 所有用户都存, 剥离 emotional_history(datetime不可序列化)
            rels = {}
            for name, r in self.social.relationships.items():
                if isinstance(r, dict):
                    rels[name] = {k: v for k, v in r.items() if k != "emotional_history"}

            data = {
                "boredom": self.boredom,
                "last_interact": self.last_interact,
                "last_proactive": self.last_proactive,
                "used_today": self.used_today,
                "day": self._day,
                "stopped": self.stopped,
                "memory_glow": self.memory_glow,
                "last_consolidate": self.last_consolidate,
                "emotion_state": self.emotion.state,
                "social": rels,
                "thoughts": self.thoughts,
            }
            with open(self._save_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1, default=float)
        except Exception:
            pass

    def load(self):
        """启动恢复 (容错: 无存档/损坏则用默认)"""
        try:
            if not os.path.exists(self._save_path):
                return
            with open(self._save_path, encoding="utf-8") as f:
                d = json.load(f)
            self.boredom = float(d.get("boredom", 0.1))
            self.last_interact = float(d.get("last_interact", time.time()))
            self.last_proactive = float(d.get("last_proactive", -9999.0))
            self.used_today = int(d.get("used_today", 0))
            self._day = int(d.get("day", time.localtime().tm_yday))
            self.stopped = bool(d.get("stopped", False))
            self.memory_glow = float(d.get("memory_glow", 0.0))
            self.last_consolidate = float(d.get("last_consolidate", 0.0))
            es = d.get("emotion_state")
            if isinstance(es, dict) and es:
                self.emotion.state.update(es)
            # ★ 恢复社交关系 (多用户; 兼容旧格式: 单个雾弥 dict)
            rel = d.get("social")
            if isinstance(rel, dict):
                if any(isinstance(v, dict) for v in rel.values()):
                    # 新格式: {用户: 关系dict}
                    for name, r in rel.items():
                        if isinstance(r, dict):
                            clean = {k: v for k, v in r.items() if k != "emotional_history"}
                            clean["emotional_history"] = []
                            self.social.relationships[name] = clean
                else:
                    # 旧格式: 单个关系dict (雾弥)
                    clean = {k: v for k, v in rel.items() if k != "emotional_history"}
                    clean["emotional_history"] = []
                    self.social.relationships["雾弥"] = clean
            # ★ 恢复内心独白流
            th = d.get("thoughts")
            if isinstance(th, list):
                self.thoughts = [str(t) for t in th[-10:]]
        except Exception:
            pass

    # ---- 睡眠期判定 ----
    def is_sleeping(self, now=None):
        """睡眠期: [SLEEP_START_HOUR, SLEEP_END_HOUR) 区间 (跨午夜)"""
        now = now or time.time()
        h = time.localtime(now).tm_hour
        if config.SLEEP_START_HOUR < config.SLEEP_END_HOUR:
            return config.SLEEP_START_HOUR <= h < config.SLEEP_END_HOUR
        # 跨午夜 (如 23~7)
        return h >= config.SLEEP_START_HOUR or h < config.SLEEP_END_HOUR

    def is_deep_sleeping(self, now=None):
        """深睡期 (2026-08-20 放宽深夜限制): 2-7点完全静默; 23-2点允许主动
        原 is_sleeping() 23-7 全禁 → 雾弥觉得 23-2 完全禁太死 (深夜她可能还想开口)
        深睡 = 凌晨2点到7点 (跨午夜判断)"""
        now = now or time.time()
        h = time.localtime(now).tm_hour
        return 2 <= h < 7

    def should_consolidate(self):
        """睡眠期 + 距上次巩固够久 → 触发记忆巩固"""
        if not self.is_sleeping():
            return False
        if self.last_consolidate > 0 and time.time() - self.last_consolidate < config.CONSOLIDATE_ONCE_PER_HOURS * 3600:
            return False
        return True

    # ---- 每 tick 演化 ----
    def tick(self):
        now = time.time()
        # 跨天重置预算
        today = time.localtime().tm_yday
        if today != self._day:
            self._day = today
            self.used_today = 0

        # 情绪时间演化(每tick)
        self.emotion.update()
        # ★ 打通两套无聊度: 把Kiri的boredom同步进emotion的deep_affect(动机系统读这里)
        try:
            self.emotion.state["deep_affect"]["current_boredom"] = self.boredom
        except Exception:
            pass
        # ★ 动机系统更新 (每60秒由MotivationSystem内部节流)
        try:
            self.motivation.update(self.emotion.state, None)
        except Exception:
            pass
        # ★ 生理节律更新 (精力/注意力)
        try:
            self.biorhythm.update(self.emotion.state)
        except Exception:
            pass

        # 无聊度: 沉默时上升
        silence = (now - self.last_interact) / 60
        self.boredom = min(1.0, self.boredom + config.BOREDOM_RISE_PER_TICK)
        if self.last_proactive > 0 and now - self.last_proactive < 600:
            pass  # 主动后短暂抑制
        else:
            # 长时间沉默额外加无聊
            if silence > 60:
                self.boredom = min(1.0, self.boredom + 0.002)

        # 记忆涌现(glow)
        if self.memory_glow <= 0 and self.rng.random() < config.MEMORY_GLOW_PROB:
            self.memory_glow = 0.8
        self.memory_glow = max(0.0, self.memory_glow - config.MEMORY_GLOW_DECAY)

    # ---- 互动 ----
    def interact(self, text, llm_emotion=None, user=None):
        # ★ LLM 情绪解析结果优先 (替代关键词匹配); 失败回退关键词
        ctx = {"llm_emotion": llm_emotion} if llm_emotion else None
        self.emotion.update(text, ctx)          # 事件→情绪瀑布
        self.boredom = max(0.05, self.boredom - config.BOREDOM_RELIEF)
        self.last_interact = time.time()
        if text.strip().startswith(config.STOP_WORD):
            self.stopped = True
        # ★ 更新社交关系 (多用户: 和谁说话就更新谁的关系)
        who = user or "雾弥"
        try:
            self.social.update_relationship(who, text, self.emotion.state)
        except Exception:
            pass
        # NEKO活动状态机: 用户发消息 → 记录 (欢迎窗/追问窗/charge)
        try:
            self.activity.note_user_message(who, text)
        except Exception:
            pass

    # ---- 预算制主动检查 ----
    def want_score(self, silence_min, is_night, glow):
        w = config.WANT_WEIGHTS
        s = min(silence_min / 720.0, 1.0)
        n = 0.5 if is_night else 0.0
        return w[0] * self.boredom + w[1] * s + w[2] * n + w[3] * glow

    def should_proactive(self, has_event=False):
        """返回 (是否触发, 触发原因)
        NEKO activity吸收: PASS门顺序 busy→gaming→欢迎窗→追问窗→意愿分
        预算制已取消: 频率由 意愿分阈值+最小间隔+深夜+刚互动 控制
        has_event: 存在高情绪强度事件由头 (M3) → 意愿分提升, 更易开口 (有真实落点)"""
        now = time.time()
        if self.stopped:
            return False, "stopped"
        # NEKO PASS门: busy(生成中)/gaming(打OSU) 不主动
        try:
            snap = self.activity.snapshot(user=self.DEFAULT_USER, now=now)
            if snap["state"] == "busy":
                return False, "activity_busy"
            if snap["state"] == "gaming":
                return False, "activity_gaming"
            # 回归欢迎窗: 用户刚从 away 回来 → 她可以先开口 (压倒 too_recent)
            if snap.get("greeting_window") and now - self.last_proactive >= 60:
                return True, "greeting"
            # unfinished_thread 追问窗: 她上轮问句没被回 → 5min内可追问一次
            if snap.get("followup_active"):
                return True, "followup"
        except Exception:
            pass
        if now - self.last_proactive < (config.MIN_INTERVAL_MINUTES + self.rng.random() * config.PROACTIVE_JITTER_MIN) * 60:
            return False, "min_interval"
        silence = (now - self.last_interact) / 60
        if silence < 20:                    # 刚互动过不主动
            return False, "too_recent"
        is_night = time.localtime().tm_hour >= config.NIGHT_HOUR or time.localtime().tm_hour < 2
        # ★ 深夜静默保护 (2026-08-20 放宽): 23-2点记忆涌现/强烈意愿才主动, 否则安静
        #   2-7点已被 is_deep_sleeping 在 proactive() 入口拦截, 这里只管 23-2 的"别硬凑"
        if is_night and self.memory_glow <= 0.5:
            return False, "night_silence"
        score = self.want_score(silence, is_night, self.memory_glow)
        if has_event:                      # ★ M3: 有真实事件由头 → 提升开口意愿 (不编动机也有话说)
            score += 0.15
        if score >= config.WANT_THRESHOLD:
            # ★ 触发原因优先级: 记忆涌现 > 深夜 > 低落陪伴 > 动机系统(想亲近/求刺激) > 无聊
            if self.memory_glow > 0.5:
                reason = "memory"
            elif is_night:
                reason = "night"
            else:
                reason = self._motivation_reason()
                # ★ 用户情绪画像 (NEKO吸收): 雾弥最近低落+起伏 → 主动倾向"陪着"而非玩闹
                #   只保留 memory(记忆涌现)/night(深夜) 的原始语义, 其余一律切陪伴
                try:
                    if reason not in ("memory", "night") and self.mood_profile.should_be_gentle(self.DEFAULT_USER):
                        reason = "cheerup"
                except Exception:
                    pass
            return True, reason
        return False, "low_want"

    def _motivation_reason(self):
        """把动机系统最高优先级映射成主动原因 (让主动发言动机更多样)"""
        try:
            mot = self.motivation.get_highest_priority_motivation()
            if not mot:
                return "boredom"
            m = mot["type"]
            if m == "build_relationships":
                return "miss"          # 想亲近雾弥
            if m == "seek_stimulation":
                return "stimulation"   # 想被逗/求刺激
            if m == "self_expression":
                return "share"         # 想说心里话
            if m == "maintain_positive_mood":
                return "cheerup"       # 想让自己心情好
            if m == "manage_boredom":
                return "boredom"
            return "boredom"
        except Exception:
            return "boredom"

    def consume_budget(self):
        """主动后状态更新 (预算已取消, 只更新间隔+降无聊)"""
        self.last_proactive = time.time()
        self.boredom = max(0.2, self.boredom - 0.3)

    # ---- 状态描述(注入 LLM) ----
    def describe(self, user=None):
        e = self.emotion.state
        who = user or "雾弥"
        surf = e["surface_emotion"]
        deep = e["deep_affect"]
        # ★ PAD 三维度 → 更细腻的情绪描述 (不只开心/低落/平静三档)
        mood = deep["current_mood"]
        pleasure = surf["pleasure"]
        arousal = surf["arousal"]
        dominance = surf["dominance"]
        # 心境 (深层, 慢)
        if mood > 0.5: mood_txt = "心情很好"
        elif mood > 0.15: mood_txt = "心情不错"
        elif mood > -0.15: mood_txt = "心情平静"
        elif mood > -0.5: mood_txt = "有点低落"
        else: mood_txt = "很低落"
        # 愉悦 (表层, 快)
        if pleasure > 0.5: plea_txt = "很愉悦"
        elif pleasure > 0.15: plea_txt = "略愉悦"
        elif pleasure > -0.15: plea_txt = "中性"
        elif pleasure > -0.5: plea_txt = "有点不悦"
        else: plea_txt = "很不悦"
        # 唤醒度 (兴奋/平静)
        if arousal > 0.5: ar_txt = "兴奋"
        elif arousal > 0.15: ar_txt = "略兴奋"
        elif arousal > -0.15: ar_txt = "平稳"
        elif arousal > -0.5: ar_txt = "有点蔫"
        else: ar_txt = "很蔫"
        # 支配度 (主动/顺从)
        if dominance > 0.3: dom_txt = "想主导"
        elif dominance < -0.3: dom_txt = "想被主导"
        else: dom_txt = ""
        bored = "有点无聊" if self.boredom > 0.6 else ("还好" if self.boredom > 0.3 else "充实")
        silence = int((time.time() - self.last_interact) / 60)
        # ★ 当前内驱力 (动机系统最高优先级) → 让Kiri有"此刻想做什么"的方向
        drive = self._drive_text()
        dom_part = f", {dom_txt}" if dom_txt else ""
        base = (f"你的情绪: {mood_txt}({mood:.2f}), {plea_txt}({pleasure:.2f}), "
                f"{ar_txt}({arousal:.2f}){dom_part}, 无聊度{self.boredom:.2f}({bored}), "
                f"{who}已沉默约{silence}分钟")
        # ★ 时间感知 (让她知道现在几点, 有"此刻是白天还是深夜"的意识)
        time_txt = self._time_text()
        if time_txt:
            base += f"。{time_txt}"
        if drive:
            base += f"。你此刻隐隐想: {drive}"
        # ★ 关系状态 (多用户: 和谁说话就看和谁的关系)
        rel = self._relation_text(user=who)
        if rel:
            base += f"。{rel}"
        # ★ 生理节律 (精力/注意力) → 让她说话状态有昼夜起伏
        bio = self._bio_text()
        if bio:
            base += f"。{bio}"
        # ★ 用户情绪感知 (NEKO用户情绪画像吸收): 她"知道"雾弥最近心情 — 只读不传染
        try:
            mood_line = self.mood_profile.describe_for(who)
            if mood_line:
                base += f"。你对他情绪的感知: {mood_line}"
        except Exception:
            pass
        # ★ 当前身体语言 (情绪→耳朵尾巴精确映射) → 让她用对的动作
        body = self._body_text(mood, arousal, pleasure)
        if body:
            base += f"。{body}"
        return base

    def _time_text(self):
        """时间感知: 今天是几号几点, 让她有日期+昼夜意识 (之前只有时分, 答不了'今天几号')"""
        try:
            h = time.localtime().tm_hour
            m = time.localtime().tm_min
            date_txt = time.strftime("%Y年%m月%d日 %A")
            if 5 <= h < 12:
                seg = f"上午{h}点{m:02d}分"
            elif 12 <= h < 14:
                seg = f"中午{h}点{m:02d}分"
            elif 14 <= h < 18:
                seg = f"下午{h}点{m:02d}分"
            elif 18 <= h < 23:
                seg = f"晚上{h}点{m:02d}分"
            else:
                seg = f"深夜{h}点{m:02d}分"
            return f"今天是{date_txt}，现在是{seg}"
        except Exception:
            return ""

    def _body_text(self, mood, arousal, pleasure):
        """情绪 → 表达提示 (拟真: 只用单字情绪词, 长串动作留给整活)
        日常不注入; 只有强烈情绪时给简短情绪词提示 (回复别写长串动作)"""
        try:
            if arousal > 0.6 and pleasure > 0.4:
                return "你此刻心跳有点快（最多用（羞）（慌）这类单字情绪词带过一句，别写长串动作）"
            if mood < -0.5:
                return "你此刻有点蔫、提不起劲（最多用（蔫）（悲）带过，别写长串动作）"
            if pleasure > 0.6:
                return "你此刻心情不错（最多用（喜）带过一句，别写长串动作）"
            return ""
        except Exception:
            return ""

    def _bio_text(self):
        """把生理节律翻译成'她此刻的精力/注意力状态'"""
        try:
            e = getattr(self.biorhythm, "energy", None)
            a = getattr(self.biorhythm, "attention", None)
            if e is None or a is None:
                return ""
            energy_txt = "精力充沛" if e > 0.6 else ("有点疲惫" if e < 0.35 else "精力还行")
            attention_txt = "注意力集中" if a > 0.6 else ("有点走神" if a < 0.4 else "注意力一般")
            return f"你{energy_txt}({e:.2f})，{attention_txt}({a:.2f})"
        except Exception:
            return ""

    def _relation_text(self, user=None):
        """把社交关系状态翻译成'和XX的关系感知' (多用户)"""
        who = user or "雾弥"
        try:
            r = self.social.relationships.get(who)
            if not r:
                return ""
            fam = r.get("familiarity", 0.0)
            trust = r.get("trust", 0.5)
            intimacy = r.get("intimacy", 0.0)
            aff = r.get("deep_affinity", 0.0)
            # 关系阶段
            if intimacy > 0.5:
                stage = "你们已经很亲近了"
            elif intimacy > 0.2:
                stage = "你们渐渐熟络起来"
            elif fam > 0.3:
                stage = "你们开始熟悉彼此"
            else:
                stage = "你们还不太熟"
            aff_txt = ""
            if aff > 0.3:
                aff_txt = "，你对他越来越有好感"
            elif aff < -0.3:
                aff_txt = "，你对他有点疏远"
            return f"你和{who}的关系: {stage}(熟悉{fam:.2f}/信任{trust:.2f}/亲密{intimacy:.2f}){aff_txt}"
        except Exception:
            return ""

    def relation_stage(self, user=None):
        """关系阶段 → 人格侧重 (让她的行为随关系演化, 不只是静态人设)
        早期高冷防御 / 中期活泼小恶魔 / 后期傲娇软化深层依赖浮现
        多用户: 雾弥走恋人线; 其他用户走朋友线(高冷→熟络→小恶魔, 不到恋人)"""
        who = user or "雾弥"
        try:
            r = self.social.relationships.get(who)
            if not r:
                return "stranger"
            intimacy = r.get("intimacy", 0.0)
            fam = r.get("familiarity", 0.0)
            # 雾弥: 可以到 intimate (恋人线)
            if who == "雾弥":
                if intimacy > 0.5:
                    return "intimate"
                if intimacy > 0.2 or fam > 0.4:
                    return "close"
                if fam > 0.15:
                    return "acquainted"
                return "stranger"
            # 其他用户: 朋友线, 最高到 close (熟络/小恶魔), 不到 intimate
            if fam > 0.5 or intimacy > 0.3:
                return "close"
            if fam > 0.2:
                return "acquainted"
            return "stranger"
        except Exception:
            return "stranger"

    def _drive_text(self):
        """把动机系统最高优先级翻译成'她此刻的内驱力'"""
        try:
            mot = self.motivation.get_highest_priority_motivation()
            if not mot:
                return ""
            # 动机 → 自然的内心倾向描述 (傲娇猫娘口吻)
            m = {
                "manage_boredom": "找点新鲜事，别让自己太无聊",
                "seek_stimulation": "想被逗一逗，或者来点刺激",
                "build_relationships": "想和雾弥更亲近一点",
                "self_expression": "想说点心里话",
                "maintain_positive_mood": "想让自己心情好起来",
                "reduce_dissonance": "有点别扭，想理顺自己的情绪",
                "avoid_overload": "有点累了，想放松",
                "maintain_attention": "想集中精神",
                "manage_energy": "想省点力气",
            }.get(mot["type"], "")
            return m
        except Exception:
            return ""

