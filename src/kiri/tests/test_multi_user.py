# test_multi_user.py — 临时: 多用户核心测试 (用完即删)
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kiri as kiri_mod

k = kiri_mod.Kiri()

print("=== 1. 记忆分库 ===")
print(f"雾弥库: {k.memory.count('雾弥')} 条")
print(f"阿明库(新): {k.memory.count('阿明')} 条 (应0)")
print(f"小美库(新): {k.memory.count('小美')} 条 (应0)")

print("\n=== 2. 对朋友说话 → 朋友库记记忆 ===")
r1 = k.respond("你好啊，我是阿明，你认识雾弥吗", user="阿明")
print(f"[阿明] 你好啊... → [Kiri] {r1[:70]}")
print(f"阿明库: {k.memory.count('阿明')} 条 (应>0)")

print("\n=== 3. 朋友记忆不污染雾弥 ===")
print(f"雾弥库: {k.memory.count('雾弥')} 条")
print(f"阿明库: {k.memory.count('阿明')} 条")
print(f"用户列表: {k.memory.users()}")

print("\n=== 4. 人格分流: 同一句话对雾弥 vs 对阿明 ===")
import prompt as prompt_mod
import engine
for user in ["雾弥", "阿明"]:
    stage = k.state.relation_stage(user=user)
    mems = k.memory.retrieve("帮我个忙", user=user)
    sys_p = prompt_mod.respond_system(k.state.describe(user=user), mems, None, [], [], stage, user)
    user_p = prompt_mod.respond_user([], "帮我个忙", user=user)
    reply = engine.generate(sys_p, user_p, max_tokens=200, temperature=0.85)
    reply = reply.strip().split("\n")[0].strip()
    print(f"  [{user} 关系={stage}] {reply[:65]}")
