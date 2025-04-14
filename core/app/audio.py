# coding=utf-8
"""
@project: multirag
@Author：龙
@file： audio.py
@date：2024/7/26 11:00
@desc:
"""
import re

from api.db import LLMType
from api.db.db_models import SessionLocal
# from api.db.database import SessionLocal
from core.nlp import rag_tokenizer
from api.db.services.llm_service import LLMBundle
from core.nlp import tokenize


def chunk(filename, binary, tenant_id, lang, callback=None, **kwargs):
    doc = {
        "docnm_kwd": filename,
        "title_tks": rag_tokenizer.tokenize(re.sub(r"\.[a-zA-Z]+$", "", filename))
    }
    doc["title_sm_tks"] = rag_tokenizer.fine_grained_tokenize(doc["title_tks"])

    # is it English
    eng = lang.lower() == "english"  # is_english(sections)
    try:
        callback(0.1, "USE Sequence2Txt LLM to transcription the audio")
        db = SessionLocal()
        seq2txt_mdl = LLMBundle(db, tenant_id, LLMType.SPEECH2TEXT, lang=lang)
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as temp_audio_file:
            temp_audio_file.write(binary)
            audio_file_path = temp_audio_file.name
        # ans = seq2txt_mdl.transcription(binary)
        ans = seq2txt_mdl.transcription(audio=audio_file_path)
        callback(0.8, "Sequence2Txt LLM respond: %s ..." % ans[:32])
        tokenize(doc, ans, eng)
        return [doc]
    except Exception as e:
        callback(prog=-1, msg=str(e))

    return []
