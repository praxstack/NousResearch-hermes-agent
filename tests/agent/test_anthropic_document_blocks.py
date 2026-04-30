"""Tests for Anthropic document/PDF content block conversion.

The ``_convert_content_part_to_anthropic`` helper accepts both native
Anthropic document blocks and OpenAI-style convenience shapes, and
normalizes them into the SDK-expected
  {"type": "document", "source": {...}, "title"?, "context"?, "citations"?}
shape. These tests pin each input form.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from agent.anthropic_adapter import (
    _convert_content_part_to_anthropic,
    _document_source_from_part,
    convert_messages_to_anthropic,
)


class TestDocumentSourceFromPart:
    """The low-level source-builder accepts every documented shape."""

    def test_passthrough_native_base64_source(self):
        part = {
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": "QUJD",
            }
        }
        src = _document_source_from_part(part)
        assert src == {"type": "base64", "media_type": "application/pdf", "data": "QUJD"}

    def test_passthrough_native_url_source(self):
        part = {"source": {"type": "url", "url": "https://example.com/a.pdf"}}
        assert _document_source_from_part(part) == {
            "type": "url",
            "url": "https://example.com/a.pdf",
        }

    def test_passthrough_native_text_source(self):
        part = {"source": {"type": "text", "media_type": "text/plain", "data": "hi"}}
        src = _document_source_from_part(part)
        assert src == {"type": "text", "media_type": "text/plain", "data": "hi"}

    def test_shorthand_url_string(self):
        part = {"document": "https://example.com/paper.pdf"}
        src = _document_source_from_part(part)
        assert src == {"type": "url", "url": "https://example.com/paper.pdf"}

    def test_shorthand_data_url_pdf(self):
        part = {"document": "data:application/pdf;base64,QUJD"}
        src = _document_source_from_part(part)
        assert src == {"type": "base64", "media_type": "application/pdf", "data": "QUJD"}

    def test_shorthand_document_url_key(self):
        part = {"document_url": "https://x.com/doc.pdf"}
        src = _document_source_from_part(part)
        assert src == {"type": "url", "url": "https://x.com/doc.pdf"}

    def test_shorthand_dict_url(self):
        part = {"document": {"url": "https://example.com/p.pdf"}}
        src = _document_source_from_part(part)
        assert src == {"type": "url", "url": "https://example.com/p.pdf"}

    def test_shorthand_dict_base64_with_default_media_type(self):
        part = {"document": {"base64": "QUJD"}}
        src = _document_source_from_part(part)
        assert src == {"type": "base64", "media_type": "application/pdf", "data": "QUJD"}

    def test_shorthand_dict_base64_with_explicit_media_type(self):
        part = {"document": {"base64": "QUJD", "media_type": "application/xml"}}
        src = _document_source_from_part(part)
        assert src == {"type": "base64", "media_type": "application/xml", "data": "QUJD"}

    def test_shorthand_dict_data_plus_media_type_text(self):
        part = {"document": {"data": "hello", "media_type": "text/plain"}}
        src = _document_source_from_part(part)
        assert src == {"type": "text", "media_type": "text/plain", "data": "hello"}

    def test_shorthand_dict_data_plus_media_type_pdf_binary(self):
        part = {"document": {"data": "QUJD", "media_type": "application/pdf"}}
        src = _document_source_from_part(part)
        assert src == {"type": "base64", "media_type": "application/pdf", "data": "QUJD"}

    def test_shorthand_dict_text(self):
        part = {"document": {"text": "some content"}}
        src = _document_source_from_part(part)
        assert src == {"type": "text", "media_type": "text/plain", "data": "some content"}

    def test_shorthand_dict_file_path_reads_and_encodes(self, tmp_path: Path):
        pdf_bytes = b"%PDF-1.4 fake"
        pdf_file = tmp_path / "doc.pdf"
        pdf_file.write_bytes(pdf_bytes)

        part = {"document": {"file_path": str(pdf_file)}}
        src = _document_source_from_part(part)
        assert src["type"] == "base64"
        assert src["media_type"] == "application/pdf"
        assert src["data"] == base64.standard_b64encode(pdf_bytes).decode("ascii")

    def test_shorthand_dict_path_alias(self, tmp_path: Path):
        pdf_file = tmp_path / "doc.pdf"
        pdf_file.write_bytes(b"hi")
        part = {"document": {"path": str(pdf_file)}}
        src = _document_source_from_part(part)
        assert src["data"] == base64.standard_b64encode(b"hi").decode("ascii")

    def test_empty_string_returns_none(self):
        assert _document_source_from_part({"document": "   "}) is None

    def test_missing_document_key_returns_none(self):
        assert _document_source_from_part({}) is None
        assert _document_source_from_part({"type": "text", "text": "hi"}) is None


class TestConvertContentPart:
    """High-level converter produces correct Anthropic block envelopes."""

    def test_native_document_type_passthrough(self):
        part = {
            "type": "document",
            "source": {"type": "url", "url": "https://x/doc.pdf"},
            "title": "Paper",
            "context": "Published 2026",
            "citations": {"enabled": True},
        }
        block = _convert_content_part_to_anthropic(part)
        assert block == {
            "type": "document",
            "source": {"type": "url", "url": "https://x/doc.pdf"},
            "title": "Paper",
            "context": "Published 2026",
            "citations": {"enabled": True},
        }

    def test_pdf_alias_type(self):
        part = {"type": "pdf", "document_url": "https://x/p.pdf", "title": "P"}
        block = _convert_content_part_to_anthropic(part)
        assert block == {
            "type": "document",
            "source": {"type": "url", "url": "https://x/p.pdf"},
            "title": "P",
        }

    def test_file_alias_type(self):
        part = {"type": "file", "document": {"base64": "QUJD"}}
        block = _convert_content_part_to_anthropic(part)
        assert block["type"] == "document"
        assert block["source"]["type"] == "base64"
        assert block["source"]["data"] == "QUJD"

    def test_input_file_alias_type(self):
        part = {"type": "input_file", "document": "https://x/y.pdf"}
        block = _convert_content_part_to_anthropic(part)
        assert block == {
            "type": "document",
            "source": {"type": "url", "url": "https://x/y.pdf"},
        }

    def test_document_url_alias_type(self):
        part = {"type": "document_url", "document_url": "https://x/z.pdf"}
        block = _convert_content_part_to_anthropic(part)
        assert block["source"]["url"] == "https://x/z.pdf"

    def test_cache_control_preserved_on_document_block(self):
        part = {
            "type": "document",
            "source": {"type": "url", "url": "https://x/doc.pdf"},
            "cache_control": {"type": "ephemeral"},
        }
        block = _convert_content_part_to_anthropic(part)
        assert block["cache_control"] == {"type": "ephemeral"}

    def test_alias_returns_none_when_no_doc_reference(self):
        # type="pdf" without any document / document_url / source is a
        # malformed message; return None so callers can filter it out
        # rather than send a half-built block to the API.
        part = {"type": "pdf"}
        assert _convert_content_part_to_anthropic(part) is None

    def test_unknown_type_still_passes_through(self):
        # Guards regression: we didn't accidentally swallow non-document
        # block types. A custom block type should still round-trip.
        part = {"type": "unknown_custom_block", "foo": "bar"}
        block = _convert_content_part_to_anthropic(part)
        assert block == {"type": "unknown_custom_block", "foo": "bar"}


class TestConvertMessagesWithDocument:
    """End-to-end: a user message carrying a PDF converts cleanly."""

    def test_message_with_pdf_attachment(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "pdf", "document_url": "https://arxiv.org/pdf/2401.00001"},
                    {"type": "text", "text": "Summarize section 3."},
                ],
            }
        ]
        _system, ant_msgs = convert_messages_to_anthropic(messages)
        assert len(ant_msgs) == 1
        content = ant_msgs[0]["content"]
        assert content[0]["type"] == "document"
        assert content[0]["source"] == {
            "type": "url",
            "url": "https://arxiv.org/pdf/2401.00001",
        }
        assert content[1]["type"] == "text"
        assert content[1]["text"] == "Summarize section 3."

    def test_message_with_pdf_and_image_together(self):
        """PDF + image + text in one message should all convert."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "pdf", "document": "https://x.com/a.pdf", "title": "A"},
                    {"type": "image_url", "image_url": {"url": "https://x.com/i.png"}},
                    {"type": "text", "text": "Compare both."},
                ],
            }
        ]
        _system, ant_msgs = convert_messages_to_anthropic(messages)
        content = ant_msgs[0]["content"]
        types = [b["type"] for b in content]
        assert types == ["document", "image", "text"]
        assert content[0]["title"] == "A"
