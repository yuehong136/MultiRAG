# coding=utf-8
"""
@project: multirag
@Author：龙
@file： qwen_chat.py
@date：2024/10/29 12:00
@desc:
"""
import json
import logging
import os
import re

from dashscope import Generation

from core.nlp import is_chinese, is_english
from core.llm.chat_model.base import Base, LENGTH_NOTIFICATION_CN, LENGTH_NOTIFICATION_EN


class QWenChat(Base):
    def __init__(self, key, model_name=Generation.Models.qwen_turbo, **kwargs):
        super().__init__(key, model_name, base_url=None)

        import dashscope

        dashscope.api_key = key
        self.model_name = model_name
        if self.is_reasoning_model(self.model_name):
            super().__init__(key, model_name, "https://dashscope.aliyuncs.com/compatible-mode/v1")

    def chat_with_tools(self, system: str, history: list, gen_conf: dict) -> tuple[str, int]:
        if "max_tokens" in gen_conf:
            del gen_conf["max_tokens"]
        # if self.is_reasoning_model(self.model_name):
        #     return super().chat(system, history, gen_conf)

        stream_flag = str(os.environ.get("QWEN_CHAT_BY_STREAM", "true")).lower() == "true"
        if not stream_flag:
            from http import HTTPStatus

            tools = self.tools

            if system:
                history.insert(0, {"role": "system", "content": system})

            response = Generation.call(self.model_name, messages=history, result_format="message", tools=tools, **gen_conf)
            ans = ""
            tk_count = 0
            if response.status_code == HTTPStatus.OK:
                assistant_output = response.output.choices[0].message
                if not ans and "tool_calls" not in assistant_output and "reasoning_content" in assistant_output:
                    ans += "<think>" + ans + "</think>"
                ans += response.output.choices[0].message.content

                if "tool_calls" not in assistant_output:
                    tk_count += self.total_token_count(response)
                    if response.output.choices[0].get("finish_reason", "") == "length":
                        if is_chinese([ans]):
                            ans += LENGTH_NOTIFICATION_CN
                        else:
                            ans += LENGTH_NOTIFICATION_EN
                    return ans, tk_count

                tk_count += self.total_token_count(response)
                history.append(assistant_output)

                while "tool_calls" in assistant_output:
                    tool_info = {"content": "", "role": "tool", "tool_call_id": assistant_output.tool_calls[0]["id"]}
                    tool_name = assistant_output.tool_calls[0]["function"]["name"]
                    if tool_name:
                        arguments = json.loads(assistant_output.tool_calls[0]["function"]["arguments"])
                        tool_info["content"] = self.toolcall_session.tool_call(name=tool_name, arguments=arguments)
                    history.append(tool_info)

                    response = Generation.call(self.model_name, messages=history, result_format="message", tools=self.tools, **gen_conf)
                    if response.output.choices[0].get("finish_reason", "") == "length":
                        tk_count += self.total_token_count(response)
                        if is_chinese([ans]):
                            ans += LENGTH_NOTIFICATION_CN
                        else:
                            ans += LENGTH_NOTIFICATION_EN
                        return ans, tk_count

                    tk_count += self.total_token_count(response)
                    assistant_output = response.output.choices[0].message
                    if assistant_output.content is None:
                        assistant_output.content = ""
                    history.append(response)
                ans += assistant_output["content"]
                return ans, tk_count
            else:
                return "**ERROR**: " + response.message, tk_count
        else:
            result_list = []
            for result in self._chat_streamly_with_tools(system, history, gen_conf, incremental_output=True):
                result_list.append(result)
            error_msg_list = [result for result in result_list if str(result).find("**ERROR**") >= 0]
            if len(error_msg_list) > 0:
                return "**ERROR**: " + "".join(error_msg_list), 0
            else:
                return "".join(result_list[:-1]), result_list[-1]

    def chat(self, system, history, gen_conf):
        if "max_tokens" in gen_conf:
            del gen_conf["max_tokens"]
        if self.is_reasoning_model(self.model_name):
            return super().chat(system, history, gen_conf)

        stream_flag = str(os.environ.get("QWEN_CHAT_BY_STREAM", "true")).lower() == "true"
        if not stream_flag:
            from http import HTTPStatus

            if system:
                history.insert(0, {"role": "system", "content": system})

            response = Generation.call(self.model_name, messages=history, result_format="message", **gen_conf)
            ans = ""
            tk_count = 0
            if response.status_code == HTTPStatus.OK:
                ans += response.output.choices[0]["message"]["content"]
                tk_count += self.total_token_count(response)
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
                return "**ERROR**: " + "".join(error_msg_list), 0
            else:
                return "".join(result_list[:-1]), result_list[-1]

    def _wrap_toolcall_message(self, old_message, message):
        if not old_message:
            return message
        tool_call_id = message["tool_calls"][0].get("id")
        if tool_call_id:
            old_message.tool_calls[0]["id"] = tool_call_id
        function = message.tool_calls[0]["function"]
        if function:
            if function.get("name"):
                old_message.tool_calls[0]["function"]["name"] = function["name"]
            if function.get("arguments"):
                old_message.tool_calls[0]["function"]["arguments"] += function["arguments"]
        return old_message

    def _chat_streamly_with_tools(self, system: str, history: list, gen_conf: dict, incremental_output=True):
        from http import HTTPStatus

        if system:
            history.insert(0, {"role": "system", "content": system})
        if "max_tokens" in gen_conf:
            del gen_conf["max_tokens"]
        ans = ""
        tk_count = 0
        try:
            response = Generation.call(self.model_name, messages=history, result_format="message", tools=self.tools, stream=True, incremental_output=incremental_output, **gen_conf)
            tool_info = {"content": "", "role": "tool"}
            toolcall_message = None
            tool_name = ""
            tool_arguments = ""
            finish_completion = False
            reasoning_start = False
            while not finish_completion:
                for resp in response:
                    if resp.status_code == HTTPStatus.OK:
                        assistant_output = resp.output.choices[0].message
                        ans = resp.output.choices[0].message.content
                        if not ans and "tool_calls" not in assistant_output and "reasoning_content" in assistant_output:
                            ans = resp.output.choices[0].message.reasoning_content
                            if not reasoning_start:
                                reasoning_start = True
                                ans = "<think>" + ans
                            else:
                                ans = ans + "</think>"

                        if "tool_calls" not in assistant_output:
                            reasoning_start = False
                            tk_count += self.total_token_count(resp)
                            if resp.output.choices[0].get("finish_reason", "") == "length":
                                if is_chinese([ans]):
                                    ans += LENGTH_NOTIFICATION_CN
                                else:
                                    ans += LENGTH_NOTIFICATION_EN
                            finish_reason = resp.output.choices[0]["finish_reason"]
                            if finish_reason == "stop":
                                finish_completion = True
                                yield ans
                                break
                            yield ans
                            continue

                        tk_count += self.total_token_count(resp)
                        toolcall_message = self._wrap_toolcall_message(toolcall_message, assistant_output)
                        if "tool_calls" in assistant_output:
                            tool_call_finish_reason = resp.output.choices[0]["finish_reason"]
                            if tool_call_finish_reason == "tool_calls":
                                try:
                                    tool_arguments = json.loads(toolcall_message.tool_calls[0]["function"]["arguments"])
                                except Exception as e:
                                    logging.exception(msg="_chat_streamly_with_tool tool call error")
                                    yield ans + "\n**ERROR**: " + str(e)
                                    finish_completion = True
                                    break

                                tool_name = toolcall_message.tool_calls[0]["function"]["name"]
                                history.append(toolcall_message)
                                tool_info["content"] = self.toolcall_session.tool_call(name=tool_name, arguments=tool_arguments)
                                history.append(tool_info)
                                tool_info = {"content": "", "role": "tool"}
                                tool_name = ""
                                tool_arguments = ""
                                toolcall_message = None
                                response = Generation.call(self.model_name, messages=history, result_format="message", tools=self.tools, stream=True, incremental_output=incremental_output, **gen_conf)
                    else:
                        yield (
                            ans + "\n**ERROR**: " + resp.output.choices[0].message
                            if not re.search(r" (key|quota)", str(resp.message).lower())
                            else "Out of credit. Please set the API key in **settings > Model providers.**"
                        )
        except Exception as e:
            logging.exception(msg="_chat_streamly_with_tool")
            yield ans + "\n**ERROR**: " + str(e)
        yield tk_count

    def _chat_streamly(self, system, history, gen_conf, incremental_output=True):
        from http import HTTPStatus

        if system:
            history.insert(0, {"role": "system", "content": system})
        if "max_tokens" in gen_conf:
            del gen_conf["max_tokens"]
        ans = ""
        tk_count = 0
        try:
            response = Generation.call(self.model_name, messages=history, result_format="message", stream=True, incremental_output=incremental_output, **gen_conf)
            for resp in response:
                if resp.status_code == HTTPStatus.OK:
                    ans = resp.output.choices[0]["message"]["content"]
                    tk_count = self.total_token_count(resp)
                    if resp.output.choices[0].get("finish_reason", "") == "length":
                        if is_chinese([ans]):
                            ans += LENGTH_NOTIFICATION_CN
                        else:
                            ans += LENGTH_NOTIFICATION_EN
                    yield ans
                else:
                    yield (
                        ans + "\n**ERROR**: " + resp.message
                        if not re.search(r" (key|quota)", str(resp.message).lower())
                        else "Out of credit. Please set the API key in **settings > Model providers.**"
                    )
        except Exception as e:
            yield ans + "\n**ERROR**: " + str(e)

        yield tk_count

    def chat_streamly_with_tools(self, system: str, history: list, gen_conf: dict, incremental_output=True):
        if "max_tokens" in gen_conf:
            del gen_conf["max_tokens"]

        for txt in self._chat_streamly_with_tools(system, history, gen_conf, incremental_output=incremental_output):
            yield txt

    def chat_streamly(self, system, history, gen_conf):
        if "max_tokens" in gen_conf:
            del gen_conf["max_tokens"]
        if self.is_reasoning_model(self.model_name):
            return super().chat_streamly(system, history, gen_conf)

        return self._chat_streamly(system, history, gen_conf)

    @staticmethod
    def is_reasoning_model(model_name: str) -> bool:
        return any(
            [
                model_name.lower().find("deepseek") >= 0,
                model_name.lower().find("qwq") >= 0 and model_name.lower() != "qwq-32b-preview",
            ]
        )