def string_to_bytes(string):
    return string if isinstance(
        string, bytes) else string.encode(encoding="utf-8")


def bytes_to_string(byte):
    return byte.decode(encoding="utf-8")