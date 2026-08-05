import re
from html.parser import HTMLParser


class _ReadableHTMLParser(HTMLParser):
    SKIPPED_TAGS = {'script', 'style', 'head', 'title', 'noscript', 'svg'}
    BLOCK_TAGS = {
        'address', 'article', 'aside', 'blockquote', 'div', 'dl', 'dt', 'dd',
        'fieldset', 'figcaption', 'figure', 'footer', 'form', 'h1', 'h2',
        'h3', 'h4', 'h5', 'h6', 'header', 'hr', 'li', 'main', 'nav', 'ol',
        'p', 'pre', 'section', 'table', 'tbody', 'thead', 'tfoot', 'tr', 'ul',
    }
    CELL_TAGS = {'td', 'th'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def _line_break(self):
        if self.parts and not self.parts[-1].endswith('\n'):
            self.parts.append('\n')

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        if tag in self.SKIPPED_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == 'br' or tag in self.BLOCK_TAGS:
            self._line_break()
        elif tag in self.CELL_TAGS and self.parts:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag in self.SKIPPED_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if not self.skip_depth and (tag in self.BLOCK_TAGS or tag in self.CELL_TAGS):
            self._line_break()

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)


def html_to_text(value):
    if not value:
        return ''
    parser = _ReadableHTMLParser()
    parser.feed(str(value))
    parser.close()
    text = ''.join(parser.parts).replace('\xa0', ' ').replace('\x00', '')
    lines = [re.sub(r'[ \t\f\v]+', ' ', line).strip() for line in text.splitlines()]
    result = []
    for line in lines:
        if line:
            result.append(line)
        elif result and result[-1] != '':
            result.append('')
    return '\n'.join(result).strip()
