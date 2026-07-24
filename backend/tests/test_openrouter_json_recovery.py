from backend.app.services.llm import OpenRouterClient


def test_salvages_items_from_malformed_openrouter_json() -> None:
    client = object.__new__(OpenRouterClient)
    content = """
    {
      "items": [
        {"ingredient_name": "Citric Acid", "normalized_name": "citric acid", "price_per_unit": 10.5},
        {"ingredient_name": "Nicotinamide", "normalized_name": "nicotinamide", "price_per_unit": 99.02},
    """

    payload = client._parse_json_response(content)

    assert len(payload["items"]) == 2
    assert payload["items"][1]["normalized_name"] == "nicotinamide"


def test_repairs_trailing_commas_in_openrouter_json() -> None:
    client = object.__new__(OpenRouterClient)
    content = '{"items":[{"ingredient_name":"Glycine","price_per_unit":3.88,},],}'

    payload = client._parse_json_response(content)

    assert payload["items"][0]["price_per_unit"] == 3.88
