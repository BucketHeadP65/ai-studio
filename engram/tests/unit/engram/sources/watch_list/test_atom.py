from datetime import UTC, datetime
from xml.etree.ElementTree import ParseError

import pytest

from engram.sources.infrastructure.atom import parse_atom_or_rss

YOUTUBE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <title>Some Channel</title>
 <entry>
  <id>yt:video:abc123XYZ_-</id>
  <yt:videoId>abc123XYZ_-</yt:videoId>
  <title>Agents that plan</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=abc123XYZ_-"/>
  <published>2026-08-24T10:00:00+00:00</published>
  <media:group><media:description>A talk about planning.</media:description></media:group>
 </entry>
</feed>"""

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Blog</title>
 <item><title>On harnesses</title><link>https://blog.example.org/harnesses</link><pubDate>Sun, 24 Aug 2026 08:00:00 GMT</pubDate></item>
 <item><title>No date</title><link>https://blog.example.org/undated</link></item>
</channel></rss>"""


def test_youtube_atom_entries_carry_the_watch_url_title_and_date() -> None:
    entries = parse_atom_or_rss(YOUTUBE_ATOM)
    assert [e.url for e in entries] == ["https://www.youtube.com/watch?v=abc123XYZ_-"]
    assert entries[0].title == "Agents that plan"
    assert entries[0].published == datetime(2026, 8, 24, 10, 0, tzinfo=UTC)


def test_rss_items_parse_with_and_without_dates() -> None:
    entries = parse_atom_or_rss(RSS)
    assert [e.url for e in entries] == ["https://blog.example.org/harnesses", "https://blog.example.org/undated"]
    assert entries[0].published == datetime(2026, 8, 24, 8, 0, tzinfo=UTC)
    assert entries[1].published is None


def test_an_rss_pubdate_with_a_minus_zero_offset_is_utc_aware() -> None:
    # `email.utils.parsedate_to_datetime` reads `-0000` as "offset unknown" and answers naive.
    document = """<?xml version="1.0"?>
<rss version="2.0"><channel>
 <item><title>A post</title><link>https://blog.example/a</link><pubDate>Mon, 18 Aug 2026 10:00:00 -0000</pubDate></item>
</channel></rss>"""
    published = parse_atom_or_rss(document)[0].published
    assert published == datetime(2026, 8, 18, 10, 0, tzinfo=UTC)


def test_an_atom_timestamp_with_no_offset_is_read_as_utc() -> None:
    document = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <entry><title>A talk</title><link rel="alternate" href="https://videos.example/a"/><published>2026-08-18T10:00:00</published></entry>
</feed>"""
    published = parse_atom_or_rss(document)[0].published
    assert published == datetime(2026, 8, 18, 10, 0, tzinfo=UTC)


def test_garbage_is_an_empty_list_not_an_exception() -> None:
    assert parse_atom_or_rss("<html>not a document</html>") == []


def test_malformed_xml_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_atom_or_rss('<rss version="2.0"><channel>')
