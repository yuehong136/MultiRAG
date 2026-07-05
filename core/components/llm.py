import os

import openai
import streamlit as st

from core.llm.chat_model.chat_factory import ChatFactory


def zhipuai(messages, temperature, max_tokens, sys_prompt, api_token=os.environ.get("ZHIPUAI_API_KEY", ""), model="glm-4-0520"):
    """
    智谱GLM4对话
    """
    factory = ChatFactory(api_token, model)
    chat_instance = factory.get_chat_instance()
    history_with_system_prompt = [{"role": "system", "content": sys_prompt}] + messages
    gen_conf = {
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    response_container = st.empty()
    response_content = ""

    try:
        for chunk in chat_instance.chat_streamly(sys_prompt, history_with_system_prompt, gen_conf):
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
