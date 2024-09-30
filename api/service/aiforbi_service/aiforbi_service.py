import json
import logging

import pandas as pd
from fastapi import HTTPException

from api.db import LLMType
from api.db.services.llm_service import LLMBundle
from api.db.services.user_service import TenantService
from api.service.aiforbi_service.echarts_temp import load_chart_template
from api.service.aiforbi_service.llm_prompts import PromptTemplateLoader
from api.service.aiforbi_service.utils.llm_client import AsyncLLMClient
from api.settings import AIFORBI_MODEL_ID
from core.settings import aiforbi_logger


class AIForBIService:

    @staticmethod
    async def nl2sql(nl2sql_req_body, db, user_id):
        loader = PromptTemplateLoader()
        prompt = loader.fill_template("nl2sql_temp.txt", nl2sql_req_body)
        aiforbi_logger.info(f"nl2sql prompt: {prompt}")
        tenants = TenantService.get_by_user_id(db, user_id)
        if not tenants:
            raise HTTPException(status_code=404, detail="Tenant not found!")

        chat_model = LLMBundle(db, tenants[0]["tenant_id"], LLMType.CHAT, "ep-20240929094134-47cvj")
        resp = chat_model.chat(system="", history=[{"role": "user", "content": prompt}], gen_conf={})
        aiforbi_logger.info(f"nl2sql resp: {resp}")
        return resp

    @staticmethod
    async def chart_type(chart_type_req_body, db, user_id):
        pd_df = pd.DataFrame(chart_type_req_body.sql_result['data'])
        columns = chart_type_req_body.sql_result['metadata']['columns']
        loader = PromptTemplateLoader()
        prompt = loader.fill_template("chart_type_temp.txt",
                                      user_question=chart_type_req_body.user_question,
                                      columns=columns,
                                      sql_result=chart_type_req_body.sql_result['data'],
                                      sql_result_pandas=pd_df)
        aiforbi_logger.info(f"chart_type prompt: {prompt}")
        tenants = TenantService.get_by_user_id(db, user_id)
        if not tenants:
            raise HTTPException(status_code=404, detail="Tenant not found!")

        chat_model = LLMBundle(db, tenants[0]["tenant_id"], LLMType.CHAT, "ep-20240929094134-47cvj")
        resp = chat_model.chat(system="", history=[{"role": "user", "content": prompt}], gen_conf={})
        aiforbi_logger.info(f"chart_type resp: {resp}")
        chart_type_list = json.loads(resp)
        return chart_type_list

    @staticmethod
    async def dynamic_chart_option_function(dynamic_chart_option_function_req_body, db, user_id):
        chart_template = load_chart_template(dynamic_chart_option_function_req_body.chart_type)

        loader = PromptTemplateLoader()
        prompt = loader.fill_template("dynamic_chart_option_function_temp.txt",
                                      user_question=dynamic_chart_option_function_req_body.user_question,
                                      chart_type=dynamic_chart_option_function_req_body.chart_type,
                                      columns=dynamic_chart_option_function_req_body.sql_result['metadata']['columns'],
                                      chart_template=chart_template)
        aiforbi_logger.info(f"dynamic_chart_option_function prompt: {prompt}")
        tenants = TenantService.get_by_user_id(db, user_id)
        if not tenants:
            raise HTTPException(status_code=404, detail="Tenant not found!")

        chat_model = LLMBundle(db, tenants[0]["tenant_id"], LLMType.CHAT, "ep-20240929094134-47cvj")
        resp = chat_model.chat(system="", history=[{"role": "user", "content": prompt}], gen_conf={})
        aiforbi_logger.info(f"dynamic_chart_option_function resp: {resp}")
        return resp
