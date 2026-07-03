import re
from typing import Any


def parse_template(template: str, values: dict[str, Any]) -> str:
    """
    Parse a template string and replace placeholders with actual values.

    Args:
        template (str): Template string containing placeholders in {{...}} format
        values (Dict[str, Any]): Dictionary containing actual values

    Returns:
        str: Parsed string with all placeholders replaced with actual values

    Example:
        template = "Hello {{name}}, items: {{items[0]}}"
        values = {"name": "John", "items": ["apple", "banana"]}
        result = parse_template(template, values)  # "Hello John, items: apple"
    """

    if len(template) == 0:
        return ""

    def get_nested_value(key_path: str, data: dict[str, Any]) -> str | Any:
        """Helper function to get nested dictionary values."""
        # Handle array index access (e.g., key[0])
        array_index_match = re.match(r'(.+)\[(\d+)\]$', key_path)
        if array_index_match:
            key = array_index_match.group(1)
            index = int(array_index_match.group(2))
            try:
                return data.get(key, [])[index]
            except (IndexError, TypeError):
                return f"{{{{Invalid array index: {key_path}}}}}"

        # Handle nested dictionary access (e.g., key.subkey)
        if '.' in key_path:
            parts = key_path.split('.')
            current = data
            for part in parts:
                if isinstance(current, dict):
                    current = current.get(part, None)
                    if current is None:
                        return f"{{{{Invalid nested key: {key_path}}}}}"
                else:
                    return f"{{{{Invalid nested key: {key_path}}}}}"
            return current

        # Simple key access
        return data.get(key_path, f"{{{{Invalid key: {key_path}}}}}")

    def replace_placeholder(match: re.Match) -> str:
        """Helper function to replace each placeholder with its value."""
        key = match.group(1).strip()
        value = get_nested_value(key, values)

        # Handle different types of values
        if isinstance(value, (dict, list)):
            return str(value)
        return str(value)

    # Find all placeholders {{...}} and replace them
    pattern = r'\{\{([^}]+)\}\}'
    result = re.sub(pattern, replace_placeholder, template)

    return result

# # Example usage and test cases
# if __name__ == "__main__":
#     # Test template and values
#     template = """bot_user_input:{{BOT_USER_INPUT}}
# code_key1:{{code_key1}}
# code_key1_0:{{code_key1[0]}}
# code_key2:{{code_key2}}
# code_key2_key21:{{code_key2.key21}}
# code_key3:{{a}}"""
#
#     values = {
#         "BOT_USER_INPUT": "a",
#         "code_key1": ["hello", "world"],
#         "code_key2": {"key21": "hi"},
#         "a": "{(a}}"
#     }
#
#     result = parse_template(template, values)
#     print("Parsed result:")
#     print(result)
#
#     # Additional test cases
#     test_template = "Nested: {{obj.deep.key}}, Array: {{arr[1]}}"
#     test_values = {
#         "obj": {"deep": {"key": "value"}},
#         "arr": ["first", "second"]
#     }
#
#     print("\nAdditional test case:")
#     print(parse_template(test_template, test_values))
