# -*- coding: utf-8 -*-
"""test_night_loop.py — 夜间循环回归测试 (2026-08-30)
背景事故: app.py daemon_loop 每次 tick 新建 NightLoop 实例 →
stage_count/last_stage/history 全部归零 → 30分钟间隔与每晚10次上限失效,
睡眠期整晚跑了 1063 次 consolidate (日志全是"第1个").
本测试锁定: 单实例复用时 间隔/上限 必须生效; 每次新建则每 tick 都跑 (防回归).

纯逻辑测试, 不调用 run_stage (避免 summarize/data_log 副作用), 只测 should_run.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import night_loop as nl_mod

TICKS = 40          # 模拟 40 个 tick (daemon 每 TICK_SECONDS=15s 一次)
STEP = 15           # 每个 tick 模拟经过的秒数


class FakeState:
    def is_sleeping(self, now=None):
        return True


class FakeKiri:
    def __init__(self):
        self.state = FakeState()


def old_behavior_runs_per_tick():
    """修复前行为: 每次 tick 新建实例 → 每个 tick 都 should_run True"""
    runs = 0
    for _ in range(TICKS):
        nl = nl_mod.NightLoop(FakeKiri())   # 每次新建 (原 daemon_loop 写法)
        if nl.should_run():
            runs += 1
    return runs


def new_behavior_single_instance():
    """修复后行为: 模块级单实例复用 → 30分钟间隔生效, 40 tick 只跑 1 次"""
    nl = nl_mod.NightLoop(FakeKiri())
    runs = 0
    for _ in range(TICKS):
        if nl.should_run():
            runs += 1
            # 模拟 run_stage 完成: 更新 last_stage 与 stage_count (不真跑, 无副作用)
            nl.last_stage = time.time()
            nl.stage_count += 1
    return runs


def test_interval_respected():
    """单实例: 距上阶段 <30分钟 → False; 超过 → True"""
    nl = nl_mod.NightLoop(FakeKiri())
    nl.last_stage = time.time()
    assert nl.should_run() is False, "间隔内不应触发"
    nl.last_stage = time.time() - nl_mod.STAGE_INTERVAL - 1
    assert nl.should_run() is True, "超过30分钟应触发"
    return True


def test_max_stages_cap():
    """每晚最多 MAX_STAGES_PER_NIGHT 次"""
    nl = nl_mod.NightLoop(FakeKiri())
    nl.stage_count = nl_mod.MAX_STAGES_PER_NIGHT
    nl.last_stage = 0.0
    assert nl.should_run() is False, "达到上限不应再触发"
    nl.stage_count = nl_mod.MAX_STAGES_PER_NIGHT - 1
    nl.last_stage = time.time() - nl_mod.STAGE_INTERVAL - 1
    assert nl.should_run() is True, "上限内+间隔到应触发"
    return True


def test_choose_stage_fixed():
    """阶段选择固定为 consolidate (osu 已移除, 不再调 LLM)"""
    stage, reason = nl_mod.NightLoop(FakeKiri())._choose_stage()
    assert stage == "consolidate", f"阶段应为 consolidate, got {stage!r}"
    assert reason, "reason 不应为空"
    return True


def main():
    ok = True
    checks = [
        ("每次新建实例: 40 tick 全触发 (旧bug行为复现)", old_behavior_runs_per_tick(), TICKS),
        ("单实例复用: 40 tick 只触发 1 次 (30分钟间隔生效)", new_behavior_single_instance(), 1),
    ]
    for name, got, want in checks:
        mark = "[PASS]" if got == want else "[FAIL]"
        if got != want:
            ok = False
        print(f"{mark} {name}: {got} (期望 {want})")
    for fn in (test_interval_respected, test_max_stages_cap, test_choose_stage_fixed):
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except AssertionError as e:
            ok = False
            print(f"[FAIL] {fn.__name__}: {e}")
    print("\n" + ("[PASS] 夜间循环回归测试全部通过" if ok else "[FAIL] 有失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
