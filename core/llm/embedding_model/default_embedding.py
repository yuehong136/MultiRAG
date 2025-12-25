# embedding_model/default_embedding.py
import re
import os
import numpy as np
from huggingface_hub import snapshot_download
import threading
from common import settings
from api.utils.file_utils import get_home_cache_dir
from common.token_utils import num_tokens_from_string, truncate
from core.llm.embedding_model.base import Base

class DefaultEmbedding(Base):
    _model = None
    _model_name = ""
    _model_lock = threading.Lock()

    def __init__(self, key, model_name, **kwargs):
        super().__init__(key, model_name)

        if not settings.LIGHTEN:
            input_cuda_visible_devices = None
            with DefaultEmbedding._model_lock:
                import torch
                from FlagEmbedding import FlagModel
                if "CUDA_VISIBLE_DEVICES" in os.environ:
                    input_cuda_visible_devices = os.environ["CUDA_VISIBLE_DEVICES"]
                    os.environ["CUDA_VISIBLE_DEVICES"] = "0" # handle some issues with multiple GPUs when initializing the model

                if not DefaultEmbedding._model or model_name != DefaultEmbedding._model_name:
                    try:
                        DefaultEmbedding._model = FlagModel(
                            os.path.join(get_home_cache_dir(), re.sub(r"^[a-zA-Z0-9]+/", "", model_name)),
                            query_instruction_for_retrieval="为这个句子生成表示以用于检索相关文章：",
                            use_fp16=torch.cuda.is_available())
                        DefaultEmbedding._model_name = model_name
                    except Exception:
                        model_dir = snapshot_download(repo_id="BAAI/bge-large-zh-v1.5",
                                                      local_dir=os.path.join(get_home_cache_dir(), re.sub(r"^[a-zA-Z0-9]+/", "", model_name)),
                                                      local_dir_use_symlinks=False)
                        DefaultEmbedding._model = FlagModel(model_dir,
                                                            query_instruction_for_retrieval="为这个句子生成表示以用于检索相关文章：",
                                                            use_fp16=torch.cuda.is_available())
                    finally:
                        if input_cuda_visible_devices:
                            # restore CUDA_VISIBLE_DEVICES
                            os.environ["CUDA_VISIBLE_DEVICES"] = input_cuda_visible_devices
        self._model = DefaultEmbedding._model
        self._model_name = DefaultEmbedding._model_name

    def encode(self, texts: list):
        batch_size = 16
        texts = [truncate(t, 2048) for t in texts]
        token_count = 0
        for t in texts:
            token_count += num_tokens_from_string(t)
        ress = None
        for i in range(0, len(texts), batch_size):
            if ress is None:
                ress = self._model.encode(texts[i : i + batch_size], convert_to_numpy=True)
            else:
                ress = np.concatenate((ress, self._model.encode(texts[i : i + batch_size], convert_to_numpy=True)), axis=0)
        return ress, token_count

    def encode_queries(self, text: str):
        token_count = num_tokens_from_string(text)
        return self._model.encode_queries([text], convert_to_numpy=False)[0][0].cpu().numpy(), token_count
