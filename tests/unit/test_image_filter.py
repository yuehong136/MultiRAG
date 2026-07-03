"""单元测试：ImageFilter 图片过滤器"""

from common.image_utils import ImageFilter


class TestImageFilter:
    """测试 ImageFilter 类"""

    def test_force_drop_patterns(self):
        """测试强制删除规则"""
        test_cases = [
            ("https://example.com/_visitcount?id=123", True, "tracking"),
            ("https://example.com/blank.gif", True, "placeholder"),
            ("https://example.com/lazy.png", True, "placeholder"),
            ("https://example.com/article/image.png", False, "content"),
        ]

        for url, should_drop, desc in test_cases:
            dropped, reason = ImageFilter.should_drop_image(url, "", "", "minimal", "")
            assert dropped == should_drop, f"Failed on {desc}: {url}"

    def test_strict_mode(self):
        """测试严格模式"""
        # Logo 路径
        should_drop, _ = ImageFilter.should_drop_image("https://example.com/_upload/site/logo.png", "", "", "strict", "")
        assert should_drop

        # 模板路径
        should_drop, _ = ImageFilter.should_drop_image("https://example.com/_upload/tpl/template/logo.png", "", "", "strict", "")
        assert should_drop

        # 正文图片
        should_drop, _ = ImageFilter.should_drop_image("https://example.com/_upload/article/images/photo.png", "", "", "strict", "")
        assert not should_drop

    def test_homepage_logo_link(self):
        """测试首页 Logo 检测"""
        base_url = "https://example.com/article/page.html"

        # 指向首页的链接
        should_drop, _ = ImageFilter.should_drop_image("https://example.com/logo.png", "https://example.com/index.html", "", "strict", base_url)
        assert should_drop

        # 不指向首页的链接
        should_drop, _ = ImageFilter.should_drop_image("https://example.com/photo.png", "https://example.com/article/detail.html", "", "strict", base_url)
        assert not should_drop

    def test_relative_path(self):
        """测试相对路径处理"""
        should_drop, reason = ImageFilter.should_drop_image("/images/logo.png", "", "", "strict", "")
        assert should_drop
        assert reason == "relative_path"

    def test_clean_markdown_images(self):
        """测试 Markdown 文本清理"""
        markdown = """
# Title

[![Logo](/site/logo.png)](https://example.com/)

![Content](https://example.com/article/photo.png)

![Tracking](https://example.com/_visitcount?id=123)
"""

        cleaned, dropped, total = ImageFilter.clean_markdown_images(markdown, mode="strict", base_url="https://example.com")

        assert total == 3
        assert len(dropped) == 2  # Logo + Tracking
        assert "article/photo.png" in cleaned
        assert "_visitcount" not in cleaned
        assert "/site/logo.png" not in cleaned

    def test_all_images_dropped(self):
        """测试所有图片被删除的情况"""
        markdown = """
![Tracking](https://example.com/_visitcount?id=123)
![Logo](/site/logo.png)
"""

        cleaned, dropped, total = ImageFilter.clean_markdown_images(markdown, mode="strict", base_url="https://example.com")

        # 所有图片被删除，应返回清洗后的文本（不含图片）
        assert cleaned.strip() == ""  # 只剩空白
        assert total == 2
        assert len(dropped) == 2
        assert "_visitcount" not in cleaned
        assert "logo.png" not in cleaned

    def test_filter_image_urls(self):
        """测试图片 URL 列表过滤"""
        urls = [
            "https://example.com/_upload/site/logo.png",
            "https://example.com/_upload/article/images/photo1.png",
            "https://example.com/_visitcount?id=123",
            "https://example.com/_upload/article/images/photo2.png",
            "https://example.com/_upload/tpl/template/logo.png",
        ]

        kept, dropped = ImageFilter.filter_image_urls(urls, mode="strict", base_url="")

        assert len(kept) == 2  # photo1, photo2
        assert len(dropped) == 3  # site logo, visitcount, template logo
        assert "photo1.png" in kept[0] or "photo1.png" in kept[1]
        assert "photo2.png" in kept[0] or "photo2.png" in kept[1]

    def test_balanced_mode(self):
        """测试平衡模式"""
        # 平衡模式保留更多图片
        should_drop, _ = ImageFilter.should_drop_image("https://example.com/_upload/site/logo.png", "", "", "balanced", "")
        assert not should_drop  # 平衡模式不删除 site logo

        # 但仍删除模板图片
        should_drop, _ = ImageFilter.should_drop_image("https://example.com/themes/default/images/bg.png", "", "", "balanced", "")
        assert should_drop

    def test_minimal_mode(self):
        """测试最小模式"""
        # 最小模式仅删除强制规则
        should_drop, _ = ImageFilter.should_drop_image("https://example.com/_visitcount?id=123", "", "", "minimal", "")
        assert should_drop  # 统计图强制删除

        should_drop, _ = ImageFilter.should_drop_image("https://example.com/_upload/site/logo.png", "", "", "minimal", "")
        assert not should_drop  # 最小模式不删除 site logo

    def test_empty_markdown(self):
        """测试空文本"""
        cleaned, dropped, total = ImageFilter.clean_markdown_images("", mode="strict", base_url="https://example.com")
        assert cleaned == ""
        assert dropped == []
        assert total == 0
