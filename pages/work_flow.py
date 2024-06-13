import io
import atexit

import streamlit as st
import pandas as pd
import json
import subprocess
from core.components.file_operations import upload_file
from core.components.data_processing import process_data
from core.components.nl2sql import input_nl_query, semantic_parsing, db_schema_understanding, generate_sql
from core.components.sql_operations import execute_sql
from configs import VERSION

st.set_page_config(
    page_title="工作流管理",
    page_icon="🐇",
    layout="centered",
    initial_sidebar_state="auto",
    menu_items={
        'Get Help': 'https://cake-doom-0c6.notion.site/4b6c4b3a5338497494620b3dd82e4acc?pvs=4',
        'Report a bug': "https://cake-doom-0c6.notion.site/BUG-cb6ea80282fc4de49d58ff96b4c5431a?pvs=4",
        'About': "- 欢迎在此编排组件构建应用!"
    }
)

FASTAPI_PROCESS = None

# 保存编排为 JSON 文件
def save_workflow():
    if 'steps' in st.session_state:
        steps = [{"step": step, "action": st.session_state[f"workflow_step_{i}"],
                  "params": st.session_state.get(f"workflow_params_{i}", {})} for i, step in
                 enumerate(st.session_state.steps)]
        workflow_json = json.dumps(steps, ensure_ascii=False, indent=4)
        st.download_button("下载当前编排", workflow_json, "workflow.json", "application/json")
    else:
        st.error("没有步骤可以保存！")


# 导入 JSON 文件生成编排
def import_workflow():
    uploaded_file = st.file_uploader("上传编排的 JSON 文件", type="json")
    if uploaded_file is not None:
        workflow_json = json.load(uploaded_file)
        st.session_state.steps = [f"步骤 {i + 1}" for i in range(len(workflow_json))]
        for i, step in enumerate(workflow_json):
            st.session_state[f"workflow_step_{i}"] = step["action"]
            st.session_state[f"workflow_params_{i}"] = step["params"]
        st.success("编排导入成功！")


# 生成 API
def generate_api():
    global FASTAPI_PROCESS
    if 'steps' in st.session_state:
        steps = [{"step": step, "action": st.session_state[f"workflow_step_{i}"],
                  "params": st.session_state.get(f"workflow_params_{i}", {})} for i, step in
                 enumerate(st.session_state.steps)]
        workflow_json = json.dumps(steps, ensure_ascii=False, indent=4)

        # 保存 JSON 文件
        with open("workflow.json", "w", encoding="utf-8") as f:
            f.write(workflow_json)

        # 启动 FastAPI 服务器
        command = ["uvicorn", "utils.work_flow_api:app", "--reload"]
        FASTAPI_PROCESS = subprocess.Popen(command)
        st.success("API 生成成功！访问 http://127.0.0.1:8000/docs 查看 API 文档。")
    else:
        st.error("没有步骤可以生成 API！")

# 停止 FastAPI 服务器
def stop_fastapi_server():
    global FASTAPI_PROCESS
    if FASTAPI_PROCESS is not None:
        FASTAPI_PROCESS.terminate()
        FASTAPI_PROCESS.wait()
        FASTAPI_PROCESS = None
        st.success("FastAPI 服务器已停止")

# 确保在 Python 解释器关闭时终止 FastAPI 服务器
atexit.register(stop_fastapi_server)

# 主函数构建工作流界面
def main():
    with st.sidebar:
        st.image(
            "E:/Project/python/study/RAG/assets/imgs/logo.png",
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

    st.title("自定义工作流构建器")

    # 侧边栏用于构建工作流
    st.sidebar.title("构建你的工作流")

    if 'steps' not in st.session_state:
        st.session_state.steps = []
    add_step = st.sidebar.button("添加工作流步骤")

    if add_step:
        st.session_state.steps.append(f"步骤 {len(st.session_state.steps) + 1}")

    # 在侧边栏中显示每个步骤
    steps_to_remove = []
    for i, step in enumerate(st.session_state.steps):
        cols = st.sidebar.columns([4, 1])
        selected_step = cols[0].selectbox(f"{step}：", [
            "上传文件",
            "数据处理",
            "显示结果",
            "输入自然语言查询",
            "语义解析",
            "数据库模式理解",
            "生成 SQL",
            "执行 SQL"
        ], key=f"workflow_step_{i}")

        if selected_step == "数据处理":
            st.session_state[f"workflow_params_{i}"] = {
                "method": cols[0].selectbox("选择处理方式", ["数值列求和", "数值列求平均"],
                                            key=f"processing_method_{i}")}

        if selected_step == "输入自然语言查询":
            st.session_state[f"workflow_params_{i}"] = {"query": cols[0].text_input("输入查询", key=f"nl_query_{i}")}

        if cols[1].button("删除", key=f"delete_step_{i}"):
            steps_to_remove.append(i)

    # 删除选中的步骤并重新编号
    if steps_to_remove:
        for i in sorted(steps_to_remove, reverse=True):
            st.session_state.steps.pop(i)
            del st.session_state[f"workflow_step_{i}"]
            if f"workflow_params_{i}" in st.session_state:
                del st.session_state[f"workflow_params_{i}"]
        # 重新编号
        st.session_state.steps = [f"步骤 {i + 1}" for i in range(len(st.session_state.steps))]
        st.experimental_rerun()

    # 保存编排和导入编排
    with st.sidebar:
        st.subheader("保存和导入编排")
        save_workflow()
        import_workflow()
        st.subheader("生成 API")
        if st.button("生成 API"):
            generate_api()
        if st.button("停止 API"):
            stop_fastapi_server()

    # 主界面执行工作流
    for i, step in enumerate(st.session_state.steps):
        selected_step = st.session_state[f"workflow_step_{i}"]

        if selected_step == "上传文件":
            st.header(f"{step}：上传文件")
            uploaded_file = st.file_uploader(f"选择一个CSV文件", type="csv", key=f"file_uploader_{i}")
            if uploaded_file is not None:
                file_content = uploaded_file.getvalue()
                st.session_state[f"file_content_{i}"] = file_content
                data = pd.read_csv(io.StringIO(file_content.decode('utf-8')))
                st.session_state['data'] = data
                st.write("上传的文件内容：")
                st.dataframe(data)

        elif selected_step == "数据处理":
            st.header(f"{step}：数据处理")
            method = st.session_state.get(f"workflow_params_{i}", {}).get("method", "数值列求和")
            if 'data' in st.session_state:
                data = st.session_state['data']
                processed_data = process_data(data, method)
                st.session_state['processed_data'] = processed_data
                st.write("处理后的数据：")
                st.dataframe(processed_data)

        elif selected_step == "显示结果":
            st.header(f"{step}：显示结果")
            if 'processed_data' in st.session_state:
                processed_data = st.session_state['processed_data']
                st.write("处理后的数据：")
                st.dataframe(processed_data)
                if st.checkbox("显示统计摘要"):
                    st.write(processed_data.describe())
            else:
                st.error("没有处理后的数据可显示！")

        elif selected_step == "输入自然语言查询":
            st.header(f"{step}：输入自然语言查询")
            query = st.session_state.get(f"workflow_params_{i}", {}).get("query", "")
            nl_query = input_nl_query(query)
            st.session_state['nl_query'] = nl_query
            st.write("自然语言查询：", nl_query)

        elif selected_step == "语义解析":
            st.header(f"{step}：语义解析")
            if 'nl_query' in st.session_state:
                nl_query = st.session_state['nl_query']
                parsed_query = semantic_parsing(nl_query)
                st.session_state['parsed_query'] = parsed_query
                st.write("解析后的查询：", parsed_query)
            else:
                st.error("请先输入自然语言查询！")

        elif selected_step == "数据库模式理解":
            st.header(f"{step}：数据库模式理解")
            schema = db_schema_understanding()
            st.session_state['db_schema'] = schema
            st.write("数据库模式：", schema)

        elif selected_step == "生成 SQL":
            st.header(f"{step}：生成 SQL")
            if 'parsed_query' in st.session_state and 'db_schema' in st.session_state:
                parsed_query = st.session_state['parsed_query']
                schema = st.session_state['db_schema']
                sql_query = generate_sql(parsed_query, schema)
                st.session_state['sql_query'] = sql_query
                st.write("生成的 SQL 查询：", sql_query)
            else:
                st.error("请先完成前面的步骤！")

        elif selected_step == "执行 SQL":
            st.header(f"{step}：执行 SQL")
            if 'sql_query' in st.session_state:
                sql_query = st.session_state['sql_query']
                sql_result = execute_sql(sql_query)
                st.session_state['sql_result'] = sql_result
                st.write("SQL 执行结果：", sql_result)
            else:
                st.error("请先生成 SQL 查询！")

if __name__ == "__main__":
    main()