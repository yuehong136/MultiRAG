import re


def safe_format_double_braces(template, **kwargs):
    def replacer(match):
        key = match.group(1)
        return str(kwargs.get(key, f"{{{{{key}}}}}"))

    pattern = r"\{\{(\w+)\}\}"
    return re.sub(pattern, replacer, template)
