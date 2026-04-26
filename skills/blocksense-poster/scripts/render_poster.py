#!/usr/bin/env python3
"""render_poster.py — Render a community poster HTML from highlights + LLM text.

Usage:
    python3 scripts/render_poster.py \
        --highlights <highlights.json> \
        --headline "Your Neighborhood at a Glance" \
        --summary "Brief balanced description." \
        [--template offline|horizontal|analytical] \
        [--output poster.html] \
        [--demo]
"""

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from jinja2 import Environment, FileSystemLoader, TemplateNotFound
except ImportError:
    print(
        "Error: jinja2 is not installed. Run: bash bootstrap.sh",
        file=sys.stderr,
    )
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
TEMPLATES_DIR = PROJECT_DIR / "templates"
DEMO_CACHE_DIR = SCRIPT_DIR / "demo_cache"

TEMPLATE_MAP = {
    "offline": "poster_offline.html",
    "card": "poster.html",
    "horizontal": "horizontal.html",
    "analytical": "analytical.html",
}

EMOJI_MAP = {
    "shield": "\U0001f6e1\ufe0f",
    "bus": "\U0001f68c",
    "tree": "\U0001f333",
    "building": "\U0001f3e2",
}


def load_highlights(path):
    """Load and validate the highlights JSON file."""
    p = Path(path)
    if not p.exists():
        print(f"Error: highlights file not found: {p}", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in {p}: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"Error: highlights JSON must be an object, got {type(data).__name__}", file=sys.stderr)
        sys.exit(1)
    return data


def render(highlights, headline, summary, template_name):
    """Render the poster HTML from highlights, headline, summary, and template choice."""
    template_file = TEMPLATE_MAP.get(template_name)
    if template_file is None:
        print(
            f"Error: unknown template '{template_name}'. "
            f"Choose from: {', '.join(TEMPLATE_MAP.keys())}",
            file=sys.stderr,
        )
        sys.exit(1)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )

    # Register emoji filter for templates
    def emoji_icon(icon_name):
        return EMOJI_MAP.get(icon_name, "")
    env.filters["emoji"] = emoji_icon

    try:
        template = env.get_template(template_file)
    except TemplateNotFound:
        print(
            f"Error: template file not found: {TEMPLATES_DIR / template_file}",
            file=sys.stderr,
        )
        sys.exit(1)

    context = {**highlights, "headline": headline, "summary": summary}
    return template.render(**context)


def main():
    parser = argparse.ArgumentParser(
        description="Render a community poster HTML from highlights + LLM text."
    )
    parser.add_argument(
        "--highlights",
        required=True,
        help="Path to the highlights JSON file (output of extract_highlights.py)",
    )
    parser.add_argument(
        "--headline",
        required=True,
        help="LLM-generated poster headline (<15 words)",
    )
    parser.add_argument(
        "--summary",
        required=True,
        help="LLM-generated poster summary (<50 words)",
    )
    parser.add_argument(
        "--template",
        choices=list(TEMPLATE_MAP.keys()),
        default="offline",
        help="Template choice: offline (default), horizontal, analytical",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output path for the poster HTML (default: stdout)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Check demo_cache/ for pre-computed results",
    )
    args = parser.parse_args()

    input_basename = os.path.basename(args.highlights)

    # Demo mode: check cache first
    if args.demo:
        cache_file = DEMO_CACHE_DIR / f"{input_basename}.poster.html"
        if cache_file.exists():
            cached_html = cache_file.read_text(encoding="utf-8")
            if args.output:
                Path(args.output).write_text(cached_html, encoding="utf-8")
                print(f"Demo cache hit: {cache_file}", file=sys.stderr)
            else:
                print(cached_html)
            return

    highlights = load_highlights(args.highlights)
    html = render(highlights, args.headline, args.summary, args.template)

    if args.output:
        Path(args.output).write_text(html, encoding="utf-8")
        print(f"Poster written to {args.output}", file=sys.stderr)
    else:
        print(html)


if __name__ == "__main__":
    main()
