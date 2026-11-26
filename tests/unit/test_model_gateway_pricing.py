"""Prices from LiteLLM's cost map and the arithmetic applied to a call's usage."""

from decimal import Decimal

import pytest

from api.model_gateway.pricing import CUSTOM, LITELLM, PriceTable, custom_price, default_table, price_from_entry, resolve_price

D = Decimal
TABLE = PriceTable(
    {
        "sample_spec": {"input_cost_per_token": 0, "litellm_provider": "one of ..."},
        "gpt-4o": {
            "litellm_provider": "openai",
            "input_cost_per_token": 2.5e-06,
            "output_cost_per_token": 1e-05,
            "cache_read_input_token_cost": 1.25e-06,
            "max_input_tokens": 128000,
            "max_output_tokens": 16384,
        },
        "dashscope/qwen-plus": {"litellm_provider": "dashscope", "input_cost_per_token": 4e-07, "output_cost_per_token": 1.2e-06, "max_input_tokens": 129024},
        "gemini/gemini-2.5-pro": {
            "litellm_provider": "gemini",
            "input_cost_per_token": 1.25e-06,
            "output_cost_per_token": 1e-05,
            "cache_read_input_token_cost": 1.25e-07,
            "input_cost_per_token_above_200k_tokens": 2.5e-06,
            "output_cost_per_token_above_200k_tokens": 1.5e-05,
        },
        "dashscope/qwen-flash": {
            "litellm_provider": "dashscope",
            "tiered_pricing": [
                {"input_cost_per_token": 5e-08, "output_cost_per_token": 4e-07, "range": [0, 256000.0]},
                {"input_cost_per_token": 2.5e-07, "output_cost_per_token": 2e-06, "range": [256000.0, 1000000.0]},
            ],
        },
        "azure/gpt-4o": {"litellm_provider": "azure", "input_cost_per_token": 9e-06},
        "dall-e-3": {"litellm_provider": "openai", "output_cost_per_image": 0.04},
    }
)


def test_lookup_goes_by_provider_and_upstream_model_name():
    assert TABLE.lookup("OpenAI", "gpt-4o").base.input == D("0.0000025")
    assert TABLE.lookup("OpenAI", "GPT-4o").max_input_tokens == 128000
    assert TABLE.lookup("Tongyi-Qianwen", "qwen-plus").base.output == D("0.0000012")
    # A bare entry belongs to the provider it names; another provider's model of the same name has no price.
    assert TABLE.lookup("Tongyi-Qianwen", "gpt-4o") is None
    # Self-hosted servers and unknown models have no listed price.
    assert TABLE.lookup("VLLM", "gpt-4o") is None
    assert TABLE.lookup("OpenAI", "gpt-unknown") is None
    assert TABLE.lookup("OpenAI", "") is None
    # An entry without a per-token input rate (an image model) is not a token price.
    assert TABLE.lookup("OpenAI", "dall-e-3") is None


def test_cached_prompt_tokens_are_charged_at_the_cache_rate_and_taken_out_of_the_prompt():
    price = TABLE.lookup("OpenAI", "gpt-4o")

    assert price.cost(1000, 500) == D("0.0025") + D("0.005")
    assert price.cost(1000, 500, 400) == 600 * D("0.0000025") + 400 * D("0.00000125") + 500 * D("0.00001")
    # More cached tokens than prompt tokens cannot make the prompt negative.
    assert price.cost(100, 0, 500) == 100 * D("0.00000125")
    # Without a cache rate cached tokens cost what any prompt token costs.
    assert TABLE.lookup("Tongyi-Qianwen", "qwen-plus").cost(1000, 0, 400) == 1000 * D("0.0000004")


def test_unknown_usage_has_no_cost_and_partial_usage_counts_what_is_known():
    price = TABLE.lookup("OpenAI", "gpt-4o")

    assert price.cost(None, None) is None
    assert price.cost(2048, None) == 2048 * D("0.0000025")
    assert price.cost(0, 0) == 0


def test_a_prompt_above_a_threshold_uses_that_tiers_rates_for_the_whole_call():
    price = TABLE.lookup("Gemini", "gemini-2.5-pro")

    assert price.cost(200_000, 10) == 200_000 * D("0.00000125") + 10 * D("0.00001")
    assert price.cost(200_001, 10) == 200_001 * D("0.0000025") + 10 * D("0.000015")
    # The tier has no cache rate of its own, so the base one applies.
    assert price.rates_for(300_000).cache_read == D("0.000000125")


def test_tiered_pricing_ranges_pick_one_tier_by_prompt_size():
    price = TABLE.lookup("Tongyi-Qianwen", "qwen-flash")

    assert price.cost(256_000, 100) == 256_000 * D("0.00000005") + 100 * D("0.0000004")
    assert price.cost(256_001, 100) == 256_001 * D("0.00000025") + 100 * D("0.000002")
    # Beyond the last range the last tier applies.
    assert price.rates_for(5_000_000).input == D("0.00000025")


def test_an_operators_price_replaces_the_listed_one_and_keeps_the_context_limits():
    custom = custom_price(D("0.000001"), D("0.000002"), None)
    priced = resolve_price("OpenAI", "gpt-4o", custom, TABLE)

    assert (priced.source, priced.base.input, priced.max_input_tokens) == (CUSTOM, D("0.000001"), 128000)
    assert priced.cost(10, 10, 5) == D("0.00003")
    assert resolve_price("VLLM", "qwen3", custom, TABLE).source == CUSTOM
    assert resolve_price("OpenAI", "gpt-4o", None, TABLE).source == LITELLM
    assert resolve_price("VLLM", "qwen3", None, TABLE) is None
    # A price needs an input rate; an output rate alone is not one.
    assert custom_price(None, D("0.000002"), None) is None


@pytest.mark.parametrize("value", [True, "abc", -1, float("nan"), None, []])
def test_malformed_rates_are_not_prices(value):
    assert price_from_entry({"input_cost_per_token": value}) is None


def test_the_cost_map_bundled_with_litellm_is_read_without_importing_it():
    import sys

    already_imported = "litellm" in sys.modules
    table = default_table()

    assert len(table) > 1000
    assert ("litellm" in sys.modules) == already_imported
    assert table.lookup("OpenAI", "gpt-4o").base.input > 0
    assert table.lookup("DeepSeek", "deepseek-chat").base.cache_read is not None
    assert table.lookup("Tongyi-Qianwen", "qwen-plus") is not None
    assert table.lookup("Moonshot", "moonshot-v1-8k") is not None
