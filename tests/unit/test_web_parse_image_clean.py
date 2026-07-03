"""集成测试：web_parse 接口图片清洗功能"""

from unittest.mock import Mock, patch


class TestWebParseImageCleaning:
    """测试 web_parse 接口的图片清洗功能"""

    @patch("core.utils.tavily_conn.Tavily")
    def test_clean_images_disabled_by_default(self, mock_tavily_class):
        """测试默认不启用图片清洗"""
        from api.db.services.document_service import DocumentService

        # Mock Tavily 响应
        mock_client = Mock()
        mock_tavily_class.return_value = mock_client
        mock_client.extract.return_value = {
            "results": [{"raw_content": "![Image](https://example.com/logo.png)", "images": ["https://example.com/logo.png"]}],
            "failed_results": [],
            "response_time": 1.0,
            "request_id": "test",
        }

        result = DocumentService.parse_web_by_provider(url="https://example.com", provider="tavily", options={}, credentials={"api_key": "test_key"})

        # 默认不清洗，texts 保持原样
        assert "logo.png" in result["texts"][0]
        assert "filtered_images" not in result
        assert "image_cleaning_stats" not in result

    @patch("core.utils.tavily_conn.Tavily")
    def test_clean_images_enabled(self, mock_tavily_class):
        """测试启用图片清洗"""
        from api.db.services.document_service import DocumentService

        # Mock Tavily 响应
        mock_client = Mock()
        mock_tavily_class.return_value = mock_client
        mock_client.extract.return_value = {
            "results": [
                {
                    "raw_content": (
                        "[![Logo](https://example.com/_upload/site/logo.png)](https://example.com/)\n![Content](https://example.com/article/photo.png)\n![Track](https://example.com/_visitcount?id=1)"
                    ),
                    "images": ["https://example.com/_upload/site/logo.png", "https://example.com/article/photo.png", "https://example.com/_visitcount?id=1"],
                }
            ],
            "failed_results": [],
            "response_time": 1.0,
            "request_id": "test",
        }

        result = DocumentService.parse_web_by_provider(url="https://example.com", provider="tavily", options={"clean_images": True, "image_filter_mode": "strict"}, credentials={"api_key": "test_key"})

        # 验证文本已清洗
        assert "article/photo.png" in result["texts"][0]
        assert "_visitcount" not in result["texts"][0]
        assert "_upload/site/logo.png" not in result["texts"][0]

        # 验证 filtered_images
        assert "filtered_images" in result
        assert len(result["filtered_images"]) == 1
        assert "article/photo.png" in result["filtered_images"][0]

        # 验证统计信息
        assert "image_cleaning_stats" in result
        stats = result["image_cleaning_stats"]
        assert stats["enabled"] is True
        assert stats["mode"] == "strict"
        assert stats["total_images"] == 3
        assert stats["dropped_count"] == 2
        assert stats["kept_count"] == 1
        assert len(stats["dropped_details"]) == 2

        # 验证 raw 数据未被修改
        assert len(result["raw"]["results"][0]["images"]) == 3

    @patch("core.utils.tavily_conn.Tavily")
    def test_balanced_mode(self, mock_tavily_class):
        """测试平衡模式"""
        from api.db.services.document_service import DocumentService

        mock_client = Mock()
        mock_tavily_class.return_value = mock_client
        mock_client.extract.return_value = {
            "results": [
                {
                    "raw_content": (
                        "![Site Logo](https://example.com/_upload/site/logo.png)\n![Theme](https://example.com/themes/default/images/bg.png)\n![Content](https://example.com/article/photo.png)"
                    ),
                    "images": ["https://example.com/_upload/site/logo.png", "https://example.com/themes/default/images/bg.png", "https://example.com/article/photo.png"],
                }
            ],
            "failed_results": [],
            "response_time": 1.0,
            "request_id": "test",
        }

        result = DocumentService.parse_web_by_provider(
            url="https://example.com", provider="tavily", options={"clean_images": True, "image_filter_mode": "balanced"}, credentials={"api_key": "test_key"}
        )

        # 平衡模式保留 site logo，但删除 themes 图片
        stats = result["image_cleaning_stats"]
        assert stats["mode"] == "balanced"
        assert stats["dropped_count"] == 1  # 仅删除 themes
        assert stats["kept_count"] == 2  # 保留 site logo + content

    @patch("core.utils.tavily_conn.Tavily")
    def test_minimal_mode(self, mock_tavily_class):
        """测试最小模式"""
        from api.db.services.document_service import DocumentService

        mock_client = Mock()
        mock_tavily_class.return_value = mock_client
        mock_client.extract.return_value = {
            "results": [
                {
                    "raw_content": ("![Logo](https://example.com/_upload/site/logo.png)\n![Track](https://example.com/_visitcount?id=1)\n![Content](https://example.com/article/photo.png)"),
                    "images": ["https://example.com/_upload/site/logo.png", "https://example.com/_visitcount?id=1", "https://example.com/article/photo.png"],
                }
            ],
            "failed_results": [],
            "response_time": 1.0,
            "request_id": "test",
        }

        result = DocumentService.parse_web_by_provider(
            url="https://example.com", provider="tavily", options={"clean_images": True, "image_filter_mode": "minimal"}, credentials={"api_key": "test_key"}
        )

        # 最小模式仅删除统计图
        stats = result["image_cleaning_stats"]
        assert stats["mode"] == "minimal"
        assert stats["dropped_count"] == 1  # 仅删除 visitcount
        assert stats["kept_count"] == 2  # 保留 logo + content

    @patch("core.utils.tavily_conn.Tavily")
    def test_homepage_logo_link_consistency(self, mock_tavily_class):
        """测试文本清洗与URL过滤的一致性（修复矛盾问题）"""
        from api.db.services.document_service import DocumentService

        mock_client = Mock()
        mock_tavily_class.return_value = mock_client
        mock_client.extract.return_value = {
            "results": [
                {
                    "raw_content": (
                        # 这是一个带首页链接的图片，应该在文本中被删除
                        "[![Logo](http://example.com/uploads/3.png)](http://example.com/)\n![Content](http://example.com/article/photo.png)"
                    ),
                    "images": [
                        "http://example.com/uploads/3.png",  # 路径不符合严格规则，但有首页链接
                        "http://example.com/article/photo.png",
                    ],
                }
            ],
            "failed_results": [],
            "response_time": 1.0,
            "request_id": "test",
        }

        result = DocumentService.parse_web_by_provider(
            url="http://example.com/page.html", provider="tavily", options={"clean_images": True, "image_filter_mode": "strict"}, credentials={"api_key": "test_key"}
        )

        # 验证 3.png 在文本中被删除
        assert "3.png" not in result["texts"][0]

        # 验证 3.png 不在 filtered_images 中（修复后的行为）
        assert len(result["filtered_images"]) == 1
        assert "article/photo.png" in result["filtered_images"][0]
        assert "3.png" not in str(result["filtered_images"])

        # 验证统计信息
        stats = result["image_cleaning_stats"]
        assert stats["total_images"] == 2
        assert stats["dropped_count"] == 1  # 3.png
        assert stats["kept_count"] == 1  # photo.png

        # 验证 3.png 在 dropped_details 中
        dropped_urls = [d["url"] for d in stats["dropped_details"]]
        assert "http://example.com/uploads/3.png" in dropped_urls
