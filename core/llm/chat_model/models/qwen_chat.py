# coding=utf-8
"""
@project: multirag
@Author：龙
@file： qwen_chat.py
@date：2024/10/29 12:00
@desc:
"""
import os
import re

from dashscope import Generation

from core.nlp import is_chinese
from core.llm.chat_model.base import Base, LENGTH_NOTIFICATION_CN, LENGTH_NOTIFICATION_EN


class QWenChat(Base):
    def __init__(self, key, model_name=Generation.Models.qwen_turbo, **kwargs):
        import dashscope
        dashscope.api_key = key
        self.model_name = model_name

    def chat(self, system, history, gen_conf):
        stream_flag = str(os.environ.get('QWEN_CHAT_BY_STREAM', 'true')).lower() == 'true'
        if not stream_flag:
            from http import HTTPStatus
            if system:
                history.insert(0, {"role": "system", "content": system})

            response = Generation.call(
                self.model_name,
                messages=history,
                result_format='message',
                **gen_conf
            )
            ans = ""
            tk_count = 0
            if response.status_code == HTTPStatus.OK:
                ans += response.output.choices[0]['message']['content']
                tk_count += response.usage.total_tokens
                if response.output.choices[0].get("finish_reason", "") == "length":
                    if is_chinese([ans]):
                        ans += LENGTH_NOTIFICATION_CN
                    else:
                        ans += LENGTH_NOTIFICATION_EN
                return ans, tk_count

            return "**ERROR**: " + response.message, tk_count
        else:
            g = self._chat_streamly(system, history, gen_conf, incremental_output=True)
            result_list = list(g)
            error_msg_list = [item for item in result_list if str(item).find("**ERROR**") >= 0]
            if len(error_msg_list) > 0:
                return "**ERROR**: " + "".join(error_msg_list) , 0
            else:
                return "".join(result_list[:-1]), result_list[-1]

    def _chat_streamly(self, system, history, gen_conf, incremental_output=False):
        from http import HTTPStatus
        if system:
            history.insert(0, {"role": "system", "content": system})
        ans = ""
        tk_count = 0
        try:
            response = Generation.call(
                self.model_name,
                messages=history,
                result_format='message',
                stream=True,
                incremental_output=incremental_output,
                **gen_conf
            )
            for resp in response:
                if resp.status_code == HTTPStatus.OK:
                    ans = resp.output.choices[0]['message']['content']
                    tk_count = resp.usage.total_tokens
                    if resp.output.choices[0].get("finish_reason", "") == "length":
                        if is_chinese([ans]):
                            ans += LENGTH_NOTIFICATION_CN
                        else:
                            ans += LENGTH_NOTIFICATION_EN
                    yield ans
                else:
                    yield ans + "\n**ERROR**: " + resp.message if not re.search(r" (key|quota)", str(resp.message).lower()) else "Out of credit. Please set the API key in **settings > Model providers.**"
        except Exception as e:
            yield ans + "\n**ERROR**: " + str(e)

        yield tk_count

    def chat_streamly(self, system, history, gen_conf):
        return self._chat_streamly(system, history, gen_conf)
