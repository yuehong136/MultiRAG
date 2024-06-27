import streamlit as st

from configs import VERSION

# 页面配置
st.set_page_config(
    page_title="知识库管理",
    page_icon="🧷",
    layout="centered",
    initial_sidebar_state="auto",
    menu_items={
        'Get Help': 'https://cake-doom-0c6.notion.site/4b6c4b3a5338497494620b3dd82e4acc?pvs=4',
        'Report a bug': "https://cake-doom-0c6.notion.site/BUG-cb6ea80282fc4de49d58ff96b4c5431a?pvs=4",
        'About': "# This is an SQL Dialect Translator app!"
    }
)

# 侧边栏
with st.sidebar:
    st.image(
        r"E:\Project\python\study\RAG\assets\imgs\logo2.png",
        use_column_width=True
    )
    st.caption(
        f"""<p align="right">当前版本：{VERSION}</p>""",
        unsafe_allow_html=True,
    )
    st.page_link("app.py", label="对话", icon="📝")
    st.page_link("pages/kb_serve.py", label="知识库管理", icon="🧷", use_container_width=True)
    st.page_link("pages/sql_trans.py", label="SQL翻译机", icon="🛠️", use_container_width=True)
    st.page_link("pages/work_flow.py", label="工作流管理", icon="⚡", use_container_width=True)
    st.page_link("pages/agent_serve.py", label="Agent智能体", icon="⭐", use_container_width=True)

# 主界面
st.title("知识库管理")
with st.expander('新建或选择知识库', expanded=True):
    # 知识库选择下拉菜单
    st.selectbox("请选择知识库新建类型:", ["samples (faiss @ bge-large-zh-v1.5)"])

    # 文件上传
    st.file_uploader("上传知识文件:", type=['html', 'md', 'json', 'csv', 'pdf', 'png', 'jpg', 'jpeg', 'bmp', 'eml', 'msg', 'rst', 'rtf', 'txt', 'xml', 'docx', 'epub', 'odt', 'ppt', 'pptx', 'tsv', 'htm'])



# 文件处理配置
with st.expander('文件处理配置', expanded=True):
    max_length = st.number_input("单段文本最大长度:", min_value=1, value=250)
    min_length = st.number_input("相似文本重合长度:", min_value=1, value=50)
    enable_chinese_tokenization = st.checkbox("开启中文标点增强")

# 添加文件按钮
if st.button("添加文件到知识库"):
    st.write("文件已添加到知识库")

# 知识库中已有文件
with st.expander('知识库 samples 中已有文件:', expanded=True):
    st.write("知识库中包含源文件与向量库，请从下表中选择文件后操作")

    # 文件列表表格（示例数据）
    import pandas as pd

    data = {
        "分词器": ["StructuredFileLoader"],
        "文档数量": [299],
        "深文件": ["RecursiveCharacterTextSplitter"]
    }
    df = pd.DataFrame(data)
    st.table(df)

# 文件操作按钮
col1, col2, col3, col4 = st.columns(4)
col1.button("下载选中文档", use_container_width=True)
col2.button("添加至向量库", use_container_width=True)
col3.button("从向量库删除", use_container_width=True)
col4.button("从知识库中删除", use_container_width=True)

# 根据源文件建立向量库按钮
if st.button("根据源文件建立向量库", use_container_width=True):
    st.write("向量库已建立")

# 删除知识库按钮
if st.button("删除知识库"):
    st.write("知识库已删除")


