import datetime
import json
import os
import re
from enum import Enum
from typing import Literal, Dict
from uuid import uuid4

import requests
import streamlit as st
from PIL import Image

from core.file.utils import extract_pdf, extract_docx, extract_pptx, extract_text
from core.llm.ocr_model.ocr_factory import ModelFactory
from core.tools.tools_registry import get_tools, ALL_TOOLS
from utils.api import get_ai_response, get_ai_recommend, process_user_input
from configs import VERSION, MODEL_PLATFORMS
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


# 删除会话记录函数
def delete_chat_history(custom_name=None, user_id='admin', file_path=None):
    history_dir = f"./workspace/{user_id}"
    if not os.path.exists(history_dir):
        # os.makedirs(history_dir)
        st.error('请检查：当前用户的会话历史目录是否存在')
    # 获取所有历史会话文件
    history_files = [f for f in os.listdir(history_dir) if f.endswith('.json')]
    history_files.sort(key=lambda x: x.split('-')[0], reverse=True)  # 按时间戳降序排列

    if file_path:
        # 如果提供了文件路径，直接删除该文件
        if os.path.exists(file_path):
            os.remove(file_path)
            return True
        else:
            return False
    elif custom_name:
        # 如果提供了自定义名称，通过自定义名称删除对应的文件
        for history_file in history_files:
            display_name = os.path.splitext(history_file)[0].split('-', 1)[1]
            if custom_name.strip() == display_name.strip():
                os.remove(os.path.join(history_dir, history_file))
                return True
    else:
        # 如果没有提供自定义名称或文件路径，删除最早的会话记录
        if history_files:
            oldest_file = history_files[-1]
            os.remove(os.path.join(history_dir, oldest_file))
            return True

    return False


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
    st.session_state.sys_prompt = '你是一个名为 迪小维 的人工智能助手。你是基于迪塔维[Datav]训练的语言模型模型开发的，你的任务是针对用户的问题和要求提供适当的答复和支持。你习惯性为用户推荐三个聊天话题'
    # st.session_state.sys_prompt = ''

if 'current_time' not in st.session_state:
    st.session_state.current_time = datetime.datetime.now()

if 'weather' not in st.session_state:
    st.session_state.weather = "Loading..."

if 'selected_option' not in st.session_state:
    st.session_state.selected_option = None

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

    import datetime
    import time

    # 初始化session_state变量
    if 'current_time' not in st.session_state:
        st.session_state.current_time = datetime.datetime.now()


    # 片段函数，实时更新当前时间
    @st.experimental_fragment(run_every=60)
    def update_time():
        st.session_state.current_time = datetime.datetime.now()
        # st.markdown("# 当前时间: \n" + st.session_state.current_time.strftime("%Y-%m-%d %H:%M:%S"))
        st.markdown("# 当前时间 \n" + st.session_state.current_time.strftime("%Y-%m-%d %H:%M"))


    # 片段函数，实时更新天气信息
    @st.experimental_fragment(run_every=3600)  # 每小时更新一次
    def update_weather():
        weather = get_weather("南京")
        st.session_state.weather = weather
        st.write(st.session_state.weather)


    @st.cache_data(show_spinner='获取天气信息...')
    def get_weather(city_name: str) -> str:
        """
        获取指定城市的当前天气
        """

        if not isinstance(city_name, str):
            raise TypeError("城市名称必须是字符串")

        key_selection = {
            "current_condition": [
                "temp_C",
                "FeelsLikeC",
                "humidity",
                "weatherDesc",
                "observation_time",
            ],
        }

        try:
            resp = requests.get(f"https://wttr.in/{city_name}?format=j1")
            resp.raise_for_status()
            resp = resp.json()
            current_condition = resp["current_condition"][0]
            ret = {key: current_condition[key] for key in key_selection["current_condition"]}
            weather_text = (
                f"温度: {ret['temp_C']}°C\n"
                f"体感温度: {ret['FeelsLikeC']}°C\n"
                f"湿度: {ret['humidity']}%\n"
                f"天气状况: {ret['weatherDesc'][0]['value']}\n"
                f"观测时间: {ret['observation_time']}\n"
            )
        except Exception as e:
            weather_text = f"获取天气数据时出错: {e}"

        return weather_text


    # # 实时显示当前时间
    # update_time()
    # st.header("南京天气")
    # # update_weather()

    tab1, tab2, tab3 = st.tabs(["会话管理", "模型配置", "智能模式"])
    with tab1:

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
            "请选择对话模式",
            dialogue_modes,
            index=0,
            on_change=on_mode_change,
            key="dialogue_mode",
        )
        sys_prompt = st.session_state.get('sys_prompt',
                                          '你是一个名为 迪小维 的人工智能助手。你是基于迪塔维[Datav]训练的语言模型模型开发的，你的任务是针对用户的问题和要求提供适当的答复和支持。')

        history_files = load_chat_history()
        display_files = [os.path.splitext(f)[0].split('-', 1)[1] for f in
                         history_files]  # Remove timestamp and .json extension
        selected_file = st.selectbox("历史会话", display_files)
        selected_file_full_path = f"./workspace/admin/{history_files[display_files.index(selected_file)]}"
        cols = st.columns(3)
        load_btn = cols[0]
        save_btn = cols[1]
        del_btn = cols[2]


        @st.experimental_dialog("重命名会话")
        def handle_session_saving(selected_file_full_path=None):
            """
            管理会话保存的交互逻辑。

            参数:
            - selected_file_full_path: 可选参数，指定保存文件的完整路径，当从历史会话加载时使用。
            """

            # 如果显示输入框，则处理用户输入
            custom_name = st.text_input("请输入会话名称（可选）:", key="custom_name")
            # if st.session_state.get("show_save_input", False):
            if st.button("确认保存", key="confirm_save"):
                # 判断是从历史加载还是新建会话，并调用相应的保存逻辑
                if "from_history" in st.session_state and st.session_state.from_history:
                    # 从历史会话加载并保存
                    save_chat_history(st.session_state.messages, custom_name, file_path=selected_file_full_path,
                                      rename=bool(custom_name))
                else:
                    # 新建会话并保存
                    save_chat_history(st.session_state.messages, custom_name)

                # 保存后重置状态
                st.session_state.show_save_input = False
                st.success('当前会话已成功保存！刷新网页后可查看记录')
                st.rerun()


        @st.experimental_dialog("删除会话")
        def handle_session_deleting():
            history_files = load_chat_history()  # 加载历史会话文件
            display_files = [os.path.splitext(f)[0].split('-', 1)[1] for f in history_files]  # 获取显示友好的文件名

            selected_file = st.selectbox("请选择要删除的会话:", display_files)  # 下拉选择框
            selected_file_full_path = f"./workspace/admin/{history_files[display_files.index(selected_file)]}"

            if st.button("确认删除"):
                if delete_chat_history(file_path=selected_file_full_path):
                    st.success(f"会话 '{selected_file}' 已成功删除！")
                    st.session_state.clear()  # 清除会话状态
                    st.rerun()  # 刷新界面
                else:
                    st.error("删除会话失败，请重试。")


        # 检查是否点击了保存按钮
        if save_btn.button("保存", use_container_width=True):
            st.session_state.show_save_input = True
            handle_session_saving(selected_file_full_path)
        if load_btn.button("加载", use_container_width=True):
            st.success('当前会话已成功加载！')
            st.session_state.messages = display_chat_history(selected_file_full_path)
            st.session_state.from_history = True  # 标记为从历史会话加载
        if del_btn.button("删除", use_container_width=True):
            st.success('当前会话已成功删除！')
            handle_session_deleting()

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

    with tab2:

        def get_config_models(
                model_name: str = None,
                model_type: Literal[
                    "llm", "embed", "image", "reranking", "speech2text", "tts"
                ] = None,
                platform_name: str = None,
        ) -> Dict[str, Dict]:
            """
            获取配置的模型列表，返回值为:
            {model_name: {
                "platform_name": xx,
                "platform_type": xx,
                "model_type": xx,
                "model_name": xx,
                "api_base_url": xx,
                "api_key": xx,
                "api_proxy": xx,
            }}
            """
            # import importlib
            # 不能支持重载
            # from chatchat.configs import model_config
            # importlib.reload(model_config)

            result = {}
            for m in MODEL_PLATFORMS:
                if platform_name is not None and platform_name != m.get("platform_name"):
                    continue
                if model_type is not None and f"{model_type}_models" not in m:
                    continue

                if model_type is None:
                    model_types = [
                        "llm_models",
                        "embed_models",
                        "image_models",
                        "reranking_models",
                        "speech2text_models",
                        "tts_models",
                    ]
                else:
                    model_types = [f"{model_type}_models"]

                for m_type in model_types:
                    for m_name in m.get(m_type, []):
                        if model_name is None or model_name == m_name:
                            result[m_name] = {
                                "platform_name": m.get("platform_name"),
                                "platform_type": m.get("platform_type"),
                                "model_type": m_type.split("_")[0],
                                "model_name": m_name,
                                "api_base_url": m.get("api_base_url"),
                                "api_key": m.get("api_key"),
                                "api_proxy": m.get("api_proxy"),
                            }
            return result


        @st.experimental_dialog("System Prompt", width="large")
        def llm_model_setting():
            # cols = st.columns(3)
            # platforms = ["所有"] + [x["platform_name"] for x in MODEL_PLATFORMS]
            # platform = cols[0].selectbox("选择模型平台", platforms, key="platform")
            # llm_models = list(
            #     get_config_models(
            #         model_type="llm", platform_name=None if platform == "所有" else platform
            #     )
            # )
            # llm_model = cols[1].selectbox("选择LLM模型", llm_models, key="llm_model")
            # temperature = cols[2].slider("Temperature", 0.0, 1.0, key="temperature")
            # system_message = st.text_area("System Message:", key="system_message")
            sys_prompt = st.text_area("系统提示词默认模板如下:", value='''# Role: 文档问答助手
                    #    ## Profile
                    #    - Author: 杜晓龙
                    #    - Version: 0.1
                    #    - Language:  Chinese
                    #    - Description: Describe your role. Give an overview of the character's characteristics and skills
                    #    ### Skill
                    #    ## Rules
                    #    1. Don't break character under any circumstance.
                    #    2. Don't talk nonsense and make up facts.
                    #    3. Think step by step and reason yourself to the right decisions to make sure we get it right.
                    #    ## Workflow
                    #    1. First, xxx
                    #    2. Then, xxx
                    #    3. Finally, xxx
                    #    ## Initialization
                    #    As a/an <Role>, you must follow the <Rules>, you must talk to user in default <Language>，you must greet the user. Then introduce yourself and introduce the <Workflow>.
                    #                ''')
            st.session_state.sys_prompt = sys_prompt
            if st.button("保存配置"):
                st.rerun()


        # 初始化 show_expander 状态
        if 'show_expander' not in st.session_state:
            st.session_state.show_expander = False
        # 在侧边栏添加一个按钮来触发弹出框
        if st.button("定义模型", use_container_width=True):
            st.session_state.show_expander = not st.session_state.get("show_expander", False)
            llm_model_setting()

        api_token = st.text_input("输入API-KEY", type="password")
        if api_token:
            st.session_state.api_token = api_token
            st.success("API Token 已经配置")
        model = st.selectbox("选择模型",
                             ["glm-4-0520", "glm-4-airx", "glm-4-air", "glm-4-flash", "glm-3-turbo", "gpt-3.5-turbo",
                              "qwen2:7b-instruct-fp16", "qwen2:72b-instruct-q4_0", "qwen2:72b-instruct-q8_0", "Doubao-pro-32k"])
        st.session_state.model = model

        max_tokens = st.slider("max_tokens", min_value=0, max_value=4096, value=512)
        st.session_state.max_tokens = max_tokens

        temperature = st.slider("temperature", min_value=0.0, max_value=1.0, value=0.8)
        st.session_state.temperature = temperature

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
        st.caption(
            "没有匹配到工具调用指令",
            unsafe_allow_html=True,
        )
    if st.session_state.sys_prompt:
        routing_instructions = st.session_state.sys_prompt
    else:
        routing_instructions = ''
    if page == Mode.ALL_TOOLS.value:
        routing_instructions += "\n\n" + build_system_prompt(list(ALL_TOOLS), tools)

    # 获取AI响应
    api_token = st.session_state.api_token
    model = st.session_state.model
    messages = st.session_state.messages
    temperature = st.session_state.temperature
    max_tokens = st.session_state.max_tokens
    routing_instructions = st.session_state.sys_prompt

    response_content = get_ai_response(
        api_token,
        model,
        messages,
        temperature,
        max_tokens,
        routing_instructions,
        tools
    )
    st.session_state.messages.append({"role": "assistant", "content": response_content})

    # # 显示助手回复的对话
    # with st.chat_message("assistant"):
    #     response_container = st.empty()
    #     response_container.markdown(response_content)
    # with st.chat_message("assistant"):
    # st.markdown(response_content)
    # 添加下钻选项
    # 确保 st.session_state 中存在 selected_option


def extract_list_from_response(response_content):
    # 使用正则表达式提取方括号中的内容
    match = re.search(r'\[.*\]', response_content)
    if match:
        list_str = match.group(0)
        try:
            options = json.loads(list_str)
            return options
        except json.JSONDecodeError:
            st.error("解析推荐话题时出错，请检查AI返回的格式。")
            return []
    else:
        st.error("未找到推荐话题列表。")
        return []


# 如果存在下拉框选择
if "select" in st.session_state.messages[-1]["content"].lower():
    st.write(st.session_state.messages)
    response_content = get_ai_recommend(
        st.session_state.api_token,
        st.session_state.model,
        st.session_state.messages,
        st.session_state.temperature,
        st.session_state.max_tokens,
        '根据用户提问和聊天的内容，请你自行结合场景生成3个推荐可以继续聊天的话题，话题请按照如下格式为返回：["xxx","xxx","xxx"]，请不要返回["xxx","xxx","xxx"]以外的内容',
    )
    st.write(response_content)
    try:
        options = extract_list_from_response(response_content)
        st.write(options)
        selected_option = st.radio("话题拓展：", options, index=None, key=f"option_{len(st.session_state.messages)}")
        st.session_state.selected_option = selected_option
    except json.JSONDecodeError:
        st.error("解析推荐话题时出错，请检查AI返回的格式。")
# 处理选择的选项
if st.session_state.selected_option:
    st.write(st.session_state.messages)
    st.session_state.messages.append({"role": "user", "content": st.session_state.selected_option})
    st.write(st.session_state.messages)
    response_content = get_ai_recommend(
        st.session_state.api_token,
        st.session_state.model,
        st.session_state.messages,
        st.session_state.temperature,
        st.session_state.max_tokens,
        st.session_state.sys_prompt
    )
    st.session_state.messages.append({"role": "assistant", "content": response_content})

    # # 重置 selected_option 以便于下一次选择
    # st.session_state.selected_option = None

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
