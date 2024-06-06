import streamlit as st
from typing import List


def export2md(
    chat_name: str = None,
    user_avatar: str = "User",
    ai_avatar: str = "AI",
    sys_avatar: str = "SYSTEM",
    user_bg_color: str = "#DCFDC8",
    ai_bg_color: str = "#E0F7FA",
    sys_bg_color: str = "#0EF8FC",
    callback: callable = None,
) -> List[str]:
    '''
    默认导出消息为文本表格。
    使用 callback(msg) 自定义导出的内容。
    '''
    if 'chat_name' not in st.session_state:
        st.session_state.chat_name = 'default'
    if chat_name is None:
        chat_name = st.session_state.chat_name

    lines = [
        "<style> td, th {border: none!important;}</style>\n",
        "|  |  |\n",
        "|--|--|\n",
    ]

    def set_bg_color(text, bg_color):
        text = text.replace("\n", "<br>")
        return f"<div style=\"background-color:{bg_color}\">{text}</div>"

    history = st.session_state.messages
    for msg in history:
        if callable(callback):
            line = callback(msg)
        else:
            content = msg["content"].replace("\n", "<br>")
            if msg["role"] == "user":
                content = set_bg_color(content, user_bg_color)
                avatar = set_bg_color(user_avatar, user_bg_color)
            elif msg["role"] == "system":
                content = set_bg_color(content, sys_bg_color)
                avatar = set_bg_color(sys_avatar, sys_bg_color)
            else:
                content = set_bg_color(content, ai_bg_color)
                avatar = set_bg_color(ai_avatar, ai_bg_color)
            line = f"|{avatar}|{content}|\n"
        lines.append(line)
    return lines



def reset_history(name=None):
    if 'messages' not in st.session_state:
        st.session_state.messages = []

    if name is None:
        name = 'default'

    st.session_state.messages = [{"role": "assistant", "content": "你好，我是你的文档问答小助手，有什么可以帮助你的？"}]
    st.session_state.chat_displayed = 0  # 重置显示的聊天记录索引

