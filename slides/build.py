#!/usr/bin/env python3
"""Build the slide deck from the TL;DR sections of docs/*.md.

    python slides/build.py            # writes slides/dist/index.html

Each note contributes its "## TL;DR" section and nothing else. Inside a TL;DR,
an HTML comment <!-- slide --> starts a new slide (used for a code sample, a
table or a diagram). Fenced ```mermaid blocks are rendered by mermaid.js.
The deck is reveal.js loaded from a CDN: no Node toolchain, no build step for
the browser, the output is one static HTML file for GitHub Pages.

Standard library only.
"""

import html
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
TEMPLATE = ROOT / "slides" / "template.html"
OUTPUT = ROOT / "slides" / "dist" / "index.html"

# Deck order: the topics, then the version differences, and the audit
# checklist as the final slide.
ORDER = [
    "01-which-errors-are-retried.md",
    "02-attempts-vs-retries.md",
    "03-integrity-error-never-retried.md",
    "04-between-attempts.md",
    "05-uploaded-files.md",
    "06-backoff.md",
    "07-logging-and-diagnosis.md",
    "08-where-retry-does-not-apply.md",
    "09-reducing-contention.md",
    "00-version-diff.md",
    "10-write-path-audit.md",
]

TITLE = "Odoo 19 concurrency retries"
SUBTITLE = "What Odoo re-runs on a conflict, and what that breaks"
SLIDE_BREAK = "<!-- slide -->"


def read_note(path):
    """Return (title, [slide markdown, ...]) for one note."""
    text = path.read_text(encoding="utf-8")
    title_match = re.search(r"^# (.+)$", text, flags=re.MULTILINE)
    if not title_match:
        raise SystemExit(f"{path.name}: no '# ' title")
    tldr_match = re.search(r"^## TL;DR\s*$(.*?)(?=^## |\Z)", text, flags=re.MULTILINE | re.DOTALL)
    if not tldr_match:
        raise SystemExit(f"{path.name}: no '## TL;DR' section")
    # "01 - Which errors are retried" -> "Which errors are retried": the
    # footer of each slide already names the note.
    title = re.sub(r"^\d+\s*-\s*", "", title_match.group(1).strip())
    parts = [part.strip() for part in tldr_match.group(1).split(SLIDE_BREAK)]
    return title, [part for part in parts if part]


def mermaid_to_html(markdown):
    """Replace ```mermaid fences by raw <div class="mermaid"> blocks.

    A <div> HTML block ends at the first blank line, so blank lines are
    dropped from the diagram source.
    """

    def replace(match):
        source = "\n".join(line for line in match.group(1).splitlines() if line.strip())
        return f'<div class="mermaid">{html.escape(source)}</div>'

    return re.sub(r"```mermaid\n(.*?)```", replace, markdown, flags=re.DOTALL)


def markdown_section(markdown, title, source, extra_class=""):
    """A reveal.js section rendered by its markdown plugin.

    The browser decodes character references inside a <textarea>, so the
    whole markdown is escaped once to reach the plugin unchanged. Mermaid
    sources were escaped once more by mermaid_to_html(): they stay text
    inside their raw HTML block, and mermaid decodes them itself.
    """
    body = html.escape(f"## {title}\n\n{mermaid_to_html(markdown)}", quote=False)
    classes = f' class="{extra_class}"' if extra_class else ""
    return (
        f'<section data-markdown data-source="{html.escape(source)}"{classes}>\n'
        f"<textarea data-template>\n{body}\n</textarea>\n</section>"
    )


def build():
    slides = []
    for name in ORDER:
        title, parts = read_note(DOCS / name)
        source = f"docs/{name}"
        for index, part in enumerate(parts):
            heading = title if index == 0 else f"{title} <span class=\"cont\">(cont.)</span>"
            css = "final" if name.startswith("10-") else ""
            slides.append(markdown_section(part, heading, source, css))
    template = TEMPLATE.read_text(encoding="utf-8")
    page = (
        template.replace("{{TITLE}}", html.escape(TITLE))
        .replace("{{SUBTITLE}}", html.escape(SUBTITLE))
        .replace("{{BUILT}}", date.today().isoformat())
        .replace("{{SLIDES}}", "\n\n".join(slides))
    )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(page, encoding="utf-8")
    print(f"{OUTPUT.relative_to(ROOT)}: {len(slides) + 1} slides (title included)")


if __name__ == "__main__":
    build()
