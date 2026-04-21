def my_extractor(texts: list[str]) -> str | None:
    for text in texts:
        if "[result]" in text:
            return text[text.find("[result]"):]
    return None
