import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from api.db import LLMType
from api.db.services.llm_service import LLMBundle
from api.utils.prompt_template_util import PromptTemplateUtil

logger = logging.getLogger(__name__)


class ConversationQueryRewriter:
    """基于对话上下文改写用户追问的服务类"""

    JSON_PATTERN = re.compile(r'```json\s*([\s\S]*?)```')

    def __init__(self, db: Session, user_id: Any, prompt_dir: Optional[str] = None):
        """初始化会话查询改写器"""
        self.db = db
        self.user_id = user_id

        if prompt_dir is None:
            current_dir = os.path.dirname(__file__)
            self.prompt_dir = os.path.join(os.path.dirname(current_dir), "prompt")
        else:
            self.prompt_dir = prompt_dir

    def _extract_json_from_response(self, response: str) -> Tuple[Optional[Dict[str, Any]], bool]:
        """从LLM响应中提取JSON对象"""
        json_match = self.JSON_PATTERN.search(response)

        if json_match:
            json_str = json_match.group(1).strip()
            try:
                data = json.loads(json_str)
                if isinstance(data, dict):
                    return data, True
                logger.warning(f"Expected JSON object but got: {type(data)}")
                return {}, False
            except json.JSONDecodeError as error:
                logger.warning(f"Failed to parse JSON from code block: {error}")

        try:
            data = json.loads(response)
            if isinstance(data, dict):
                return data, True
            logger.warning(f"Expected JSON object but got: {type(data)}")
            return {}, False
        except json.JSONDecodeError:
            logger.warning("Failed to parse response as JSON")
            return {}, False

    @staticmethod
    def _to_bool(value: Any) -> bool:
        """宽松地解析布尔值"""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes"}
        return bool(value)

    def _normalize_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """验证并规整LLM返回的结构"""
        normalized: Dict[str, Any] = {
            "is_related": False,
            "rewritten_question": ""
        }

        if not isinstance(result, dict):
            logger.warning(f"Unexpected result type: {type(result)}")
            return normalized

        if "is_related" in result:
            normalized["is_related"] = self._to_bool(result["is_related"])

        rewritten = result.get("rewritten_question")
        if isinstance(rewritten, str):
            normalized["rewritten_question"] = rewritten.strip()

        reason = result.get("reason")
        if isinstance(reason, str) and reason.strip():
            normalized["reason"] = reason.strip()

        if not normalized["is_related"] and normalized["rewritten_question"]:
            logger.info("LLM marked question as unrelated but provided rewrite; clearing to comply with spec")
            normalized["rewritten_question"] = ""

        return normalized

    async def rewrite_question(
            self,
            conversation_history: List[Dict[str, Any]],
            new_user_question: str,
            llm_name: str
    ) -> Dict[str, Any]:
        """调用LLM判断新问题是否与上下文相关并进行改写"""
        try:
            llm_model_instance = LLMBundle(self.db, self.user_id, LLMType.CHAT, llm_name=llm_name)

            template_path = os.path.join(self.prompt_dir, "conversation_query_rewriter_prompt.txt")
            prompt_template = PromptTemplateUtil.load_template_from_file(template_path)

            if isinstance(conversation_history, list):
                history_payload = conversation_history
            else:
                logger.warning("conversation_history is not a list; defaulting to empty list")
                history_payload = []

            question_text = new_user_question if isinstance(new_user_question, str) else str(new_user_question or "")

            template_values = {
                "conversation_history": json.dumps(history_payload, ensure_ascii=False, indent=2),
                "new_user_question": question_text
            }

            prompt = PromptTemplateUtil.fill_template(prompt_template, template_values)
            history = [{"role": "user", "content": prompt}]

            gen_conf = {
                "temperature": 0.0,
                "top_p": 0.9,
                "max_tokens": 1024
            }

            response = await asyncio.to_thread(
                llm_model_instance.chat,
                system="",
                history=history,
                gen_conf=gen_conf
            )

            extracted_result, success = self._extract_json_from_response(response)

            if success:
                return self._normalize_result(extracted_result)

            logger.error("Failed to extract valid JSON from LLM response for conversation rewrite")
            return {
                "is_related": False,
                "rewritten_question": ""
            }

        except Exception as error:
            logger.error(f"Error in rewrite_question: {error}", exc_info=True)
            return {
                "is_related": False,
                "rewritten_question": ""
            }
