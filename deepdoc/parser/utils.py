from io import BytesIO

from pypdf import PdfReader as pdf2_read

from core.nlp import find_codec


def get_text(fnm: str, binary=None) -> str:
    txt = ""
    if binary is not None:
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


def extract_pdf_outlines(source):
    try:
        with pdf2_read(source if isinstance(source, str) else BytesIO(source)) as pdf:
            outlines = []

            def dfs(nodes, depth):
                for node in nodes:
                    if isinstance(node, list):
                        dfs(node, depth + 1)
                    else:
                        outlines.append((node["/Title"], depth, pdf.get_destination_page_number(node) + 1))

            dfs(pdf.outline, 0)
            return outlines
    except Exception:
        return []
