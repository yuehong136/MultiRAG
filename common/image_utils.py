"""网页图片去噪过滤器

用于从网页抓取结果中过滤无用图片（logo、统计像素图、占位图等），
保留正文中有语义价值的配图。
"""

import re
from urllib.parse import urlparse


class ImageFilter:
    """网页图片去噪过滤器"""

    # 强制删除规则（适用所有模式）
    FORCE_DROP_PATTERNS = [
        r'_visitcount',
        r'_counter',
        r'\bstat\?',
        r'analytics',
        r'tracking',
        r'blank\.gif',
        r'transparent\.gif',
        r'1x1\.(png|gif)',
        r'pixel\.(png|gif)',
        r'lazy\.(png|jpg|jpeg)',
        r'placeholder\.',
        r'loading\.gif',
        r'favicon\.ico',
        r'apple-touch-icon',
    ]

    # 严格模式额外规则
    STRICT_DROP_PATTERNS = [
        r'/_upload/site/',
        r'/site/[^/]*/logo',
        r'/logo/',
        r'/tpl/',
        r'/template/',
        r'/themes/',
        r'/assets/images/',
        r'/plugins/',
        r'/wp-content/themes/',
    ]

    # 平衡模式规则（比严格模式少一些）
    BALANCED_DROP_PATTERNS = [
        r'/tpl/',
        r'/template/',
        r'/themes/.*/images/',
        r'/plugins/',
    ]

    # Markdown 图片正则
    LINKED_IMAGE_PATTERN = re.compile(r'\[!\[([^\]]*)\]\(([^)]+)\)\]\(([^)]+)\)')
    SIMPLE_IMAGE_PATTERN = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')

    @staticmethod
    def clean_markdown_images(
        markdown_text: str,
        mode: str = "strict",
        base_url: str = ""
    ) -> tuple[str, list[dict], int]:
        """清理 Markdown 文本中的无用图片

        边界情况处理：
        - 所有图片被删除：返回原始文本
        - Markdown 格式异常：保持原样
        - 相对路径 URL：直接删除

        Args:
            markdown_text: Markdown 格式文本
            mode: 过滤模式 (strict/balanced/minimal)
            base_url: 网页基础 URL，用于判断同域链接

        Returns:
            (cleaned_text, dropped_images, total_images)
            - cleaned_text: 清理后的文本
            - dropped_images: 被删除的图片列表 [{"url": str, "reason": str}]
            - total_images: 原始图片总数
        """
        if not markdown_text:
            return markdown_text, [], 0

        dropped_details = []
        total_images = 0
        result = markdown_text

        try:
            # 先处理带链接的图片 [![]()]()
            def replace_linked_image(match):
                nonlocal total_images
                total_images += 1
                alt_text = match.group(1)
                img_url = match.group(2)
                link_url = match.group(3)

                should_drop, reason = ImageFilter.should_drop_image(
                    img_url, link_url, alt_text, mode, base_url
                )

                if should_drop:
                    dropped_details.append({"url": img_url, "reason": reason})
                    return ""  # 删除整个图片和链接
                return match.group(0)  # 保留

            result = ImageFilter.LINKED_IMAGE_PATTERN.sub(replace_linked_image, result)

            # 再处理普通图片 ![]()
            def replace_simple_image(match):
                nonlocal total_images
                total_images += 1
                alt_text = match.group(1)
                img_url = match.group(2)

                should_drop, reason = ImageFilter.should_drop_image(
                    img_url, "", alt_text, mode, base_url
                )

                if should_drop:
                    dropped_details.append({"url": img_url, "reason": reason})
                    return ""  # 删除图片
                return match.group(0)  # 保留

            result = ImageFilter.SIMPLE_IMAGE_PATTERN.sub(replace_simple_image, result)

            # 清理多余空行
            result = re.sub(r'\n{3,}', '\n\n', result)

            # 始终返回清洗后的文本（即使所有图片都被删除）
            # 用户启用了图片清洗功能，就应该尊重过滤结果
            return result, dropped_details, total_images

        except Exception:
            # Markdown 格式异常，保持原样
            return markdown_text, [], 0

    @staticmethod
    def should_drop_image(
        image_url: str,
        link_url: str,
        alt_text: str,
        mode: str,
        base_url: str
    ) -> tuple[bool, str]:
        """判断图片是否应该删除

        Args:
            image_url: 图片 URL
            link_url: 外链 URL（如果是 [![]()]() 格式）
            alt_text: 图片 alt 文本
            mode: 过滤模式
            base_url: 网页基础 URL

        Returns:
            (should_drop, reason)
            - should_drop: 是否应该删除
            - reason: 删除原因（如 "tracking_visitcount", "site_logo"）
        """
        # 相对路径判断
        if image_url.startswith('/') and not image_url.startswith('//'):
            return True, "relative_path"

        # 强制删除规则（所有模式）
        for pattern in ImageFilter.FORCE_DROP_PATTERNS:
            if re.search(pattern, image_url, re.IGNORECASE):
                reason_name = pattern.replace(r'\b', '').replace(r'\.', '_').replace('\\', '')
                return True, f"force_drop_{reason_name}"

        # 严格模式
        if mode == "strict":
            # Logo 启发式：外链指向首页
            if link_url and ImageFilter._is_homepage_link(link_url, base_url):
                return True, "homepage_logo_link"

            # 严格模式规则
            for pattern in ImageFilter.STRICT_DROP_PATTERNS:
                if re.search(pattern, image_url, re.IGNORECASE):
                    return True, "strict_pattern"

        # 平衡模式
        elif mode == "balanced":
            for pattern in ImageFilter.BALANCED_DROP_PATTERNS:
                if re.search(pattern, image_url, re.IGNORECASE):
                    return True, "balanced_pattern"

        # minimal 模式只应用强制删除规则（已在上面处理）

        return False, ""

    @staticmethod
    def filter_image_urls(
        image_urls: list[str],
        mode: str = "strict",
        base_url: str = ""
    ) -> tuple[list[str], list[dict]]:
        """过滤图片 URL 列表

        Args:
            image_urls: 原始图片 URL 列表
            mode: 过滤模式
            base_url: 网页基础 URL

        Returns:
            (kept_urls, dropped_details)
            - kept_urls: 保留的图片 URL 列表
            - dropped_details: 被删除的图片详情
        """
        kept_urls = []
        dropped_details = []

        for img_url in image_urls:
            should_drop, reason = ImageFilter.should_drop_image(
                img_url, "", "", mode, base_url
            )

            if should_drop:
                dropped_details.append({"url": img_url, "reason": reason})
            else:
                kept_urls.append(img_url)

        return kept_urls, dropped_details

    @staticmethod
    def _is_homepage_link(link_url: str, base_url: str) -> bool:
        """判断链接是否指向首页

        Args:
            link_url: 链接 URL
            base_url: 网页基础 URL

        Returns:
            是否指向首页
        """
        if not link_url or not base_url:
            return False

        try:
            link_parsed = urlparse(link_url)
            base_parsed = urlparse(base_url)

            # 同域 + 路径为根或主页
            if link_parsed.netloc == base_parsed.netloc:
                path = link_parsed.path.strip('/')
                if path in ['', 'index.html', 'index.htm', 'main.htm', 'home', 'index.php']:
                    return True
        except Exception:
            pass

        return False
