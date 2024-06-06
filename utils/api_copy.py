# dialogue/api.py
import requests
import streamlit as st
from zhipuai import ZhipuAI

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

@st.cache_data(show_spinner="Fetching data from GLM-4...", ttl=60, experimental_allow_widgets=True)
def get_ai_response(api_token, model, messages, temperature, max_tokens, routing_instructions):
    now_temperature = float(st.text_input(label="当前的temperature为: ", value=temperature,
                                          help="temperature是范围0-1的浮点数"))
    client = ZhipuAI(api_key=api_token)
    stream = client.chat.completions.create(
        model=model,
        messages=[
                     {"role": "system",
                      "content": routing_instructions}
                 ] + [
                     {"role": m["role"], "content": m["content"]}
                     for m in messages
                 ],
        temperature=now_temperature if now_temperature else temperature,
        max_tokens=max_tokens,
        stream=True,
    )

    response_content = ""
    for chunk in stream:
        if hasattr(chunk.choices[0].delta, 'content'):
            content = chunk.choices[0].delta.content
            response_content += content
    st.success("Fetched data from GLM-4!")
    return response_content

@st.cache_data
def process_user_input(input_text):
    return input_text.strip()
