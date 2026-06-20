# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Unit tests for the HTML→text JD cleaner."""

from tools.text import html_to_text


def test_html_to_text_strips_tags_and_unescapes() -> None:
    html = "<p>Hello&amp;world</p><ul><li>One</li><li>Two</li></ul>"
    out = html_to_text(html)
    assert "<" not in out and ">" not in out
    assert "Hello&world" in out  # entity unescaped
    assert "One" in out and "Two" in out


def test_html_to_text_br_becomes_newline() -> None:
    assert html_to_text("a<br>b<br/>c") == "a\nb\nc"


def test_html_to_text_empty() -> None:
    assert html_to_text("") == ""
