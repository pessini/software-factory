"""Separate BB's ACP transport instructions from the submitted request."""

import re
from typing import Any

from acp.schema import TextContentBlock


def request_text(prompt: list[Any]) -> str:
    text = "\n".join(block.text for block in prompt if isinstance(block, TextContentBlock)).strip()
    if text.startswith("<system_instructions>\n"):
        _, separator, text = text.partition("\n</system_instructions>\n")
        if not separator:
            raise ValueError("BB instruction wrapper is incomplete")
    text = text.strip()
    # BB adds sender metadata to child follow-ups, including correction and
    # repair requests. Worker authorization still checks the saved parent/run.
    return re.sub(r"^\[bb message from thread:[A-Za-z0-9_-]+\]\n\n", "", text).strip()
