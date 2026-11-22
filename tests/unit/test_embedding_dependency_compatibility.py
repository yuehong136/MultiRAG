"""Exercise the production embedding adapters with local random model weights."""

from pathlib import Path
from typing import Any

import numpy as np
import pytest
import torch
from tokenizers import Tokenizer
from tokenizers.models import WordPiece
from tokenizers.normalizers import BertNormalizer
from tokenizers.pre_tokenizers import BertPreTokenizer
from tokenizers.processors import TemplateProcessing
from transformers import BertConfig, BertForSequenceClassification, BertModel, PreTrainedTokenizerFast

from core.llm import embedding, rerank


@pytest.fixture(params=["safetensors", "pytorch_bin"])
def local_weights(request: pytest.FixtureRequest, tmp_path: Path) -> dict[str, Path]:
    """Create tiny weights ourselves; package loaders must never contact a model host."""
    torch.manual_seed(717)
    tokens = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]", "hello", "world", "query", "first", "second", "passage", "document"]
    backend = Tokenizer(WordPiece({token: index for index, token in enumerate(tokens)}, unk_token="[UNK]"))
    backend.normalizer = BertNormalizer(lowercase=True)
    backend.pre_tokenizer = BertPreTokenizer()
    backend.post_processor = TemplateProcessing(single="[CLS] $A [SEP]", pair="[CLS] $A [SEP] $B:1 [SEP]:1", special_tokens=[("[CLS]", 2), ("[SEP]", 3)])
    tokenizer = PreTrainedTokenizerFast(tokenizer_object=backend, unk_token="[UNK]", pad_token="[PAD]", cls_token="[CLS]", sep_token="[SEP]", mask_token="[MASK]", model_max_length=64)
    config = BertConfig(vocab_size=len(tokens), hidden_size=16, num_hidden_layers=1, num_attention_heads=2, intermediate_size=32, max_position_embeddings=64, num_labels=1)
    paths = {}
    for name, model_type in [("bce-embedding-base_v1", BertModel), ("bce-reranker-base_v1", BertForSequenceClassification)]:
        path = tmp_path / name
        tokenizer.save_pretrained(path)
        model = model_type(config)
        if request.param == "safetensors":
            model.save_pretrained(path)
            assert (path / "model.safetensors").is_file()
        else:
            model.config.save_pretrained(path)
            torch.save(model.state_dict(), path / "pytorch_model.bin")
            assert not (path / "model.safetensors").exists()
        paths[name] = path
    return paths


def test_production_embedding_and_rerank_apis_use_local_weights(monkeypatch: pytest.MonkeyPatch, local_weights: dict[str, Path]) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setattr(embedding.settings, "LIGHTEN", 0)
    cache = local_weights["bce-embedding-base_v1"].parent
    monkeypatch.setattr(embedding, "get_home_cache_dir", lambda: str(cache))
    monkeypatch.setattr(rerank, "get_home_cache_dir", lambda: str(cache))
    for cls, attribute in [(embedding.DefaultEmbedding, "_model"), (embedding.YoudaoEmbed, "_client"), (rerank.DefaultRerank, "_model"), (rerank.YoudaoRerank, "_model")]:
        monkeypatch.setattr(cls, attribute, None)
    monkeypatch.setattr(embedding.DefaultEmbedding, "_model_key", None)
    monkeypatch.setattr(embedding.DefaultEmbedding, "_model_name", "")

    baai = embedding.DefaultEmbedding(None, "local/tiny-bert", model_path=str(local_weights["bce-embedding-base_v1"]), query_instruction="query ")
    youdao = embedding.YoudaoEmbed()
    for model in [baai, youdao]:
        vectors, tokens = model.encode(["hello world", "first document"])
        assert vectors is not None and vectors.shape == (2, 16) and tokens > 0
        assert np.isfinite(vectors).all()
        np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), [1, 1], atol=1e-5)
        query, query_tokens = model.encode_queries("hello query")
        assert query.shape == (16,) and query_tokens > 0 and np.isfinite(query).all()

    for model in [rerank.DefaultRerank(None, "local/bce-reranker-base_v1"), rerank.YoudaoRerank()]:
        scores, tokens = model._compute_rank("query", ["first passage", "second document"])
        assert scores.shape == (2,) and tokens > 0 and np.isfinite(scores).all()
        assert ((scores >= 0) & (scores <= 1)).all()
        single, _ = model._compute_rank("query", ["first passage"])
        assert single.shape == (1,) and np.isfinite(single).all()


def test_sentence_transformer_normal_inference(local_weights: dict[str, Path]) -> None:
    from sentence_transformers import CrossEncoder, SentenceTransformer, models

    transformer = models.Transformer(str(local_weights["bce-embedding-base_v1"]), max_seq_length=32)
    model = SentenceTransformer(modules=[transformer, models.Pooling(16, pooling_mode="cls")], device="cpu")
    vectors: Any = model.encode(["hello world", "first document"], normalize_embeddings=True, show_progress_bar=False)
    assert vectors.shape == (2, 16) and np.isfinite(vectors).all()
    ranker = CrossEncoder(str(local_weights["bce-reranker-base_v1"]), device="cpu", max_length=32)
    scores = ranker.predict([("query", "first passage"), ("query", "second document")], show_progress_bar=False)
    assert scores.shape == (2,) and np.isfinite(scores).all()
