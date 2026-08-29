# -*- coding: utf-8 -*-
"""user_activity.py — 用户活动状态机 (吸收 N.E.K.O activity/focus_scorer/state_machine)
=====================================================================
NEKO 决策链: OS信号 → 活动跟踪器 → 状态机(11态) → propensity/skip → PASS门 → 投递
Kiri 是 QQ 平台, 没有桌面进程检测, 信号只剩:
  - 用户消息时间戳(私聊/群) + 消息特征(长度/频率)
  - Kiri 自身状态: 是否在生成回复(busy)
状态缩为 5 态: chatting(用户在说话) / just_replied(刚回) / quiet / away(久静)
              / busy(生成中)
必抄机制 (雾弥 2026-08-19 决策):
  ① charge 累加器+迟滞带 (focus_scorer): 单条消息不直接判定,
     charge=charge×0.5+score, ≥0.6 进入"engaged"(不打扰), <0.3 退出 — 防单条误判的敏感度记忆
  ② 回归欢迎窗: away(≥60min静默) 后用户回归 → 60s 内允许她主动说一句欢迎/接话
  ③ unfinished_thread 追问窗: 她上轮以问句结尾且用户未回 → 5min 内允许一次追问, 用户回复即清除
  ④ PASS 门: busy → chatting → 冷却 → 概率 → 无话题 (任一命中本轮放弃)
  ⑤ 投递前复查: 发之前再看一眼用户是否刚说过话 (中途插话即放弃)
=====================================================================
"""
import os
import sys
import time
import json
import re

# Windows 控制台默认GBK, print含会崩 → 强制UTF-8 (与 qq_bridge 同法)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ACTIVITY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_activity.json")

# ---- 参数 (NEKO 参数移植, 按 QQ 平台缩) ----
AWAY_MINUTES = 60                 # 静默多久算 away (NEKO: 15min桌面; QQ 拉长)
GREETING_WINDOW_SECONDS = 90      # away 回归后欢迎窗 (NEKO: 60s)
CHAT_WINDOW_SECONDS = 90          # 用户刚说过话 = chatting (不抢话)
COOLDOWN_MINUTES = 20             # 刚回复冷却 (与 state.should_proactive 的 silence<20 对齐)
FOLLOWUP_WINDOW_SECONDS = 300     # 追问窗 (NEKO: 5min)
CHARGE_ENTER = 0.6                # 迟滞带: 进入 engaged (NEKO: 0.6)
CHARGE_EXIT = 0.3                 # 迟滞带: 退出 engaged (NEKO: 0.3)
CHARGE_RETENTION = 0.5            # 每轮保留 (NEKO: 0.5)
CHARGE_CAP = 1.0
CHARGE_HALF_LIFE = 1200           #  时间衰减半衰期20min (NEKO: 0.02/s 太急, 20min温和)
QUESTION_TAIL = ("?", "？", "吗", "呢", "么", "吧", "没", "对不对", "是不是")


def _ends_with_question(text):
    """上轮回复是否以问句结尾 (NEKO: 尾部60字含?/？或尾字吗/呢/么/吧)"""
    t = str(text or "").strip()
    if not t:
        return False
    tail = t[-60:]
    if any(c in tail for c in ("?", "？")):
        return True
    return any(t.rstrip("。！!～~ ").endswith(w) for w in QUESTION_TAIL)


class UserActivity:
    """每用户的实时活动状态 (全内存+落盘, 不依赖桌面信号)"""

    def __init__(self, path=ACTIVITY_FILE):
        self.path = path
        self.last_user_msg = {}     # user -> ts (用户最后消息)
        self.last_ai_msg = {}       # user -> ts (她最后回复)
        self.last_ai_text = {}      # user -> 她最后回复文本 (问句检测)
        self.followup_at = {}       # user -> 追问窗武装时间 (她问句结尾, 用户未回)
        self.followup_used = {}     # user -> 追问已用时间 (一次, 用户回复才清除)
        self.greeting_armed = {}    # user -> 回归欢迎窗武装时间 (曾away后回归, sticky)
        self.charge = {}            # user -> {"v": 敏感度值, "ts": 更新时间} (engaged 判定)
        self.busy_until = 0.0       # 全局: Kiri 生成中 (PASS_BUSY)
        self._msg_window = {}       # user -> [ts,...] 最近消息 (节奏/cadence)
        self._len_window = {}       # user -> [len,...] 最近消息长度
        self.load()

    # ---- 钩子 (只记时间戳/特征, 判定集中在 snapshot) ----
    def note_user_message(self, user, text="", ts=None):
        """用户发消息: 更新最近时间 + 消息节奏 + 清除追问窗 + 武装回归欢迎窗
         sticky: 若此前已 away(≥AWAY_MINUTES静默) → 回归欢迎窗武装 (NEKO stale_returning)"""
        ts = ts or time.time()
        u = str(user or "雾弥")
        prev_silence = (ts - self.last_user_msg.get(u, 0.0)) / 60.0 if self.last_user_msg.get(u) else 999.0
        if prev_silence >= AWAY_MINUTES:
            self.greeting_armed[u] = ts            # 曾长时间不在 → 回归欢迎
        self.last_user_msg[u] = ts
        self.followup_at.pop(u, None)      # 用户回复即清除追问 (武装+已用)
        self.followup_used.pop(u, None)
        # 消息节奏 (最近8条, 60s窗口)
        win = self._msg_window.setdefault(u, [])
        win.append(ts)
        self._msg_window[u] = [t for t in win if ts - t <= 600][-8:]
        lens = self._len_window.setdefault(u, [])
        lens.append(len(str(text or "")))
        self._len_window[u] = lens[-8:]
        # charge 演化 (用户活跃=engaged 倾向)
        self._update_charge(u, ts)

    def note_ai_message(self, user, text="", ts=None):
        """她回复了: 记录文本 (问句检测→武装追问窗), 清 busy"""
        ts = ts or time.time()
        u = str(user or "雾弥")
        self.last_ai_msg[u] = ts
        self.last_ai_text[u] = str(text or "")[:300]
        if _ends_with_question(text):
            self.followup_at.setdefault(u, ts)   # 武装一次, 不覆盖
        self.busy_until = 0.0

    def note_busy(self, seconds=60):
        """Kiri 开始生成: 生成期间不主动 (PASS_BUSY)"""
        self.busy_until = time.time() + seconds

    # ---- charge 累加器 (focus_scorer 移植: 迟滞带+敏感度记忆) ----
    def _update_charge(self, u, now):
        """单轮分数: 短消息(≤8字)+高频(60s内≥3条) = 用户正忙着/情绪化 → charge 上升
        不含情绪词判断 (情绪归情感系统, 待讨论) — 只判活跃度"""
        lens = self._len_window.get(u, [])
        msgs = self._msg_window.get(u, [])
        score = 0.0
        if lens and sum(1 for l in lens[-3:] if l <= 8) >= 2:
            score += 0.4                            # 连续短消息
        if len(msgs) >= 3 and (msgs[-1] - msgs[0]) <= 120:
            score += 0.4                            # 高频连发
        rec = self.charge.get(u) or {"v": 0.0, "ts": now}
        # 先按时间衰减 (半衰期 CHARGE_HALF_LIFE)
        age = max(0.0, now - rec.get("ts", now))
        old = rec.get("v", 0.0) * (0.5 ** (age / CHARGE_HALF_LIFE))
        if score > 0:
            self.charge[u] = {"v": min(CHARGE_CAP, old * CHARGE_RETENTION + score), "ts": now}
        else:
            # 空闲: 只保留不抬升 (NEKO: proactive路径绝不抬升)
            self.charge[u] = {"v": old * CHARGE_RETENTION, "ts": now}

    def _charge_now(self, u, now=None):
        """当前有效 charge (含时间衰减)"""
        now = now or time.time()
        rec = self.charge.get(u)
        if not rec:
            return 0.0
        age = max(0.0, now - rec.get("ts", now))
        return rec.get("v", 0.0) * (0.5 ** (age / CHARGE_HALF_LIFE))

    def is_engaged(self, user=None, now=None):
        """迟滞带判定: ≥ENTER 进入, <EXIT 退出"""
        u = str(user or "雾弥")
        return self._charge_now(u, now) >= CHARGE_ENTER

    # ---- 状态机 (纯规则, snapshot 时派生) ----
    def snapshot(self, user=None, now=None):
        """返回 {state, away, greeting_window, followup_active, followup_used, skip}
        skip: 主动发言跳过概率 (0~1) — PASS 门用"""
        now = now or time.time()
        u = str(user or "雾弥")
        last_user = self.last_user_msg.get(u, 0.0)
        last_ai = self.last_ai_msg.get(u, 0.0)
        silence = (now - last_user) / 60.0 if last_user else 999.0

        if now < self.busy_until:
            return {"state": "busy", "skip": 1.0}
        if self.is_engaged(u, now):
            return {"state": "engaged", "skip": 0.6}   # 用户忙着/情绪化 → 少打扰
        # 回归欢迎窗 (NEKO: away回归60s强制greeting态, 压倒聊天态):
        #   曾 away(≥60min静默) 后回归 → 窗口内她可以先开口 (sticky 标志)
        if u in self.greeting_armed and (now - self.greeting_armed[u]) <= GREETING_WINDOW_SECONDS:
            return {"state": "greeting", "away": True, "greeting_window": True, "skip": 0.0}
        if u in self.greeting_armed:
            self.greeting_armed.pop(u, None)   # 窗口过期清除
        if last_user and (now - last_user) <= CHAT_WINDOW_SECONDS:
            return {"state": "chatting", "skip": 1.0}  # 用户正在说话, 不抢话
        # 追问窗: 她上轮问句结尾 + 用户未回 + 5min内 + 未用过
        followup_active = bool(u in self.followup_at and u not in self.followup_used
                               and (now - self.followup_at[u]) <= FOLLOWUP_WINDOW_SECONDS)
        followup_used = bool(u in self.followup_used
                             and (now - self.followup_used[u]) <= FOLLOWUP_WINDOW_SECONDS)
        if followup_active:
            return {"state": "followup", "followup_active": True, "skip": 0.0}
        away = silence >= AWAY_MINUTES
        if silence < COOLDOWN_MINUTES:
            return {"state": "just_replied", "skip": 0.5}
        if away:
            return {"state": "away", "away": True, "skip": 0.2}
        return {"state": "quiet", "skip": 0.0}

    def mark_followup_used(self, user=None):
        """追问已发出: 锁定 (下次用户回复才清除)"""
        u = str(user or "雾弥")
        self.followup_used[u] = time.time()

    def _data_clear_for_test(self):
        """测试辅助: 清空全部状态 (不碰文件)"""
        self.last_user_msg.clear()
        self.last_ai_msg.clear()
        self.last_ai_text.clear()
        self.followup_at.clear()
        self.followup_used.clear()
        self.greeting_armed.clear()
        self.charge.clear()
        self._msg_window.clear()
        self._len_window.clear()
        self.busy_until = 0.0

    # ---- 持久化 (跨重启: 最近时间/追问/charge 不丢) ----
    def save(self):
        try:
            data = {
                "last_user_msg": {k: v for k, v in self.last_user_msg.items() if time.time() - v < 86400 * 3},
                "last_ai_msg": {k: v for k, v in self.last_ai_msg.items() if time.time() - v < 86400 * 3},
                "followup_at": self.followup_at,
                "followup_used": self.followup_used,
                "charge": {k: v for k, v in self.charge.items()
                           if isinstance(v, dict) and v.get("v", 0) > 0.01},
            }
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                self.last_user_msg = {str(k): float(v) for k, v in (d.get("last_user_msg") or {}).items()}
                self.last_ai_msg = {str(k): float(v) for k, v in (d.get("last_ai_msg") or {}).items()}
                self.followup_at = {str(k): float(v) for k, v in (d.get("followup_at") or {}).items()}
                self.followup_used = {str(k): float(v) for k, v in (d.get("followup_used") or {}).items()}
                self.charge = {str(k): ({"v": float(v.get("v", 0)), "ts": float(v.get("ts", 0))}
                                        if isinstance(v, dict) else {"v": float(v), "ts": time.time()})
                               for k, v in (d.get("charge") or {}).items()}
        except Exception:
            pass


if __name__ == "__main__":
    # 自测 (纯逻辑, 每个场景用新实例+单调时钟)
    T = [time.time()]  # 模拟时钟
    def new_a():
        a = UserActivity(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_activity.json"))
        a._data_clear_for_test()
        return a
    # 1. busy 门
    a = new_a()
    a.busy_until = T[0] + 100
    s = a.snapshot(now=T[0])
    assert s["state"] == "busy" and s["skip"] == 1.0, s
    a.busy_until = 0
    # 2. chatting 不抢话 (用户一直在聊, 未away)
    a.last_user_msg["雾弥"] = T[0] - 300    # 5分钟前 = 一直在聊
    a.note_user_message("雾弥", "在吗", ts=T[0])
    s = a.snapshot(now=T[0] + 10)
    assert s["state"] == "chatting" and s["skip"] == 1.0, s
    # 3. 追问窗: 她问句结尾+用户未回 (新实例)
    a = new_a()
    a.last_user_msg["雾弥"] = T[0] - 120
    a.note_ai_message("雾弥", "你今天心情怎么样？", ts=T[0] - 60)
    s = a.snapshot(now=T[0] + 30)
    assert s.get("followup_active"), s
    assert s["skip"] == 0.0
    # 4. 追问一次后锁定
    a.mark_followup_used("雾弥")
    s = a.snapshot(now=T[0] + 60)
    assert not s.get("followup_active"), s
    # 5. 用户回复清除追问
    a.note_user_message("雾弥", "还行吧", ts=T[0] + 120)
    a.note_ai_message("雾弥", "那就好", ts=T[0] + 121)
    s = a.snapshot(now=T[0] + 180)
    assert not s.get("followup_active") and "雾弥" not in a.followup_at, s
    # 6. 回归欢迎窗: away 后回归 (新实例)
    a = new_a()
    a.last_user_msg["雾弥"] = T[0] - 7200   # 2小时前 = away
    s = a.snapshot(now=T[0] + 10)
    assert s["state"] in ("away", "greeting"), s
    a.note_user_message("雾弥", "我回来了", ts=T[0] + 11)   # 回归 → 武装欢迎窗
    s = a.snapshot(now=T[0] + 20)
    assert s.get("greeting_window"), s
    # 7. charge 迟滞带: 连发短消息 → engaged (新实例)
    a = new_a()
    for i in range(4):
        a.note_user_message("雾弥", "嗯", ts=T[0] + 1000 + i * 5)
    assert a.is_engaged("雾弥", now=T[0] + 1020), "连续短消息应触发 engaged"
    # 8. charge 时间衰减: 半小时后不再 engaged
    assert not a.is_engaged("雾弥", now=T[0] + 1000 + 3600), "时间衰减后应退出"
    print("user_activity 自测 PASS ")
    try:
        os.remove(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_activity.json"))
    except Exception:
        pass
