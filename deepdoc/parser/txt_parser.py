# coding=utf-8
"""
@project: multirag
@Author：龙
@file： txt_parser.py
@date：2024/8/6 17:40
@desc:
"""
from core.nlp import find_codec,num_tokens_from_string
import re

class RAGFlowTxtParser:
    def __call__(self, fnm, binary=None, chunk_token_num=128, delimiter="\n!?;。；！？"):
        txt = ""
        if binary:
            encoding = find_codec(binary)
            txt = binary.decode(encoding, errors="ignore")
        else:
            with open(fnm, "r") as f:
                while True:
                    l = f.readline()
                    if not l:
                        break
                    txt += l
        return self.parser_txt(txt, chunk_token_num, delimiter)

    @classmethod
    def parser_txt(cls, txt, chunk_token_num=128, delimiter="\n!?;。；！？"):
        if type(txt) != str:
            raise TypeError("txt type should be str!")
        sections = []
        for sec in re.split(r"[%s]+"%delimiter, txt):
            if sections and sec in delimiter:
                sections[-1][0] += sec
                continue
            if num_tokens_from_string(sec) > 10 * int(chunk_token_num):
                sections.append([sec[: int(len(sec) / 2)], ""])
                sections.append([sec[int(len(sec) / 2) :], ""])
            else:
                sections.append([sec, ""])
        return sections