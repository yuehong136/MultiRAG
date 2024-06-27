import streamlit as st
from core.agent.agent import Agent
from configs import VERSION

# from my_agent import MyAgent  # 假设已经实现了一个简单的 Agent 类
# 定义 Agent 类（示例）
st.set_page_config(
    page_title="Agent智能体",
    page_icon="🧸",  # 🧊
    layout="centered",
    initial_sidebar_state="auto",
    menu_items={
        'Get Help': 'https://cake-doom-0c6.notion.site/4b6c4b3a5338497494620b3dd82e4acc?pvs=4',
        'Report a bug': "https://cake-doom-0c6.notion.site/BUG-cb6ea80282fc4de49d58ff96b4c5431a?pvs=4",
        'About': "- 测试ing!"
    })


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
st.title("智能助手")


# 初始化 Agent
API_KEY = "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"
agent = Agent(API_KEY)

# 展示 Agent 的 Profile 信息
profile = agent.get_profile()
st.sidebar.header("Agent Profile")
st.sidebar.write(profile)

# 用户输入区
user_input = st.text_input("输入问题或命令")

# 输出展示区
if st.button("提交"):
    if user_input:
        # Agent 处理输入并生成响应
        response = agent.handle(user_input)
        st.write("回应：", response)

        # 展示 Memory 信息
        memory = agent.get_memory()
        st.sidebar.header("Memory")
        st.sidebar.subheader("短期记忆")
        st.sidebar.write(memory["short_term"])
        st.sidebar.subheader("长期记忆")
        st.sidebar.write(memory["long_term"])