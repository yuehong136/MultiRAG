# sql_trans.py
import re
import streamlit as st
from utils.api import get_ai_response
from configs import VERSION

# 设置页面配置
st.set_page_config(
    page_title="SQL方言翻译器",
    page_icon="🛠️",
    layout="wide",
    initial_sidebar_state="auto",
    menu_items={
        'Get Help': 'https://www.extremelycoolapp.com/help',
        'Report a bug': "https://www.extremelycoolapp.com/bug",
        'About': "# This is an SQL Dialect Translator app!"
    }
)

# 自定义CSS样式
st.markdown("""
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
    """, unsafe_allow_html=True)

# 页面标题和说明
st.title("SQL方言翻译器")
st.markdown("""
    使用此工具，您可以将SQL查询从一种方言转换为另一种方言。
    选择源方言和目标方言，输入要转换的SQL查询，然后点击***Translate***按钮即可获取转换后的SQL查询。
""")

# 侧边栏
with st.sidebar:
    st.image(
        r"E:\Project\python\study\RAG\assets\imgs\logo.png",
        use_column_width=True
    )
    st.caption(
        f"""<p align="right">当前版本：{VERSION}</p>""",
        unsafe_allow_html=True,
    )
    st.page_link("test.py", label="对话", icon="📝")
    st.page_link("pages/kb_serve.py", label="知识库管理", icon="🧷", use_container_width=True)
    st.page_link("pages/sql_trans.py", label="SQL翻译机", icon="🛠️", use_container_width=True)
    st.sidebar.title("Datav")
    st.sidebar.markdown("当前版本: v0.0.1-preview")

# 创建两列布局
col1, col2 = st.columns(2)

# 输入区域
with col1:
    st.subheader("输入")
    from_dialect = st.selectbox("从:", ["Snowflake", "MySQL", "PostgreSQL", "SQLite", "Oracle"], index=0, help="选择源SQL方言")
    sql_input = st.text_area("输入 SQL", height=200, help="输入需要转换的SQL查询", key="sql_input")

# 输出区域
with col2:
    st.subheader("输出")
    to_dialect = st.selectbox("到:", ["Snowflake", "MySQL", "PostgreSQL", "SQLite", "Oracle"], index=1, help="选择目标SQL方言")
    translated_sql_placeholder = st.empty()

# 定义API参数
api_token = "7ae32940233e38153d5ebaf94844f3e2.gwrz4P0tH9IDijUv"
model = "glm-4-0520"
temperature = 0.8
max_tokens = 512

# SQL翻译函数
def translate_sql(input_sql, from_dialect, to_dialect):
    system_prompt = f"""
    您是SQL翻译助理。您的任务是将SQL查询从一种SQL方言转换为另一种。
    请将以下SQL查询从 {from_dialect} 翻译到 {to_dialect}.
    确保输出仅为SQL查询，没有任何解释：
    SQL 查询: {input_sql}
    """
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": input_sql}]
    response = get_ai_response(api_token, model, messages, temperature, max_tokens, system_prompt)

    # 改进正则表达式提取SQL语句
    sql_match = re.search(r"SELECT.*?(?:FROM|INTO|UPDATE|DELETE).*?;", response, re.IGNORECASE | re.DOTALL)
    if sql_match:
        return sql_match.group(0)
    else:
        return "转换错误：在响应中找不到SQL查询。"

# 翻译按钮
if st.button("翻译"):
    if not sql_input.strip():
        st.error("请输入要翻译的SQL。")
    else:
        translated_sql_value = translate_sql(sql_input, from_dialect, to_dialect)
        translated_sql_placeholder.text_area("翻译后的 SQL", value=translated_sql_value, height=200, help="转换后的SQL查询将显示在这里", key="translated_sql_output")

# 错误消息
st.error("不支持的表达式类型SwapTable") if "SwapTable" in sql_input else ''
