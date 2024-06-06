# app.py
import datetime
import streamlit as st
from utils.api import query_chroma, upload_file, get_ai_response, process_user_input
from configs import VERSION
from web_ui.dialogue.dialogue import reset_history, export2md

# Set Streamlit page configuration
st.set_page_config(
    page_title="演示demo",
    page_icon="🧊",
    layout="centered",
    initial_sidebar_state="auto",
    menu_items={
        'Get Help': 'https://www.extremelycoolapp.com/help',
        'Report a bug': "https://www.extremelycoolapp.com/bug",
        'About': "# This is a header. This is an *extremely* cool app!"
    })

pages = {
        "对话": {
            "icon": "chat",
            # "func": dialogue_page,
        },
        "知识库管理": {
            "icon": "hdd-stack",
            # "func": knowledge_base_page,
        },
}
# name = st.text_input('Name')
# if not name or name!= "杜晓龙":
#   st.warning('Please input a real name.')
#   st.stop()
# st.success('Thank you for inputting a name.')
default_model = 'GLM-4'
st.toast(
    f"欢迎使用 [`Datav-RAG`](https://dcs.dataonv.com/#/home) ! \n\n"
    f"当前运行的模型`{default_model}`, 您可以开始提问了."
)

# st.page_link("pages/3-购物助手.py", label="Page 2(敬请期待，暂未开发完成)", icon="2️⃣", disabled=True)

# Main code goes here
st.title("文档对话")

api_key = "7ae32940233e38153d5ebaf94844f3e2.gwrz4P0tH9IDijUv"
fastapi_url = "http://127.0.0.1:8000"  # FastAPI 服务的URL

if 'api_token' not in st.session_state:
    st.session_state.api_token = api_key

if 'model' not in st.session_state:
    st.session_state.model = "glm-4"

if 'chat_name' not in st.session_state:
    st.session_state.chat_name = 'default'

if 'messages' not in st.session_state:
    st.session_state.messages = [{"role": "system", "content": "你是一个名为 迪小维 的人工智能助手。你是基于迪塔维[Datav]训练的语言模型模型开发的，你的任务是针对用户的问题和要求提供适当的答复和支持。"}]

if 'max_tokens' not in st.session_state:
    st.session_state.max_tokens = 512

if 'temperature' not in st.session_state:
    st.session_state.temperature = 0.8

# # 显示聊天记录
# for message in st.session_state.messages:
#     with st.chat_message(message["role"]):
#         st.markdown(message["content"])

# 显示聊天记录（增量更新）
if 'chat_displayed' not in st.session_state:
    st.session_state.chat_displayed = 0

new_messages = st.session_state.messages[st.session_state.chat_displayed:]
for message in new_messages:
    # if message["role"] != "system":
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
st.session_state.chat_displayed = len(st.session_state.messages)
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
    st.page_link("app.py", label="对话", icon="🧸")
    st.page_link("pages/kb_serve.py", label="知识库管理", icon="🧷", use_container_width=True)
    st.page_link("pages/sql_trans.py", label="SQL翻译机", icon="🐉", use_container_width=True)

    def on_mode_change():
        mode = st.session_state.dialogue_mode
        text = f"已切换到 {mode} 模式。"
        if mode == "知识库问答":
            cur_kb = st.session_state.get("selected_kb")
            if cur_kb:
                text = f"{text} 当前知识库： `{cur_kb}`。"
        st.toast(text)


    # running_models = list(api.list_running_models())
    # available_models = []
    # config_models = api.list_config_models()
    # if not is_lite:
    #     for k, v in config_models.get("local", {}).items():
    #         if (v.get("model_path_exists")
    #                 and k not in running_models):
    #             available_models.append(k)
    # for k, v in config_models.get("online", {}).items():
    #     if not v.get("provider") and k not in running_models and k in LLM_MODELS:
    #         available_models.append(k)
    # llm_models = running_models + available_models
    # llm_model = st.selectbox(
    #     "选择LLM模型：",
    #     llm_models,
    #     index,
    #     format_func=llm_model_format_func,
    #     on_change=on_llm_change,
    #     key="llm_model",
    # )
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

    max_tokens = st.slider("max_tokens", min_value=0, max_value=4096, value=512)
    st.session_state.max_tokens = max_tokens

    temperature = st.slider("temperature", min_value=0.0, max_value=1.0, value=0.8)
    st.session_state.temperature = temperature
    sys_prompt = st.text_area("请定义你的LLM：", value='''
        # Role: 文档问答助手

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
    # 上传文件部分
    uploaded_file = st.file_uploader("上传您的 JSON 文件", type=["json"])
    if st.button("上传并处理"):
        upload_file(uploaded_file)
    now = datetime.datetime.now()
    with st.sidebar:

        cols = st.columns(2)
        export_btn = cols[0]
        if cols[1].button(
                "清空对话",
                use_container_width=True,
        ):
            reset_history()
            st.rerun()

    export_btn.download_button(
        "导出记录",
        "".join(export2md()),
        file_name=f"{now:%Y-%m-%d %H.%M}_对话记录.md",
        mime="text/markdown",
        use_container_width=True,
    )

# 用户输入框
if prompt := st.chat_input("请输入您的问题："):
    processed_prompt = process_user_input(prompt)
    st.session_state.messages.append({"role": "user", "content": processed_prompt})
    with st.chat_message("user"):
        st.markdown(processed_prompt)

    query_results = query_chroma(processed_prompt)
    if query_results:
        if sys_prompt:
            routing_instructions = sys_prompt
        else:
            routing_instructions = f"""
            你是一个知识渊博的学术助手，负责回答用户提出的各种学术问题。用户的问题可能涉及论文分析、研究方法、理论应用等方面。为了帮助你更好地回答问题，我们将提供一些相关的论文信息。每篇论文的信息包括以下几个部分：
            1. 摘要 (tgt1): 这是论文的摘要，提供了对论文内容的简要概述。
            2. 关键字 (tgt2): 这些是与论文内容相关的关键术语，帮助你理解论文的主要研究主题。
            3. 学科分类 (tgt3): 这是论文所属的学科领域。
            4. 期刊来源 (tgt4): 这是论文发表的期刊或来源。
    
            当用户提出一个问题时，请根据提供的论文信息进行回答。你的回答应基于论文的摘要 (tgt1)，并参考关键字 (tgt2)、学科分类 (tgt3) 和期刊来源 (tgt4) 以确保回答的准确性和专业性。
    
            用户问题: {processed_prompt}
    
            相关论文信息:
            - 摘要 (tgt1): {query_results[0]['metadata']['tgt1']}
            - 关键字 (tgt2): {query_results[0]['metadata']['tgt2']}
            - 学科分类 (tgt3): {query_results[0]['metadata']['tgt3']}
            - 期刊来源 (tgt4): {query_results[0]['metadata']['tgt4']}
    
            请根据上述信息回答用户的问题。
            """
        with st.chat_message("assistant"):
            response_content = get_ai_response(
                st.session_state.api_token,
                st.session_state.model,
                st.session_state.messages,
                st.session_state.temperature,
                st.session_state.max_tokens,
                routing_instructions
            )
            response_container = st.empty()
            # response_container.markdown(response_content)
            st.session_state.messages.append({"role": "assistant", "content": response_content})
