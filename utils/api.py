# dialogue/api.py
import openai
import streamlit as st
from core.llm.chat_model.chat_factory import ChatFactory
import requests

fastapi_url = "http://127.0.0.1:8000"  # FastAPI 服务的URL

def query_chroma(query):
    response = requests.post(f"{fastapi_url}/query_chroma", json={"query": query})
    if response.status_code == 200:
        return response.json()
    else:
        st.error(f"Query failed: {response.text}")
        return None

def upload_file(uploaded_file):
    if uploaded_file is not None:
        files = {"file": (uploaded_file.name, uploaded_file)}
        response = requests.post(f"{fastapi_url}/upload_file", files=files)
        if response.status_code == 200:
            st.success(f"File {uploaded_file.name} uploaded and processed successfully.")
        else:
            st.error(f"File upload failed: {response.text}")


# @st.cache_data(show_spinner="Fetching data from GLM-4...", ttl=60, experimental_allow_widgets=True)
# def get_ai_response(api_token, model, messages, temperature, max_tokens, system_prompt):
#     now_temperature = float(st.text_input(label="当前的temperature为: ", value=temperature,
#                                           help="temperature是范围0-1的浮点数"))
#
#
#     factory = ChatFactory(api_token, model)
#     chat_instance = factory.get_chat_instance()
#
#     gen_conf = {
#         "temperature": now_temperature if now_temperature else temperature,
#         "max_tokens": max_tokens,
#     }
#
#     response_container = st.empty()  # 创建一个空的 Streamlit 容器，用于更新响应内容
#     response_content = ""
#
#     # 添加调试信息
#     # st.write(f"Gen Config: {gen_conf}")
#     # st.write(f"Routing Instructions: {routing_instructions}")
#
#     # 创建消息历史记录的副本，并在副本中插入系统提示
#     history_with_system_prompt = [{"role": "system", "content": system_prompt}] + messages
#
#     # 直接传递 history_with_system_prompt 给 chat_streamly 方法
#     for chunk in chat_instance.chat_streamly(system_prompt, history_with_system_prompt, gen_conf):
#         if chunk:
#             response_content = chunk  # 更新response_content
#             response_container.markdown(response_content)  # 实时更新 Streamlit UI
#         else:
#             st.error("Received empty chunk")
#     st.success("Fetched data from GLM-4!")
#     return response_content
@st.cache_data(show_spinner="Fetching data from LLM...", ttl=60, experimental_allow_widgets=True)
def get_ai_response(api_token, model, messages, temperature, max_tokens, system_prompt):
    now_temperature = float(st.text_input(label="当前的temperature为: ", value=temperature,
                                          help="temperature是范围0-1的浮点数"))


    factory = ChatFactory(api_token, model)
    chat_instance = factory.get_chat_instance()

    gen_conf = {
        "temperature": now_temperature if now_temperature else temperature,
        "max_tokens": max_tokens,
    }

    response_container = st.empty()  # 创建一个空的 Streamlit 容器，用于更新响应内容
    response_content = ""

    # 添加调试信息
    # st.write(f"Gen Config: {gen_conf}")
    # st.write(f"Routing Instructions: {routing_instructions}")

    # 创建消息历史记录的副本，并在副本中插入系统提示
    history_with_system_prompt = [{"role": "system", "content": system_prompt}] + messages

    # 直接传递 history_with_system_prompt 给 chat_streamly 方法
    try:
        for chunk in chat_instance.chat_streamly(system_prompt, history_with_system_prompt, gen_conf):
            if chunk:
                response_content = chunk  # 更新response_content
                response_container.markdown(response_content)  # 实时更新 Streamlit UI
            else:
                st.error("Received empty chunk")
        st.success("Fetched data successfully!")
    except openai.APIError as e:
        st.error(f"API error: {e}")
        return f"**ERROR**: {e}"
    except openai.RateLimitError as e:
        st.error(f"Rate limit exceeded: {e}")
        return f"**ERROR**: {e}"
    except openai.AuthenticationError as e:
        st.error(f"Authentication error: {e}")
        return f"**ERROR**: {e}"
    except openai.OpenAIError as e:
        st.error(f"OpenAI error: {e}")
        return f"**ERROR**: {e}"
    except Exception as e:
        st.error(f"Unexpected error: {e},***看看API-KEY是否配置正确了呢？***")
        return f"**ERROR**: {e}*"
    return response_content
@st.cache_data
def process_user_input(input_text):
    return input_text.strip()
