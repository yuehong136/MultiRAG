import json
import streamlit as st
import subprocess
import psutil

FASTAPI_PROCESS = None

def generate_api():
    global FASTAPI_PROCESS
    command = ["uvicorn", "api.work_flow_api:app", "--reload"]
    FASTAPI_PROCESS = subprocess.Popen(command)

def stop_fastapi_server():
    global FASTAPI_PROCESS
    if FASTAPI_PROCESS is not None:
        parent = psutil.Process(FASTAPI_PROCESS.pid)
        for child in parent.children(recursive=True):
            child.terminate()
        parent.terminate()
        parent.wait()
        FASTAPI_PROCESS = None
        st.success("API 停止成功！")
    else:
        st.warning("API 没有运行！")


def save_workflow():
    if 'steps' in st.session_state:
        steps = [{"step": step, "action": st.session_state[f"workflow_step_{i}"],
                  "params": st.session_state.get(f"workflow_params_{i}", {})} for i, step in
                 enumerate(st.session_state.steps)]
        workflow_json = json.dumps(steps, ensure_ascii=False, indent=4)
        st.download_button("导出当前编排", workflow_json, "workflow.json", "application/json")
    else:
        st.error("没有步骤可以保存！")

def import_workflow():
    uploaded_file = st.file_uploader("上传编排的 JSON 文件", type="json")
    if uploaded_file is not None:
        workflow_json = json.load(uploaded_file)
        st.session_state.steps = [f"步骤 {i + 1}" for i in range(len(workflow_json))]
        for i, step in enumerate(workflow_json):
            st.session_state[f"workflow_step_{i}"] = step["action"]
            st.session_state[f"workflow_params_{i}"] = step["params"]
        st.success("编排导入成功！")
