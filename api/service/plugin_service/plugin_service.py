import aiohttp
from typing import Optional

from api.settings import SCRIPT_SCHEDULER_PORT


class PluginService:

    @staticmethod
    async def install_dep(plugin_id: str, package_name: str, package_version: Optional[str] = None):
        """
        安装插件依赖

        Args:
            plugin_id: 插件ID
            package_name: 包名
            package_version: 包版本（可选）

        Returns:
            安装结果
        """
        async with aiohttp.ClientSession() as session:
            url = f"http://localhost:{SCRIPT_SCHEDULER_PORT}/api/v1/plugin/install-dep"
            payload = {
                "plugin_id": plugin_id,
                "package_name": package_name,
                "package_version": package_version
            }

            async with session.post(url, json=payload) as response:
                response_data = await response.json()
                if response.status != 200:
                    raise Exception(f"Failed to install dependency: {response_data.get('message', 'Unknown error')}")
                return response_data.get('data')

    @staticmethod
    async def uninstall_dep(plugin_id: str, package_name: str):
        """
        卸载插件依赖

        Args:
            plugin_id: 插件ID
            package_name: 包名

        Returns:
            卸载结果
        """
        async with aiohttp.ClientSession() as session:
            url = f"http://localhost:{SCRIPT_SCHEDULER_PORT}/api/v1/plugin/uninstall-dep"
            payload = {
                "plugin_id": plugin_id,
                "package_name": package_name
            }

            async with session.post(url, json=payload) as response:
                response_data = await response.json()
                if response.status != 200:
                    raise Exception(f"Failed to uninstall dependency: {response_data.get('message', 'Unknown error')}")
                return response_data.get('data')
