import json
import re

import streamlit as st
from zhipuai import ZhipuAI

from configs import VERSION

st.set_page_config(
    page_title="流程可视化",
    page_icon="🎨",
    layout="wide",
    initial_sidebar_state="auto",
    menu_items={
        'Get Help': 'https://cake-doom-0c6.notion.site/4b6c4b3a5338497494620b3dd82e4acc?pvs=4',
        'Report a bug': "https://cake-doom-0c6.notion.site/BUG-cb6ea80282fc4de49d58ff96b4c5431a?pvs=4",
        'About': "- 快来可视化你的思路、流程!"
    }
)
def main():
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
        st.page_link("pages/flowchat.py", label="流程可视化", icon="🎨", use_container_width=True)
        st.page_link("pages/agent_serve.py", label="Agent智能体", icon="⭐", use_container_width=True)
        st.page_link("pages/visual_data_analysis.py", label="可视化数据分析", icon="🔎️", use_container_width=True)

    st.header("工作流程可视化")
    # st.markdown("*" + "可在侧边栏添加编排功能组件构建流程应用，可选择开放API供三方调用。" + "*")
    st.markdown("可在侧边栏添加编排功能组件构建流程应用，可选择开放API供三方调用。")

    user_input = st.text_area("请描述一个工作流程:",
                              "描述一个计算任务的处理流程，从开始到结束，包括数据读取、处理、存储等步骤。")
    client = ZhipuAI(api_key="7ae32940233e38153d5ebaf94844f3e2.gwrz4P0tH9IDijUv")  # 请填写您自己的APIKey

    if st.button("生成流程图"):
        # 调用大模型生成 JSON 数据
        response = client.chat.completions.create(
            model="glm-4-0520",  # 填写需要调用的模型编码
            messages=[
                {"role": "system", "content": '''
                    请将用户问题描述的工作流程转换为 JSON 格式，其中键表示当前步骤，值表示下一步骤，下一步骤也是可以指向之前步骤的。例如：
                    {{
                        "步骤1": "步骤2",
                        "步骤2": "步骤3"
                    }}
                '''},
                {"role": "user", "content": user_input},
            ],
            stream=False,
        )

        # 打印完整的返回内容，供调试使用
        raw_content = response.choices[0].message.content
        # st.write("模型返回的原始内容：")
        # st.code(raw_content)

        # 提取多个 JSON 部分
        try:
            json_matches = re.findall(r'\{[\s\S]*?\}', raw_content)
            if json_matches:
                # 选择最后一个 JSON 块
                json_content = json_matches[-1]

                # 将字符串转为 JSON 对象
                json_data = json.loads(json_content)

                # 打印生成的 JSON 数据以供检查
                st.json(json_data)

                # 处理嵌套结构，展开为线性结构
                def flatten_json(nested_json):
                    flat_json = {}

                    def _flatten(item, parent_key=""):
                        if isinstance(item, dict):
                            for k, v in item.items():
                                new_key = f"{parent_key}->{k}" if parent_key else k
                                _flatten(v, new_key)
                        elif isinstance(item, list):
                            for i, v in enumerate(item):
                                new_key = f"{parent_key}->{i}" if parent_key else str(i)
                                _flatten(v, new_key)
                        else:
                            flat_json[parent_key] = item

                    _flatten(nested_json)
                    return flat_json

                flat_json_data = flatten_json(json_data)

                # 将平面 JSON 数据转换为 st.graphviz_chart 所需的格式
                def convert_to_graphviz(flat_json_data):
                    graphviz_format = "digraph {\n"
                    for key, value in flat_json_data.items():
                        graphviz_format += f"    {key} -> {value}\n"
                    graphviz_format += "}"
                    return graphviz_format

                # 将 JSON 数据转换为 Graphviz 格式的代码
                graphviz_code = convert_to_graphviz(flat_json_data)

                # 在 Streamlit 中展示生成的流程图
                st.graphviz_chart(graphviz_code)
            else:
                st.error("未找到有效的 JSON 数据块。")

        except json.JSONDecodeError as e:
            st.error(f"解析 JSON 数据时发生错误: {e}")


if __name__ == "__main__":
    main()
