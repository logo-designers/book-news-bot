from datetime import datetime, timedelta, timezone

from bookbot.feeds import discover_feed_links, html_to_text, parse_feed
from bookbot.filters import auto_tags, in_quiet_hours, is_fresh, passes, title_key
from bookbot.formatter import CAPTION_LIMIT, build_post, tg_len, truncate, visible_len

from .conftest import wordpress_rss


def test_wordpress_feed_parsed(feed_bytes):
    feed = parse_feed(feed_bytes, "https://example.ua/feed/")
    assert feed.title == "Тестове медіа"
    assert len(feed.entries) == 4
    first = feed.entries[0]
    assert first.title.startswith("Видавництво Старого Лева")
    # хвіст WordPress «Запис … спершу з'явився на …» прибрано
    assert "спершу" not in first.summary
    assert first.summary.startswith("Новинка вийде друком у грудні.")
    # картинка взята з content:encoded
    assert first.image_url == "https://example.ua/wp-content/uploads/cover.jpg"
    assert first.published and first.published.tzinfo is not None
    # media:content і «[…]» → «…»
    second = feed.entries[1]
    assert second.image_url == "https://example.ua/img/bbc.jpg"
    assert second.summary.endswith("дитячі книжки.")


def test_google_news_title_and_summary():
    xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>Google News</title>
    <item><title>Вийшла нова книжка Андруховича - Українська правда</title>
    <link>https://news.google.com/rss/articles/abc</link>
    <description>&lt;a href="x"&gt;Вийшла нова книжка Андруховича&lt;/a&gt;&amp;nbsp;&amp;nbsp;&lt;font&gt;Українська правда&lt;/font&gt;</description>
    <source url="https://pravda.com.ua">Українська правда</source></item></channel></rss>""".encode()
    e = parse_feed(xml, "https://news.google.com/rss").entries[0]
    assert e.title == "Вийшла нова книжка Андруховича"
    assert e.origin == "Українська правда"
    assert e.summary == ""  # дубль заголовка відкинуто


def test_atom_feed():
    xml = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"><title>Atom-медіа</title>
      <entry><title>Нова збірка поезій</title><link href="/posts/1"/><id>tag:1</id>
      <updated>2026-11-18T10:00:00Z</updated><summary>Коротко про збірку.</summary></entry>
    </feed>""".encode()
    feed = parse_feed(xml, "https://atom.example/")
    assert feed.entries[0].link == "https://atom.example/posts/1"
    assert feed.entries[0].published == datetime(2026, 11, 18, 10, tzinfo=timezone.utc)


def test_html_page_is_not_feed_and_autodiscovery():
    page = """<html><head>
      <link rel="alternate" type="application/rss+xml" title="RSS" href="/feed/">
      <link rel="alternate" type="application/rss+xml" title="Comments" href="/comments/feed/">
    </head><body>Hi</body></html>"""
    parsed = parse_feed(page.encode(), "https://site.ua/")
    assert not parsed.entries
    assert discover_feed_links(page, "https://site.ua/") == ["https://site.ua/feed/"]


def test_html_to_text_handles_entities_and_scripts():
    raw = "<p>Перший&nbsp;абзац &amp; ще</p><script>alert(1)</script><p>Другий</p>"
    assert html_to_text(raw) == "Перший абзац & ще\n\nДругий"


def test_filters():
    assert passes("Нова книжка", [], [])
    assert passes("Нова КНИЖКА", ["книжк"], [])
    assert not passes("Нова книжка", ["роман"], [])
    assert not passes("Нова книжка, промокод", ["книжк"], ["промокод"])
    # різні апострофи вважаються однаковими
    assert passes("Чорна п’ятниця", [], []) and not passes("Чорна п’ятниця", [], ["чорна п'ятниця"])


def test_auto_tags_and_title_key():
    rules = {"#новинки": ["новинк"], "#премії": ["премі"], "#переклади": ["переклад"]}
    assert auto_tags("Новинка: переклад роману", rules) == ["#новинки", "#переклади"]
    assert title_key("«Книга року BBC»: шортлист!") == title_key("Книга року BBC — шортлист")


def test_freshness_and_quiet_hours():
    now = datetime.now(timezone.utc)
    assert is_fresh(now - timedelta(days=1), now, 3)
    assert not is_fresh(now - timedelta(days=5), now, 3)
    assert is_fresh(None, now, 3)
    assert in_quiet_hours(23, (23, 8)) and in_quiet_hours(3, (23, 8)) and not in_quiet_hours(8, (23, 8))
    assert in_quiet_hours(14, (13, 15)) and not in_quiet_hours(15, (13, 15))
    assert not in_quiet_hours(3, None)


def test_truncate_prefers_sentence_end():
    text = "Перше речення тут. Друге речення трохи довше за перше. Третє речення."
    out = truncate(text, 60)
    assert out == "Перше речення тут. Друге речення трохи довше за перше."
    assert tg_len(truncate("слово " * 100, 50)) <= 50


def test_post_html_escaped_and_caption_fits():
    long_summary = ("Дуже довгий опис книжки з <тегами> & символами. " * 80).strip()
    post = build_post(
        title='Роман "<Тест>" & ко', link="https://example.ua/a?b=1&c=2", summary=long_summary,
        source_name="Медіа", image_url="https://example.ua/i.jpg", tags=["#новинки", "#новинки", "#Тест"],
        signature="@test_channel",
    )
    assert "&lt;Тест&gt;" in post.text and "&amp; ко" in post.text
    assert 'href="https://example.ua/a?b=1&amp;c=2"' in post.text
    assert visible_len(post.text) <= CAPTION_LIMIT
    assert post.text.count("#новинки") == 1
    assert post.text.rstrip().endswith("@test_channel")


def test_post_without_image_and_summary():
    post = build_post(title="Заголовок", link="https://x.ua/", summary="", source_name="Медіа",
                      origin="Українська правда")
    assert post.photo_url is None
    assert "Українська правда" in post.text
    assert post.text.startswith("📚 <b>Заголовок</b>\n\n👉")


def test_broken_html_does_not_crash():
    xml = wordpress_rss([{"title": "Тест <b>жирний", "link": "https://e.ua/1", "desc": "<p>незакритий <div><img src=x"}])
    feed = parse_feed(xml, "https://e.ua/")
    assert feed.entries[0].title  # не впали


def test_page_main_image():
    from bookbot.feeds import page_main_image
    page = """<html><head>
      <meta name="twitter:image" content="https://cdn.ua/tw.jpg">
      <meta property="og:image" content="/uploads/cover.jpg">
    </head></html>"""
    assert page_main_image(page, "https://site.ua/news/1") == "https://site.ua/uploads/cover.jpg"
    assert page_main_image('<meta property="og:image" content="/img/logo.png">', "https://s.ua/") is None
    assert page_main_image("<html></html>", "https://s.ua/") is None
