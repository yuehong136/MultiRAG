import logging
import re

import pandas as pd
from fastapi import HTTPException

from api.db import LLMType
from api.db.services.llm_service import LLMBundle
from api.db.services.tenant_llm_service import TenantLLMService
from api.db.services.user_service import TenantService
from api.service.aiforbi_service.echarts_temp import load_chart_template
from api.service.aiforbi_service.llm_prompts import PromptTemplateLoader
from api.service.aiforbi_service.utils.extract_brackets import extract_brackets
from api.service.aiforbi_service.utils.extract_js_func import extract_js_function
from api.service.aiforbi_service.utils.extract_sql import extract_sql


class AIForBIService:

    @staticmethod
    async def nl2sql(nl2sql_req_body, db, user_id, llm_name):
        loader = PromptTemplateLoader()
        prompt = loader.fill_template("nl2sql_temp.txt", nl2sql_req_body)
        logging.info(f"====nl2sql prompt: {prompt}")
        tenants = TenantService.get_info_by(db, user_id)
        if not tenants:
            raise HTTPException(status_code=404, detail="Tenant not found!")

        chat_model = LLMBundle(db, tenants[0]["tenant_id"], LLMType.CHAT, llm_name)
        resp = chat_model.chat(system="", history=[{"role": "user", "content": prompt}], gen_conf={})
        logging.info(f"====nl2sql resp:\n {resp}")
        sql = extract_sql(resp)
        logging.info("====经过提取后的SQL语句：\n" + sql)
        return sql

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
        logging.info(f"====chart_type prompt:\n {prompt}")
        tenants = TenantService.get_info_by(db, user_id)
        if not tenants:
            raise HTTPException(status_code=404, detail="Tenant not found!")

        chat_model = LLMBundle(db, tenants[0]["tenant_id"], LLMType.CHAT, llm_name)
        resp = chat_model.chat(system="", history=[{"role": "user", "content": prompt}], gen_conf={})
        logging.info(f"====chart_type resp:\n {resp}")
        chart_type_list = extract_brackets(resp, merge=True)
        return chart_type_list

    @staticmethod
    async def dynamic_chart_option_function(dynamic_chart_option_function_req_body, db, user_id, llm_name):
        chart_template = load_chart_template(dynamic_chart_option_function_req_body.chart_type)

        loader = PromptTemplateLoader()
        tenants = TenantService.get_info_by(db, user_id)
        llm_list = TenantLLMService.get_all(db)
        is_local_llm = False
        for llm in llm_list:
            if llm.llm_name == llm_name and llm.llm_factory in ["OpenAI-API-Compatible", "Ollama", "Xinference"]:
                is_local_llm = True
                break
        logging.info(f"is_local_llm: {is_local_llm}")

        # 本地模型和在线模型都使用dynamic_chart_option_function_temp_for_local_llm.txt
        chart_data = {
            "columns": dynamic_chart_option_function_req_body.sql_result["metadata"]["columns"],
            "data": dynamic_chart_option_function_req_body.sql_result["data"]
        }
        prompt = loader.fill_template("dynamic_chart_option_function_temp_for_local_llm.txt",
                                      user_question=dynamic_chart_option_function_req_body.user_question,
                                      chart_type=dynamic_chart_option_function_req_body.chart_type,
                                      columns=dynamic_chart_option_function_req_body.sql_result['metadata'][
                                          'columns'],
                                      chart_template=chart_template,
                                      chart_data=chart_data)

        logging.info(f"====dynamic_chart_option_function prompt: {prompt}")

        if not tenants:
            raise HTTPException(status_code=404, detail="Tenant not found!")

        chat_model = LLMBundle(db, tenants[0]["tenant_id"], LLMType.CHAT, llm_name)
        resp = chat_model.chat(system="", history=[{"role": "user", "content": prompt}], gen_conf={})
        logging.info(f"====dynamic_chart_option_function resp:\n {resp}")
        js_func = extract_js_function(resp, function_name="generateChartOption")
        logging.info("====经过提取后的JS函数：\n" + js_func)
        return js_func

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
        logging.info(f"chart_type prompt: {prompt4ChartType}")

        chart_type_res = chat_model.chat(system="", history=[{"role": "user", "content": prompt4ChartType}],
                                         gen_conf={})
        logging.info(f"大模型返回的图表类型：{chart_type_res}")
        chart_type_list = extract_brackets(chart_type_res)
        chart_template = load_chart_template(chart_type_list[0])
        prompt4Option = loader.fill_template("static_chart_option.txt",
                                             chart_type=chart_type_list[0],
                                             columns=columns,
                                             tab=tab,
                                             chart_template=chart_template)
        logging.info(f"static_chart_option prompt: {prompt4Option}")

        option = chat_model.chat(system="", history=[{"role": "user", "content": prompt4Option}], gen_conf={})
        logging.info(f"static_chart_option resp: {option}")
        return option
