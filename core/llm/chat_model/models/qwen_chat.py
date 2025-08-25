# coding=utf-8
"""
@project: multirag
@Author：龙
@file： qwen_chat.py
@date：2025/7/16 18:00
@desc:
"""
from dashscope import Generation

from core.llm.chat_model.base import Base


class QWenChat(Base):
    def __init__(self, key, model_name=Generation.Models.qwen_turbo, base_url=None, **kwargs):
        if not base_url:
            base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        super().__init__(key, model_name, base_url=base_url, **kwargs)
        # Set default max_length for QWenChat, can be overridden by config
        self.max_length = kwargs.get("max_length", 8192)
        return