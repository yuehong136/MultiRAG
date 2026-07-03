"""
Firecrawl Plugin for MultiRAG

This plugin integrates Firecrawl's web scraping capabilities into MultiRAG,
allowing users to import web content directly into their RAG workflows.
"""

__version__ = "1.0.0"
__author__ = "Firecrawl Team"
__description__ = "Firecrawl integration for MultiRAG - Web content scraping and import"

from firecrawl_config import FirecrawlConfig
from firecrawl_connector import FirecrawlConnector

__all__ = ["FirecrawlConfig", "FirecrawlConnector"]
