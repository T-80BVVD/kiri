# -*- coding: utf-8 -*-
"""Kiri 夜间自主循环 (night_loop.py) — 睡前选阶段, 做完再选下一个
=====================================================================
设计 (雾弥 2026-08-17):
  她"睡觉"时不是死寂 — 夜间是她的"自我时间":
  - 睡前阶段: 整理记忆(回放今天→巩固→睡前回想)
  - 做完一个阶段 → LLM 再看状态选下一个 → 循环到早晨
  阶段选择是自主的 (LLM 看情绪/最近整理时间/训练状态决定)
记录: data_log.jsonl (kind=night_stage) — 夜间活动的分析数据
=====================================================================
"""
import time
import json
import re
import logging

import engine
import prompt as prompt_mod
import config as config_mod

logger = logging.getLogger("kiri")

# 配置 (统一在 config.py, 2026-08-30 集中化)
STAGE_INTERVAL = config_mod.NIGHT_STAGE_INTERVAL        # 每阶段约30分钟
MAX_STAGES_PER_NIGHT = config_mod.NIGHT_MAX_STAGES      # 一晚最多10个阶段


class NightLoop:
    def __init__(self, kiri):
        self.kiri = kiri
        self.stage_count = 0
        self.last_stage = 0.0
        self.history = []          # [{stage, reason, result, ts}]

    def should_run(self):
        """睡眠期 + 距上阶段够久 + 未超阶段上限"""
        if not self.kiri.state.is_sleeping():
            return False
        if self.stage_count >= MAX_STAGES_PER_NIGHT:
            return False
        return time.time() - self.last_stage >= STAGE_INTERVAL

    # ---- 阶段选择 ----
    def _choose_stage(self):
        """阶段选择: 固定整理记忆 (osu 训练已移除, 夜间只做记忆巩固)
        ★ 2026-08-30 优化: 原实现每次调 LLM 选阶段, 但代码强制 stage="consolidate"
           (osu 移除后只剩一个选择) — LLM 调用纯浪费, 还产出"练音游"幻觉理由。
           直接返回 consolidate, 每晚省 MAX_STAGES_PER_NIGHT 次 API 调用。"""
        return "consolidate", "整理记忆"

    # ---- 执行阶段 ----
    def run_stage(self):
        """执行一个夜间阶段 → 记录 data_log"""
        stage, reason = self._choose_stage()
        t0 = time.time()
        result = self._do_consolidate()
        latency = round(time.time() - t0, 1)
        self.stage_count += 1
        self.last_stage = time.time()
        self.history.append({"stage": stage, "reason": reason, "result": result[:60],
                             "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        # 记录
        try:
            import data_log
            data_log._write("night_stage", stage=stage, reason=reason,
                            result=str(result)[:150], latency=latency,
                            stage_no=self.stage_count)
        except Exception as e:
            logger.warning(f"夜间阶段 data_log 写入失败: {e!r}")
        logger.info(f"夜间阶段[{stage}] 第{self.stage_count}个: {reason} → {str(result)[:40]}")
        return stage, result

    def _do_consolidate(self):
        """整理记忆: 知识页合成 + 巩固 + 当天总结 + 睡前回想
        ★ 2026-08-30: 各步骤异常不再静默吞掉 — 记 warning, 让"夜间循环在空转"这类
          问题可被发现 (本次 NightLoop 整晚1063次事故就是靠日志才定位的)"""
        parts = []
        # 0. 知识页合成 (Hindsight式: 每用户综合画像, 优先注入) — 记忆核心升级
        try:
            import memory_knowledge
            kb = memory_knowledge.KnowledgeBase(self.kiri.memory)
            for u in self.kiri.memory.users():
                n = kb.synthesize(u)
                if n:
                    parts.append(f"更新了对{u}的了解({n}条画像)")
        except Exception as e:
            logger.warning(f"夜间阶段·知识页合成失败: {e!r}")
        # 1. 记忆巩固 (回放→长期记忆; 有18小时间隔限制)
        try:
            if self.kiri.consolidate_memory():
                parts.append("巩固了今天的重要记忆")
        except Exception as e:
            logger.warning(f"夜间阶段·记忆巩固失败: {e!r}")
        # 2. 当天总结 (如果还没生成)
        try:
            import os
            today = time.strftime("%Y-%m-%d")
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "summaries", f"{today}.md")
            if not os.path.exists(p):
                import summarize
                if summarize.summarize(today, kiri=self.kiri):
                    parts.append("写了今天的日记")
        except Exception as e:
            logger.warning(f"夜间阶段·当天总结失败: {e!r}")
        # 3. 睡前回想 (检索最近记忆, 生成'今晚想到的事' — 像人睡前回想)
        try:
            mems = self.kiri.memory.retrieve("今天发生了什么 最近的事", current_mood=0.0,
                                             n=4, user=self.kiri.DEFAULT_USER)
            if mems:
                mem_txt = "\n".join(f"- {m['text'][:50]}" for m in mems[:3])
                sys_p = ("你是Kiri, 睡前回想今天。看这几条记忆, 写一句今晚最想记住的事"
                         "(10-20字, 像睡前在心里过一遍):")
                thought = engine.generate(sys_p, mem_txt, max_tokens=200, temperature=0.7)
                thought = thought.strip().split("\n")[0].strip()[:60]
                if thought:
                    parts.append(f"睡前想的事: {thought}")
                    try:
                        self.kiri.memory.encode_thought(thought, 0.4, "night", user=self.kiri.DEFAULT_USER)
                    except Exception as e:
                        logger.warning(f"夜间阶段·睡前回想入库失败: {e!r}")
        except Exception as e:
            logger.warning(f"夜间阶段·睡前回想生成失败: {e!r}")
        return "；".join(parts) if parts else "今晚没什么可整理的"
