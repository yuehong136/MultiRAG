import requests
from pymilvus import MilvusClient
import streamlit as st
client = MilvusClient(
    uri="http://172.30.129.150:19530",
    token="root:Milvus"
)
# client.create_collection(collection_name="test_collection", dimension=5)
kb_list = client.list_collections()

def request_milvus() -> list:
    url = "http://192.168.188.60:8010/milvus/search"
    params = {'query': st.session_state.get('kb_query'), 'collection_name': st.session_state.get('selected_kb'), 'topn': st.session_state.get('kb_topn', 10)}
    headers = {'accept': 'application/json'}

    response = requests.post(url, headers=headers, params=params)
    if response.status_code == 200:
        return response.json().get('results', [])
    else:
        print(f"Failed to retrieve data from {st.session_state.get('selected_kb')}. Status code: {response.status_code}")
        return []

def process_schema_response(data: list) -> list:
    result = []
    for item in data:
        lines = item['page_content'].split('\n')
        table_name = lines[0]
        table_chinese_name = lines[1].strip(':')
        column_name = lines[2].strip(':')
        column_chinese_name = lines[3].strip(':')
        column_type = lines[4].strip(':')
        column_constraint = lines[5].strip(':')
        result.append(
            [table_name, table_chinese_name, column_name, column_chinese_name, column_type, column_constraint])
    return result

