import json
from datetime import datetime

import streamlit as st


def export2md(
    chat_name: str = None,
    user_avatar: str = "User",
    ai_avatar: str = "AI",
    sys_avatar: str = "SYSTEM",
    tool_avatar: str = "Tool",
    user_bg_color: str = "#DCFDC8",
    ai_bg_color: str = "#E0F7FA",
    sys_bg_color: str = "#0EF8FC",
    tool_bg_color: str = "#0AC1AB",
    callback: callable = None,
) -> list[str]:
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

    # 确保导出记录时不会修改历史记录
    history = st.session_state.messages.copy()
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
            elif msg["role"] == "tool":
                content = set_bg_color(content, tool_bg_color)
                avatar = set_bg_color(tool_avatar, tool_bg_color)
            else:
                content = set_bg_color(content, ai_bg_color)
                avatar = set_bg_color(ai_avatar, ai_bg_color)
            line = f"|{avatar}|{content}|\n"
        lines.append(line)
    return lines


sys_prompt = st.session_state.get('sys_prompt',
                                      '你是一个名为 迪小维 的人工智能助手。你是基于迪塔维[Datav]训练的语言模型模型开发的，你的任务是针对用户的问题和要求提供适当的答复和支持。')
DATE_PROMPT = "当前日期: %Y-%m-%d"
TOOL_SYSTEM_PROMPTS = {
    "python": "当你向 `python` 发送包含 Python 代码的消息时，该代码将会在一个有状态的 Jupyter notebook 环境中执行。\n`python` 返回代码执行的输出，或在执行 60 秒后返回超时。\n`/mnt/data` 将会持久化存储你的文件。在此会话中，`python` 无法访问互联网。不要使用 `python` 进行任何网络请求或者在线 API 调用，这些在线内容的访问将不会成功。",
}

def build_system_prompt(
        enabled_tools: list[str],
        functions: list[dict],
):
    value = sys_prompt
    value += "\n\n" + datetime.now().strftime(DATE_PROMPT)
    value += "\n\n# 可用工具"
    contents = []
    for tool in enabled_tools:
        contents.append(f"\n\n## {tool}\n\n{TOOL_SYSTEM_PROMPTS[tool]}")
    for function in functions:
        content = f"\n\n## {function['name']}\n\n{json.dumps(function, ensure_ascii=False, indent=4)}"
        content += "\n在调用上述函数时，请使用 Json 格式表示调用的参数。"
        contents.append(content)
    value += "".join(contents)
    return value

