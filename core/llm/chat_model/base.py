import os
import random
from abc import ABC
import openai
from openai import OpenAI
from core.nlp import is_chinese, is_english
from core.utils import num_tokens_from_string
import logging
import time


# Error message constants
ERROR_PREFIX = "**ERROR**"
ERROR_RATE_LIMIT = "RATE_LIMIT_EXCEEDED"
ERROR_AUTHENTICATION = "AUTH_ERROR"
ERROR_INVALID_REQUEST = "INVALID_REQUEST"
ERROR_SERVER = "SERVER_ERROR"
ERROR_TIMEOUT = "TIMEOUT"
ERROR_CONNECTION = "CONNECTION_ERROR"
ERROR_MODEL = "MODEL_ERROR"
ERROR_CONTENT_FILTER = "CONTENT_FILTERED"
ERROR_QUOTA = "QUOTA_EXCEEDED"
ERROR_MAX_RETRIES = "MAX_RETRIES_EXCEEDED"
ERROR_GENERIC = "GENERIC_ERROR"


LENGTH_NOTIFICATION_CN = "······\n由于大模型的上下文窗口大小限制，回答已经被大模型截断。"
LENGTH_NOTIFICATION_EN = "...\nThe answer is truncated by your chosen LLM due to its limitation on context length."


class Base(ABC):
    def __init__(self, key, model_name, base_url):
        timeout = int(os.environ.get("LM_TIMEOUT_SECONDS", 600))
        self.client = OpenAI(api_key=key, base_url=base_url, timeout=timeout)
        self.model_name = model_name
        # Configure retry parameters
        self.max_retries = int(os.environ.get("LLM_MAX_RETRIES", 5))
        self.base_delay = float(os.environ.get("LLM_BASE_DELAY", 2.0))

    def _get_delay(self, attempt):
        """Calculate retry delay time"""
        return self.base_delay * (2**attempt) + random.uniform(0, 0.5)

    def _classify_error(self, error):
        """Classify error based on error message content"""
        error_str = str(error).lower()

        if "rate limit" in error_str or "429" in error_str or "tpm limit" in error_str or "too many requests" in error_str or "requests per minute" in error_str:
            return ERROR_RATE_LIMIT
        elif "auth" in error_str or "key" in error_str or "apikey" in error_str or "401" in error_str or "forbidden" in error_str or "permission" in error_str:
            return ERROR_AUTHENTICATION
        elif "invalid" in error_str or "bad request" in error_str or "400" in error_str or "format" in error_str or "malformed" in error_str or "parameter" in error_str:
            return ERROR_INVALID_REQUEST
        elif "server" in error_str or "502" in error_str or "503" in error_str or "504" in error_str or "500" in error_str or "unavailable" in error_str:
            return ERROR_SERVER
        elif "timeout" in error_str or "timed out" in error_str:
            return ERROR_TIMEOUT
        elif "connect" in error_str or "network" in error_str or "unreachable" in error_str or "dns" in error_str:
            return ERROR_CONNECTION
        elif "quota" in error_str or "capacity" in error_str or "credit" in error_str or "billing" in error_str or "limit" in error_str and "rate" not in error_str:
            return ERROR_QUOTA
        elif "filter" in error_str or "content" in error_str or "policy" in error_str or "blocked" in error_str or "safety" in error_str or "inappropriate" in error_str:
            return ERROR_CONTENT_FILTER
        elif "model" in error_str or "not found" in error_str or "does not exist" in error_str or "not available" in error_str:
            return ERROR_MODEL
        else:
            return ERROR_GENERIC

    def chat(self, system, history, gen_conf):
        if system:
            history.insert(0, {"role": "system", "content": system})
        if "max_tokens" in gen_conf:
            del gen_conf["max_tokens"]

        # Implement exponential backoff retry strategy
        for attempt in range(self.max_retries):
            try:
                response = self.client.chat.completions.create(model=self.model_name, messages=history, **gen_conf)

                if any([not response.choices, not response.choices[0].message,
                        not response.choices[0].message.content]):
                    return "", 0
                ans = response.choices[0].message.content.strip()
                if response.choices[0].finish_reason == "length":
                    if is_chinese(ans):
                        ans += LENGTH_NOTIFICATION_CN
                    else:
                        ans += LENGTH_NOTIFICATION_EN
                return ans, self.total_token_count(response)
            except Exception as e:
                # Classify the error
                error_code = self._classify_error(e)

                # Check if it's a rate limit error or server error and not the last attempt
                should_retry = (
                                           error_code == ERROR_RATE_LIMIT or error_code == ERROR_SERVER) and attempt < self.max_retries - 1

                if should_retry:
                    delay = self._get_delay(attempt)
                    logging.warning(
                        f"Error: {error_code}. Retrying in {delay:.2f} seconds... (Attempt {attempt + 1}/{self.max_retries})")
                    time.sleep(delay)
                else:
                    # For non-rate limit errors or the last attempt, return an error message
                    if attempt == self.max_retries - 1:
                        error_code = ERROR_MAX_RETRIES
                    return f"{ERROR_PREFIX}: {error_code} - {str(e)}", 0

    def chat_streamly(self, system, history, gen_conf):
        if system:
            history.insert(0, {"role": "system", "content": system})
        if "max_tokens" in gen_conf:
            del gen_conf["max_tokens"]
        ans = ""
        total_tokens = 0
        reasoning_start = False
        try:
            response = self.client.chat.completions.create(model=self.model_name, messages=history, stream=True, **gen_conf)
            for resp in response:
                if not resp.choices:
                    continue
                if not resp.choices[0].delta.content:
                    resp.choices[0].delta.content = ""
                if hasattr(resp.choices[0].delta, "reasoning_content") and resp.choices[0].delta.reasoning_content:
                    ans = ""
                    if not reasoning_start:
                        reasoning_start = True
                        ans = "<think>"
                    ans += resp.choices[0].delta.reasoning_content + "</think>"
                else:
                    reasoning_start = False
                    ans = resp.choices[0].delta.content

                tol = self.total_token_count(resp)
                if not tol:
                    total_tokens += num_tokens_from_string(resp.choices[0].delta.content)
                else:
                    total_tokens = tol

                if resp.choices[0].finish_reason == "length":
                    if is_chinese(ans):
                        ans += LENGTH_NOTIFICATION_CN
                    else:
                        ans += LENGTH_NOTIFICATION_EN
                yield ans

        except openai.APIError as e:
            yield ans + "\n**ERROR**: " + str(e)

        yield total_tokens

    def total_token_count(self, resp):
        try:
            return resp.usage.total_tokens
        except Exception:
            pass
        try:
            return resp["usage"]["total_tokens"]
        except Exception:
            pass
            return 0

    def _calculate_dynamic_ctx(self, history):
        """Calculate dynamic context window size"""

        def count_tokens(text):
            """Calculate token count for text"""
            # Simple calculation: 1 token per ASCII character
            # 2 tokens for non-ASCII characters (Chinese, Japanese, Korean, etc.)
            total = 0
            for char in text:
                if ord(char) < 128:  # ASCII characters
                    total += 1
                else:  # Non-ASCII characters (Chinese, Japanese, Korean, etc.)
                    total += 2
            return total

        # Calculate total tokens for all messages
        total_tokens = 0
        for message in history:
            content = message.get("content", "")
            # Calculate content tokens
            content_tokens = count_tokens(content)
            # Add role marker token overhead
            role_tokens = 4
            total_tokens += content_tokens + role_tokens

        # Apply 1.2x buffer ratio
        total_tokens_with_buffer = int(total_tokens * 1.2)

        if total_tokens_with_buffer <= 8192:
            ctx_size = 8192
        else:
            ctx_multiplier = (total_tokens_with_buffer // 8192) + 1
            ctx_size = ctx_multiplier * 8192

        return ctx_size