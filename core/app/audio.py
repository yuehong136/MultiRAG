import logging
import os
import re
import tempfile

from api.db.db_models import db_connection
from api.db.joint_services.tenant_model_service import get_tenant_default_model_by_type
from api.db.services.llm_service import LLMBundle
from common.constants import LLMType
from core.nlp import rag_tokenizer, tokenize


def chunk(filename, binary, tenant_id, lang, callback=None, **kwargs):
    doc = {"docnm_kwd": filename, "title_tks": rag_tokenizer.tokenize(re.sub(r"\.[a-zA-Z]+$", "", filename))}
    doc["title_sm_tks"] = rag_tokenizer.fine_grained_tokenize(doc["title_tks"])

    # is it English
    is_english = lang.lower() == "english"  # is_english(sections)
    try:
        _, ext = os.path.splitext(filename)
        if not ext:
            raise RuntimeError("No extension detected.")

        if ext not in [".da", ".wave", ".wav", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".aiff", ".au", ".midi", ".wma", ".realaudio", ".vqf", ".oggvorbis", ".aac", ".ape"]:
            raise RuntimeError(f"Extension {ext} is not supported yet.")

        tmp_path = ""
        converted_path = ""
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmpf:
            tmpf.write(binary)
            tmpf.flush()
            tmp_path = os.path.abspath(tmpf.name)

        # 转换音频为 16kHz 单声道格式（通义千问要求）
        try:
            import subprocess

            converted_path = tmp_path.replace(ext, "_16k.wav")
            # ffmpeg -i input -ac 1 -ar 16000 -sample_fmt s16 output.wav
            subprocess.run(["ffmpeg", "-i", tmp_path, "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", converted_path, "-y"], capture_output=True, check=True)
            audio_path = converted_path
        except (FileNotFoundError, subprocess.CalledProcessError):
            # FFmpeg 不可用或转换失败，使用原始文件
            audio_path = tmp_path

        callback(0.1, "USE Sequence2Txt LLM to transcription the audio")
        with db_connection() as db:
            model_config = get_tenant_default_model_by_type(db, tenant_id, LLMType.SPEECH2TEXT)
            seq2txt_mdl = LLMBundle(db, tenant_id, model_config, lang=lang)
        ans = seq2txt_mdl.transcription(audio_path)
        callback(0.8, "Sequence2Txt LLM respond: %s ..." % ans[:32])

        tokenize(doc, ans, is_english)
        return [doc]
    except Exception as e:
        callback(prog=-1, msg=str(e))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        if converted_path and os.path.exists(converted_path):
            try:
                os.unlink(converted_path)
            except Exception as e:
                logging.exception(f"Failed to remove temporary file: {tmp_path}, exception: {e}")
                pass
    return []
