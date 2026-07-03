import datetime
import logging
import time


def current_timestamp():
    """
    Get the current timestamp in milliseconds.

    Returns:
        int: Current Unix timestamp in milliseconds (13 digits)

    Example:
        >>> current_timestamp()
        1704067200000
    """
    return int(time.time() * 1000)


def timestamp_to_date(timestamp, format_string="%Y-%m-%d %H:%M:%S"):
    """
    Convert a timestamp to formatted date string.

    Args:
        timestamp: Unix timestamp in milliseconds. If None or empty, uses current time.
        format_string: Format string for the output date (default: "%Y-%m-%d %H:%M:%S")

    Returns:
        str: Formatted date string

    Example:
        >>> timestamp_to_date(1704067200000)
        '2024-01-01 08:00:00'
    """
    if not timestamp:
        timestamp = time.time()
    timestamp = int(timestamp) / 1000
    time_array = time.localtime(timestamp)
    str_date = time.strftime(format_string, time_array)
    return str_date


def date_string_to_timestamp(time_str, format_string="%Y-%m-%d %H:%M:%S"):
    """
    Convert a date string to timestamp in milliseconds.

    Args:
        time_str: Date string to convert
        format_string: Format of the input date string (default: "%Y-%m-%d %H:%M:%S")

    Returns:
        int: Unix timestamp in milliseconds

    Example:
        >>> date_string_to_timestamp("2024-01-01 00:00:00")
        1704067200000
    """
    time_array = time.strptime(time_str, format_string)
    time_stamp = int(time.mktime(time_array) * 1000)
    return time_stamp


def datetime_format(date_time: datetime.datetime) -> datetime.datetime:
    """
    Normalize a datetime object by removing microsecond component.

    Creates a new datetime object with only year, month, day, hour, minute, second.
    Microseconds are set to 0.

    Args:
        date_time: datetime object to normalize

    Returns:
        datetime.datetime: New datetime object without microseconds

    Example:
        >>> dt = datetime.datetime(2024, 1, 1, 12, 30, 45, 123456)
        >>> datetime_format(dt)
        datetime.datetime(2024, 1, 1, 12, 30, 45)
    """
    return datetime.datetime(date_time.year, date_time.month, date_time.day, date_time.hour, date_time.minute, date_time.second)


def get_format_time() -> datetime.datetime:
    """
    Get current datetime normalized without microseconds.

    Returns:
        datetime.datetime: Current datetime with microseconds set to 0

    Example:
        >>> get_format_time()
        datetime.datetime(2024, 1, 1, 12, 30, 45)
    """
    return datetime_format(datetime.datetime.now())


def delta_seconds(date_input: str | datetime.datetime):
    """
    Calculate seconds elapsed from a given date to now.

    Args:
        date_input: Date string in "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD HH:MM:SS.ffffff" format,
                    or datetime object

    Returns:
        float: Number of seconds between the given date and current time

    Example:
        >>> delta_seconds("2024-01-01 12:00:00")
        3600.0  # If current time is 2024-01-01 13:00:00
        >>> delta_seconds("2024-01-01 12:00:00.123456")
        3600.0
        >>> delta_seconds(datetime.datetime(2024, 1, 1, 12, 0, 0))
        3600.0
    """
    if isinstance(date_input, datetime.datetime):
        dt = date_input
    else:
        # 尝试多种日期格式
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.datetime.strptime(date_input, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"time data '{date_input}' does not match expected formats")
    return (datetime.datetime.now() - dt).total_seconds()


def format_iso_8601_to_ymd_hms(time_str: str) -> str:
    """
    Convert ISO 8601 formatted string to "YYYY-MM-DD HH:MM:SS" format.

    Args:
        time_str: ISO 8601 date string (e.g. "2024-01-01T12:00:00Z")

    Returns:
        str: Date string in "YYYY-MM-DD HH:MM:SS" format

    Example:
        >>> format_iso_8601_to_ymd_hms("2024-01-01T12:00:00Z")
        '2024-01-01 12:00:00'
    """
    from dateutil import parser

    try:
        if parser.isoparse(time_str):
            dt = datetime.datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        else:
            return time_str
    except Exception as e:
        logging.error(str(e))
        return time_str
