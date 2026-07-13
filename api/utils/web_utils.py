import asyncio
import base64
import ipaddress
import json
import logging
import re
import smtplib
import socket
from collections.abc import Sequence
from email.message import EmailMessage
from email.utils import formataddr
from urllib.parse import urlparse

from jinja2 import Template
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.expected_conditions import staleness_of
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from api.utils.email_templates import EMAIL_TEMPLATES
from common import settings


def _get_mail_sender() -> tuple[str, str] | None:
    """
    Resolve sender display name and email address from SMTP settings.
    If SMTP is not configured, return None.
    """
    if not settings.SMTP_CONF:
        return None

    sender_email = settings.MAIL_DEFAULT_SENDER[1] if settings.MAIL_DEFAULT_SENDER else settings.MAIL_USERNAME
    sender_name = settings.MAIL_DEFAULT_SENDER[0] if settings.MAIL_DEFAULT_SENDER else "MultiRAG"
    if not sender_email:
        return None
    return sender_name, sender_email


def _build_email_message(subject: str, to_email: str, html_body: str) -> EmailMessage:
    sender = _get_mail_sender()
    if sender is None:
        raise ValueError("SMTP sender is not configured")

    sender_name, sender_email = sender
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((sender_name, sender_email))
    message["To"] = to_email
    message.set_content("This email requires an HTML-capable client.")
    message.add_alternative(html_body, subtype="html")
    return message


def _send_email_sync(message: EmailMessage) -> None:
    if settings.MAIL_USE_SSL:
        with smtplib.SMTP_SSL(settings.MAIL_SERVER, settings.MAIL_PORT) as smtp:
            if settings.MAIL_USERNAME:
                smtp.login(settings.MAIL_USERNAME, settings.MAIL_PASSWORD)
            smtp.send_message(message)
        return

    with smtplib.SMTP(settings.MAIL_SERVER, settings.MAIL_PORT) as smtp:
        if settings.MAIL_USE_TLS:
            smtp.starttls()
        if settings.MAIL_USERNAME:
            smtp.login(settings.MAIL_USERNAME, settings.MAIL_PASSWORD)
        smtp.send_message(message)


async def _send_email_message(message: EmailMessage) -> None:
    await asyncio.to_thread(_send_email_sync, message)


OTP_LENGTH = 4
OTP_TTL_SECONDS = 5 * 60
ATTEMPT_LIMIT = 5
ATTEMPT_LOCK_SECONDS = 30 * 60
RESEND_COOLDOWN_SECONDS = 60


CONTENT_TYPE_MAP = {
    # Office
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "pdf": "application/pdf",
    "csv": "text/csv",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    # Text/code
    "txt": "text/plain",
    "py": "text/plain",
    "js": "text/plain",
    "java": "text/plain",
    "c": "text/plain",
    "cpp": "text/plain",
    "h": "text/plain",
    "php": "text/plain",
    "go": "text/plain",
    "ts": "text/plain",
    "sh": "text/plain",
    "cs": "text/plain",
    "kt": "text/plain",
    "sql": "text/plain",
    # Web
    "md": "text/markdown",
    "markdown": "text/markdown",
    "mdx": "text/markdown",
    "htm": "text/html",
    "html": "text/html",
    "json": "application/json",
    # Image formats
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
    "tif": "image/tiff",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "ico": "image/x-icon",
    "avif": "image/avif",
    "heic": "image/heic",
    # Video formats
    "mp4": "video/mp4",
    "avi": "video/x-msvideo",
    "mov": "video/quicktime",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
    "wmv": "video/x-ms-wmv",
    "flv": "video/x-flv",
    # PPTX
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


FORCE_ATTACHMENT_EXTENSIONS = {
    "htm",
    "html",
    "shtml",
    "xht",
    "xhtml",
    "xml",
    "mhtml",
    "svg",
}


FORCE_ATTACHMENT_CONTENT_TYPES = {
    "text/html",
    "image/svg+xml",
    "application/xhtml+xml",
    "text/xml",
    "application/xml",
    "multipart/related",
}


def should_force_attachment(ext: str | None, content_type: str | None = None) -> bool:
    normalized_ext = (ext or "").lower().strip(".")
    if normalized_ext in FORCE_ATTACHMENT_EXTENSIONS:
        return True
    normalized_type = (content_type or "").lower()
    return normalized_type in FORCE_ATTACHMENT_CONTENT_TYPES


def apply_safe_file_response_headers(response, content_type: str | None, ext: str | None = None):
    if content_type:
        response.headers["Content-Type"] = content_type
    if should_force_attachment(ext, content_type):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Disposition"] = "attachment"
    return response


def html2pdf(
    source: str,
    timeout: int = 2,
    install_driver: bool = True,
    print_options=None,
):
    if print_options is None:
        print_options = {}
    result = __get_pdf_from_html(source, timeout, install_driver, print_options)
    return result


def __send_devtools(driver, cmd, params=None):
    # if params is None:
    #     params = {}
    # resource = "/session/%s/chromium/send_command_and_get_result" % driver.session_id
    # url = driver.command_executor._url + resource
    # body = json.dumps({"cmd": cmd, "params": params})
    # response = driver.command_executor._request("POST", url, body)
    #
    # if not response:
    #     raise Exception(response.get("value"))
    #
    # return response.get("value")

    """
    Sends a Chrome DevTools Protocol command to the browser.

    Args:
        driver: The WebDriver instance.
        command: The CDP command to execute (e.g., "Page.printToPDF").
        params: The parameters for the CDP command.

    Returns:
        The result of the command execution.
    """
    try:
        # 使用 Selenium 提供的 execute_cdp_cmd 方法执行命令
        return driver.execute_cdp_cmd(cmd, params)
    except AttributeError:
        raise RuntimeError("This Selenium WebDriver does not support execute_cdp_cmd. Ensure you are using a compatible driver and browser.")


def __get_pdf_from_html(path: str, timeout: int, install_driver: bool, print_options: dict):
    webdriver_options = Options()
    webdriver_prefs: dict = {}
    webdriver_options.add_argument("--headless")
    webdriver_options.add_argument("--disable-gpu")
    webdriver_options.add_argument("--no-sandbox")
    webdriver_options.add_argument("--disable-dev-shm-usage")
    webdriver_options.experimental_options["prefs"] = webdriver_prefs

    webdriver_prefs["profile.default_content_settings"] = {"images": 2}

    if install_driver:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=webdriver_options)
    else:
        driver = webdriver.Chrome(options=webdriver_options)

    driver.get(path)

    try:
        WebDriverWait(driver, timeout).until(staleness_of(driver.find_element(by=By.TAG_NAME, value="html")))
    except TimeoutException:
        calculated_print_options = {
            "landscape": False,
            "displayHeaderFooter": False,
            "printBackground": True,
            "preferCSSPageSize": True,
        }
        calculated_print_options.update(print_options)
        result = __send_devtools(driver, "Page.printToPDF", calculated_print_options)
        driver.quit()
        return base64.b64decode(result["data"])


def is_private_ip(ip: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip)
        return ip_obj.is_private
    except ValueError:
        return False


def is_valid_url(url: str) -> bool:
    if not re.match(r"(https?)://[-A-Za-z0-9+&@#/%?=~_|!:,.;]+[-A-Za-z0-9+&@#/%=~_|]", url):
        return False
    parsed_url = urlparse(url)
    hostname = parsed_url.hostname

    if not hostname:
        return False
    try:
        ip = socket.gethostbyname(hostname)
        if is_private_ip(ip):
            return False
    except socket.gaierror:
        return False
    return True


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """SSRF 视角的不可出网地址：私网、环回、link-local（含云 metadata 169.254.169.254）、保留段。"""
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


def validate_outbound_url(url: str, allowed_hosts: Sequence[str] | None = None) -> None:
    """校验用户提供的出网地址，不通过即抛 ``ValueError``（消息可直接回给调用方）。

    规则：
    - scheme 必须是 http/https，且必须有 hostname；
    - ``allowed_hosts`` 非空时按白名单裁决（大小写不敏感的 hostname 精确匹配）——
      命中即放行（自部署内网 Langfuse 等场景的显式逃生口），未命中直接拒绝；
    - 白名单为空（默认）时解析 hostname 的**全部**地址（getaddrinfo 覆盖多 A 记录与
      IPv6），任一落在私网/环回/link-local/保留段即拒绝。

    注意：DNS 解析是阻塞 IO——只用于配置写入/读取这类冷路径，禁止进聊天热路径。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL scheme must be http or https, got: {parsed.scheme or '(none)'}")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL must have a valid hostname")

    if allowed_hosts:
        if hostname.lower() in {h.strip().lower() for h in allowed_hosts if h.strip()}:
            return
        raise ValueError(f"Host is not in the configured allowlist: {hostname}")

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked_ip(literal):
            raise ValueError(f"URL points at a private or reserved address: {hostname}")
        return

    try:
        infos = socket.getaddrinfo(hostname, parsed.port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"Failed to resolve hostname: {hostname}") from e

    for info in infos:
        resolved = ipaddress.ip_address(info[4][0])
        if _is_blocked_ip(resolved):
            raise ValueError(f"URL resolves to a private or reserved address: {resolved}")


def safe_json_parse(data: str | dict) -> dict:
    if isinstance(data, dict):
        return data
    try:
        return json.loads(data) if data else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def get_float(req: dict, key: str, default: float | int = 10.0) -> float:
    try:
        parsed = float(req.get(key, default))
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


# Email functionality
async def send_email_html(subject: str, to_email: str, template_key: str, **context):
    """
    Generic HTML email sender using shared templates.
    template_key must exist in EMAIL_TEMPLATES.

    Args:
        subject: Email subject
        to_email: Recipient email address
        template_key: Key in EMAIL_TEMPLATES dictionary
        **context: Template variables to render

    Raises:
        ValueError: If template_key is not found
        Exception: If email sending fails
    """
    tmpl = EMAIL_TEMPLATES.get(template_key)
    if not tmpl:
        raise ValueError(f"Unknown email template: {template_key}")

    if _get_mail_sender() is None:
        logging.warning("SMTP not configured, skipping email send")
        return

    # Render email template
    template = Template(tmpl)
    html_body = template.render(**context)

    message = _build_email_message(subject, to_email, html_body)
    await _send_email_message(message)
    logging.info(f"Email '{subject}' sent to {to_email}")


async def send_invite_email(to_email: str, invite_url: str, tenant_id: str, inviter: str):
    """
    Send invitation email to a user.
    Reuses the generic HTML sender with 'invite' template.

    Args:
        to_email: Recipient email address
        invite_url: URL for accepting the invitation
        tenant_id: Tenant ID
        inviter: Name or email of the person sending the invitation
    """
    await send_email_html(
        subject="MultiRAG Invitation",
        to_email=to_email,
        template_key="invite",
        email=to_email,
        invite_url=invite_url,
        tenant_id=tenant_id,
        inviter=inviter,
    )


def otp_keys(email: str) -> tuple[str, str, str, str]:
    """
    Generate Redis keys for OTP management.

    Args:
        email: User email address

    Returns:
        Tuple of (otp_key, attempts_key, last_sent_key, lock_key)
    """
    email = (email or "").strip().lower()
    return (
        f"otp:{email}",
        f"otp_attempts:{email}",
        f"otp_last_sent:{email}",
        f"otp_lock:{email}",
    )


def hash_code(code: str, salt: bytes) -> str:
    """
    Generate a secure hash of a code using HMAC-SHA256.

    Args:
        code: The code to hash
        salt: Salt bytes for hashing

    Returns:
        Hexadecimal hash string
    """
    import hashlib
    import hmac

    return hmac.new(salt, (code or "").encode("utf-8"), hashlib.sha256).hexdigest()


def captcha_key(email: str) -> str:
    """
    Generate Redis key for captcha storage.

    Args:
        email: User email address

    Returns:
        Redis key string
    """
    return f"captcha:{email}"


def verified_key(email: str) -> str:
    """
    Generate Redis key for OTP verified state storage.

    Args:
        email: User email address

    Returns:
        Redis key string
    """
    return f"otp:verified:{email}"
