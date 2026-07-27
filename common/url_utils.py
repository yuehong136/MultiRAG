"""LLM 服务端点 base_url 的规范化工具。

历史写法 ``urljoin(base_url, "v1")`` 把 URL 的最后一段当成「文件名」替换掉，
对任何带版本路径的厂商都是错的：

- ``https://open.bigmodel.cn/api/paas/v4``  → ``https://open.bigmodel.cn/api/paas/v1``（v4 被顶替）
- ``https://open.bigmodel.cn/api/paas/v4/`` → ``https://open.bigmodel.cn/api/paas/v4/v1``（重复）

两种填法都打不到真实端点，用户无论加不加尾斜杠都接不进来。本模块提供
「已含版本段就原样使用，否则才追加」的语义，并且所有判断都落在 URL 的 path 上，
避免 host、query、fragment 干扰。
"""

import re
from urllib.parse import SplitResult, urlsplit, urlunsplit

DEFAULT_API_VERSION = "v1"

# 版本段：/v1、/v2、/v1beta、/v1beta2、/v2alpha1、/v1.5（大小写不敏感）。
# 只匹配 path，避免把 v1.example.com 这样的主机名误判成已带版本；
# 也刻意不匹配 /vision、/v2ray 这类「像版本但不是版本」的路径段。
_VERSION_SEGMENT_RE = re.compile(r"/v\d+(?:\.\d+)?(?:alpha|beta)?\d*(?:/|$)", re.IGNORECASE)


def _split(base_url: str | None) -> SplitResult:
    return urlsplit((base_url or "").strip().rstrip("/"))


def _normalize(base_url: str | None) -> str:
    return (base_url or "").strip().rstrip("/")


def has_api_version(base_url: str | None) -> bool:
    """base_url 的**路径**里是否已经有版本段（/v1、/v2、/v1beta、/v1.5 …）。"""
    path = _split(base_url).path
    return bool(_VERSION_SEGMENT_RE.search(f"{path}/"))


def ensure_api_version(base_url: str | None, version: str = DEFAULT_API_VERSION) -> str:
    """把 base_url 规范成 OpenAI 兼容的版本化根地址。

    - 空值原样返回（由调用方自己决定是否报错）
    - 路径里已有版本段：原样返回（``.../api/paas/v4``、``.../compatible-mode/v1`` 都不动）
    - 其余：追加 ``/{version}``（``http://host:11434`` → ``http://host:11434/v1``）

    注意：只用于「走 OpenAI ``/v1/...`` 形态」的通道。原生协议（Ollama 的 ``/api/embed``、
    DashScope SDK、Anthropic 的 ``/v1/messages``）不要调它。

    Examples:
        >>> ensure_api_version("https://open.bigmodel.cn/api/paas/v4")
        'https://open.bigmodel.cn/api/paas/v4'
        >>> ensure_api_version("http://localhost:9997")
        'http://localhost:9997/v1'
        >>> ensure_api_version("http://localhost:9997/xinference")
        'http://localhost:9997/xinference/v1'
    """
    normalized = _normalize(base_url)
    if not normalized:
        return normalized

    parsed = urlsplit(normalized)
    if _VERSION_SEGMENT_RE.search(f"{parsed.path}/"):
        return normalized
    return urlunsplit(parsed._replace(path=f"{parsed.path}/{version}"))


def append_path_segment(base_url: str | None, segment: str) -> str:
    """在 base_url 的路径末尾追加片段，已经以该片段结尾时不重复追加。

    与 ``urljoin`` 不同：不会因为缺尾斜杠而吃掉 base_url 的最后一段，也不会
    因为 segment 以 ``/`` 开头而丢弃整个路径。

    Examples:
        >>> append_path_segment("https://open.bigmodel.cn/api/paas/v4", "rerank")
        'https://open.bigmodel.cn/api/paas/v4/rerank'
        >>> append_path_segment("http://localhost:9997/v1/rerank", "rerank")
        'http://localhost:9997/v1/rerank'
    """
    normalized = _normalize(base_url)
    if not normalized:
        return normalized

    seg = segment.strip("/")
    if not seg:
        return normalized

    parsed = urlsplit(normalized)
    if parsed.path.endswith(f"/{seg}"):
        return normalized
    return urlunsplit(parsed._replace(path=f"{parsed.path}/{seg}"))


def strip_trailing_segment(base_url: str | None, segment: str) -> str:
    """去掉 base_url 路径末尾的指定片段（不存在时原样返回）。

    用于原生协议厂商：Anthropic 的 ``/v1/messages`` 由 SDK/LiteLLM 自己拼，
    用户沿 OpenAI 习惯多填的 ``/v1`` 必须先摘掉，否则会拼成 ``/v1/v1/messages``。

    Examples:
        >>> strip_trailing_segment("https://api.anthropic.com/v1", "v1")
        'https://api.anthropic.com'
        >>> strip_trailing_segment("https://open.bigmodel.cn/api/anthropic", "v1")
        'https://open.bigmodel.cn/api/anthropic'
        >>> strip_trailing_segment("https://gw/anthropic/v1/messages", "v1/messages")
        'https://gw/anthropic'
    """
    normalized = _normalize(base_url)
    seg = segment.strip("/")
    if not normalized or not seg:
        return normalized

    parsed = urlsplit(normalized)
    if not parsed.path.endswith(f"/{seg}"):
        return normalized
    return urlunsplit(parsed._replace(path=parsed.path[: -(len(seg) + 1)]))
