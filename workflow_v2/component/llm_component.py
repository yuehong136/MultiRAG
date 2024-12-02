from typing import Dict, Any, Optional, List
from dataclasses import dataclass

from alembic.command import history

from api.db import LLMType
from api.db.services.llm_service import TenantLLMService, LLMBundle
from workflow_v2.component.base_component import BaseComponent
from workflow_v2.utils import parse_template
from workflow_v2.workflow_logging_config import WorkflowContextLogger


@dataclass
class BatchInputConfig:
    """批处理输入配置"""
    name: str
    input_type: str
    schema_type: str
    source: str
    block_id: str
    ref_name: str

    @classmethod
    def from_input_config(cls, config: Dict[str, Any]) -> 'BatchInputConfig':
        """从输入配置创建实例"""
        return cls(
            name=config.get('name'),
            input_type=config.get('input', {}).get('type'),
            schema_type=config.get('input', {}).get('schema', {}).get('type'),
            source=config.get('input', {}).get('value', {}).get('content', {}).get('source'),
            block_id=config.get('input', {}).get('value', {}).get('content', {}).get('blockID'),
            ref_name=config.get('input', {}).get('value', {}).get('content', {}).get('name')
        )


@dataclass
class BatchConfig:
    """批处理配置类"""
    batch_enable: bool = False
    batch_size: int = 100
    concurrent_size: int = 10
    input_lists: List[BatchInputConfig] = None

    @classmethod
    def from_batch_config(cls, config: Dict[str, Any]) -> 'BatchConfig':
        """从批处理配置创建实例"""
        if not config:
            return cls()

        input_lists = [
            BatchInputConfig.from_input_config(input_config)
            for input_config in config.get('inputLists', [])
        ]

        return cls(
            batch_enable=config.get('batchEnable', False),
            batch_size=config.get('batchSize', 100),
            concurrent_size=config.get('concurrentSize', 10),
            input_lists=input_lists
        )


@dataclass
class LLMParams:
    """LLM参数配置类"""
    model_name: str
    temperature: float = 0.7
    top_p: float = 1.0
    max_tokens: Optional[int] = None
    frequency_penalty: float = 0.0
    response_format: int = 2
    prompt: Optional[str] = None
    system_prompt: Optional[str] = None
    model_type: Optional[int] = None
    generation_diversity: Optional[str] = None
    enable_chat_history: Optional[bool] = None

    @classmethod
    def _extract_param_value(cls, param: Dict[str, Any]) -> Any:
        """从参数对象中提取实际值"""
        return param.get('input', {}).get('value', {}).get('content')

    @classmethod
    def _convert_value(cls, value: Any, param_type: str) -> Any:
        """根据参数类型转换值"""
        if value is None:
            return None

        if param_type == 'float':
            return float(value)
        elif param_type == 'integer':
            return int(value)
        elif param_type == 'boolean':
            return bool(value)
        return value

    @classmethod
    def from_params_list(cls, params_list: List[Dict[str, Any]]) -> 'LLMParams':
        """从参数列表创建LLMParams实例"""
        # 创建参数映射
        param_map = {param['name']: {
            'value': cls._extract_param_value(param),
            'type': param.get('input', {}).get('type')
        } for param in params_list}

        return cls(
            model_name=cls._convert_value(
                param_map.get('modelName', {}).get('value') or 'gpt-3.5-turbo',
                param_map.get('modelName', {}).get('type')),
            temperature=cls._convert_value(
                param_map.get('temperature', {}).get('value', 0.7),
                param_map.get('temperature', {}).get('type')),
            top_p=cls._convert_value(
                param_map.get('topP', {}).get('value', 1.0),
                param_map.get('topP', {}).get('type')),
            max_tokens=cls._convert_value(
                param_map.get('maxTokens', {}).get('value'),
                param_map.get('maxTokens', {}).get('type')),
            frequency_penalty=cls._convert_value(
                param_map.get('frequencyPenalty', {}).get('value', 0.0),
                param_map.get('frequencyPenalty', {}).get('type')),
            response_format=cls._convert_value(
                param_map.get('responseFormat', {}).get('value', 2),
                param_map.get('responseFormat', {}).get('type')),
            prompt=cls._convert_value(
                param_map.get('prompt', {}).get('value'),
                param_map.get('prompt', {}).get('type')),
            system_prompt=cls._convert_value(
                param_map.get('systemPrompt', {}).get('value'),
                param_map.get('systemPrompt', {}).get('type')),
            model_type=cls._convert_value(
                param_map.get('modelType', {}).get('value'),
                param_map.get('modelType', {}).get('type')),
            generation_diversity=cls._convert_value(
                param_map.get('generationDiversity', {}).get('value'),
                param_map.get('generationDiversity', {}).get('type')),
            enable_chat_history=cls._convert_value(
                param_map.get('enableChatHistory', {}).get('value'),
                param_map.get('enableChatHistory', {}).get('type'))
        )


class LLMComponent(BaseComponent):
    """LLM组件"""

    def __init__(self, component_id: str, title: str, node_data: Dict[str, Any],
                 logger: WorkflowContextLogger, **kwargs):
        super().__init__(component_id, title, logger)
        self.llm_params: LLMParams = self._extract_llm_params(node_data)
        self.batch_config: BatchConfig = self._extract_batch_config(node_data)

        self.db = kwargs.get('db', None)
        self.user = kwargs.get('user', None)

    def _extract_llm_params(self, node_data: Dict[str, Any]) -> LLMParams:
        """从节点数据中提取LLM参数"""
        params_data = node_data['data']['inputs'].get('llmParam', [])
        return LLMParams.from_params_list(params_data)

    def _extract_batch_config(self, node_data: Dict[str, Any]) -> BatchConfig:
        """从节点数据中提取批处理配置"""
        batch_data = node_data['data']['inputs'].get('batch', {})
        return BatchConfig.from_batch_config(batch_data)

    async def execute(self) -> Dict[str, Any]:
        self.logger.info(f"LLMComponent {self.title} execute")
        self.logger.info(f"LLMComponent {self.title} inputs: {self.inputs}")
        model = "ep-20241008085710-w9hk2"
        api_key = TenantLLMService.get_api_key(self.db, self.user.id, model).api_key

        if self.batch_config.batch_enable:
            # 批处理
            pass
        else:
            actual_system_prompt = parse_template(self.llm_params.system_prompt, self.inputs)
            actual_prompt = parse_template(self.llm_params.prompt, self.inputs)
            chat_mdl = LLMBundle(self.db, self.user.id, LLMType.CHAT, model)
            history = [{"role": "user", "content": actual_prompt}]
            response = chat_mdl.chat(system=actual_system_prompt,
                                     history=history,
                                     gen_conf={"temperature": self.llm_params.temperature,
                                               "top_p": self.llm_params.top_p,
                                               "max_tokens": self.llm_params.max_tokens,
                                               "frequency_penalty": self.llm_params.frequency_penalty,
                                               })
            return {"output": response}
        return {"output": "LLM response"}
