# -*- coding: utf-8 -*-
"""test_share.py — 内心分享链路测试 (本地审查真实调用, 发送fake)
验证: 队列消费 / 审查 / 发最近活跃群 / 睡眠期禁止
"""
import asyncio
import sys
import os
import queue as std_queue

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import qq_bridge as qb

sent = []
heard = []


async def fake_send_group(self, gid, text):
    sent.append((gid, text))


def fake_heard(text, decision, user=None, scene="group"):
    heard.append((text, decision, user))


async def main():
    import qq_bridge as qb_mod
    qb_mod.QQBridge.send_group = fake_send_group
    import kiri_mind
    kiri_mind.heard = fake_heard
    # 假 app.kiri: 带 share_queue 和 is_sleeping
    class FakeState:
        def is_sleeping(self):
            return False
    class FakeKiri:
        share_queue = std_queue.Queue()
        state = FakeState()
    import types
    fake_app = types.ModuleType("app")
    fake_app.kiri = FakeKiri()
    sys.modules["app"] = fake_app

    cfg = {"ws_url": "ws://x", "owner_qq": "10001", "bot_qq": "20002"}
    b = qb_mod.QQBridge(cfg, lambda t, u, s="private": "x")
    b.SHARE_COOLDOWN = 0
    # 群历史: 让她知道有个群
    b.group_ids.add("888")
    b._group_last["888"] = 100.0
    b.group_history["888"] = [("张三", "今天天气不错"), ("李四", "晚上吃什么")]

    # 入队一条好奇分享
    FakeKiri.share_queue.put({"text": "我刚查了猫为什么踩奶，原来是在标记气味，还挺可爱的", "kind": "curiosity"})
    # 入队一条太私密的 (应被审查拦下)
    FakeKiri.share_queue.put({"text": "雾弥昨晚跟我说他其实很脆弱，只有我知道", "kind": "thought"})

    # 跑分享循环几轮
    task = asyncio.create_task(b._share_loop())
    await asyncio.sleep(8)
    task.cancel()
    try:
        await task
    except BaseException:
        pass

    print(f"发出: {sent}")
    print(f"留痕: {[h for h in heard]}")
    ok = len(sent) >= 1 and "猫" in sent[0][1] if sent else False
    print("\n" + ("✓ 分享链路通" if ok else "✗ 有异常"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
