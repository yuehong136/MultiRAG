import ipaddress
import re
import json
import base64
import socket
from urllib.parse import urlparse

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support.expected_conditions import staleness_of
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By


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
        raise RuntimeError(
            "This Selenium WebDriver does not support execute_cdp_cmd. "
            "Ensure you are using a compatible driver and browser."
        )

def __get_pdf_from_html(
        path: str,
        timeout: int,
        install_driver: bool,
        print_options: dict
):
    webdriver_options = Options()
    webdriver_prefs = {}
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
        WebDriverWait(driver, timeout).until(
            staleness_of(driver.find_element(by=By.TAG_NAME, value="html"))
        )
    except TimeoutException:
        calculated_print_options = {
            "landscape": False,
            "displayHeaderFooter": False,
            "printBackground": True,
            "preferCSSPageSize": True,
        }
        calculated_print_options.update(print_options)
        # result = __send_devtools(
        #     driver, "Page.printToPDF", calculated_print_options)
        result = driver.execute_cdp_cmd("Page.printToPDF", calculated_print_options)
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
