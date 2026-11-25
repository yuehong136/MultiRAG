"""What a call costs, from LiteLLM's model cost map.

The gateway does not keep its own price list. It reads the cost map that
ships with the ``litellm`` dependency (the same file LiteLLM's proxy falls
back to), keeps LiteLLM's field names, and applies the same arithmetic as
``litellm.litellm_core_utils.llm_cost_calc.utils.generic_cost_per_token``
for text tokens: cached prompt tokens are charged at the cache-read rate and
taken out of the prompt, and a prompt above a ``_above_<n>k_tokens`` threshold
or inside a ``tiered_pricing`` range is charged at that tier's rates.

The file is read from disk; ``litellm`` itself is not imported, so the data
plane does not pay its import time. Prices are USD per token. A model
without a price costs ``None``, never zero.
"""

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from importlib.util import find_spec
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_COST_MAP_FILE = "model_prices_and_context_window_backup.json"
_ABOVE = re.compile(r"^input_cost_per_token_above_(\d+)k_tokens$")
LITELLM = "litellm"
CUSTOM = "custom"

# Registered provider name -> LiteLLM ``litellm_provider`` values whose prices
# apply. Self-hosted servers and resellers are absent: their price is whatever
# the operator sets on the model.
PROVIDER_SLUGS: dict[str, tuple[str, ...]] = {
    "OpenAI": ("openai", "text-completion-openai"),
    "Tongyi-Qianwen": ("dashscope",),
    "DeepSeek": ("deepseek",),
    "Moonshot": ("moonshot",),
    "ZHIPU-AI": ("zai",),
    "MiniMax": ("minimax",),
    "VolcEngine": ("volcengine",),
    "NovitaAI": ("novita",),
    "OpenRouter": ("openrouter",),
    "xAI": ("xai",),
    "Groq": ("groq",),
    "TogetherAI": ("together_ai",),
    "DeepInfra": ("deepinfra",),
    "Mistral": ("mistral",),
    "Anthropic": ("anthropic",),
    "Gemini": ("gemini",),
}


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        return None
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        return None
    return number if number.is_finite() and number >= 0 else None


@dataclass(frozen=True, slots=True)
class Rates:
    """USD per token for one price tier."""

    input: Decimal
    output: Decimal = Decimal(0)
    # ``None`` charges cached prompt tokens like any other prompt token.
    cache_read: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """The rates of one model and where they come from."""

    base: Rates
    source: str = LITELLM
    # ``(prompt tokens above, rates)``, ascending: LiteLLM's ``_above_<n>k_tokens`` keys.
    above: tuple[tuple[int, Rates], ...] = ()
    # ``(prompt tokens up to, rates)``, ascending: LiteLLM's ``tiered_pricing`` ranges.
    ranges: tuple[tuple[float, Rates], ...] = ()
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None

    def rates_for(self, prompt_tokens: int) -> Rates:
        if self.ranges:
            # ``lo < tokens <= hi``; a prompt beyond the last range uses the last tier.
            return next((rates for upper, rates in self.ranges if prompt_tokens <= upper), self.ranges[-1][1])
        chosen = self.base
        for threshold, rates in self.above:
            if prompt_tokens > threshold:
                chosen = rates
        return chosen

    def cost(self, prompt_tokens: int | None, completion_tokens: int | None, cache_read_tokens: int | None = None) -> Decimal | None:
        """The price of one call, or ``None`` when its usage is unknown."""
        if prompt_tokens is None and completion_tokens is None:
            return None
        prompt, completion = prompt_tokens or 0, completion_tokens or 0
        rates = self.rates_for(prompt)
        # OpenAI-shaped usage counts cached tokens inside the prompt.
        cached = min(cache_read_tokens or 0, prompt)
        cache_rate = rates.cache_read if rates.cache_read is not None else rates.input
        return (prompt - cached) * rates.input + cached * cache_rate + completion * rates.output


def custom_price(input_cost_per_token: Decimal | None, output_cost_per_token: Decimal | None, cache_read_input_token_cost: Decimal | None) -> ModelPrice | None:
    """An operator's price for a model; it needs at least the input rate."""
    if input_cost_per_token is None:
        return None
    return ModelPrice(base=Rates(input=input_cost_per_token, output=output_cost_per_token or Decimal(0), cache_read=cache_read_input_token_cost), source=CUSTOM)


def _rates(entry: Mapping[str, Any], suffix: str = "", fallback: Rates | None = None) -> Rates | None:
    cost = _decimal(entry.get(f"input_cost_per_token{suffix}"))
    if cost is None:
        return None
    output = _decimal(entry.get(f"output_cost_per_token{suffix}"))
    cache_read = _decimal(entry.get(f"cache_read_input_token_cost{suffix}"))
    return Rates(
        input=cost,
        output=output if output is not None else (fallback.output if fallback else Decimal(0)),
        cache_read=cache_read if cache_read is not None else (fallback.cache_read if fallback else None),
    )


def _limit(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def price_from_entry(entry: Mapping[str, Any]) -> ModelPrice | None:
    """One cost map entry as a price; ``None`` when it has no per-token input rate."""
    ranges: list[tuple[float, Rates]] = []
    for tier in entry.get("tiered_pricing") or []:
        bounds = tier.get("range") if isinstance(tier, dict) else None
        rates = _rates(tier) if isinstance(tier, dict) else None
        if rates is not None and isinstance(bounds, list) and len(bounds) == 2 and isinstance(bounds[1], int | float):
            ranges.append((float(bounds[1]), rates))
    base = _rates(entry) or (min(ranges, key=lambda item: item[0])[1] if ranges else None)
    if base is None:
        return None
    above = []
    for name in entry:
        match = _ABOVE.match(name)
        rates = _rates(entry, name.removeprefix("input_cost_per_token"), base) if match else None
        if match and rates is not None:
            above.append((int(match.group(1)) * 1000, rates))
    return ModelPrice(
        base=base,
        above=tuple(sorted(above, key=lambda item: item[0])),
        ranges=tuple(sorted(ranges, key=lambda item: item[0])),
        max_input_tokens=_limit(entry.get("max_input_tokens")) or _limit(entry.get("max_tokens")),
        max_output_tokens=_limit(entry.get("max_output_tokens")),
    )


class PriceTable:
    """LiteLLM's cost map, looked up by registered provider and upstream model name."""

    def __init__(self, entries: Mapping[str, Any]) -> None:
        self._entries = {name.lower(): entry for name, entry in entries.items() if isinstance(entry, dict)}

    def __len__(self) -> int:
        return len(self._entries)

    def lookup(self, provider: str, model_name: str) -> ModelPrice | None:
        slugs = PROVIDER_SLUGS.get(provider)
        if not slugs or not model_name:
            return None
        name = model_name.lower()
        for slug in slugs:
            entry = self._entries.get(f"{slug}/{name}")
            if entry is not None:
                return price_from_entry(entry)
        # OpenAI models are listed without a prefix; the entry names its provider.
        entry = self._entries.get(name)
        if entry is not None and entry.get("litellm_provider") in slugs:
            return price_from_entry(entry)
        return None


def cost_map_path() -> Path | None:
    spec = find_spec("litellm")
    locations = list(spec.submodule_search_locations or []) if spec else []
    path = Path(locations[0]) / _COST_MAP_FILE if locations else None
    return path if path is not None and path.is_file() else None


@lru_cache(maxsize=1)
def default_table() -> PriceTable:
    """The cost map bundled with the installed ``litellm``; empty when it cannot be read."""
    path = cost_map_path()
    if path is None:
        logger.warning("model gateway: LiteLLM cost map not found; calls are recorded without a cost unless a model has its own price")
        return PriceTable({})
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("model gateway: failed to read the LiteLLM cost map at %s", path)
        return PriceTable({})
    return PriceTable(entries if isinstance(entries, dict) else {})


def resolve_price(provider: str, model_name: str, custom: ModelPrice | None = None, table: PriceTable | None = None) -> ModelPrice | None:
    """The operator's price when there is one, else LiteLLM's."""
    if custom is not None:
        # Context limits are facts about the model, whoever prices it.
        listed = (table or default_table()).lookup(provider, model_name)
        if listed is None:
            return custom
        return ModelPrice(base=custom.base, source=CUSTOM, max_input_tokens=listed.max_input_tokens, max_output_tokens=listed.max_output_tokens)
    return (table or default_table()).lookup(provider, model_name)
