#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""spaCy NER plus MGranRAG keyword stacking and sentence co-occurrence.

Models are installed with the application; extraction never downloads resources.
Inference runs off the event loop and fails the document on any chunk error.
"""

import re
from collections import defaultdict
from collections.abc import Callable
from threading import RLock
from typing import Any

from spacy.language import Language
from spacy.tokens import Doc, Span

from api.db.services.task_service import has_canceled
from common.exceptions import TaskCanceledException
from core.graphrag.general.extractor import Extractor
from core.graphrag.llm_protocol import GraphRAGCompletionLLM
from core.graphrag.persistence import settled_io

_MODEL_LOCK = RLock()
_MODELS: dict[str, Language] = {}


def model_for_language(language: str | None) -> str:
    lang = (language or "English").strip().lower()
    if lang in {"english", "en", "en-us", "en-gb"}:
        return "en_core_web_sm"
    if lang in {"chinese", "zh", "zh-cn", "zh-tw", "zh-hans", "zh-hant", "traditional chinese"}:
        return "zh_core_web_sm"
    raise ValueError(f"GraphRAG NER supports English and Chinese, got {language!r}")


def _load_spacy_model(model_name: str) -> Language:
    import spacy

    with _MODEL_LOCK:
        if model_name not in _MODELS:
            try:
                _MODELS[model_name] = spacy.load(model_name)
            except OSError as exc:
                raise RuntimeError(f"GraphRAG NER model {model_name!r} is missing; install the locked application dependencies before running NER") from exc
        return _MODELS[model_name]


SPACY_TO_APP_ENTITY_TYPE: dict[str, str] = {
    "PERSON": "person",
    "PER": "person",
    "ORG": "organization",
    "GPE": "geo",
    "LOC": "geo",
    "FAC": "geo",
    "EVENT": "event",
    "PRODUCT": "category",
    "WORK_OF_ART": "category",
    "LAW": "category",
    "LANGUAGE": "category",
    "NORP": "category",
    "MONEY": "category",
    "QUANTITY": "category",
    "TIME": "event",
    "DATE": "event",
}

# Labels to skip entirely (from LinearRAG: ordinals / cardinals are rarely
# useful as graph nodes).
_SKIP_SPACY_LABELS = {"ORDINAL", "CARDINAL"}


# ---------------------------------------------------------------------------
# MGranRAG-style multi-pass keyword extraction
# ---------------------------------------------------------------------------


def _has_uppercase(text: str) -> bool:
    return any(c.isupper() for c in text)


def _replace_word(word: str) -> str:
    """Normalise spaces around hyphens and apostrophes (from MGranRAG)."""
    return word.replace(" - ", "-").replace(" -", "-").replace("- ", "-").replace(" 's", "'s").replace(" 'S", "'S")


def extract_keywords(spacy_doc: Doc | Span) -> set[str]:
    """MGranRAG-style 3-pass stacking keyword extraction.

    Phase 1 — Hyphen / apostrophe merging:
        Tokens connected by ``-`` or ``'s`` are merged into a single
        phrase labelled ``NP`` (e.g. ``New-York``, ``cat's``).

    Phase 2 — Capitalised-word merging:
        Consecutive tokens whose ``shape_`` contains ``X`` (i.e. start
        with an uppercase letter) are merged.  Function words (ADP, CCONJ,
        DET, PART) between them are absorbed as well, producing phrases
        like ``King of England``.  Merged results are labelled ``NX``
        unless already ``PROPN``.

    Phase 3 — Continuous noun / number merging:
        Consecutive tokens with POS in ``[PROPN, NOUN, NUM, NX, NP]``
        are merged and labelled ``NNN`` (unless already ``PROPN``).

    Finally, results with a trailing lowercase non-noun word are
    truncated, and coordinating conjunctions (``and``, ``or``) inside a
    merged phrase cause it to be split so that each proper noun is
    extracted individually (e.g. ``Bob and Lucy`` → ``Bob``, ``Lucy``).
    """
    # ── Phase 1: hyphen / apostrophe ──────────────────────────────────
    f1_word: list[str] = []
    f1_shape: list[str] = []
    f1_pos: list[str] = []
    f1_pos_list: list[list[str]] = []
    f1_word_list: list[list[str]] = []

    is_right = False
    for token in spacy_doc:
        if token.shape_ in ("'x", "-") and token.pos_ in ("PUNCT", "PART"):
            if token.shape_ == "-":
                is_right = True
            if f1_word:
                f1_word[-1] += token.text
                f1_pos[-1] = "NP"
                f1_pos_list[-1].append(token.pos_)
                f1_word_list[-1].append(token.text)
        elif is_right:
            is_right = False
            if f1_word:
                f1_word[-1] += token.text
                f1_pos[-1] = "NP"
                f1_pos_list[-1].append(token.pos_)
                f1_word_list[-1].append(token.text)
        else:
            f1_word.append(token.text)
            f1_shape.append(token.shape_)
            f1_pos.append(token.pos_)
            f1_pos_list.append([token.pos_])
            f1_word_list.append([token.text])

    # ── Phase 2: capitalised-word merging ───────────────────────────
    f2_word: list[str] = []
    f2_shape: list[str] = []
    f2_pos: list[str] = []
    f2_pos_list: list[list[str]] = []
    f2_word_list: list[list[str]] = []

    for cur in range(len(f1_word)):
        cw = f1_word[cur]
        cs = f1_shape[cur]
        cp = f1_pos[cur]
        cpl = f1_pos_list[cur]
        cwl = f1_word_list[cur]

        if "X" in cs or cp in ("ADP", "CCONJ", "DET", "PART"):
            if f2_word and "X" in f2_shape[-1]:
                # Merge with previous capitalised token.
                f2_word[-1] += " " + cw
                f2_shape[-1] += "X"
                if f2_pos[-1] != "PROPN":
                    f2_pos[-1] = "NX"
                f2_pos_list[-1].extend(cpl)
                f2_word_list[-1].extend(cwl)
            else:
                f2_word.append(cw)
                f2_shape.append(cs + "Start" if "X" in cs else cs)
                f2_pos.append(cp)
                f2_pos_list.append(cpl)
                f2_word_list.append(cwl)
        else:
            f2_word.append(cw)
            f2_shape.append(cs)
            f2_pos.append(cp)
            f2_pos_list.append(cpl)
            f2_word_list.append(cwl)

    # ── Phase 3: continuous noun / number merging ───────────────────
    f3_word: list[str] = []
    f3_shape: list[str] = []
    f3_pos: list[str] = []
    f3_pos_list: list[list[str]] = []
    f3_word_list: list[list[str]] = []

    _noun_pos = {"PROPN", "NOUN", "NUM", "NX", "NP"}
    _noun_pos_ext = _noun_pos | {"NNN"}

    for cur in range(len(f2_word)):
        cw = f2_word[cur]
        cs = f2_shape[cur]
        cp = f2_pos[cur]
        cpl = f2_pos_list[cur]
        cwl = f2_word_list[cur]

        if cp in _noun_pos:
            if f3_word and f3_pos[-1] in _noun_pos_ext:
                f3_word[-1] += " " + cw
                f3_shape[-1] += "X"
                if f3_pos[-1] != "PROPN":
                    f3_pos[-1] = "NNN"
                f3_pos_list[-1].extend(cpl)
                f3_word_list[-1].extend(cwl)
            else:
                f3_word.append(cw)
                f3_shape.append(cs)
                f3_pos.append(cp)
                f3_pos_list.append(cpl)
                f3_word_list.append(cwl)
        else:
            f3_word.append(cw)
            f3_shape.append(cs)
            f3_pos.append(cp)
            f3_pos_list.append(cpl)
            f3_word_list.append(cwl)

    # ── Final keyword collection ────────────────────────────────────
    keywords: set[str] = set()
    for cur in range(len(f3_word)):
        cw = f3_word[cur]
        cp = f3_pos[cur]
        cpl = f3_pos_list[cur]
        cwl = f3_word_list[cur]

        if cp not in _noun_pos_ext:
            continue

        # Truncate trailing lowercase non-noun / non-number words.
        if (
            cwl
            and not _has_uppercase(cwl[-1])
            and cpl[-1]
            not in (
                "PROPN",
                "NOUN",
                "NUM",
                "PART",
            )
        ):
            for i in range(len(cpl) - 1, 0, -1):
                if cpl[i] in ("PROPN", "NOUN", "NUM", "PART") or _has_uppercase(cwl[i]):
                    break
            word = _replace_word(" ".join(cwl[: i + 1]))
            keywords.add(word)
        else:
            word = _replace_word(cw)
            keywords.add(word)

        # Split on coordinating conjunctions (and/or) inside merged
        # phrases so that individual proper nouns are also extracted
        # (e.g. ``Bob and Lucy`` → ``Bob``, ``Lucy``).
        if any(p in ("PROPN", "NOUN", "NUM") for p in cpl):
            cur_kws: list[str] = []
            for pidx, pos in enumerate(cpl):
                if pos == "CCONJ" and cwl[pidx] and cwl[pidx][0].islower():
                    if cur_kws:
                        keywords.add(_replace_word(" ".join(cur_kws)))
                    cur_kws = []
                else:
                    cur_kws.append(cwl[pidx])
            if cur_kws:
                keywords.add(_replace_word(" ".join(cur_kws)))

    return keywords


def get_ner(spacy_doc: Doc | Span) -> dict[str, str]:
    """Return ``{entity_text: spaCy_label}`` for all NER entities."""
    entities_dict: dict[str, str] = {}
    for ent in spacy_doc.ents:
        if ent.label_ in _SKIP_SPACY_LABELS:
            continue
        text = ent.text.strip()
        for t in text.split("\n"):
            t = t.strip()
            if t:
                entities_dict[t] = ent.label_
    return entities_dict


def ner_all_keywords(spacy_doc: Doc | Span) -> set[str]:
    """Combine rule-based keyword extraction with spaCy NER (MGranRAG).

    Returns the union of:
    - keywords from the 3-pass stacking algorithm (``extract_keywords``)
    - entity texts from spaCy NER (``get_ner``)
    """
    keywords = extract_keywords(spacy_doc)
    ner_dict = get_ner(spacy_doc)
    return keywords.union(ner_dict.keys())


class GraphExtractor(Extractor):
    """Use local NLP for extraction; inherited merging may summarize with the LLM."""

    def __init__(
        self,
        llm_invoker: GraphRAGCompletionLLM,
        language: str | None = "English",
        entity_types: list[str] | None = None,
        spacy_model: str | None = None,
        max_sentence_distance: int = 0,
        relationship_strength: int = 1,
        use_tf_weight: bool = False,
    ) -> None:
        super().__init__(llm_invoker, language, entity_types)
        if max_sentence_distance < 0 or relationship_strength <= 0:
            raise ValueError("Sentence distance must be nonnegative and relationship strength positive")
        self._model_name = spacy_model or model_for_language(language)
        self._max_sentence_distance = max_sentence_distance
        self._relationship_strength = relationship_strength
        self._use_tf_weight = use_tf_weight
        self.callback: Callable[..., Any] | None = None

    def _extract_chunk(self, content: str, source_id: str) -> tuple[dict[str, list[dict[str, Any]]], dict[tuple[str, str], list[dict[str, Any]]], int]:
        # spaCy/Chinese tokenizer objects are shared only under this lock. The
        # caller uses settled_io so cancellation drains inference before exit.
        with _MODEL_LOCK:
            doc = _load_spacy_model(self._model_name)(content)
        sentences = list(doc.sents)
        nodes: dict[str, list[dict[str, Any]]] = defaultdict(list)
        occurrences: dict[str, set[int]] = defaultdict(set)
        allowed = {t.lower() for t in self._entity_types}
        for si, sentence in enumerate(sentences):
            labels = get_ner(sentence)
            # Work sentence by sentence: keyword stacking cannot cross sentence
            # boundaries, and repeated entities retain every occurrence.
            keywords = set()
            for keyword in ner_all_keywords(sentence):
                # Stacking inserts spaces between tokens. Recover the actual
                # source span (Chinese has no inter-word spaces), so the NER
                # and keyword paths do not create duplicate identities. Keep
                # original case: an earlier ordinary "apple" must not replace
                # a later named "Apple" and lose its NER label.
                pattern = r"\s*".join(re.escape(word) for word in keyword.split())
                match = re.search(pattern, sentence.text) if pattern else None
                if match:
                    keywords.add(match.group().strip())
            for keyword in sorted(keywords):
                label = labels.get(keyword)
                app_type = SPACY_TO_APP_ENTITY_TYPE.get(label, "category") if label else self._infer_type_from_pos(sentence, keyword)
                if app_type not in allowed:
                    continue
                name = keyword.upper()
                occurrences[name].add(si)
                nodes[name].append({"entity_name": name, "entity_type": app_type.upper(), "description": sentence.text.strip(), "source_id": source_id})
        edges: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        names = sorted(nodes)
        total = sum(len(indices) for indices in occurrences.values())
        for ai, a in enumerate(names):
            for b in names[ai + 1 :]:
                bridges = {(i, j) for i in occurrences[a] for j in occurrences[b] if abs(i - j) <= self._max_sentence_distance}
                if not bridges:
                    continue
                indices = sorted({si for pair in bridges for si in pair})
                weight = (len(occurrences[a]) + len(occurrences[b])) / total if self._use_tf_weight else self._relationship_strength
                edges[(a, b)].append({"src_id": a, "tgt_id": b, "weight": weight, "description": " ".join(sentences[i].text.strip() for i in indices), "keywords": [a, b], "source_id": source_id})
        return dict(nodes), dict(edges), len(doc)

    @staticmethod
    def _infer_type_from_pos(sentence: Span, keyword: str) -> str:
        # The stacking algorithm inserts spaces; token matching avoids assigning
        # an unrelated prefix (e.g. Ann vs Anna) to an entity.
        first = re.split(r"\s+", keyword)[0].casefold()
        token = next((token for token in sentence if token.text.casefold() == first), None)
        return {"PROPN": "person", "NUM": "event"}.get(token.pos_, "category") if token is not None else "category"

    async def _check_canceled(self, task_id: str) -> None:
        if task_id and await settled_io(has_canceled, task_id):
            raise TaskCanceledException(f"Task {task_id} was cancelled during NER extraction")

    async def __call__(
        self,
        doc_id: str,
        chunks: list[str],
        callback: Callable[..., Any] | None = None,
        task_id: str = "",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        # Local inference failures must propagate without publishing a partial graph.
        self.callback = callback
        nodes: dict[str, list[dict[str, Any]]] = defaultdict(list)
        edges: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for i, content in enumerate(chunks):
            await self._check_canceled(task_id)
            mnodes, medges, tokens = await settled_io(self._extract_chunk, content, doc_id)
            await self._check_canceled(task_id)
            for name, values in mnodes.items():
                nodes[name].extend(values)
            for pair, values in medges.items():
                edges[pair].extend(values)
            if callback:
                callback(msg=f"[spacy] Chunk {i + 1}/{len(chunks)}: {len(mnodes)} nodes, {len(medges)} edges, {tokens} tokens")
        entities: list[dict[str, Any]] = []
        relationships: list[dict[str, Any]] = []
        for name, records in nodes.items():
            await self._check_canceled(task_id)
            await self._merge_nodes(name, records, entities, task_id)
        for (a, b), records in edges.items():
            await self._check_canceled(task_id)
            await self._merge_edges(a, b, records, relationships, task_id)
        await self._check_canceled(task_id)
        return entities, relationships
