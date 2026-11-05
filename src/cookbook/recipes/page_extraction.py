"""Extract bounded, non-recipe-specific metadata from fetched HTML."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .ingest import FetchedPage


_HIDDEN_TAGS = {"head", "script", "style", "noscript", "template", "svg", "iframe"}
_COOKIE_BANNER = re.compile(
    r"(?:cookie|consent|privacy)[\w -]{0,32}(?:banner|notice|dialog|prompt|popup)"
    r"|(?:banner|notice|dialog|prompt|popup)[\w -]{0,32}(?:cookie|consent|privacy)",
    re.IGNORECASE,
)
_BLOCK_TAGS = {
    "address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "li", "main", "ol", "p", "pre", "section", "table", "td", "th",
    "tr", "ul",
}


def _clean_text(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


def _absolute_http_url(value: str | None, base_url: str) -> str | None:
    if not value:
        return None
    absolute = urljoin(base_url, value.strip())
    if urlsplit(absolute).scheme.casefold() not in {"http", "https"}:
        return None
    return absolute


class _PageParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self.visible_parts: list[str] = []
        self.json_ld_blocks: list[str] = []
        self._description: str | None = None
        self._og_description: str | None = None
        self._og_title: str | None = None
        self._og_site_name: str | None = None
        self._canonical: str | None = None
        self._og_image: str | None = None
        self._in_title = False
        self._script_is_json_ld = False
        self._script_parts: list[str] = []
        self._hidden: list[str] = []

    def _finish_json_ld(self) -> None:
        value = "".join(self._script_parts).strip()
        if self._script_is_json_ld and value:
            self.json_ld_blocks.append(value)
        self._script_is_json_ld = False
        self._script_parts = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        values = {key.casefold(): value for key, value in attrs if value is not None}
        if tag == "meta":
            key = (values.get("name") or values.get("property") or "").strip().casefold()
            content = values.get("content", "").strip()
            if key == "description" and content:
                self._description = self._description or content
            elif key == "og:description" and content:
                self._og_description = self._og_description or content
            elif key == "og:title" and content:
                self._og_title = self._og_title or content
            elif key == "og:site_name" and content:
                self._og_site_name = self._og_site_name or content
            elif key == "og:image" and content:
                self._og_image = self._og_image or content
        elif tag == "link":
            rel = {part.casefold() for part in values.get("rel", "").split()}
            if "canonical" in rel and values.get("href"):
                self._canonical = self._canonical or values["href"]
        elif tag == "title":
            self._in_title = True
        elif tag == "script":
            script_type = values.get("type", "").split(";", 1)[0].strip().casefold()
            self._script_is_json_ld = script_type == "application/ld+json"
            self._script_parts = []

        selectors = f"{values.get('id', '')} {values.get('class', '')}"
        hidden = (
            tag in _HIDDEN_TAGS
            or tag == "nav"
            or values.get("role", "").casefold() == "navigation"
            or _COOKIE_BANNER.search(selectors) is not None
            or values.get("hidden") is not None
            or values.get("aria-hidden", "").casefold() == "true"
        )
        if hidden:
            self._hidden.append(tag)
        if tag in _BLOCK_TAGS and not self._hidden:
            self.visible_parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "title":
            self._in_title = False
        if tag == "script":
            self._finish_json_ld()
        if tag in self._hidden:
            reverse_index = self._hidden[::-1].index(tag)
            del self._hidden[len(self._hidden) - reverse_index - 1 :]
        if tag in _BLOCK_TAGS and not self._hidden:
            self.visible_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._script_is_json_ld:
            self._script_parts.append(data)
        if not self._hidden and not self._in_title:
            self.visible_parts.append(data)

    def finish(self) -> None:
        if self._script_is_json_ld:
            self._finish_json_ld()

    def make_page(
        self,
        *,
        source_url: str,
        final_url: str,
        status_code: int,
        content_type: str,
        html: str,
    ) -> FetchedPage:
        self.finish()
        title = _clean_text(self.title_parts) or (self._og_title or "").strip()
        description = self._description or self._og_description
        return FetchedPage(
            source_url=source_url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            html=html,
            title=title,
            site_name=(self._og_site_name or "").strip() or None,
            description=_clean_text([description]) if description else None,
            canonical_url=_absolute_http_url(self._canonical, self.base_url),
            og_image=_absolute_http_url(self._og_image, self.base_url),
            visible_text=_clean_text(self.visible_parts),
            json_ld=tuple(self.json_ld_blocks),
        )


def extract_page(
    html: str,
    *,
    source_url: str,
    final_url: str,
    status_code: int,
    content_type: str,
) -> FetchedPage:
    """Extract basic HTML metadata and content without interpreting recipe schemas."""
    parser = _PageParser(final_url)
    parser.feed(html)
    parser.close()
    return parser.make_page(
        source_url=source_url,
        final_url=final_url,
        status_code=status_code,
        content_type=content_type,
        html=html,
    )
