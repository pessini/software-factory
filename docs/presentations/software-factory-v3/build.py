"""Translate the v2 presentation while preserving layout, assets, anchors, and code."""
from html import escape
from html.parser import HTMLParser
from pathlib import Path
import json
import re
from english import COPY

ROOT = Path(__file__).parent
SOURCE = ROOT.parent / 'software-factory-v2' / 'software-factory-v2.html'
source_text = json.loads((ROOT / 'source-text.json').read_text())
translations = {source_text[index]: value for index, value in COPY.items()}

class Translator(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.result = []
        self.raw = False
        self.in_h1 = False

    def start(self, tag, attrs):
        raw = self.get_starttag_text()
        for key, value in attrs:
            replacement = None
            if key == 'lang':
                replacement = 'en'
            elif key in ('alt', 'aria-label'):
                replacement = translations.get(value)
            elif tag == 'meta' and key == 'content' and dict(attrs).get('name') == 'description':
                replacement = 'Agentic Software Factory: Grilling and Wayfinder, the planning prototype, execution workflow, architecture, and pending integrations.'
            if replacement is not None:
                raw = raw.replace(f'{key}="{escape(value, quote=True)}"', f'{key}="{escape(replacement, quote=True)}"')
        self.result.append(raw)
        if tag in ('style', 'script'):
            self.raw = True
        if tag == 'h1':
            self.in_h1 = True

    handle_starttag = start
    handle_startendtag = start

    def handle_endtag(self, tag):
        self.result.append(f'</{tag}>')
        if tag in ('style', 'script'):
            self.raw = False
        if tag == 'h1':
            self.in_h1 = False

    def handle_data(self, data):
        if self.raw:
            self.result.append(data)
        elif self.in_h1 and data == 'Software':
            self.result.append('Agentic<br>Software')
        else:
            self.result.append(translations.get(data, data))

    def handle_entityref(self, name):
        self.result.append(f'&{name};')

    def handle_charref(self, name):
        self.result.append(f'&#{name};')

    def handle_decl(self, decl):
        self.result.append(f'<!{decl}>')

    def handle_comment(self, comment):
        self.result.append(f'<!--{comment}-->')

p = Translator()
p.feed(SOURCE.read_text())
document = ''.join(p.result)
(ROOT / 'agentic-software-factory-v3.html').write_text(document)

class Notes(HTMLParser):
    def __init__(self):
        super().__init__()
        self.heading = False
        self.note = False
        self.text = []
        self.current = None
        self.sections = []
    def handle_starttag(self, tag, attrs):
        if tag == 'h2': self.heading = True; self.text = []
        if tag == 'aside' and dict(attrs).get('class') == 'speaker-note': self.note = True; self.text = []
    def handle_endtag(self, tag):
        if tag == 'h2': self.current = ''.join(self.text); self.heading = False; self.text = []
        if tag == 'aside' and self.note:
            text = ''.join(self.text).removeprefix('Speaker notes')
            self.sections.append((self.current, text)); self.note = False; self.text = []
    def handle_data(self, data):
        if self.heading or self.note: self.text.append(data)

n = Notes(); n.feed(document)
notes = '# Speaker notes · Agentic Software Factory\n\nDeveloper team presentation. Suggested duration: 25–30 minutes, including discussion.\n\n'
for index, (title, text) in enumerate(n.sections, 1):
    notes += f'## {index:02}. {title}\n\n{text}\n\n'
(ROOT / 'speaker-notes.md').write_text(notes)
print(f'Built English presentation and {len(n.sections)} speaker notes.')
