#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import asyncio
from abc import ABC
from typing import Any

from crawl4ai import AsyncWebCrawler

from agent.tools.base import ToolBase, ToolParamBase


class CrawlerParam(ToolParamBase):
    """
    Define the Crawler component parameters.
    """

    def __init__(self):
        super().__init__()
        self.proxy = None
        self.extract_type = "markdown"

    def check(self):
        self.check_valid_value(self.extract_type, "Type of content from the crawler", ["html", "markdown", "content"])


class Crawler(ToolBase, ABC):
    component_name = "Crawler"

    def _run(self, history: Any, **kwargs: Any) -> Any:
        from common.ssrf_guard import assert_url_is_safe, pin_dns_global

        ans = self.get_input()
        ans = " - ".join(ans["content"]) if "content" in ans else ""
        try:
            hostname, ip = assert_url_is_safe(ans)
        except ValueError:
            return Crawler.be_output("URL not valid")
        try:
            with pin_dns_global(hostname, ip):
                result = asyncio.run(self.get_web(ans))

            return Crawler.be_output(result)

        except Exception as e:
            return Crawler.be_output(f"An unexpected error occurred: {e!s}")

    async def get_web(self, url: str) -> str | None:
        if self.check_if_canceled("Crawler async operation"):
            return

        proxy = self._param.proxy if self._param.proxy else None
        async with AsyncWebCrawler(verbose=True, proxy=proxy) as crawler:
            result = await crawler.arun(url=url, bypass_cache=True)

            if self.check_if_canceled("Crawler async operation"):
                return

            if self._param.extract_type == "html":
                return result.cleaned_html
            elif self._param.extract_type == "markdown":
                return result.markdown
            elif self._param.extract_type == "content":
                return result.extracted_content
            return result.markdown
