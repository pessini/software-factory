import pytest
from acp.schema import TextContentBlock

from software_factory.bb.acp_input import request_text


def test_bb_wrapped_request_excludes_transport_instructions():
    text = '<system_instructions>\nBB context\n</system_instructions>\n{"factory_worker":1}'
    assert request_text([TextContentBlock(type="text", text=text)]) == '{"factory_worker":1}'


def test_plain_request_and_embedded_markup_are_preserved():
    text = "Implement a parser for <system_instructions> tags."
    assert request_text([TextContentBlock(type="text", text=text)]) == text


def test_incomplete_wrapper_cannot_become_a_request():
    with pytest.raises(ValueError, match="incomplete"):
        request_text([TextContentBlock(type="text", text="<system_instructions>\ncontext")])
