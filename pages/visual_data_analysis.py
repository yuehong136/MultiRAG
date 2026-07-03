import pandas as pd
import streamlit as st
from pygwalker.api.streamlit import StreamlitRenderer

from configs import VERSION

# 设置页面配置
st.set_page_config(
    page_title="可视化数据分析",
    page_icon="🔎️",
    layout="wide",
    initial_sidebar_state="auto",
    menu_items={"Get Help": "https://www.extremelycoolapp.com/help", "Report a bug": "https://www.extremelycoolapp.com/bug", "About": "- 在此进行数据可视化分析!"},
)

# 自定义CSS样式
st.markdown(
    """
    <style>
        .sidebar .sidebar-content {
            padding: 10px;
        }
        .stButton button {
            border-radius: 8px;
            padding: 8px 20px;
        }
        .stTextArea textarea {
            border-radius: 8px;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

# 页面标题和说明
st.markdown("# " + "***" + "可视化数据分析" + "***")
st.markdown("*" + "使用此工具，允许数据科学家通过简单的拖放操作甚至自然语言查询来可视化/清理/注释数据" + "*")

# 侧边栏
with st.sidebar:
    st.image(r"E:\Project\python\study\RAG\assets\imgs\logo2.png", use_column_width=True)
    st.caption(
        f"""<p align="right">当前版本：{VERSION}</p>""",
        unsafe_allow_html=True,
    )
    st.page_link("app.py", label="对话", icon="📝")
    st.page_link("pages/kb_serve.py", label="知识库管理", icon="🧷", use_container_width=True)
    st.page_link("pages/sql_trans.py", label="SQL翻译机", icon="🛠️", use_container_width=True)
    st.page_link("pages/work_flow.py", label="工作流管理", icon="⚡", use_container_width=True)
    st.page_link("pages/flowchat.py", label="流程可视化", icon="🎨", use_container_width=True)
    st.page_link("pages/agent_serve.py", label="Agent智能体", icon="⭐", use_container_width=True)
    st.page_link("pages/visual_data_analysis.py", label="可视化数据分析", icon="🔎️", use_container_width=True)

    # 文件上传功能
    uploaded_file = st.file_uploader("上传 CSV 或 Excel 文件", type=["csv", "xlsx"])
    if uploaded_file:
        # 读取上传的文件
        if uploaded_file.name.endswith(".csv"):
            df = pd.read_csv(uploaded_file)
        else:
            df = pd.read_excel(uploaded_file)


@st.cache_resource
def get_pyg_renderer(dataframe: pd.DataFrame) -> "StreamlitRenderer":
    # If you want to use feature of saving chart config, set `spec_io_mode="rw"`
    return StreamlitRenderer(dataframe, spec="./gw_config.json", spec_io_mode="rw", encoding="utf-8")


if "df" in locals():
    renderer = get_pyg_renderer(df)
    renderer.explorer()
else:
    st.warning("请上传一个 CSV 或 Excel 文件以进行可视化分析。")
