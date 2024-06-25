# dialogue/api.py
import openai
import streamlit as st
from core.llm.chat_model.chat_factory import ChatFactory
from core.tools.tools_registry import dispatch_tool

# fastapi_url = "http://127.0.0.1:8000"  # FastAPI 服务的URL
#
# def query_chroma(query):
#     response = requests.post(f"{fastapi_url}/query_chroma", json={"query": query})
#     if response.status_code == 200:
#         return response.json()
#     else:
#         st.error(f"Query failed: {response.text}")
#         return None
#
# def upload_file(uploaded_file):
#     if uploaded_file is not None:
#         files = {"file": (uploaded_file.name, uploaded_file)}
#         response = requests.post(f"{fastapi_url}/upload_file", files=files)
#         if response.status_code == 200:
#             st.success(f"File {uploaded_file.name} uploaded and processed successfully.")
#         else:
#             st.error(f"File upload failed: {response.text}")


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

# @st.cache_data(show_spinner="Fetching data from LLM...", ttl=60)
def get_ai_recommend(api_token, model, messages, temperature, max_tokens, system_prompt):
    factory = ChatFactory(api_token, model)
    chat_instance = factory.get_chat_instance()

    gen_conf = {
        "temperature": temperature,
        "max_tokens": max_tokens
    }

    response_container = st.empty()
    response_content = ""

    history_with_system_prompt = [{"role": "system", "content": system_prompt}] + messages
    # st.write(history_with_system_prompt)
    try:
        response_content, _ = chat_instance.chat(system_prompt, history_with_system_prompt, gen_conf)
        # response_container.markdown(response_content)
        # st.success("Fetched data successfully!")
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

@st.cache_data(show_spinner="Fetching data from LLM...", ttl=60)
def get_ai_response(api_token, model, messages, temperature, max_tokens, system_prompt, tools=None):

    # 定义工具配置
    def format_tool_params(params):
        properties = {}
        for param in params:
            param_type = param['type']
            if param_type == 'int':
                param_type = 'integer'
            elif param_type == 'str':
                param_type = 'string'
            elif param_type == 'tuple[int, int]':
                param_type = {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 2, 'maxItems': 2}
            properties[param['name']] = {"type": param_type, "description": param['description']}
        return properties

    tool_definitions = []
    for tool in tools:
        tool_definition = {
            "type": "function",
            "function": {
                "name": tool['name'],
                "description": tool['description'],
                "parameters": {
                    "type": "object",
                    "properties": format_tool_params(tool['params']),
                    "required": [param['name'] for param in tool['params'] if param['required']]
                }
            }
        }
        tool_definitions.append(tool_definition)

    gen_conf_default = {
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    gen_conf_has_tool = {
        "temperature": temperature,
        "max_tokens": max_tokens,
        "tools": tool_definitions,
        "tool_choice": "auto"
    }
    if model.startswith("Doubao"):
        model = 'ep-20240623093120-66vmh'
        # print(model)
        gen_conf = gen_conf_default
    else:
        gen_conf = gen_conf_has_tool

    factory = ChatFactory(api_token, model)
    chat_instance = factory.get_chat_instance()

    # response_container = st.empty()
    # response_content = ""

    # 添加调试信息
    # st.write(f"Gen Config: {gen_conf}")
    # st.write(f"Routing Instructions: {system_prompt}")
    # st.write(f"Tools: {tools}")
    # st.write(f"Tool Definitions: {tool_definitions}")
    # st.write(f"Messages: {messages}")

    # 检查 system_prompt 是否为 None
    if system_prompt is None:
        st.error("system_prompt is None")
        system_prompt = ""

    # 检查 messages 中的每个消息的 content 是否为 None
    for idx, message in enumerate(messages):
        if message.get("content") is None:
            st.error(f"Message at index {idx} has None content: {message}")
            message["content"] = ""
        # 增加处理工具调用的逻辑
    for message in messages:
        if message["role"] == "assistant" and "tool_name" in message:
            tool_response = dispatch_tool(message["tool_name"], message["tool_code"],
                                          session_id=st.session_state.chat_name)
            st.session_state.messages.append({"role": "tool", "content": tool_response})
            st.write(f"tool_response: {tool_response}")

            return tool_response
    # 确保 system_prompt 和 messages 中的内容都不是 None
    safe_system_prompt = system_prompt or ""
    safe_messages = [{"role": msg["role"], "content": msg.get("content", "")} for msg in messages]

    # 创建消息历史记录的副本，并在副本中插入系统提示
    history_with_system_prompt = [{"role": "system", "content": safe_system_prompt}] + safe_messages




    # 直接传递 history_with_system_prompt 给 chat_streamly 方法
    try:
        with st.status("LLM疯狂输出ing...") as s:
            response_container = st.empty()
            response_content = ""
            for chunk in chat_instance.chat_streamly(safe_system_prompt, history_with_system_prompt, gen_conf):
                if chunk:
                    response_content = chunk  # 更新response_content
                    response_container.markdown(response_content)  # 实时更新 Streamlit UI
                    s.update(label="💫Over", expanded=True)
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
