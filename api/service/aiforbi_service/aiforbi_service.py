import json
import logging

import pandas as pd

from api.service.aiforbi_service.echarts_temp import load_chart_template
from api.service.aiforbi_service.llm_prompts import PromptTemplateLoader
from api.service.aiforbi_service.utils.llm_client import AsyncLLMClient
from api.settings import AIFORBI_MODEL_ID
from core.settings import aiforbi_logger


class AIForBIService:

    @staticmethod
    async def nl2sql(nl2sql_req_body):
        loader = PromptTemplateLoader()
        prompt = loader.fill_template("nl2sql_temp.txt", nl2sql_req_body)
        aiforbi_logger.info(f"nl2sql prompt: {prompt}")
        llm_client = AsyncLLMClient(AIFORBI_MODEL_ID)
        resp = await llm_client.standard_request(user_content=prompt)
        aiforbi_logger.info(f"nl2sql resp: {resp}")
        return resp

    @staticmethod
    async def chart_type(chart_type_req_body):
        pd_df = pd.DataFrame(chart_type_req_body.sql_result['data'])
        columns = chart_type_req_body.sql_result['metadata']['columns']
        loader = PromptTemplateLoader()
        prompt = loader.fill_template("chart_type_temp.txt",
                                      user_question=chart_type_req_body.user_question,
                                      columns=columns,
                                      sql_result=chart_type_req_body.sql_result['data'],
                                      sql_result_pandas=pd_df)
        aiforbi_logger.info(f"chart_type prompt: {prompt}")
        llm_client = AsyncLLMClient(AIFORBI_MODEL_ID)
        resp = await llm_client.standard_request(user_content=prompt)
        aiforbi_logger.info(f"chart_type resp: {resp}")
        chart_type_list = json.loads(resp)
        return chart_type_list

    @staticmethod
    async def dynamic_chart_option_function(dynamic_chart_option_function_req_body):
        chart_template = load_chart_template(dynamic_chart_option_function_req_body.chart_type)

        loader = PromptTemplateLoader()
        prompt = loader.fill_template("dynamic_chart_option_function_temp.txt",
                                      user_question=dynamic_chart_option_function_req_body.user_question,
                                      chart_type=dynamic_chart_option_function_req_body.chart_type,
                                      columns=dynamic_chart_option_function_req_body.sql_result['metadata']['columns'],
                                      chart_template=chart_template)
        aiforbi_logger.info(f"dynamic_chart_option_function prompt: {prompt}")
        llm_client = AsyncLLMClient(AIFORBI_MODEL_ID)
        resp = await llm_client.standard_request(user_content=prompt)
        aiforbi_logger.info(f"dynamic_chart_option_function resp: {resp}")
        return resp
