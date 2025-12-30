import logging
import json

from fastapi import HTTPException
from sqlalchemy.orm import Session

from common.constants import LLMType
from api.db.services.llm_service import LLMBundle
from api.db.services.user_service import TenantService
from api.service.ai_translate_service.llm_prompts import PromptTemplateLoader
from errors.exceptions import AITranslateException


class AITranslateService:

    @staticmethod
    async def ai_translate(ai_translate_req_body, db: Session, user_id, llm_name):
        logging.info(f"ai_translate_req_body: {ai_translate_req_body}")
        loader = PromptTemplateLoader()
        prompt = loader.fill_template("ai_translate_temp.txt", input=ai_translate_req_body.zh_text)

        tenants = TenantService.get_info_by(db, user_id)
        if not tenants:
            raise HTTPException(status_code=404, detail="Tenant not found!")

        chat_model = LLMBundle(db, tenants[0]["tenant_id"], LLMType.CHAT, llm_name)
        resp = chat_model.chat(system="", history=[{"role": "user", "content": prompt}], gen_conf={})
        logging.info(f"ai_translate resp: {resp}")
        if contains_chinese_unicode(resp):
            raise AITranslateException(message="翻译内容包含中文字符，请重新翻译")
        specific_chars = ['/']
        if any(char in ai_translate_req_body.zh_text for char in specific_chars):
            return resp
        else:
            return get_first_part_from_translated_text(resp)

    @staticmethod
    async def ai_batch_translate(ai_batch_translate_req_body, db: Session, user_id, llm_name):
        logging.info(f"ai_batch_translate_req_body: {ai_batch_translate_req_body}")
        loader = PromptTemplateLoader()
        prompt = loader.fill_template("ai_batch_translate_temp.txt", input=ai_batch_translate_req_body.zh_text_list)
        tenants = TenantService.get_info_by(db, user_id)
        if not tenants:
            raise HTTPException(status_code=404, detail="Tenant not found!")

        chat_model = LLMBundle(db, tenants[0]["tenant_id"], LLMType.CHAT, llm_name)
        resp = chat_model.chat(system="", history=[{"role": "user", "content": prompt}], gen_conf={})
        logging.info(f"ai_batch_translate resp: {resp}")
        if contains_chinese_unicode(resp):
            raise AITranslateException(message="翻译内容包含中文字符，请重新翻译")

        resp = extract_content_from_markdown(resp)

        # 响应示例：[
        #   "Export Data",
        #   "Please select the target field",
        #   "Environmental Detection Items",
        #   "Detection Result",
        #   "Detect whether the database connectivity is normal"
        # ]
        #         将字符串数组转换为列表
        translate_list = json.loads(resp)
        # specific_chars = ['/', '|', '-', ',', '，', '、']
        specific_chars = ['/']
        for i in range(len(translate_list)):
            # 有时候一个中文输入可能会输出多个英文，这种情况下多是使用/分割，比如：Success/Succeed
            # 所以如果翻译中包含多个英文选项，取第一个选项
            # 但如果原本的输入中就已经包含了这些/，则不处理，直接跳过
            if any(char in ai_batch_translate_req_body.zh_text_list[i] for char in specific_chars):
                continue
            else:
                translate_list[i] = get_first_part_from_translated_text(translate_list[i])
        return translate_list


def get_first_part_from_translated_text(s, separators=['/']) -> str:
    """
    从给定的字符串中提取第一个部分，使用指定的分隔符列表。

    :param s: 输入字符串
    :param separators: 可能的分隔符列表
    :return: 提取的第一部分
    """
    # 查找第一个出现的分隔符
    first_separator_index = len(s)
    for sep in separators:
        index = s.find(sep)
        if index != -1 and index < first_separator_index:
            first_separator_index = index

    # 提取并返回第一部分
    return s[:first_separator_index].strip()


def contains_chinese_unicode(text):
    """
    使用 Unicode 范围检测字符串中是否包含中文字符
    """
    for char in text:
        if '\u4e00' <= char <= '\u9fff':
            return True
    return False


import re


def extract_content_from_markdown(text):
    # 使用正则表达式匹配 ```json 和 ``` 之间的内容
    match = re.search(r'```json\s*([\s\S]*?)\s*```', text)

    if match:
        # 如果找到匹配，返回 ``` 之间的内容
        return match.group(1).strip()
    else:
        # 如果没有匹配到 ```json，直接返回原始内容
        return text.strip()
