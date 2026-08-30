# -*- coding: utf-8 -*-
"""清理夜间循环事故写入的垃圾记忆 (source=night 的念头, 2026-08-30)
背景: app.py daemon_loop 每次 tick 重建 NightLoop → 睡眠期整晚跑了1063次
consolidate, 每次 encode_thought(睡前想的事, source="night") 写一条 →
thoughts collection 里堆了 1063 条重复"睡前想的事"垃圾 (freshness权重0.45,
检索时最先浮现, 污染"想起"/内心独白).

用法: 在 Kiri 停止时运行:  python _clean_night_mem.py
- 扫描所有 collection, 删除 source=night 的条目
- 删除前把前5条样本写入 _night_garbage_sample.txt (留档)
- 打印删除前后各 collection 计数
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "kiri"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import chromadb

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "src", "kiri", "mem_db")
SAMPLE = os.path.join(BASE, "_night_garbage_sample.txt")


def main():
    client = chromadb.PersistentClient(path=DB_PATH)
    cols = client.list_collections()
    total = 0
    print("--- 清理前各 collection 计数 ---")
    for col in cols:
        print(f"  {col.name}: {col.count()}")
    print()
    for col in cols:
        try:
            got = col.get(where={"source": "night"})
        except Exception as e:
            print(f"[skip] {col.name}: {e}")
            continue
        ids = got.get("ids") or []
        n = len(ids)
        if n == 0:
            print(f"[ok]   {col.name}: 无 night 垃圾")
            continue
        print(f"[hit]  {col.name}: {n} 条 source=night → 删除")
        docs = got.get("documents") or []
        with open(SAMPLE, "a", encoding="utf-8") as f:
            for d in docs[:5]:
                f.write(f"[{col.name}] {str(d)[:120]}\n")
        for i in range(0, n, 500):   # 分批删, 防一次太多
            col.delete(ids=ids[i:i + 500])
        total += n
    print(f"\n共删除 {total} 条 night 垃圾 (样本留档: {SAMPLE})")
    print("\n--- 清理后各 collection 计数 ---")
    for col in client.list_collections():
        print(f"  {col.name}: {col.count()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
