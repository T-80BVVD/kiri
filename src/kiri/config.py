# -*- coding: utf-8 -*-
"""Kiri 配置 — MVP v1 (2026-08-16)"""
import os
import threading

# ★ 聊天优先 (2026-08-27): 用户在聊天(respond)时, daemon(联想/主动)让路 — 本地引擎慢, 避免抢 GPU
CHAT_ACTIVE = threading.Event()
CHAT_ACTIVE_TS = [0.0]        # 最近一次置位时间; daemon 检查 "置位且 <120秒" → 让路 (防异常残留自动失效)

# ---- 引擎 ----
# ★ 2026-08-28 雾弥: 后台不再跑本地 14B (agent 架构需云端, 本地慢+占 GPU 影响游戏)
#   已切 api; 如需本地, 改回 "local" 并确保 local_serve 在 8767 跑着
ENGINE = "api"                        # "api" | "local"(本地推理服务)
API_MODEL = "deepseek-v4-flash"     # ★ V4 Flash (便宜快; API仅此+pro两个模型, deepseek-chat旧名已弃)
# ★ 每日 API 费用预算 (2026-08-29, 控制"自主行动"花费; 你@她的对话不受限)
BUDGET_SWEET = 25.0                   # 甜点区上限(元): 正常自主
BUDGET_HARD = 40.0                    # 硬上限(元): 超过自主静默
PRICE_INPUT_PER_M = 1.5               # 输入 元/百万token (DeepSeek V4 Flash 调价后)
PRICE_OUTPUT_PER_M = 4.5              # 输出 元/百万token
LOCAL_MODEL = "deepseek-llm-7b-chat"  # 本地模型(待测试, D:\alice)
LOCAL_OLLAMA_URL = "http://127.0.0.1:11434"  # (弃用, 本地走 local_serve)
LOCAL_SERVE_URL = "http://127.0.0.1:8767"    # ★ 2026-08-22 本地推理服务 (local_serve.py: 4bit+QLoRA)
# ★ 2026-08-30 开源适配: key 读取路径可配置 (engine.py 先环境变量再回退此文件)
#   默认沿用 DSH 凭据文件 (本地开发); 开源部署建议只用环境变量 DEEPSEEK_API_KEY
DSH_CREDENTIALS_PATH = os.path.expanduser(r"~\.dsh\.credentials.yaml")

# ---- 向量记忆 (bge-small-zh 本地模型) ----
# ★ 2026-08-30 开源适配: 原硬编码 D:\models\bge-small-zh-v1.5, 换机器必炸。
#   现在: 环境变量 KIRI_BGE_PATH 优先, 其次此默认值; 模型可从 HuggingFace 下载:
#   https://huggingface.co/BAAI/bge-small-zh-v1.5
BGE_MODEL_PATH = os.environ.get("KIRI_BGE_PATH", r"D:\models\bge-small-zh-v1.5")

# ---- 状态系统 ----
# ★ 事件绑定情绪 (M1, 2026-08-27, EMOTION_EVENT_PLAN.md): 心情从事件记录聚合, 不再是无对象标量
EMOTION_EVENTS_ENABLED = True            # 开关; False = 回退旧 PAD 行为
EMOTION_EVENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "emotion_events.jsonl")
EMOTION_RETENTION_RECENT = 500           # 保留最近 N 条
EMOTION_RETENTION_HIGH_REL = 0.6         # relevance≥此值 长留存
EMOTION_AGG_MIN_DECAY = 0.02             # 聚合时衰减低于此值的事件忽略
EMOTION_SERVE_URL = "http://127.0.0.1:8768"   # M2 情绪小模型
EMOTION_SMALL_TIMEOUT = 10.0          # M2 小模型评价超时(秒); 超时降级规则评价 (14B+3B 共存显存紧, 1.5B 需~8s)
EMOTION_REFINE_AFTER_REPLY = True     # ★ 阉割版 (2026-08-27): 回复生成后异步小模型补评 (聊天关键路径零延迟)
                                      #   True = 入口规则即时 + 事后补评; False = 完全规则 (彻底阉割, 零小模型)
TICK_SECONDS = 15                   # 状态检查间隔(秒)
BOREDOM_RISE_PER_TICK = 0.0004      # ★ 每tick无聊度上升 (修正: 0.02是错的时间尺度, 实际4.8/小时11分钟就顶; 0.0004≈0.1/小时, 1.6h开始无聊6h相当无聊)
BOREDOM_SILENCE_RISE_PER_TICK = 0.0005  # ★ 2026-08-30: 沉默>60min 后的额外无聊上升 (原0.002≈0.48/h 是基准5倍 → 主动轰炸; 0.0005≈0.12/h)
BOREDOM_RELIEF = 0.15               # 互动后无聊度下降
MOOD_REGRESSION = 0.02              # 情绪回归基线

# ---- 主动系统 (意愿驱动, 无预算限制) ----
# ★ 预算制已取消 (雾弥 2026-08-18): 每日12次是"资源配额"思维, 不是人格。
#   频率由 意愿分阈值 + 最小间隔 + 深夜硬禁 + 刚互动抑制 控制 (这些才是"像人"的节制)
WANT_WEIGHTS = (0.5, 0.35, 0.1, 0.05)  # ★ 无聊/沉默/深夜/记忆 (深夜权重降, 避免凌晨骚扰)
WANT_THRESHOLD = 0.18               # ★ 触发阈值 (0.45→0.25, 原值一天触发0次)
MIN_INTERVAL_MINUTES = 30           # ★ 主动间隔下限 (治'准点闹钟': 原60min+无抖动 = 每小时准点一条)
PROACTIVE_JITTER_MIN = 45           # ★ 抖动幅度: 实际间隔 = 下限 + 0~45 分钟随机 → 45~90min 不规律
NIGHT_HOUR = 23                     # 深夜起始
MEMORY_GLOW_PROB = 0.0015           # 每tick记忆涌现概率
MEMORY_GLOW_DECAY = 0.01

# ---- 内心分享 (好奇/念头 → 外发) ----
SHARE_CURIOSITY_PROB = 1.0          # 好奇查到结果 → 生成分享句入队的概率
SHARE_THOUGHT_PROB = 0.15           # 高salience念头 → 想分享的概率 (少量)
SHARE_THOUGHT_SALIENCE = 0.55       # 念头分享的salience门槛
NIGHT_SHARE_BLOCK = True            # ★ 睡眠期严格: 内心分享/主动发言全部禁止 (23-7点)

# ---- 记忆 ----
MEMORY_FILE = os.path.join(os.path.dirname(__file__), "kiri_memory.json")
MAX_MEMORIES = 200

# ---- 睡眠期 & 记忆巩固 ----
SLEEP_START_HOUR = 23                # 睡眠期开始(小时)
SLEEP_END_HOUR = 7                   # 睡眠期结束(小时)
CONSOLIDATE_ONCE_PER_HOURS = 18      # 每多少小时最多巩固一次(≈每天一次)
CONSOLIDATE_MIN_EVENTS = 3           # 最少对话事件才巩固(太少不值得)

# ---- 夜间自主循环 (night_loop.py) ----
# ★ 2026-08-30 统一配置: 原散落在 night_loop.py 顶部
NIGHT_STAGE_INTERVAL = 1800          # 每阶段间隔(秒) ≈30分钟
NIGHT_MAX_STAGES = 10                # 一晚最多阶段数 (23点~7点≈8小时)

# ---- 联想引擎 (reverie.py) ----
# ★ 2026-08-30 统一配置: 原散落在 reverie.py 顶部
REVERIE_INTERVAL_SECONDS = 600       # 联想降频: 每10分钟一次 (内省为辅)
REVERIE_ROUNDS = 2                   # 联想轮数 (更短更精, 少空想)
REVERIE_SALIENCE_THRESHOLD = 0.5     # 念头重要度>=此值写入长期记忆
RECENT_CHEWED_MAX = 20               # 近期嚼过记忆清单长度(防死循环)
REVERIE_MIN_SILENCE_MIN = 2          # 沉默>=2分钟才走神 (聊天时专注对话)
CURIOSITY_EVERY = 3                  # 每3次联想触发一次"好奇→查→学"
CURIOSITY_SEARCH_N = 2               # 每次好奇搜几条
CURIOSITY_MAX_ROUNDS = 3             # agentic好奇: 最多几轮工具调用
GOAL_PUSH_COOLDOWN = 20 * 60         # 推进目标冷却: 20分钟

# ---- 输出/克制 ----
STOP_WORD = "停"                    # 用户说"停"→静默
MAX_OUTPUT_HISTORY = 20             # 防锁死: 输出历史
DEDUP_SIMILARITY = 0.85             # 相似度阈值
