from core.nlp import find_codec


def get_text(fnm: str, binary=None) -> str:
    txt = ""
    if binary:
        encoding = find_codec(binary)
        txt = binary.decode(encoding, errors="ignore")
    else:
        with open(fnm, "r", encoding="utf-8") as f:
            while True:
                line = f.readline()
                if not line:
                    break
                txt += line

    if not txt.strip():
        raise ValueError("File content is empty")

    return txt