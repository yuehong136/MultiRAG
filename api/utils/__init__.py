import base64
import datetime
import hashlib
import os
import socket
import time
import uuid
import requests

import importlib

from .common import string_to_bytes

def current_timestamp():
    return int(time.time() * 1000)


def timestamp_to_date(timestamp, format_string="%Y-%m-%d %H:%M:%S"):
    if not timestamp:
        timestamp = time.time()
    timestamp = int(timestamp) / 1000
    time_array = time.localtime(timestamp)
    str_date = time.strftime(format_string, time_array)
    return str_date


def date_string_to_timestamp(time_str, format_string="%Y-%m-%d %H:%M:%S"):
    time_array = time.strptime(time_str, format_string)
    time_stamp = int(time.mktime(time_array) * 1000)
    return time_stamp


def get_lan_ip():
    if os.name != "nt":
        import fcntl
        import struct

        def get_interface_ip(ifname):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            return socket.inet_ntoa(
                fcntl.ioctl(s.fileno(), 0x8915, struct.pack('256s', string_to_bytes(ifname[:15])))[20:24])

    ip = socket.gethostbyname(socket.getfqdn())
    if ip.startswith("127.") and os.name != "nt":
        interfaces = [
            "bond1",
            "eth0",
            "eth1",
            "eth2",
            "wlan0",
            "wlan1",
            "wifi0",
            "ath0",
            "ath1",
            "ppp0",
        ]
        for ifname in interfaces:
            try:
                ip = get_interface_ip(ifname)
                break
            except IOError:
                pass
    return ip or ''


def from_dict_hook(in_dict: dict):
    if "type" in in_dict and "data" in in_dict:
        if in_dict["module"] is None:
            return in_dict["data"]
        else:
            return getattr(importlib.import_module(
                in_dict["module"]), in_dict["type"])(**in_dict["data"])
    else:
        return in_dict


def get_uuid():
    return uuid.uuid1().hex


def datetime_format(date_time: datetime.datetime) -> datetime.datetime:
    return datetime.datetime(date_time.year, date_time.month, date_time.day,
                             date_time.hour, date_time.minute, date_time.second)


def get_format_time() -> datetime.datetime:
    return datetime_format(datetime.datetime.now())


def str2date(date_time: str):
    return datetime.datetime.strptime(date_time, '%Y-%m-%d')


def elapsed2time(elapsed):
    seconds = elapsed / 1000
    minuter, second = divmod(seconds, 60)
    hour, minuter = divmod(minuter, 60)
    return '%02d:%02d:%02d' % (hour, minuter, second)


def download_img(url):
    if not url:
        return ""
    response = requests.get(url)
    return "data:" + \
        response.headers.get('Content-Type', 'image/jpg') + ";" + \
        "base64," + base64.b64encode(response.content).decode("utf-8")


def delta_seconds(date_string: str):
    if isinstance(date_string, str):
        date_string = datetime.datetime.strptime(date_string, "%Y-%m-%d %H:%M:%S")
    now = datetime.datetime.now()
    delta = now - date_string
    return delta.total_seconds()


def hash_str2int(line: str, mod: int = 10 ** 8) -> int:
    return int(hashlib.sha1(line.encode("utf-8")).hexdigest(), 16) % mod

HTTP_STATUS_CODES = {
    100: "Continue",
    101: "Switching Protocols",
    102: "Processing",
    103: "Early Hints",  # see RFC 8297
    200: "OK",
    201: "Created",
    202: "Accepted",
    203: "Non Authoritative Information",
    204: "No Content",
    205: "Reset Content",
    206: "Partial Content",
    207: "Multi Status",
    208: "Already Reported",  # see RFC 5842
    226: "IM Used",  # see RFC 3229
    300: "Multiple Choices",
    301: "Moved Permanently",
    302: "Found",
    303: "See Other",
    304: "Not Modified",
    305: "Use Proxy",
    306: "Switch Proxy",  # unused
    307: "Temporary Redirect",
    308: "Permanent Redirect",
    400: "Bad Request",
    401: "Unauthorized",
    402: "Payment Required",  # unused
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    406: "Not Acceptable",
    407: "Proxy Authentication Required",
    408: "Request Timeout",
    409: "Conflict",
    410: "Gone",
    411: "Length Required",
    412: "Precondition Failed",
    413: "Request Entity Too Large",
    414: "Request URI Too Long",
    415: "Unsupported Media Type",
    416: "Requested Range Not Satisfiable",
    417: "Expectation Failed",
    418: "I'm a teapot",  # see RFC 2324
    421: "Misdirected Request",  # see RFC 7540
    422: "Unprocessable Entity",
    423: "Locked",
    424: "Failed Dependency",
    425: "Too Early",  # see RFC 8470
    426: "Upgrade Required",
    428: "Precondition Required",  # see RFC 6585
    429: "Too Many Requests",
    431: "Request Header Fields Too Large",
    449: "Retry With",  # proprietary MS extension
    451: "Unavailable For Legal Reasons",
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
    505: "HTTP Version Not Supported",
    506: "Variant Also Negotiates",  # see RFC 2295
    507: "Insufficient Storage",
    508: "Loop Detected",  # see RFC 5842
    510: "Not Extended",
    511: "Network Authentication Failed",
}