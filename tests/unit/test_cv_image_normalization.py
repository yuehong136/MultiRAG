from core.llm.cv import Base


def test_image_prompt_normalizes_bytes_to_data_url():
    image_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"

    prompt = Base()._image_prompt("describe", [image_bytes])

    assert prompt[0] == {"type": "text", "text": "describe"}
    assert prompt[1]["type"] == "image_url"
    assert prompt[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "b'\\x89PNG" not in prompt[1]["image_url"]["url"]
