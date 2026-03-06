import asyncio
from typing import Any
from dataclasses import dataclass
import copy

from api.db.services.llm_service import LLMBundle
from common.constants import LLMType
from common.misc_utils import thread_pool_exec
from workflow_v2.component.base_component import BaseComponent
from workflow_v2.utils import parse_template, match_parameters, dict_arrays_to_array_dicts, map_schema_with_values
from workflow_v2.workflow_logging_config import WorkflowContextLogger


@dataclass
class BatchConfig:
    """批处理配置类"""
    batch_enable: bool = False
    batch_size: int = 100
    concurrent_size: int = 10
    input_lists: list[dict[str, Any]] = None

    @classmethod
    def from_batch_config(cls, config: dict[str, Any]) -> 'BatchConfig':
        """从批处理配置创建实例"""
        if not config:
            return cls()

        input_lists = config.get('inputLists', [])
        if not isinstance(input_lists, list):
            input_lists = [input_lists]

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
    max_tokens: int | None = None
    frequency_penalty: float = 0.0
    response_format: int = 2
    prompt: str | None = None
    system_prompt: str | None = None
    model_type: int | None = None
    generation_diversity: str | None = None
    enable_chat_history: bool | None = None

    @classmethod
    def _extract_param_value(cls, param: dict[str, Any]) -> Any:
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
    def from_params_list(cls, params_list: list[dict[str, Any]]) -> 'LLMParams':
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

    def __init__(self, component_id: str, title: str, node_data: dict[str, Any],
                 logger: WorkflowContextLogger, **kwargs):
        super().__init__(component_id, title, logger)
        self.llm_params: LLMParams = self._extract_llm_params(node_data)
        self.batch_config: BatchConfig = self._extract_batch_config(node_data)

        self.db = kwargs.get('db', None)
        self.user = kwargs.get('user', None)

    def _extract_llm_params(self, node_data: dict[str, Any]) -> LLMParams:
        """从节点数据中提取LLM参数"""
        params_data = node_data['data']['inputs'].get('llmParam', [])
        return LLMParams.from_params_list(params_data)

    def _extract_batch_config(self, node_data: dict[str, Any]) -> BatchConfig:
        """从节点数据中提取批处理配置"""
        batch_data = node_data['data']['inputs'].get('batch', {})
        return BatchConfig.from_batch_config(batch_data)

    async def execute(self) -> dict[str, Any]:
        self.logger.info(f"LLMComponent {self.title} execute")
        self.logger.info(f"LLMComponent {self.title} inputs: {self.inputs}")
        model = self.llm_params.model_name

        if self.batch_config.batch_enable:
            input_value_dict_list = []

            batch_param_list = dict_arrays_to_array_dicts(match_parameters(self.batch_config.input_lists, self.nodes))
            for batch_param_value in batch_param_list:
                temp_nodes = copy.deepcopy(self.nodes)
                temp_node_output = temp_nodes.get(self.workflow_node.id).output
                if temp_node_output:
                    temp_nodes.get(self.workflow_node.id).output.update(batch_param_value)
                else:
                    temp_nodes.get(self.workflow_node.id).output = batch_param_value
                result = match_parameters(self.workflow_node.input_schema, temp_nodes)
                input_value_dict_list.append(result)

            self.workflow_node.input = input_value_dict_list

            system_prompt_list = [parse_template(self.llm_params.system_prompt, item) for item in input_value_dict_list]
            prompt_list = [parse_template(self.llm_params.prompt, item) for item in input_value_dict_list]

            # 使用asyncio.gather并发处理
            tasks = []
            for i in range(len(input_value_dict_list)):
                tasks.append(
                    process_single_chat_async(
                        self.db,
                        self.user.id,
                        model,
                        system_prompt_list[i],
                        prompt_list[i],
                        self.llm_params,
                        input_value_dict_list[i]
                    )
                )

            # 限制并发数量
            execute_info = []
            for i in range(0, len(tasks), self.batch_config.concurrent_size):
                batch_tasks = tasks[i:i + self.batch_config.concurrent_size]
                batch_results = await asyncio.gather(*batch_tasks)
                execute_info.extend(batch_results)

            # 转换为目标格式，保持原始顺序
            output_list = [{"output": item["output"]} for item in execute_info]

            # 批处理
            return {"outputList": output_list}
        else:
            actual_system_prompt = parse_template(self.llm_params.system_prompt, self.inputs)
            actual_prompt = parse_template(self.llm_params.prompt, self.inputs)
            chat_mdl = LLMBundle(self.db, self.user.id, LLMType.CHAT, model)
            history = [{"role": "user", "content": actual_prompt}]

            # 使用 asyncio.to_thread 防止阻塞主事件循环
            response = await thread_pool_exec(
                chat_mdl.chat,
                system=actual_system_prompt,
                history=history,
                gen_conf={
                    "temperature": self.llm_params.temperature,
                    "top_p": self.llm_params.top_p,
                    "max_tokens": self.llm_params.max_tokens,
                    "frequency_penalty": self.llm_params.frequency_penalty,
                }
            )

            return {"output": response}

    async def execute_alone(self, input_value: dict, batch_value: dict | None = None) -> dict:
        self.logger.info(f"LLMComponent {self.title} execute")
        self.logger.info(f"LLMComponent {self.title} inputs: {self.inputs}")
        model = self.llm_params.model_name

        if self.batch_config.batch_enable:
            input_value_dict_list = map_schema_with_values(self.workflow_node.input_schema, input_value, batch_value)
            self.inputs = input_value_dict_list
            system_prompt_list = [parse_template(self.llm_params.system_prompt, item) for item in input_value_dict_list]
            prompt_list = [parse_template(self.llm_params.prompt, item) for item in input_value_dict_list]

            # 使用asyncio.gather并发处理，并限制并发数
            tasks = []
            for i in range(len(input_value_dict_list)):
                tasks.append(
                    process_single_chat_async(
                        self.db,
                        self.user.id,
                        model,
                        system_prompt_list[i],
                        prompt_list[i],
                        self.llm_params,
                        input_value_dict_list[i]
                    )
                )

            # 限制并发数量
            execute_info = []
            for i in range(0, len(tasks), self.batch_config.concurrent_size):
                batch_tasks = tasks[i:i + self.batch_config.concurrent_size]
                batch_results = await asyncio.gather(*batch_tasks)
                execute_info.extend(batch_results)

            # 转换为目标格式，保持原始顺序
            output_list = [{"output": item["output"]} for item in execute_info]

            # 批处理
            return {"outputList": output_list}
        else:
            self.inputs = input_value
            actual_system_prompt = parse_template(self.llm_params.system_prompt, self.inputs)
            actual_prompt = parse_template(self.llm_params.prompt, self.inputs)
            chat_mdl = LLMBundle(self.db, self.user.id, LLMType.CHAT, model)
            history = [{"role": "user", "content": actual_prompt}]

            # 使用 asyncio.to_thread 防止阻塞主事件循环
            response = await thread_pool_exec(
                chat_mdl.chat,
                system=actual_system_prompt,
                history=history,
                gen_conf={
                    "temperature": self.llm_params.temperature,
                    "top_p": self.llm_params.top_p,
                    "max_tokens": self.llm_params.max_tokens,
                    "frequency_penalty": self.llm_params.frequency_penalty,
                }
            )

            return {"output": response}


# 新增异步版本
async def process_single_chat_async(db, user_id, model, system_prompt, prompt, llm_params, input_dict):
    chat_mdl = LLMBundle(db, user_id, LLMType.CHAT, model)
    history = [{"role": "user", "content": prompt}]

    # 使用 asyncio.to_thread 防止阻塞
    response = await thread_pool_exec(
        chat_mdl.chat,
        system=system_prompt,
        history=history,
        gen_conf={
            "temperature": llm_params.temperature,
            "top_p": llm_params.top_p,
            "max_tokens": llm_params.max_tokens,
            "frequency_penalty": llm_params.frequency_penalty,
        }
    )

    return {"input": input_dict, "output": response}


# 同步版本保留，但不再直接使用
def process_single_chat(args):
    db, user_id, model, system_prompt, prompt, llm_params, input_dict = args
    chat_mdl = LLMBundle(db, user_id, LLMType.CHAT, model)
    history = [{"role": "user", "content": prompt}]
    response = chat_mdl.chat(
        system=system_prompt,
        history=history,
        gen_conf={
            "temperature": llm_params.temperature,
            "top_p": llm_params.top_p,
            "max_tokens": llm_params.max_tokens,
            "frequency_penalty": llm_params.frequency_penalty,
        }
    )
    return {"input": input_dict, "output": response}
