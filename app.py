# app.py
import datetime
import os
from enum import Enum
from uuid import uuid4
import streamlit as st

from core.file.utils import extract_pdf, extract_docx, extract_pptx, extract_text
from utils.api import query_chroma, upload_file, get_ai_response, process_user_input
from configs import VERSION
from web_ui.dialogue.dialogue import reset_history, export2md

# Set Streamlit page configuration
st.set_page_config(
    page_title="MultiRAG",
    page_icon="🧸",#🧊
    layout="centered",
    initial_sidebar_state="auto",
    menu_items={
        'Get Help': 'https://www.extremelycoolapp.com/help',
        'Report a bug': "https://www.extremelycoolapp.com/bug",
        'About': "# This is a header. This is an *extremely* cool app!"
    })


class Mode(str, Enum):
    ALL_TOOLS = "🛠️ All Tools"
    LONG_CTX = "📝 文档解读"
    # GLM4 = "🖼️ 多模态"
    VLM = "🖼️ 多模态"


# name = st.text_input('Name')
# if not name or name!= "杜晓龙":
#   st.warning('Please input a real name.')
#   st.stop()
# st.success('Thank you for inputting a name.')
default_model = 'GLM-4-520'
st.toast(
    f"欢迎使用 [`Datav-RAG`](https://dcs.dataonv.com/#/home) ! \n\n"
    f"当前运行的模型`{default_model}`, 您可以开始提问了."
)

# st.page_link("pages/3-购物助手.py", label="Page 2(敬请期待，暂未开发完成)", icon="2️⃣", disabled=True)

# Main code goes here
# st.title("文档对话")

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


api_key = "7ae32940233e38153d5ebaf94844f3e2.gwrz4P0tH9IDijUv" # 7ae32940233e38153d5ebaf94844f3e2.gwrz4P0tH9IDijUv
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

if 'sys_prompt' not in st.session_state:
    st.session_state.sys_prompt = '你是一个名为 迪小维 的人工智能助手。你是基于迪塔维[Datav]训练的语言模型模型开发的，你的任务是针对用户的问题和要求提供适当的答复和支持。'

first_round = len(st.session_state.messages) == 1
FILE_TEMPLATE = "[File Name]\n{file_name}\n[File Content]\n{file_content}"
# 确保 /tmp 目录存在
tmp_dir = "/tmp"
if not os.path.exists(tmp_dir):
    os.makedirs(tmp_dir)
if first_round and page == Mode.LONG_CTX.value:
    uploaded_files = st.file_uploader(
        "上传文件",
        type=["pdf", "txt", "py", "docx", "pptx", "json", "cpp", "md"],
        accept_multiple_files=True,
    )
    if uploaded_files and not st.session_state.files_uploaded:
        uploaded_texts = []
        for uploaded_file in uploaded_files:
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
    api_token = st.text_input("输入API-KEY:", type="password")
    if api_token:
        st.session_state.api_token = api_token
        st.success("API Token 已经配置")
    model = st.selectbox("选择模型", ["glm-4-0520", "glm-3-turbo", "gpt-3.5-turbo"])
    st.session_state.model = model


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
    sys_prompt = st.session_state.get('sys_prompt','你是一个名为 迪小维 的人工智能助手。你是基于迪塔维[Datav]训练的语言模型模型开发的，你的任务是针对用户的问题和要求提供适当的答复和支持。')
    # 在侧边栏添加一个按钮来触发弹出框
    # 初始化 show_expander 状态
    if 'show_expander' not in st.session_state:
        st.session_state.show_expander = False
    # 在侧边栏添加一个按钮来触发弹出框
    if st.sidebar.button("定义模型",use_container_width=True):
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

    # query_results = query_chroma(processed_prompt)
    # if query_results:
    if sys_prompt:
        routing_instructions = sys_prompt
    else:
        routing_instructions = ''
    response_content = get_ai_response(
        st.session_state.api_token,
        st.session_state.model,
        st.session_state.messages,
        st.session_state.temperature,
        st.session_state.max_tokens,
        routing_instructions
    )
    st.session_state.messages.append({"role": "assistant", "content": response_content})

    # # 显示助手回复的对话
    # with st.chat_message("assistant"):
    #     response_container = st.empty()
    #     response_container.markdown(response_content)
