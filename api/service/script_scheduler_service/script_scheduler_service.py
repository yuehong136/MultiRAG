import aiohttp
from typing import Any

from common.settings import SCRIPT_SCHEDULER_PORT, SCRIPT_SCHEDULER_HOST
from errors.exceptions import ScriptRunningError


class ScriptSchedulerService:

    @staticmethod
    async def run_temporary_script(script: str, args: dict[str, Any], user_id: str):
        """运行临时脚本"""
        async with aiohttp.ClientSession() as session:
            url = f"http://{SCRIPT_SCHEDULER_HOST}:{SCRIPT_SCHEDULER_PORT}/api/v1/script-scheduler/run-temporary-script"
            payload = {
                "script": script,
                "args": args
            }

            async with session.post(url, json=payload) as response:
                response_data = await response.json()
                if response.status != 200 or response_data.get('status') != 'success':
                    raise Exception(f"Failed to run plugin script: {response_data.get('message', 'Unknown error')}")
                if response_data.get('status') != 'success':
                    raise ScriptRunningError(message=response_data.get('message'))
                return response_data.get('data')

    @staticmethod
    async def run_plugin_script(plugin_id: str, script: str, args: dict[str, Any], user_id: str):
        """运行插件脚本"""
        async with aiohttp.ClientSession() as session:
            url = f"http://{SCRIPT_SCHEDULER_HOST}:{SCRIPT_SCHEDULER_PORT}/api/v1/script-scheduler/run-plugin-script"
            payload = {
                "plugin_id": plugin_id,
                "script": script,
                "args": args
            }

            async with session.post(url, json=payload) as response:
                response_data = await response.json()
                if response.status != 200 or response_data.get('status') != 'success':
                    raise Exception(f"Failed to run plugin script: {response_data.get('message', 'Unknown error')}")
                if response_data.get('status') != 'success':
                    raise ScriptRunningError(message=response_data.get('message'))
                return response_data.get('data')
