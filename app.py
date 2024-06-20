import base64
import datetime
import json
import os
import re
from enum import Enum
from uuid import uuid4
import streamlit as st
from PIL import Image

from core.file.utils import extract_pdf, extract_docx, extract_pptx, extract_text
from core.llm.ocr_model.ocr_factory import ModelFactory
from core.tools.tools_registry import get_tools, ALL_TOOLS
from utils.api import get_ai_response, process_user_input
from configs import VERSION
from web_ui.dialogue.dialogue import export2md, build_system_prompt

# Set Streamlit page configuration
st.set_page_config(
    page_title="MultiRAG",
    page_icon="🧸",  # 🧊
    layout="centered",
    initial_sidebar_state="auto",
    menu_items={
        'Get Help': 'https://cake-doom-0c6.notion.site/4b6c4b3a5338497494620b3dd82e4acc?pvs=4',
        'Report a bug': "https://cake-doom-0c6.notion.site/BUG-cb6ea80282fc4de49d58ff96b4c5431a?pvs=4",
        'About': "- 当前页面可以支持LLM、文件、图片自由对话!"
    })


class Mode(str, Enum):
    ALL_TOOLS = "🛠️ All Tools[有BUG]"
    LONG_CTX = "📝 文件解读"
    # GLM4 = "🖼️ 多模态"
    VLM = "🖼️ 多模态[未实现]"


# name = st.text_input('Name')
# if not name or name!= "杜晓龙":
#   st.warning('Please input a real name.')
#   st.stop()
# st.success('Thank you for inputting a name.')
default_model = 'GLM-4-520'

HELP = """
### 🎉 欢迎使用 MultiRAG!【文档对话版】
请在下方选取一个功能。
""".strip()

st.markdown(HELP)

page = st.radio(
    "🐖🔢每次切换功能时，请先手动清空对话历史。【后续将会优化：自动重新加载LLM并清空对话历史】",
    [mode.value for mode in Mode],
    key="page",
    horizontal=True,
    index=None,
    label_visibility="visible",
    # on_change=page_changed,
)
# exit()

# Function to save chat history
# Function to save chat history
def save_chat_history(history, custom_name=None, user_id='admin', file_path=None, rename=False):
    first_user_message = history[1]["content"] if len(history) > 1 and history[1]["role"] == "user" else "session"
    sanitized_message = "".join([c if c.isalnum() else " " for c in first_user_message])
    truncated_message = sanitized_message[:10].strip()
    if custom_name:
        sanitized_custom_name = "".join([c if c.isalnum() else " " for c in custom_name])
        truncated_message = sanitized_custom_name[:10].strip()
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    if file_path and rename:
        new_file_name = f"{os.path.dirname(file_path)}/{timestamp}-{truncated_message}.json"
        os.rename(file_path, new_file_name)
    else:
        new_file_name = f"./workspace/{user_id}/{timestamp}-{truncated_message}.json"
    os.makedirs(os.path.dirname(new_file_name), exist_ok=True)
    with open(new_file_name, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=4)

# Function to load chat history
def load_chat_history(user_id='admin'):
    history_dir = f"./workspace/{user_id}"
    if not os.path.exists(history_dir):
        os.makedirs(history_dir)
    history_files = [f for f in os.listdir(history_dir) if f.endswith('.json')]
    history_files.sort(key=lambda x: x.split('-')[0], reverse=True)  # Sort by timestamp in descending order
    return history_files

# Function to display chat history
def display_chat_history(file_name):
    with open(file_name, 'r', encoding='utf-8') as f:
        history = json.load(f)
    for message in history:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
    return history


api_key = "7ae32940233e38153d5ebaf94844f3e2.gwrz4P0tH9IDijUv"  # 7ae32940233e38153d5ebaf94844f3e2.gwrz4P0tH9IDijUv
# api_key = "" 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
fastapi_url = "http://127.0.0.1:8000"  # FastAPI 服务的URL

if 'api_token' not in st.session_state:
    st.session_state.api_token = api_key

if 'model' not in st.session_state:
    st.session_state.model = "glm-4-0520"

if 'chat_name' not in st.session_state:
    st.session_state.chat_name = 'default'

if 'messages' not in st.session_state:
    st.session_state.messages = [{"role": "assistant",
                                  "content": "你好 ！我是 迪小维，有什么可以帮助你的嘛 ?"}]

if 'max_tokens' not in st.session_state:
    st.session_state.max_tokens = 512

if 'temperature' not in st.session_state:
    st.session_state.temperature = 0.8

if "files_uploaded" not in st.session_state:
    st.session_state.files_uploaded = False

if "uploaded_texts" not in st.session_state:
    st.session_state.uploaded_texts = ''

if 'sys_prompt' not in st.session_state:
    st.session_state.sys_prompt = '你是一个名为 迪小维 的人工智能助手。你是基于迪塔维[Datav]训练的语言模型模型开发的，你的任务是针对用户的问题和要求提供适当的答复和支持。'


tools = get_tools() if page == Mode.ALL_TOOLS else []
first_round = len(st.session_state.messages) == 1
FILE_TEMPLATE = "[File Name]\n{file_name}\n[File Content]\n{file_content}"
# 确保 /tmp 目录存在
tmp_dir = "/tmp"
if not os.path.exists(tmp_dir):
    os.makedirs(tmp_dir)
if first_round and page == Mode.LONG_CTX.value:
    uploaded_files = st.file_uploader(
        "上传文件",
        type=["pdf", "txt", "py", "docx", "pptx", "json", "cpp", "md", "jpg", "jpeg", "png"],
        accept_multiple_files=True,
    )
    if uploaded_files and not st.session_state.files_uploaded:
        uploaded_texts = []
        for idx, uploaded_file in enumerate(uploaded_files):
            file_name: str = uploaded_file.name
            random_file_name = str(uuid4())
            file_extension = os.path.splitext(file_name)[1]
            file_path = os.path.join("/tmp", random_file_name + file_extension)
            with open(file_path, "wb") as f:
                f.write(uploaded_file.getbuffer())
            if file_name.endswith(".pdf"):
                content = extract_pdf(file_path)
            elif file_name.endswith(".docx"):
                content = extract_docx(file_path)
            elif file_name.endswith(".pptx"):
                content = extract_pptx(file_path)
            elif file_name.endswith(".jpg") or file_name.endswith(".jpeg") or file_name.endswith(".png"):
                # 使用图像描述模型
                with Image.open(file_path) as image:
                    index_keys = f"model_choice_{idx}"
                    model_choice = st.selectbox("选择图像描述模型:",
                                                ["zhipu_4v", "qwen_cv", "gpt_v4", "ollama_cv", "xinference_cv",
                                                 "local_cv"], key=index_keys)
                    # # API key input
                    model_key = st.session_state.api_token if st.session_state.api_token and model_choice == "zhipu_4v" else st.text_input(
                        "输入 API key:", type="password")
                    model_name = ''
                    # # Optional parameters
                    # model_name = st.text_input("输入模型名字(可选,默认为glm-4v):", value="glm-4v")
                    if model_choice == "zhipu_4v":
                        model_name = "glm-4v"
                    model = ModelFactory.get_model(model_choice, key=model_key, model_name=model_name)
                    content, _ = model.describe(image)
            else:
                content = extract_text(file_path)
            uploaded_texts.append(
                FILE_TEMPLATE.format(file_name=file_name, file_content=content)
            )
            os.remove(file_path)
        st.session_state.uploaded_texts = "\n\n".join(uploaded_texts)
        st.session_state.uploaded_file_nums = len(uploaded_files)
    else:
        st.session_state.uploaded_texts = ""
        st.session_state.uploaded_file_nums = 0

# 移除增量更新逻辑，直接显示所有对话
for message in st.session_state.messages:
    if message["role"] != "system":
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

# 侧边栏选项

with st.sidebar:
    st.image(
        r"E:\Project\python\study\RAG\assets\imgs\logo.png",
        use_column_width=True
    )
    st.caption(
        f"""<p align="right">当前版本：{VERSION}</p>""",
        unsafe_allow_html=True,
    )
    st.page_link("app.py", label="对话", icon="📝")
    st.page_link("pages/kb_serve.py", label="知识库管理", icon="🧷", use_container_width=True)
    st.page_link("pages/sql_trans.py", label="SQL翻译机", icon="🛠️", use_container_width=True)
    st.page_link("pages/work_flow.py", label="工作流管理", icon="🐇", use_container_width=True)
    st.page_link("pages/agent_serve.py", label="Agent智能体", icon="⭐", use_container_width=True)

    st.header("历史记录")
    history_files = load_chat_history()
    display_files = [os.path.splitext(f)[0].split('-', 1)[1] for f in
                     history_files]  # Remove timestamp and .json extension
    selected_file = st.selectbox("选择一个历史会话", display_files)
    selected_file_full_path = f"./workspace/admin/{history_files[display_files.index(selected_file)]}"

    cols = st.columns(2)
    save_btn = cols[0]
    load_btn = cols[1]
    # 保存会话按钮
    if save_btn.button("保存会话", use_container_width=True):
        st.session_state.show_save_input = True

    if st.session_state.get("show_save_input", False):
        custom_name = st.text_input("请输入会话名称（可选）:", key="custom_name")
        if st.button("确认保存", key="confirm_save"):
            if "from_history" in st.session_state and st.session_state.from_history:
                # 从历史会话加载
                save_chat_history(st.session_state.messages, custom_name, file_path=selected_file_full_path,
                                  rename=bool(custom_name))
            else:
                # 新建会话
                save_chat_history(st.session_state.messages, custom_name)
            st.session_state.show_save_input = False
            st.success('当前会话已成功保存！刷新网页后可查看记录')

    if load_btn.button("加载会话", use_container_width=True):
        st.success('当前会话已成功加载！')
        st.session_state.messages = display_chat_history(selected_file_full_path)
        st.session_state.from_history = True  # 标记为从历史会话加载

    api_token = st.text_input("输入API-KEY:", type="password")
    if api_token:
        st.session_state.api_token = api_token
        st.success("API Token 已经配置")
    model = st.selectbox("选择模型", ["glm-4-0520","glm-4-airx","glm-4-air","glm-4-flash", "glm-3-turbo", "gpt-3.5-turbo", "qwen2:7b-instruct-fp16"])
    st.session_state.model = model

       # 在应用的初始化部分或者适当的位置初始化上一次的model_name
    if 'previous_model_name' not in st.session_state:
        st.session_state.previous_model_name = None

    # 获取当前model_name
    model_name = st.session_state.model

    # 检查model_name是否发生变化
    if model_name != st.session_state.previous_model_name:
        # 当model_name变化时，显示toast消息，并更新previous_model_name
        st.toast(
            f"欢迎使用 [`Datav-RAG`](https://dcs.dataonv.com/#/home) ! \n\n"
            f"当前运行的模型`{model_name}`."
        )
        st.session_state.previous_model_name = model_name



    def on_mode_change():
        mode = st.session_state.dialogue_mode
        text = f"已切换到 {mode} 模式。"
        if mode == "知识库问答":
            cur_kb = st.session_state.get("selected_kb")
            if cur_kb:
                text = f"{text} 当前知识库： `{cur_kb}`。"
        st.toast(text)


    dialogue_modes = [
        "LLM 对话",
        "知识库问答【暂不支持】",
        "文件对话【暂不支持】",
        "搜索引擎问答【暂不支持】",
        "自定义Agent问答【暂不支持】",
    ]
    dialogue_mode = st.selectbox(
        "请选择对话模式：",
        dialogue_modes,
        index=0,
        on_change=on_mode_change,
        key="dialogue_mode",
    )
    sys_prompt = st.session_state.get('sys_prompt',
                                      '你是一个名为 迪小维 的人工智能助手。你是基于迪塔维[Datav]训练的语言模型模型开发的，你的任务是针对用户的问题和要求提供适当的答复和支持。')
    # DATE_PROMPT = "当前日期: %Y-%m-%d"
    # TOOL_SYSTEM_PROMPTS = {
    #     "simple_browser": "你可以使用 `simple_browser` 工具。该工具支持以下函数：\n`search(query: str, recency_days: int)`：使用搜索引擎进行查询并显示结果，可以使用 `recency_days` 参数控制搜索内容的时效性。\n`mclick(ids: list[int])`：获取一系列指定 id 的页面内容。每次调用时，须选择3-10个页面。选择多个角度的页面，同时尽可能选择可信任的信息来源。考虑到部分页面是无法加载的，你也可以多打开一些可能有用的页面而不用担心内容过多。\n`open_url(url: str)`：打开指定的 URL。\n\n使用 `【{引用 id}†{引用文本}】` 来引用内容。\n\n操作步骤：1. 使用 `search` 来获得信息列表; 2. 使用 `mclick` 来获取指定 ID 页面的内容; 3. 根据获得的内容进行回复。在回复中应当引用信息来源。\n 如果用户提供了 URL，也可以用 `open_url` 直接打开页面。\n如果初次搜索结果没有找到合适的信息，也可以再次使用 `search` 进行搜索。",
    # }
    #
    # def build_system_prompt(
    #         enabled_tools: list[str],
    #         functions: list[dict],
    # ):
    #     value = sys_prompt
    #     value += "\n\n" + datetime.now().strftime(DATE_PROMPT)
    #     value += "\n\n# 可用工具"
    #     contents = []
    #     for tool in enabled_tools:
    #         contents.append(f"\n\n## {tool}\n\n{TOOL_SYSTEM_PROMPTS[tool]}")
    #     for function in functions:
    #         content = f"\n\n## {function['name']}\n\n{json.dumps(function, ensure_ascii=False, indent=4)}"
    #         content += "\n在调用上述函数时，请使用 Json 格式表示调用的参数。"
    #         contents.append(content)
    #     value += "".join(contents)
    #     return value
    # 在侧边栏添加一个按钮来触发弹出框
    # 初始化 show_expander 状态
    if 'show_expander' not in st.session_state:
        st.session_state.show_expander = False
    # 在侧边栏添加一个按钮来触发弹出框
    if st.sidebar.button("定义模型", use_container_width=True):
        st.session_state.show_expander = not st.session_state.get("show_expander", False)
    # 在主页面显示扩展器（模态对话框）
    if st.session_state.get("show_expander", False):
        with st.expander("请定义你的LLM", expanded=True):
            sys_prompt = st.text_area("默认模板如下", value='''# Role: 文档问答助手
## Profile
- Author: 杜晓龙
- Version: 0.1
- Language:  Chinese
- Description: Describe your role. Give an overview of the character's characteristics and skills
### Skill
## Rules
1. Don't break character under any circumstance.
2. Don't talk nonsense and make up facts.
3. Think step by step and reason yourself to the right decisions to make sure we get it right.
## Workflow
1. First, xxx
2. Then, xxx
3. Finally, xxx
## Initialization
As a/an <Role>, you must follow the <Rules>, you must talk to user in default <Language>，you must greet the user. Then introduce yourself and introduce the <Workflow>.
            ''')
            st.session_state.sys_prompt = sys_prompt

    max_tokens = st.slider("max_tokens", min_value=0, max_value=4096, value=512)
    st.session_state.max_tokens = max_tokens

    temperature = st.slider("temperature", min_value=0.0, max_value=1.0, value=0.8)
    st.session_state.temperature = temperature

    # # 上传文件部分
    # uploaded_file = st.file_uploader("上传您的 JSON 文件", type=["json"])
    # if st.button("上传并处理"):
    #     upload_file(uploaded_file)
    now = datetime.datetime.now()
    cols = st.columns(2)
    export_btn = cols[0]
    clear_history = cols[1].button("清空对话", use_container_width=True)
    if clear_history:
        st.session_state.clear()
        st.session_state.files_uploaded = False
        st.session_state.uploaded_texts = ""
        st.session_state.uploaded_file_nums = 0
        st.session_state.history = []
        st.session_state.chat_displayed = 0
        st.session_state.files_uploaded = False
        st.session_state.uploaded_texts = ""
        st.session_state.uploaded_file_nums = 0
        st.session_state.messages = [{"role": "assistant",
                                      "content": "你好 ！我是 迪小维，有什么可以帮助你的嘛 ?"}]
        st.rerun()

    if export_btn.button("导出对话", use_container_width=True):
        st.write(st.session_state.messages)


        def export_callback():
            return "".join(export2md())


        st.download_button(
            label="下载对话记录",
            data=export_callback(),
            file_name=f"{now:%Y-%m-%d %H.%M}_对话记录.md",
            mime="text/markdown",
            use_container_width=True
        )

uploaded_texts = st.session_state.get("uploaded_texts", "")

# 用户输入框
if prompt := st.chat_input("请输入您的问题："):
    processed_prompt = None
    if prompt:
        processed_prompt = process_user_input(prompt)
    if processed_prompt is not None:
        # 在第一次轮询时，如果存在已上传的文本，将这些文本附加到用户输入的文本前面
        if first_round and st.session_state.uploaded_texts:
            processed_prompt = f"{st.session_state.uploaded_texts}\n\n{processed_prompt}"
            # 清空上传的文本以防止重复使用
            st.session_state.uploaded_texts = ""
    # processed_prompt = process_user_input(processed_prompt)
    st.session_state.messages.append({"role": "user", "content": processed_prompt})
    with st.chat_message("user"):
        st.markdown(processed_prompt)

        # 检查是否包含工具调用指令
    tool_call_match = re.search(r"调用工具：(\w+)", processed_prompt)
    if tool_call_match:
        tool_name = tool_call_match.group(1).strip()
        st.session_state.messages.append(
            # {"role": "assistant", "content": "正在调用工具...",
            #  "tool_calls": [{"name": tool_name, "arguments": processed_prompt}]}
            {"role": "tool", "content": "正在调用工具...",
             "tool_calls": [{"name": tool_name, "arguments": processed_prompt}]}
        )
        st.write(f"工具调用匹配成功: {tool_name}")

    else:
        st.write("没有匹配到工具调用指令")
    if st.session_state.sys_prompt:
        routing_instructions = st.session_state.sys_prompt
    else:
        routing_instructions = ''
    if page == Mode.ALL_TOOLS.value:
        routing_instructions += "\n\n" + build_system_prompt(list(ALL_TOOLS), tools)
    response_content = get_ai_response(
        st.session_state.api_token,
        st.session_state.model,
        st.session_state.messages,
        st.session_state.temperature,
        st.session_state.max_tokens,
        routing_instructions,
        tools
    )
    st.session_state.messages.append({"role": "assistant", "content": response_content})

    # # 显示助手回复的对话
    # with st.chat_message("assistant"):
    #     response_container = st.empty()
    #     response_container.markdown(response_content)

    # Save chat history only on session end or refresh
#     st.session_state.save_history = True
#
#
# def save_session_history():
#     if 'save_history' in st.session_state and st.session_state.save_history:
#         save_chat_history(st.session_state.messages)
#         st.session_state.save_history = False


code = """
<style>
    p[align="right"] {
        color:#BE0291;
    }
</style>
"""
st.html(code)
