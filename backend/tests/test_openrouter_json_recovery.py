from backend.app.services.llm import EXTRACTION_CHUNK_CHARS, ModelProviderConfig, OpenRouterClient


def test_salvages_items_from_malformed_openrouter_json() -> None:
    client = object.__new__(OpenRouterClient)
    content = """
    {
      "items": [
        {"ingredient_name": "Citric Acid", "price_per_unit": 10.5},
        {"ingredient_name": "Nicotinamide", "price_per_unit": 99.02},
    """

    payload = client._parse_json_response(content)

    assert len(payload["items"]) == 2
    assert payload["items"][1]["ingredient_name"] == "Nicotinamide"


def test_repairs_trailing_commas_in_openrouter_json() -> None:
    client = object.__new__(OpenRouterClient)
    content = '{"items":[{"ingredient_name":"Glycine","price_per_unit":3.88,},],}'

    payload = client._parse_json_response(content)

    assert payload["items"][0]["price_per_unit"] == 3.88


def test_model_router_uses_groq_before_openrouter() -> None:
    client = object.__new__(OpenRouterClient)
    client.providers = [
        ModelProviderConfig("groq", "groq-key", "groq-model", "https://groq.test"),
        ModelProviderConfig("openrouter", "openrouter-key", "openrouter-model", "https://openrouter.test"),
    ]
    called = []

    def fake_chat_with_provider(provider, messages, *, temperature=0, json_mode=False):
        called.append(provider.name)
        return "primary response"

    client._chat_with_provider = fake_chat_with_provider

    assert client._chat([{"role": "user", "content": "hello"}]) == "primary response"
    assert called == ["groq"]


def test_model_router_falls_back_to_openrouter_after_groq_failure() -> None:
    client = object.__new__(OpenRouterClient)
    client.providers = [
        ModelProviderConfig("groq", "groq-key", "groq-model", "https://groq.test"),
        ModelProviderConfig("openrouter", "openrouter-key", "openrouter-model", "https://openrouter.test"),
    ]
    called = []

    def fake_chat_with_provider(provider, messages, *, temperature=0, json_mode=False):
        called.append(provider.name)
        if provider.name == "groq":
            raise RuntimeError("primary unavailable")
        return "secondary response"

    client._chat_with_provider = fake_chat_with_provider

    assert client._chat([{"role": "user", "content": "hello"}]) == "secondary response"
    assert called == ["groq", "openrouter"]


def test_extraction_chunks_are_sized_for_primary_groq_route() -> None:
    client = object.__new__(OpenRouterClient)
    text = "\n".join(f"Vitamin C row {index} USD 5/kg" for index in range(2000))

    chunks = client._chunk_text(text)

    assert EXTRACTION_CHUNK_CHARS == 12000
    assert len(chunks) > 1
    assert all(len(chunk) <= EXTRACTION_CHUNK_CHARS + 1000 for chunk in chunks)


def test_json_chat_falls_back_when_primary_returns_invalid_json() -> None:
    client = object.__new__(OpenRouterClient)
    client.providers = [
        ModelProviderConfig("groq", "groq-key", "groq-model", "https://groq.test"),
        ModelProviderConfig("openrouter", "openrouter-key", "openrouter-model", "https://openrouter.test"),
    ]

    def fake_chat_with_provider(provider, messages, *, temperature=0, json_mode=False):
        if provider.name == "groq":
            return "not json"
        return '{"items":[{"ingredient_name":"Citric Acid"}]}'

    client._chat_with_provider = fake_chat_with_provider

    payload = client._json_chat("Return JSON", "catalogue text")

    assert payload["items"][0]["ingredient_name"] == "Citric Acid"
