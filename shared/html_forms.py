"""Small HTML form helpers shared by the platforms that log in through a
server-rendered CSRF-protected form (Bienpreter, Afranga)."""

from html.parser import HTMLParser


class _HiddenInputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        if tag != "input":
            return
        attrs = dict(attrs)
        if (attrs.get("type") or "").lower() == "hidden" and attrs.get("name"):
            self.fields[attrs["name"]] = attrs.get("value") or ""


def extract_hidden_inputs(page_html: str) -> dict:
    """name -> value of every `<input type="hidden">` on the page, whatever
    the attribute order (a real HTML parser, not a regex on the markup, so an
    extra attribute such as `data-controller` does not break the lookup)."""
    parser = _HiddenInputParser()
    parser.feed(page_html)
    return parser.fields
