# -*- coding: utf-8 -*-
"""test_auto_respond.py — 自主回应链路测试 (本地gate真实调用, respond用fake)
验证: 链路通 / 静默留痕 / 频率控制
"""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import qq_bridge as qb

sent = []       # 发出的群消息
heard = []      # 静默留痕
calls = []      # respond 调用


def fake_respond(text, user, scene):
    calls.append((text, user, scene))
    return f"（尾巴晃了晃）{text[:10]}……"


def fake_heard(text, decision, user=None, scene="group"):
    heard.append((text, decision, user))


def make_event(user_id, text):
    return {"post_type": "message", "message_type": "group",
            "group_id": 888, "user_id": user_id,
            "message": [{"type": "text", "data": {"text": text}}]}


async def main():
    import qq_bridge as qb_mod
    # patch 发送和留痕
    async def fake_send_group(self, gid, text):
        sent.append((gid, text))
    qb_mod.QQBridge.send_group = fake_send_group
    import kiri_mind
    kiri_mind.heard = fake_heard

    cfg = {"ws_url": "ws://127.0.0.1:3999", "owner_qq": "10001", "bot_qq": "20002"}
    b = qb_mod.QQBridge(cfg, fake_respond)
    b.AUTO_COOLDOWN = 0  # 测试不禁用冷却

    # 场景1: 无关闲聊 → gate1 false → 静默留痕, 不发
    await b._auto_respond(make_event("33333", "今天作业好多啊写不完了"), "888")
    print(f"场景1(无关): sent={len(sent)} 应0, heard={len(heard)} 应≥1")
    # 场景2: 提到她 → gate1 true → respond → gate2 → 发
    await b._auto_respond(make_event("33333", "你们说Kiri平时都在干嘛"), "888")
    print(f"场景2(提到她): sent={len(sent)} 应≥1, calls={len(calls)}")
    # 场景3: 频率控制 — 冷却期内不再自主回应
    b.AUTO_COOLDOWN = 999999
    n_before = len(sent)
    await b._auto_respond(make_event("33333", "再聊聊Kiri"), "888")
    print(f"场景3(冷却): sent增量={len(sent)-n_before} 应0")

    print("\n=== 留痕记录 ===")
    for h in heard[-4:]:
        print(" ", h)
    ok = len(sent) >= 1 and len(heard) >= 1
    print("\n" + ("✓ 链路通" if ok else "✗ 有问题"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
