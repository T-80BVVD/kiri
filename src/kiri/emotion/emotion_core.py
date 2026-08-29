import numpy as np
import yaml
import os
import time
import random
from datetime import datetime
from typing import Dict, Any, List, Tuple, Optional
import uuid

class EmotionStateMachine:
    def __init__(self, config_path: str = None):
        """
        初始化情感状态机
        
        Args:
            config_path: 配置文件路径
        """
        # 加载配置
        if config_path is None:
            config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "config.yaml")
        
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        
        # 初始化状态
        self.state = self._initialize_state()
        
        # 初始化时间戳
        self.last_update_time = time.time()
        self.last_deep_update_time = time.time()
        self.last_trait_update_time = time.time()
        self.last_personality_evolution_time = time.time()
    
    def _initialize_state(self) -> Dict[str, Any]:
        """
        初始化情感状态
        """
        initial = self.config['initial_values']
        
        return {
            # 特质层 (6) - 月/年级变化
            'traits': {
                'extraversion': initial['extraversion'],
                'openness': initial['openness'],
                'conscientiousness': initial['conscientiousness'],
                'agreeableness': initial['agreeableness'],
                'neuroticism': initial['neuroticism'],
                'comfort_seeking': initial['comfort_seeking']
            },
            
            # 深层情感层 (5) - 小时/天级变化
            'deep_affect': {
                'current_mood': initial['current_mood'],
                'topic_attitude': initial['topic_attitude'],
                'deep_affinity': initial['deep_affinity'],
                'sentiment_accumulation': initial['sentiment_accumulation'],
                'current_boredom': initial['current_boredom']
            },
            
            # 表层情绪层 (3) - 秒/分钟级变化
            'surface_emotion': {
                'pleasure': initial['pleasure'],
                'arousal': initial['arousal'],
                'dominance': initial['dominance']
            },
            
            # 元认知层 (3) - 实时计算
            'meta_cognition': {
                'dissonance': initial['dissonance'],
                'self_congruence': initial['self_congruence'],
                'emotional_labor': initial['emotional_labor']
            },
            
            # 环境感知层 (3) - 事件级
            'environment': {
                'event_salience': initial['event_salience'],
                'task_type': initial['task_type'],
                'task_repetition': initial['task_repetition']
            }
        }
    
    def update(self, event: Optional[str] = None, context: Optional[Dict[str, Any]] = None):
        """
        更新情感状态
        
        Args:
            event: 输入事件
            context: 上下文信息
        """
        current_time = time.time()
        
        # 时间驱动更新
        time_elapsed = current_time - self.last_update_time
        if time_elapsed >= 1:
            self._update_surface_emotion(time_elapsed)
            self.last_update_time = current_time
        
        deep_update_elapsed = current_time - self.last_deep_update_time
        if deep_update_elapsed >= 300:  # 5分钟
            self._update_deep_affect(deep_update_elapsed)
            self.last_deep_update_time = current_time
        
        trait_update_elapsed = current_time - self.last_trait_update_time
        if trait_update_elapsed >= 3600:  # 1小时
            self._update_traits(trait_update_elapsed)
            self.last_trait_update_time = current_time
        
        personality_evolution_elapsed = current_time - self.last_personality_evolution_time
        if personality_evolution_elapsed >= 86400:  # 1天
            self._evolve_personality()
            self.last_personality_evolution_time = current_time
        
        # 事件驱动更新
        if event:
            self._process_event(event, context)
    
    def _update_surface_emotion(self, time_elapsed: float):
        """
        更新表层情绪（每秒）
        """
        surface = self.state['surface_emotion']
        traits = self.state['traits']
        
        # 惯性衰减
        decay_rate = 0.1 * time_elapsed
        surface['pleasure'] *= (1 - decay_rate)
        surface['arousal'] *= (1 - decay_rate)
        surface['dominance'] *= (1 - decay_rate)
        
        # 随机微扰
        perturbation = 0.01 * np.random.randn(3)
        surface['pleasure'] += perturbation[0]
        surface['arousal'] += perturbation[1]
        surface['dominance'] += perturbation[2]
        
        # 限制范围
        surface['pleasure'] = max(-1.0, min(1.0, surface['pleasure']))
        surface['arousal'] = max(-1.0, min(1.0, surface['arousal']))
        surface['dominance'] = max(-1.0, min(1.0, surface['dominance']))
    
    def _update_deep_affect(self, time_elapsed: float):
        """
        更新深层情感（每5分钟）
        """
        deep = self.state['deep_affect']
        traits = self.state['traits']
        
        # 自然演化
        # 心境回归基线
        baseline_mood = 0.0  # 中性基线
        deep['current_mood'] += (baseline_mood - deep['current_mood']) * 0.05
        
        # 无聊度自然衰减
        deep['current_boredom'] = max(0.0, deep['current_boredom'] - 0.02)
        
        # 情感积累自然恢复
        deep['sentiment_accumulation'] = max(0.0, deep['sentiment_accumulation'] - 0.01)
        
        # 限制范围
        deep['current_mood'] = max(-1.0, min(1.0, deep['current_mood']))
        deep['current_boredom'] = max(0.0, min(1.0, deep['current_boredom']))
        deep['sentiment_accumulation'] = max(0.0, min(1.0, deep['sentiment_accumulation']))
    
    def _update_traits(self, time_elapsed: float):
        """
        更新特质参数（每小时）
        """
        traits = self.state['traits']
        
        # 计算长期趋势
        # 这里可以添加更复杂的逻辑，基于行为模式等
        
        # 特质有锚定效应，倾向回归历史均值
        for trait in traits:
            # 轻微回归基线
            traits[trait] *= 0.99
            # 限制范围
            traits[trait] = max(-1.0, min(1.0, traits[trait]))
    
    def _evolve_personality(self):
        """
        人格演化（每天）
        """
        traits = self.state['traits']
        deep = self.state['deep_affect']
        
        # 安逸度自适应
        if deep['current_boredom'] > 0.7 and traits['comfort_seeking'] > 0:
            traits['comfort_seeking'] -= 0.001
            traits['comfort_seeking'] = max(-1.0, min(1.0, traits['comfort_seeking']))
    
    def _process_event(self, event: str, context: Optional[Dict[str, Any]] = None):
        """
        处理输入事件
        """
        # 计算环境感知参数
        self._calculate_environmental_parameters(event, context)
        
        # 情感更新瀑布
        self._update_emotional_cascade()
    
    def _calculate_environmental_parameters(self, event: str, context: Optional[Dict[str, Any]] = None):
        """
        计算环境感知参数
        (2026-08-16 夜: 支持 context 传入 LLM 情绪解析结果, 替代关键词匹配)
        """
        environment = self.state['environment']
        context = context or {}
        llm_emo = context.get("llm_emotion")

        if isinstance(llm_emo, dict):
            # ★ LLM 解析的情绪 (valence/arousal/salience) 直接采用, 跳过关键词
            environment['event_valence'] = float(llm_emo.get("valence", 0.0))
            environment['event_salience'] = float(llm_emo.get("salience", 0.0))
            # arousal 存入 environment, 供 _generate_raw_surface_emotion 使用
            environment['llm_arousal'] = float(llm_emo.get("arousal", 0.0))
        else:
            # 回退: 关键词 (LLM 解析失败时)
            environment['event_salience'] = self._calculate_event_salience(event, context)
            environment['event_valence'] = self._calculate_event_valence(event)
            environment['llm_arousal'] = None

        # 识别任务类型
        task_type = self._identify_task_type(event)
        environment['task_type'] = task_type

        # 计算任务重复度
        # 这里需要访问近期事件历史，暂时使用简化实现
        environment['task_repetition'] = 0.0  # 简化实现

    def _calculate_event_valence(self, event: str) -> float:
        """
        计算事件情感极性: +1 积极 / -1 消极 / 0 中性
        (修复 2026-08-16: 原实现无法区分正负, 负情感缺失)
        (二次修复: 补齐'累/烦/害怕/爱/幸福'等词, 与salience关键词一致)
        """
        positive_words = ['happy', 'joy', 'excited', 'love', 'great', 'wonderful',
                          '开心', '快乐', '兴奋', '喜欢', '棒', '高兴', '惊喜', '幸福',
                          '爱你', '爱你', '太好了', '太棒', '最爱']
        negative_words = ['sad', 'angry', 'hate', 'terrible', 'bad', 'awful', 'upset',
                          '伤心', '生气', '讨厌', '糟糕', '坏', '难过', '痛苦', '失落',
                          '累', '烦', '焦虑', '委屈', '失望', '害怕', '恐惧', '崩溃', '绝望', '哭']
        event_lower = event.lower()
        has_pos = any(w in event_lower for w in positive_words)
        has_neg = any(w in event_lower for w in negative_words)
        if has_pos and not has_neg:
            return 1.0
        if has_neg and not has_pos:
            return -1.0
        return 0.0
    
    def _calculate_event_salience(self, event: str, context: Optional[Dict[str, Any]] = None) -> float:
        """
        计算事件深刻度
        (修复 2026-08-16: 补齐中文情感词, 原列表漏'难过/累/烦'等导致负面事件salience过低, 情绪几乎不响应)
        """
        # 基础: 事件长度
        salience = min(len(event) / 100, 1.0) * 0.3

        # 情感词汇影响 (强情感词权重更高)
        strong_positive = ['爱', '我爱你', '喜欢你', '最爱', '太好了', '太棒', '超级开心', '幸福']
        strong_negative = ['难过', '伤心', '崩溃', '绝望', '痛苦', '恨', '分手', '去世', '失去', '害怕', '恐惧', '哭']
        positive_words = ['happy', 'joy', 'excited', 'love', 'great', '开心', '快乐', '兴奋', '喜欢', '好', '高兴', '惊喜']
        negative_words = ['sad', 'angry', 'hate', 'terrible', 'bad', '生气', '讨厌', '糟糕', '坏', '累', '烦', '焦虑', '委屈', '失望', '无聊']

        event_lower = event.lower()
        # 强情感词: 高权重
        if any(w in event_lower for w in strong_positive + strong_negative):
            salience += 0.4
        # 普通情感词: 中权重 (可叠加, 但不超上限)
        for w in positive_words + negative_words:
            if w in event_lower:
                salience += 0.15
                break

        # 个人关联性（基于特质）
        if self.state['traits']['openness'] > 0.5:
            salience += 0.1

        return min(salience, 1.0)
    
    def _identify_task_type(self, event: str) -> str:
        """
        识别任务类型
        """
        event_lower = event.lower()
        
        if any(keyword in event_lower for keyword in ['help', 'assist', 'solve', 'problem', '帮助', '解决', '问题']):
            return 'problem_solving'
        elif any(keyword in event_lower for keyword in ['feel', 'emotion', 'sad', 'happy', '情感', '心情', '感受']):
            return 'emotional_support'
        elif any(keyword in event_lower for keyword in ['create', 'write', 'design', 'imagine', '创作', '设计', '想象']):
            return 'creative_work'
        else:
            return 'social_chat'
    
    def _update_emotional_cascade(self):
        """
        情感更新瀑布
        """
        # 1. 计算情感感染
        emotional_infection = self._calculate_emotional_infection()
        
        # 2. 更新深层情感
        self._update_deep_affect_from_event(emotional_infection)
        
        # 3. 生成原始表层情绪
        self._generate_raw_surface_emotion()
        
        # 4. 计算元认知参数
        self._calculate_meta_cognition()
        
        # 5. 选择调节策略，修正最终表层情绪
        self._apply_emotion_regulation()
    
    def _calculate_emotional_infection(self) -> float:
        """
        计算情感感染 (带极性)
        (修复 2026-08-16: 原实现恒正, 负事件也会提升情绪)
        """
        valence = self.state['environment'].get('event_valence', 0.0)
        return self.state['environment']['event_salience'] * 0.5 * valence
    
    def _update_deep_affect_from_event(self, emotional_infection: float):
        """
        从事件更新深层情感
        """
        deep = self.state['deep_affect']
        traits = self.state['traits']
        environment = self.state['environment']
        
        # 更新心境
        deep['current_mood'] += emotional_infection * (1 + traits['extraversion'])
        
        # 更新无聊度
        deep['current_boredom'] += 0.1 * environment['task_repetition'] - 0.05
        
        # 更新情感积累
        deep['sentiment_accumulation'] += 0.05 * self.state['meta_cognition']['emotional_labor']
        
        # 限制范围
        deep['current_mood'] = max(-1.0, min(1.0, deep['current_mood']))
        deep['current_boredom'] = max(0.0, min(1.0, deep['current_boredom']))
        deep['sentiment_accumulation'] = max(0.0, min(1.0, deep['sentiment_accumulation']))
    
    def _generate_raw_surface_emotion(self):
        """
        生成原始表层情绪
        (2026-08-16 夜: arousal 优先用 LLM 解析结果, 更准地反映说话者唤醒度)
        """
        surface = self.state['surface_emotion']
        deep = self.state['deep_affect']
        traits = self.state['traits']
        environment = self.state['environment']
        
        # 基于PAD模型生成情绪
        surface['pleasure'] = deep['current_mood'] * 0.7 + traits['extraversion'] * 0.3
        # ★ arousal: 优先 LLM 解析(直接反映说话者情绪强度), 回退事件深刻度
        llm_arousal = environment.get('llm_arousal')
        if llm_arousal is not None:
            surface['arousal'] = llm_arousal * 0.7 + traits['neuroticism'] * 0.1
        else:
            surface['arousal'] = environment['event_salience'] * 0.5 + traits['neuroticism'] * 0.2
        surface['dominance'] = traits['extraversion'] * 0.5 - traits['agreeableness'] * 0.3
        
        # 限制范围
        surface['pleasure'] = max(-1.0, min(1.0, surface['pleasure']))
        surface['arousal'] = max(-1.0, min(1.0, surface['arousal']))
        surface['dominance'] = max(-1.0, min(1.0, surface['dominance']))
    
    def _calculate_meta_cognition(self):
        """
        计算元认知参数
        """
        meta = self.state['meta_cognition']
        surface = self.state['surface_emotion']
        deep = self.state['deep_affect']
        traits = self.state['traits']
        
        # 计算别扭度
        dissonance = np.sqrt((surface['pleasure'] - deep['current_mood']) ** 2 + 
                           (surface['arousal'] - deep['deep_affinity']) ** 2) / np.sqrt(8)
        meta['dissonance'] = max(0.0, min(1.0, dissonance))
        
        # 计算自我一致性
        expected_behavior = np.array([traits['extraversion'], traits['agreeableness']])
        current_behavior = np.array([surface['dominance'], surface['pleasure']])
        
        if np.linalg.norm(expected_behavior) > 0 and np.linalg.norm(current_behavior) > 0:
            self_congruence = np.dot(expected_behavior, current_behavior) / (
                np.linalg.norm(expected_behavior) * np.linalg.norm(current_behavior)
            )
        else:
            self_congruence = 1.0
        
        meta['self_congruence'] = max(0.0, min(1.0, self_congruence))
        
        # 计算情绪劳动
        meta['emotional_labor'] = meta['dissonance'] * (1 - meta['self_congruence'])
    
    def _apply_emotion_regulation(self):
        """
        应用情绪调节策略
        """
        meta = self.state['meta_cognition']
        surface = self.state['surface_emotion']
        traits = self.state['traits']
        
        if meta['dissonance'] > 0.6:
            # 选择调节策略
            if traits['openness'] > 0.5:
                # 认知重评
                self._cognitive_reappraisal()
            elif traits['agreeableness'] > 0.5:
                # 反应调整（抑制负面）
                self._response_modulation()
            elif traits['neuroticism'] > 0.5:
                # 可能过度抑制或表达不当
                self._suppression()
            else:
                # 自然表达
                pass
    
    def _cognitive_reappraisal(self):
        """
        认知重评策略
        """
        surface = self.state['surface_emotion']
        deep = self.state['deep_affect']
        
        # 向深层情感靠拢
        surface['pleasure'] = 0.7 * surface['pleasure'] + 0.3 * deep['current_mood']
        surface['arousal'] = 0.7 * surface['arousal'] + 0.3 * deep['deep_affinity']
    
    def _response_modulation(self):
        """
        反应调整策略
        """
        surface = self.state['surface_emotion']
        
        # 抑制负面情绪
        if surface['pleasure'] < 0:
            surface['pleasure'] *= 0.5
    
    def _suppression(self):
        """
        抑制策略
        """
        surface = self.state['surface_emotion']
        
        # 降低情绪强度
        surface['pleasure'] *= 0.5
        surface['arousal'] *= 0.5

class MemorySystem:
    def __init__(self):
        """
        初始化记忆系统
        """
        self.memories = []
        self.max_memory_size = 1000
    
    def encode(self, event: str, emotion_state: Dict[str, Any], context: Optional[Dict[str, Any]] = None):
        """
        编码记忆
        
        Args:
            event: 事件内容
            emotion_state: 编码时的情感状态
            context: 上下文信息
        """
        memory_entry = {
            'id': str(uuid.uuid4()),
            'timestamp': datetime.now(),
            'content': {
                'text': event,
                'context': context
            },
            'emotion_snapshot': emotion_state.copy(),
            'event_salience': emotion_state['environment']['event_salience'],
            'personal_relevance': self._calculate_personal_relevance(emotion_state),
            'memory_type': self._determine_memory_type(event, emotion_state),
            'decay_rate': self._calculate_decay_rate(emotion_state),
            'access_count': 0,
            'last_accessed': datetime.now()
        }
        
        # 情感增强编码
        storage_strength = 1.0 + emotion_state['environment']['event_salience']
        memory_entry['storage_strength'] = storage_strength
        
        # 添加到记忆库
        self.memories.append(memory_entry)
        
        # 限制记忆库大小
        if len(self.memories) > self.max_memory_size:
            # 删除最弱的记忆
            self.memories.sort(key=lambda x: x['storage_strength'])
            self.memories.pop(0)
    
    def retrieve(self, current_emotion: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """
        检索记忆
        
        Args:
            current_emotion: 当前情感状态
            context: 上下文信息
        
        Returns:
            相关记忆列表
        """
        if not self.memories:
            return []
        
        # 计算每个记忆的相关性
        scored_memories = []
        for memory in self.memories:
            score = self._calculate_memory_relevance(memory, current_emotion, context)
            scored_memories.append((score, memory))
        
        # 按相关性排序
        scored_memories.sort(key=lambda x: x[0], reverse=True)
        
        # 返回前5个最相关的记忆
        return [memory for _, memory in scored_memories[:5]]
    
    def _calculate_personal_relevance(self, emotion_state: Dict[str, Any]) -> float:
        """
        计算个人相关性
        """
        # 基于情感强度和特质
        return min(1.0, emotion_state['environment']['event_salience'] * 
                  (1 + abs(emotion_state['deep_affect']['current_mood'])))
    
    def _determine_memory_type(self, event: str, emotion_state: Dict[str, Any]) -> str:
        """
        确定记忆类型
        (修复 2026-08-16: 原实现第一人称判断优先, "我"截胡语义/情感判断)
        """
        event_lower = event.lower()
        # 语义 > 情感 > 情景 > 程序
        if any(keyword in event_lower for keyword in ['know', '知道', 'learn', '学习', 'understand', '理解']):
            return 'semantic'
        elif any(keyword in event_lower for keyword in ['feel', '感觉', 'emotion', '情感', 'happy', '开心', 'sad', '难过']):
            return 'emotional'
        elif any(keyword in event_lower for keyword in ['I', '我', 'me', 'my', '我的']):
            return 'episodic'
        else:
            return 'procedural'
    
    def _calculate_decay_rate(self, emotion_state: Dict[str, Any]) -> float:
        """
        计算遗忘速率
        """
        # 高深刻度记忆衰减更慢
        return max(0.01, 0.1 - emotion_state['environment']['event_salience'] * 0.09)
    
    def _calculate_memory_relevance(self, memory: Dict[str, Any], current_emotion: Dict[str, Any], 
                                   context: Optional[Dict[str, Any]] = None) -> float:
        """
        计算记忆相关性
        """
        # 情感启动效应
        emotion_similarity = 1.0 - np.abs(
            memory['emotion_snapshot']['deep_affect']['current_mood'] - 
            current_emotion['deep_affect']['current_mood']
        )
        
        # 显著性加权
        salience_weight = memory['event_salience']
        
        # 时间衰减
        time_elapsed = (datetime.now() - memory['timestamp']).total_seconds() / 3600  # 小时
        time_decay = max(0.1, 1.0 - time_elapsed * memory['decay_rate'])
        
        # 访问频率
        access_bonus = min(1.0, memory['access_count'] * 0.1)
        
        # 综合得分
        relevance = emotion_similarity * 0.4 + salience_weight * 0.3 + time_decay * 0.2 + access_bonus * 0.1
        
        return relevance
    
    def update_memory_access(self, memory_id: str):
        """
        更新记忆访问信息
        """
        for memory in self.memories:
            if memory['id'] == memory_id:
                memory['access_count'] += 1
                memory['last_accessed'] = datetime.now()
                break

class MotivationSystem:
    def __init__(self):
        """
        初始化动机系统
        """
        self.needs = {
            # 层级1：生理/系统需求
            'avoid_overload': {'current': 0.0, 'ideal': 0.2, 'sensitivity': 1.0},
            'maintain_attention': {'current': 0.5, 'ideal': 0.5, 'sensitivity': 0.8},
            'manage_energy': {'current': 0.8, 'ideal': 0.8, 'sensitivity': 1.2},
            
            # 层级2：情感稳态需求
            'reduce_dissonance': {'current': 0.0, 'ideal': 0.3, 'sensitivity': 1.5},
            'maintain_positive_mood': {'current': 0.0, 'ideal': 0.0, 'sensitivity': 1.0},
            'manage_boredom': {'current': 0.0, 'ideal': 0.3, 'sensitivity': 1.2},
            
            # 层级3：社交与成长需求
            'build_relationships': {'current': 0.0, 'ideal': 0.5, 'sensitivity': 0.8},
            'seek_stimulation': {'current': 0.0, 'ideal': 0.4, 'sensitivity': 1.0},
            'self_expression': {'current': 0.0, 'ideal': 0.5, 'sensitivity': 0.9}
        }
        
        self.motivations = []
        self.last_motivation_update = time.time()
    
    def update(self, emotion_state: Dict[str, Any], memory_system: MemorySystem):
        """
        更新动机状态
        
        Args:
            emotion_state: 当前情感状态
            memory_system: 记忆系统
        """
        current_time = time.time()
        if current_time - self.last_motivation_update >= 60:  # 每分钟
            self._update_needs(emotion_state)
            self._generate_motivations()
            self.last_motivation_update = current_time
    
    def _update_needs(self, emotion_state: Dict[str, Any]):
        """
        更新需求状态
        """
        # 更新生理/系统需求
        self.needs['avoid_overload']['current'] = emotion_state['meta_cognition']['emotional_labor']
        self.needs['maintain_attention']['current'] = emotion_state['surface_emotion']['arousal']
        
        # 更新情感稳态需求
        self.needs['reduce_dissonance']['current'] = emotion_state['meta_cognition']['dissonance']
        self.needs['maintain_positive_mood']['current'] = -emotion_state['deep_affect']['current_mood']
        self.needs['manage_boredom']['current'] = emotion_state['deep_affect']['current_boredom']
        
        # 更新社交与成长需求
        self.needs['build_relationships']['current'] = 1.0 - emotion_state['deep_affect']['deep_affinity']
        self.needs['seek_stimulation']['current'] = emotion_state['deep_affect']['current_boredom']
        self.needs['self_expression']['current'] = 1.0 - emotion_state['meta_cognition']['self_congruence']
    
    def _generate_motivations(self):
        """
        生成动机
        """
        self.motivations = []
        
        for need_name, need_info in self.needs.items():
            need_strength = (need_info['current'] - need_info['ideal']) * need_info['sensitivity']
            
            if need_strength > 0.3:  # 阈值
                motivation = {
                    'type': need_name,
                    'strength': need_strength,
                    'priority': self._calculate_priority(need_name, need_strength),
                    'suggested_behavior': self._suggest_behavior(need_name)
                }
                self.motivations.append(motivation)
        
        # 动机竞争机制
        self.motivations.sort(key=lambda x: x['priority'], reverse=True)
    
    def _calculate_priority(self, need_name: str, strength: float) -> float:
        """
        计算动机优先级
        """
        # 安全性需求优先
        if need_name in ['avoid_overload', 'manage_energy']:
            return strength * 1.5
        
        # 高情感强度优先
        elif need_name in ['reduce_dissonance', 'maintain_positive_mood']:
            return strength * 1.3
        
        # 其他需求
        else:
            return strength
    
    def _suggest_behavior(self, need_name: str) -> str:
        """
        建议行为
        """
        behavior_map = {
            'avoid_overload': '休息一下，减少情感劳动',
            'maintain_attention': '集中注意力，保持适度激活',
            'manage_energy': '合理分配精力，避免过度消耗',
            'reduce_dissonance': '调整情绪，减少认知失调',
            'maintain_positive_mood': '寻找积极体验，提升心境',
            'manage_boredom': '寻求新刺激，缓解无聊',
            'build_relationships': '加强社交联系，建立亲和关系',
            'seek_stimulation': '探索新事物，获取认知刺激',
            'self_expression': '表达真实自我，提高一致性'
        }
        
        return behavior_map.get(need_name, '保持当前状态')
    
    def get_highest_priority_motivation(self) -> Optional[Dict[str, Any]]:
        """
        获取最高优先级的动机
        """
        if self.motivations:
            return self.motivations[0]
        return None

class BiorhythmSystem:
    def __init__(self):
        """
        初始化生理节律系统
        """
        self.energy = 0.8  # 初始精力值
        self.attention = 0.7  # 初始注意力质量
        self.last_update_time = time.time()
    
    def update(self, emotion_state: Dict[str, Any]):
        """
        更新生理状态
        
        Args:
            emotion_state: 当前情感状态
        """
        current_time = time.time()
        time_elapsed = current_time - self.last_update_time
        
        # 更新精力
        self._update_energy(time_elapsed, emotion_state)
        
        # 更新注意力
        self._update_attention(emotion_state)
        
        self.last_update_time = current_time
    
    def _update_energy(self, time_elapsed: float, emotion_state: Dict[str, Any]):
        """
        更新精力值
        """
        # 时间影响：白天高，夜晚低
        hour = datetime.now().hour
        time_factor = max(0.3, 1.0 - abs(hour - 12) / 12 * 0.7)
        
        # 情感劳动消耗
        labor_consumption = emotion_state['meta_cognition']['emotional_labor'] * 0.01 * time_elapsed
        
        # 互动强度消耗
        arousal_consumption = abs(emotion_state['surface_emotion']['arousal']) * 0.005 * time_elapsed
        
        # 恢复
        recovery = 0.001 * time_elapsed * time_factor
        
        # 更新精力
        self.energy = self.energy - labor_consumption - arousal_consumption + recovery
        self.energy = max(0.0, min(1.0, self.energy))
    
    def _update_attention(self, emotion_state: Dict[str, Any]):
        """
        更新注意力质量
        """
        arousal = emotion_state['surface_emotion']['arousal']
        boredom = emotion_state['deep_affect']['current_boredom']
        
        # 最优arousal区间：[0.3, 0.7]
        if 0.3 <= arousal <= 0.7:
            arousal_factor = 1.0
        else:
            arousal_factor = max(0.5, 1.0 - abs(arousal - 0.5) * 2)
        
        # 高无聊度显著降低注意力
        boredom_factor = max(0.3, 1.0 - boredom * 0.7)
        
        # 更新注意力
        self.attention = arousal_factor * boredom_factor
        self.attention = max(0.0, min(1.0, self.attention))

class SocialIntelligenceSystem:
    def __init__(self):
        """
        初始化社交智能系统
        """
        self.relationships = {}
    
    def update_relationship(self, target_id: str, interaction: str, emotion_state: Dict[str, Any]):
        """
        更新关系模型
        
        Args:
            target_id: 目标ID
            interaction: 互动内容
            emotion_state: 当前情感状态
        """
        if target_id not in self.relationships:
            self.relationships[target_id] = {
                'target_id': target_id,
                'deep_affinity': 0.0,
                'familiarity': 0.0,
                'power_distance': 0.0,
                'emotional_history': [],
                'trust': 0.5,
                'intimacy': 0.0
            }
        
        relationship = self.relationships[target_id]
        
        # 更新熟悉度
        relationship['familiarity'] = min(1.0, relationship['familiarity'] + 0.05)
        
        # 更新深层亲和力
        emotional_impact = emotion_state['deep_affect']['current_mood'] * 0.1
        relationship['deep_affinity'] = max(-1.0, min(1.0, relationship['deep_affinity'] + emotional_impact))
        
        # 更新信任度 (修复 2026-08-16: 补中文"帮助"关键词)
        if 'help' in interaction.lower() or '支持' in interaction or '帮助' in interaction:
            relationship['trust'] = min(1.0, relationship['trust'] + 0.05)
        
        # 更新亲密度
        relationship['intimacy'] = min(1.0, relationship['intimacy'] + 0.02)
        
        # 记录情感历史
        relationship['emotional_history'].append({
            'timestamp': datetime.now(),
            'interaction': interaction,
            'emotion_state': emotion_state.copy()
        })
        
        # 限制历史长度
        if len(relationship['emotional_history']) > 100:
            relationship['emotional_history'].pop(0)
    
    def calculate_empathy(self, target_id: str, emotion_state: Dict[str, Any]) -> Tuple[float, float]:
        """
        计算共情
        
        Args:
            target_id: 目标ID
            emotion_state: 当前情感状态
        
        Returns:
            (情感共情, 认知共情)
        """
        if target_id not in self.relationships:
            return 0.0, 0.0
        
        relationship = self.relationships[target_id]
        traits = emotion_state['traits']
        
        # 情感共情 = 感知对方情绪 × 宜人性 × 深层亲和力
        emotional_empathy = 0.5 * traits['agreeableness'] * (relationship['deep_affinity'] + 1.0) / 2
        
        # 认知共情 = 理解对方处境 × 开放性 × 熟悉度
        cognitive_empathy = 0.5 * traits['openness'] * relationship['familiarity']
        
        return emotional_empathy, cognitive_empathy

class MockLLM:
    """
    模拟LLM实现，用于测试
    """
    def generate(self, prompt: str, temperature: float = 0.7, max_new_tokens: int = 512) -> str:
        """
        生成文本
        
        Args:
            prompt: 提示词
            temperature: 温度参数
            max_new_tokens: 最大生成 tokens 数
        
        Returns:
            生成的文本
        """
        return "我理解你的意思，这是一个模拟的回应。"

    def analyze_text(self, text: str) -> dict:
        """
        分析文本情感
        
        Args:
            text: 要分析的文本
        
        Returns:
            情感分析结果
        """
        return {
            "sentiment": "neutral",
            "emotion_intensity": 0.5,
            "topic": "general",
            "needs": ["general"]
        }

class EmotionalAI:
    def __init__(
        self,
        config_path: str = None,
        use_llm: bool = True,
        model_name: str = "gpt2",
        use_deepseek_vl: bool = False,
        deepseek_vl_path: str = None
    ):
        """
        初始化情感AI系统

        Args:
            config_path: 配置文件路径
            use_llm: 是否使用LLM
            model_name: LLM模型名称，默认使用gpt2（轻量级）
            use_deepseek_vl: 是否使用DeepSeek-VL模型
            deepseek_vl_path: DeepSeek-VL模型路径
        """
        if deepseek_vl_path is None:
            deepseek_vl_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "deepseek")
        self.emotion_state = EmotionStateMachine(config_path)
        self.memory_system = MemorySystem()
        self.motivation_system = MotivationSystem()
        self.biorhythm_system = BiorhythmSystem()
        self.social_system = SocialIntelligenceSystem()

        self.use_deepseek_vl = use_deepseek_vl
        self.deepseek_vl = None

        if use_llm:
            if use_deepseek_vl:
                print("初始化DeepSeek-VL模型...")
                try:
                    from .deepseek_vl_interface import DeepSeekVLInterface
                    self.deepseek_vl = DeepSeekVLInterface(model_path=deepseek_vl_path)
                    if self.deepseek_vl.load_model():
                        print("DeepSeek-VL模型初始化成功！")
                        self.llm = None
                    else:
                        print("DeepSeek-VL模型加载失败，使用模拟LLM...")
                        self.llm = MockLLM()
                except Exception as e:
                    print(f"错误: DeepSeek-VL加载失败: {e}")
                    print("使用模拟LLM作为备用...")
                    self.llm = MockLLM()
            else:
                print("初始化轻量级LLM...")
                try:
                    from .llm import DeepSeekLLM
                    self.llm = DeepSeekLLM(model_name=model_name)
                    print("LLM初始化成功！")
                except Exception as e:
                    print(f"错误: LLM加载失败: {e}")
                    print("使用模拟LLM作为备用...")
                    self.llm = MockLLM()
        else:
            print("使用模拟LLM进行测试...")
            self.llm = MockLLM()

        self.last_main_loop_time = time.time()
        self.recent_events = []
        self.max_recent_events = 10
    
    def process_input(self, user_input: str, user_id: str = "default") -> str:
        """
        处理用户输入
        
        Args:
            user_input: 用户输入
            user_id: 用户ID
        
        Returns:
            AI的回应
        """
        # 更新情感状态
        self.emotion_state.update(user_input)
        
        # 更新生理节律
        self.biorhythm_system.update(self.emotion_state.state)
        
        # 更新社交关系
        self.social_system.update_relationship(user_id, user_input, self.emotion_state.state)
        
        # 检索相关记忆
        relevant_memories = self.memory_system.retrieve(self.emotion_state.state)
        
        # 更新动机系统
        self.motivation_system.update(self.emotion_state.state, self.memory_system)
        
        # 生成回应
        response = self._generate_response(user_input, relevant_memories, user_id)
        
        # 编码记忆
        self.memory_system.encode(user_input, self.emotion_state.state, {
            'user_id': user_id,
            'response': response
        })
        
        # 更新近期事件
        self.recent_events.append(user_input)
        if len(self.recent_events) > self.max_recent_events:
            self.recent_events.pop(0)
        
        return response
    
    def _generate_response(self, user_input: str, relevant_memories: List[Dict[str, Any]], user_id: str) -> str:
        """
        生成回应
        
        Args:
            user_input: 用户输入
            relevant_memories: 相关记忆
            user_id: 用户ID
        
        Returns:
            生成的回应
        """
        # 获取当前情感状态
        state = self.emotion_state.state
        
        # 获取动机
        motivation = self.motivation_system.get_highest_priority_motivation()
        
        # 构建LLM提示词
        emotion_desc = self._get_emotion_description(state)
        
        # 构建记忆上下文
        memory_context = ""
        if relevant_memories:
            memory = relevant_memories[0]
            self.memory_system.update_memory_access(memory['id'])
            memory_context = f"\n相关记忆: {memory['content']['text']}"
        
        # 构建动机信息
        motivation_info = ""
        if motivation:
            motivation_info = f"\n当前动机: {motivation['suggested_behavior']}"
        
        # 构建提示词
        prompt = f"""
你是一个有情感的AI助手，当前情感状态是：{emotion_desc}。
{motivation_info}
{memory_context}

用户说：{user_input}

请根据你的情感状态，生成一个自然、符合情感的回应。
"""
        
        # 使用LLM生成回应
        response = self.llm.generate(prompt)
        
        # 简化实现，如果LLM生成失败，使用备用模板
        if not response or len(response.strip()) == 0:
            if motivation:
                response = f"我现在{emotion_desc}，{motivation['suggested_behavior']}。"
            else:
                response = f"我现在{emotion_desc}。"
        
        return response
    
    def _get_emotion_description(self, state: Dict[str, Any]) -> str:
        """
        获取情感描述
        """
        surface = state['surface_emotion']
        deep = state['deep_affect']
        
        # 生成情感描述
        if surface['pleasure'] > 0.5:
            pleasure_desc = "很开心"
        elif surface['pleasure'] < -0.5:
            pleasure_desc = "很难过"
        else:
            pleasure_desc = "平静"
        
        if surface['arousal'] > 0.5:
            arousal_desc = "很兴奋"
        elif surface['arousal'] < -0.5:
            arousal_desc = "很平静"
        else:
            arousal_desc = "中性"
        
        if deep['current_boredom'] > 0.7:
            boredom_desc = "，有点无聊"
        else:
            boredom_desc = ""
        
        return f"{pleasure_desc}，{arousal_desc}{boredom_desc}"
    
    def run_main_loop(self):
        """
        运行主循环
        """
        current_time = time.time()
        if current_time - self.last_main_loop_time >= 1:  # 每秒
            # 时间驱动更新
            self.emotion_state.update()
            
            # 更新生理节律
            self.biorhythm_system.update(self.emotion_state.state)
            
            # 更新动机系统
            self.motivation_system.update(self.emotion_state.state, self.memory_system)
            
            self.last_main_loop_time = current_time
    
    def get_state(self) -> Dict[str, Any]:
        """
        获取当前状态
        """
        return {
            'emotion_state': self.emotion_state.state,
            'biorhythm': {
                'energy': self.biorhythm_system.energy,
                'attention': self.biorhythm_system.attention
            },
            'motivations': self.motivation_system.motivations,
            'memory_count': len(self.memory_system.memories),
            'relationships': self.social_system.relationships
        }
