"""
Main integration file for Firecrawl with MultiRAG.
This file provides the interface between MultiRAG and the Firecrawl plugin.
"""

import logging
from typing import Any

from firecrawl_config import FirecrawlConfig
from firecrawl_connector import FirecrawlConnector
from firecrawl_processor import FirecrawlProcessor, MultiRAGDocument
from firecrawl_ui import FirecrawlUIBuilder


class MultiRAGFirecrawlIntegration:
    """Main integration class for Firecrawl with MultiRAG."""

    def __init__(self, config: FirecrawlConfig):
        """Initialize the integration."""
        self.config = config
        self.connector = FirecrawlConnector(config)
        self.processor = FirecrawlProcessor()
        self.logger = logging.getLogger(__name__)

    async def scrape_and_import(self, urls: list[str],
                               formats: list[str] = None,
                               extract_options: dict[str, Any] = None) -> list[MultiRAGDocument]:
        """Scrape URLs and convert to MultiRAG documents."""
        if formats is None:
            formats = ["markdown", "html"]

        async with self.connector:
            # Scrape URLs
            scraped_contents = await self.connector.batch_scrape(urls, formats)

            # Process into MultiRAG documents
            documents = self.processor.process_batch(scraped_contents)

            return documents

    async def crawl_and_import(self, start_url: str,
                              limit: int = 100,
                              scrape_options: dict[str, Any] = None) -> list[MultiRAGDocument]:
        """Crawl a website and convert to MultiRAG documents."""
        if scrape_options is None:
            scrape_options = {"formats": ["markdown", "html"]}

        async with self.connector:
            # Start crawl job
            crawl_job = await self.connector.start_crawl(start_url, limit, scrape_options)

            if crawl_job.error:
                raise Exception(f"Failed to start crawl: {crawl_job.error}")

            # Wait for completion
            completed_job = await self.connector.wait_for_crawl_completion(crawl_job.job_id)

            if completed_job.error:
                raise Exception(f"Crawl failed: {completed_job.error}")

            # Process into MultiRAG documents
            documents = self.processor.process_batch(completed_job.data or [])

            return documents

    def get_ui_schema(self) -> dict[str, Any]:
        """Get UI schema for MultiRAG integration."""
        return FirecrawlUIBuilder.create_ui_schema()

    def validate_config(self, config_dict: dict[str, Any]) -> dict[str, Any]:
        """Validate configuration and return any errors."""
        errors = {}

        # Validate API key
        api_key = config_dict.get("api_key", "")
        if not api_key:
            errors["api_key"] = "API key is required"
        elif not api_key.startswith("fc-"):
            errors["api_key"] = "API key must start with 'fc-'"

        # Validate API URL
        api_url = config_dict.get("api_url", "https://api.firecrawl.dev")
        if not api_url.startswith("http"):
            errors["api_url"] = "API URL must start with http:// or https://"

        # Validate numeric fields
        try:
            max_retries = int(config_dict.get("max_retries", 3))
            if max_retries < 1 or max_retries > 10:
                errors["max_retries"] = "Max retries must be between 1 and 10"
        except (ValueError, TypeError):
            errors["max_retries"] = "Max retries must be a valid integer"

        try:
            timeout = int(config_dict.get("timeout", 30))
            if timeout < 5 or timeout > 300:
                errors["timeout"] = "Timeout must be between 5 and 300 seconds"
        except (ValueError, TypeError):
            errors["timeout"] = "Timeout must be a valid integer"

        try:
            rate_limit_delay = float(config_dict.get("rate_limit_delay", 1.0))
            if rate_limit_delay < 0.1 or rate_limit_delay > 10.0:
                errors["rate_limit_delay"] = "Rate limit delay must be between 0.1 and 10.0 seconds"
        except (ValueError, TypeError):
            errors["rate_limit_delay"] = "Rate limit delay must be a valid number"

        return errors

    def create_config(self, config_dict: dict[str, Any]) -> FirecrawlConfig:
        """Create FirecrawlConfig from dictionary."""
        return FirecrawlConfig.from_dict(config_dict)

    async def test_connection(self) -> dict[str, Any]:
        """Test the connection to Firecrawl API."""
        try:
            async with self.connector:
                # Try to scrape a simple URL to test connection
                test_url = "https://httpbin.org/json"
                result = await self.connector.scrape_url(test_url, ["markdown"])

                if result.error:
                    return {
                        "success": False,
                        "error": result.error,
                        "message": "Failed to connect to Firecrawl API"
                    }

                return {
                    "success": True,
                    "message": "Successfully connected to Firecrawl API",
                    "test_url": test_url,
                    "response_time": "N/A"  # Could be enhanced to measure actual response time
                }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "message": "Connection test failed"
            }

    def get_supported_formats(self) -> list[str]:
        """Get list of supported output formats."""
        return ["markdown", "html", "links", "screenshot"]

    def get_supported_scrape_types(self) -> list[str]:
        """Get list of supported scrape types."""
        return ["single", "crawl", "batch"]

    def get_help_text(self) -> dict[str, str]:
        """Get help text for users."""
        return FirecrawlUIBuilder.create_help_text()

    def get_validation_rules(self) -> dict[str, Any]:
        """Get validation rules for configuration."""
        return FirecrawlUIBuilder.create_validation_rules()


# Factory function for creating integration instance
def create_firecrawl_integration(config_dict: dict[str, Any]) -> MultiRAGFirecrawlIntegration:
    """Create a Firecrawl integration instance from configuration."""
    config = FirecrawlConfig.from_dict(config_dict)
    return MultiRAGFirecrawlIntegration(config)


# Export main classes and functions
__all__ = [
    "FirecrawlConfig",
    "FirecrawlConnector",
    "FirecrawlProcessor",
    "MultiRAGDocument",
    "MultiRAGFirecrawlIntegration",
    "create_firecrawl_integration"
]
