class DefaultDict(dict):
    def __missing__(self, key):
        return '{' + key + '}'


def safe_format(template, **kwargs):
    return template.format_map(DefaultDict(kwargs))


import re


def safe_format_double_braces(template, **kwargs):
    def replacer(match):
        key = match.group(1)
        return str(kwargs.get(key, f"{{{{{key}}}}}"))

    pattern = r'\{\{(\w+)\}\}'
    return re.sub(pattern, replacer, template)
