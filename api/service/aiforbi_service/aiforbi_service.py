import json
import re

import pandas as pd
from fastapi import HTTPException

from api.db import LLMType
from api.db.services.llm_service import LLMBundle
from api.db.services.user_service import TenantService
from api.service.aiforbi_service.echarts_temp import load_chart_template
from api.service.aiforbi_service.llm_prompts import PromptTemplateLoader
from core.settings import aiforbi_logger


class AIForBIService:

    @staticmethod
    async def nl2sql(nl2sql_req_body, db, user_id, llm_name):
        loader = PromptTemplateLoader()
        prompt = loader.fill_template("nl2sql_temp.txt", nl2sql_req_body)
        aiforbi_logger.info(f"nl2sql prompt: {prompt}")
        tenants = TenantService.get_info_by(db, user_id)
        if not tenants:
            raise HTTPException(status_code=404, detail="Tenant not found!")

        chat_model = LLMBundle(db, tenants[0]["tenant_id"], LLMType.CHAT, llm_name)
        resp = chat_model.chat(system="", history=[{"role": "user", "content": prompt}], gen_conf={})
        aiforbi_logger.info(f"nl2sql resp: {resp}")
        return resp

    @staticmethod
    async def chart_type(chart_type_req_body, db, user_id, llm_name):
        pd_df = pd.DataFrame(chart_type_req_body.sql_result['data'])
        columns = chart_type_req_body.sql_result['metadata']['columns']
        loader = PromptTemplateLoader()
        prompt = loader.fill_template("chart_type_temp.txt",
                                      user_question=chart_type_req_body.user_question,
                                      columns=columns,
                                      sql_result=chart_type_req_body.sql_result['data'],
                                      sql_result_pandas=pd_df)
        aiforbi_logger.info(f"chart_type prompt: {prompt}")
        tenants = TenantService.get_info_by(db, user_id)
        if not tenants:
            raise HTTPException(status_code=404, detail="Tenant not found!")

        chat_model = LLMBundle(db, tenants[0]["tenant_id"], LLMType.CHAT, llm_name)
        resp = chat_model.chat(system="", history=[{"role": "user", "content": prompt}], gen_conf={})
        aiforbi_logger.info(f"chart_type resp: {resp}")
        chart_type_list = json.loads(resp)
        return chart_type_list

    @staticmethod
    async def dynamic_chart_option_function(dynamic_chart_option_function_req_body, db, user_id, llm_name):
        chart_template = load_chart_template(dynamic_chart_option_function_req_body.chart_type)

        loader = PromptTemplateLoader()
        prompt = loader.fill_template("dynamic_chart_option_function_temp.txt",
                                      user_question=dynamic_chart_option_function_req_body.user_question,
                                      chart_type=dynamic_chart_option_function_req_body.chart_type,
                                      columns=dynamic_chart_option_function_req_body.sql_result['metadata']['columns'],
                                      chart_template=chart_template)
        aiforbi_logger.info(f"dynamic_chart_option_function prompt: {prompt}")
        tenants = TenantService.get_info_by(db, user_id)
        if not tenants:
            raise HTTPException(status_code=404, detail="Tenant not found!")

        chat_model = LLMBundle(db, tenants[0]["tenant_id"], LLMType.CHAT, llm_name)
        resp = chat_model.chat(system="", history=[{"role": "user", "content": prompt}], gen_conf={})
        aiforbi_logger.info(f"dynamic_chart_option_function resp: {resp}")
        return resp

    @staticmethod
    async def static_chart_option(static_chart_option_req_body, db, user_id, llm_name):
        tenants = TenantService.get_info_by(db, user_id)
        if not tenants:
            raise HTTPException(status_code=404, detail="Tenant not found!")
        chat_model = LLMBundle(db, tenants[0]["tenant_id"], LLMType.CHAT, llm_name)

        raw_data = static_chart_option_req_body.raw_data
        # 使用正则表达式提取所有 Markdown 格式的表格
        matches = re.finditer(r"(\|.*?\|(?:\n|$))+", raw_data)
        # 提取所有匹配到的表格内容并转换为字符串
        tab = ""
        for match in matches:
            tab += match.group().strip() + "\n\n"
        # 去除最后多余的换行符
        tab = tab.strip()
        header_line = tab.splitlines()[0]
        header_elements = header_line.split('|')[1:-1]  # 去掉前后边界的 '|'
        columns = [element.strip() for element in header_elements]  # 去除空格
        print(columns)
        loader = PromptTemplateLoader()
        prompt4ChartType = loader.fill_template("static_chart_type.txt", tab=tab)
        aiforbi_logger.info(f"chart_type prompt: {prompt4ChartType}")

        chart_type_res = chat_model.chat(system="", history=[{"role": "user", "content": prompt4ChartType}], gen_conf={})
        # 使用正则表达式匹配整个 JSON 数组格式的图表类型
        chart_type_pattern = r'\[(.*?)\]'
        match = re.search(chart_type_pattern, chart_type_res)
        chart_type = []
        if match:
            chart_type = match.group(1).replace('"', '').split(', ')
            print(f"推荐的图表类型: {chart_type}")
        else:
            print("未找到推荐的图表类型")
        # 仅保留 chart_type 列表中的第一个元素
        if chart_type:
            chart_type = [chart_type[0]]
        # 将图表类型记录到日志中
        aiforbi_logger.info(f"chart_type resp: {chart_type}")
        chart_type_str = ', '.join(chart_type)  # 将列表转换为字符串
        aiforbi_logger.info(f"chart_type resp: {chart_type_str}")

        chart_template = load_chart_template(chart_type_str)
        prompt4Option = loader.fill_template("static_chart_option.txt",
                                      chart_type=chart_type,
                                      columns=columns,
                                      tab=tab,
                                      chart_template=chart_template)
        aiforbi_logger.info(f"static_chart_option prompt: {prompt4Option}")

        option = chat_model.chat(system="", history=[{"role": "user", "content": prompt4Option}], gen_conf={})
        aiforbi_logger.info(f"static_chart_option resp: {option}")
        return option