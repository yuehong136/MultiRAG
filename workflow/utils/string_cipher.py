import base64


def encrypt(text, key):
    text_bytes = text.encode('utf-8')
    key_bytes = key.encode('utf-8')
    result = bytearray()

    for i in range(len(text_bytes)):
        result.append(text_bytes[i] ^ key_bytes[i % len(key_bytes)])

    return base64.b64encode(result).decode('utf-8')

def decrypt(encrypted_text, key):
    encrypted_bytes = base64.b64decode(encrypted_text)
    key_bytes = key.encode('utf-8')
    result = bytearray()

    for i in range(len(encrypted_bytes)):
        result.append(encrypted_bytes[i] ^ key_bytes[i % len(key_bytes)])

    return result.decode('utf-8')

# 测试代码
# original_text = "Hello, World"
# key = "DATAV-SK-666"
#
# encrypted_text = encrypt(original_text, key)
# print(f"Encrypted: {encrypted_text}")
#
# decrypted_text = decrypt(encrypted_text, key)
# print(f"Decrypted: {decrypted_text}")
