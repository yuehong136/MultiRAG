import re


def is_english(texts):
    eng = 0
    if not texts: return False
    for t in texts:
        if re.match(r"[a-zA-Z]{2,}", t.strip()):
            eng += 1
    if eng / len(texts) > 0.8:
        return True
    return False