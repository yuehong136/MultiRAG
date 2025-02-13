import random
import string


def generate_code(length: int = 8) -> str:
    """
    使用 random.choices 生成随机字符串
    :param length: 字符串长度,默认8位
    :return: 随机字符串
    """
    # 大写字母和数字的字符集
    characters = string.ascii_uppercase + string.digits
    # 使用 random.choices 随机选择字符
    return ''.join(random.choices(characters, k=length))


def generate_code_v2(length: int = 8) -> str:
    """
    确保同时包含字母和数字的随机字符串
    :param length: 字符串长度,默认8位
    :return: 随机字符串
    """
    # 确保至少包含一个字母和一个数字
    letters = ''.join(random.choices(string.ascii_uppercase, k=length - 1))
    digits = ''.join(random.choices(string.digits, k=1))

    # 组合并打乱顺序
    combined = list(letters + digits)
    random.shuffle(combined)
    return ''.join(combined)


# 使用示例
if __name__ == "__main__":
    # 方法1: 完全随机
    print("方法1生成:", generate_code())  # 例如: "A2B5C9DX"

    # 方法2: 确保同时包含字母和数字
    print("方法2生成:", generate_code_v2())  # 例如: "XYZ12ABC"
