"""
MultiRAG Integration Entry Point for Firecrawl

This file provides the main entry point for the Firecrawl integration with MultiRAG.
It follows MultiRAG's integration patterns and provides the necessary interfaces.
"""

import logging
from typing import Any

from firecrawl_ui import FirecrawlUIBuilder
from multirag_integration import MultiRAGFirecrawlIntegration, create_firecrawl_integration

# Set up logging
logger = logging.getLogger(__name__)


class FirecrawlMultiRAGPlugin:
    """
    Main plugin class for Firecrawl integration with MultiRAG.
    This class provides the interface that MultiRAG expects from integrations.
    """

    def __init__(self):
        """Initialize the Firecrawl plugin."""
        self.name = "firecrawl"
        self.display_name = "Firecrawl Web Scraper"
        self.description = "Import web content using Firecrawl's powerful scraping capabilities"
        self.version = "1.0.0"
        self.author = "Firecrawl Team"
        self.category = "web"
        self.icon = "🌐"

        logger.info(f"Initialized {self.display_name} plugin v{self.version}")

    def get_plugin_info(self) -> dict[str, Any]:
        """Get plugin information for MultiRAG."""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "version": self.version,
            "author": self.author,
            "category": self.category,
            "icon": self.icon,
            "supported_formats": ["markdown", "html", "links", "screenshot"],
            "supported_scrape_types": ["single", "crawl", "batch"]
        }

    def get_config_schema(self) -> dict[str, Any]:
        """Get configuration schema for MultiRAG."""
        return FirecrawlUIBuilder.create_data_source_config()["config_schema"]

    def get_ui_schema(self) -> dict[str, Any]:
        """Get UI schema for MultiRAG."""
        return FirecrawlUIBuilder.create_ui_schema()

    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Validate configuration and return any errors."""
        try:
            integration = create_firecrawl_integration(config)
            return integration.validate_config(config)
        except Exception as e:
            logger.error(f"Configuration validation error: {e}")
            return {"general": str(e)}

    def test_connection(self, config: dict[str, Any]) -> dict[str, Any]:
        """Test connection to Firecrawl API."""
        try:
            integration = create_firecrawl_integration(config)
            # Run the async test_connection method
            import asyncio
            return asyncio.run(integration.test_connection())
        except Exception as e:
            logger.error(f"Connection test error: {e}")
            return {
                "success": False,
                "error": str(e),
                "message": "Connection test failed"
            }

    def create_integration(self, config: dict[str, Any]) -> MultiRAGFirecrawlIntegration:
        """Create and return a Firecrawl integration instance."""
        return create_firecrawl_integration(config)

    def get_help_text(self) -> dict[str, str]:
        """Get help text for users."""
        return FirecrawlUIBuilder.create_help_text()

    def get_validation_rules(self) -> dict[str, Any]:
        """Get validation rules for configuration."""
        return FirecrawlUIBuilder.create_validation_rules()


# MultiRAG integration entry points
def get_plugin() -> FirecrawlMultiRAGPlugin:
    """Get the plugin instance for MultiRAG."""
    return FirecrawlMultiRAGPlugin()


def get_integration(config: dict[str, Any]) -> MultiRAGFirecrawlIntegration:
    """Get an integration instance with the given configuration."""
    return create_firecrawl_integration(config)


def get_config_schema() -> dict[str, Any]:
    """Get the configuration schema."""
    return FirecrawlUIBuilder.create_data_source_config()["config_schema"]


def get_ui_schema() -> dict[str, Any]:
    """Get the UI schema."""
    return FirecrawlUIBuilder.create_ui_schema()


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate configuration."""
    try:
        integration = create_firecrawl_integration(config)
        return integration.validate_config(config)
    except Exception as e:
        return {"general": str(e)}


def test_connection(config: dict[str, Any]) -> dict[str, Any]:
    """Test connection to Firecrawl API."""
    try:
        integration = create_firecrawl_integration(config)
        return integration.test_connection()
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": "Connection test failed"
        }


# Export main functions and classes
__all__ = [
    "FirecrawlMultiRAGPlugin",
    "MultiRAGFirecrawlIntegration",
    "create_firecrawl_integration",
    "get_config_schema",
    "get_integration",
    "get_plugin",
    "get_ui_schema",
    "test_connection",
    "validate_config"
]
