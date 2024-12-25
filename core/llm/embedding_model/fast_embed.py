import os
import re
import threading

import numpy as np
from huggingface_hub import snapshot_download

from api import settings
from api.utils.file_utils import get_home_cache_dir
from core.llm.embedding_model.base import Base


class FastEmbed(Base):
    _model = None
    _model_name = ""
    _model_lock = threading.Lock()

    def __init__(
            self,
            key: str | None = None,
            model_name: str = "BAAI/bge-small-en-v1.5",
            cache_dir: str | None = None,
            threads: int | None = None,
            **kwargs,
    ):
        if not settings.LIGHTEN and not FastEmbed._model:
            with FastEmbed._model_lock:
                from fastembed import TextEmbedding
                if not FastEmbed._model or model_name != FastEmbed._model_name:
                    try:
                        FastEmbed._model = TextEmbedding(model_name, cache_dir, threads, **kwargs)
                        FastEmbed._model_name = model_name
                    except Exception:
                        cache_dir = snapshot_download(repo_id="BAAI/bge-small-en-v1.5",
                                                      local_dir=os.path.join(get_home_cache_dir(),
                                                                             re.sub(r"^[a-zA-Z0-9]+/", "", model_name)),
                                                      local_dir_use_symlinks=False)
                        FastEmbed._model = TextEmbedding(model_name, cache_dir, threads, **kwargs)
        self._model = FastEmbed._model
        self._model_name = model_name

    def encode(self, texts: list):
        # Using the internal tokenizer to encode the texts and get the total
        # number of tokens
        encodings = self._model.model.tokenizer.encode_batch(texts)
        total_tokens = sum(len(e) for e in encodings)

        embeddings = [e.tolist() for e in self._model.embed(texts, batch_size=16)]

        return np.array(embeddings), total_tokens

    def encode_queries(self, text: str):
        # Using the internal tokenizer to encode the texts and get the total
        # number of tokens
        encoding = self._model.model.tokenizer.encode(text)
        embedding = next(self._model.query_embed(text)).tolist()

        return np.array(embedding), len(encoding.ids)

