"""Separate BB's ACP transport instructions from the submitted request."""

from typing import Any

from acp.schema import TextContentBlock


def request_text(prompt: list[Any]) -> str:
    text = "\n".join(block.text for block in prompt if isinstance(block, TextContentBlock)).strip()
    if text.startswith("<system_instructions>\n"):
        _, separator, text = text.partition("\n</system_instructions>\n")
        if not separator:
            raise ValueError("BB instruction wrapper is incomplete")
    return text.strip()
