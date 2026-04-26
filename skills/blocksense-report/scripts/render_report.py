#!/usr/bin/env python3
"""render_report.py — Render BlockSense neighborhood analysis report.

Merges segments.json (from extract_segments.py) and narratives.json (from LLM
generation in Phase 2) into a self-contained HTML report and a Markdown report.

Usage:
    python3 scripts/render_report.py \\
        --segments <segments.json> \\
        --narratives <narratives.json> \\
        --template templates/report.html \\
        [--output-html report.html] \\
        [--output-md report.md] \\
        [--demo]

If --output-html is omitted, HTML is written to stdout.
If --output-md is omitted, Markdown generation is skipped.
If --demo is passed, checks scripts/demo_cache/ for a cached result first.

Exit codes:
    0 — success
    1 — error
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
except ImportError:
    print("Error: jinja2 is required. Run: pip install Jinja2", file=sys.stderr)
    sys.exit(1)


REPORT_VERSION = "v3.7.8"

DIMENSION_LABELS = {
    "safety": "Safety",
    "transit": "Transit",
    "amenities": "Amenities",
    "building": "Building",
}


def _fallback_narrative(segment):
    """Generate a data-only fallback narrative for a dimension missing LLM output."""
    dim = segment.get("dimension", "unknown")
    score = segment.get("score")
    metrics = segment.get("metrics", [])

    label = DIMENSION_LABELS.get(dim, dim.title())
    parts = []
    if score is not None:
        parts.append(f"{label} scores {score}/100")
    if metrics:
        metric_strs = []
        for m in metrics[:4]:
            s = f"{m.get('label', '?')}: {m.get('value', '?')} {m.get('unit', '')}".strip()
            if m.get("annotation"):
                s += f" ({m['annotation']})"
            metric_strs.append(s)
        parts.append("Key metrics: " + "; ".join(metric_strs))
    if not parts:
        return f"{label} data is unavailable for this location."
    return ". ".join(parts) + "."


def _score_color(score):
    """Return a CSS color class name based on score threshold."""
    if score is None:
        return "score-na"
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "score-na"
    if s >= 80:
        return "score-green"
    elif s >= 60:
        return "score-blue"
    elif s >= 40:
        return "score-yellow"
    else:
        return "score-red"


def _trend_arrow(direction):
    """Return a Unicode trend arrow and CSS class."""
    direction = (direction or "").lower()
    if direction in ("improving", "decreasing"):
        return "\u25bc", "trend-improving"  # ▼
    elif direction in ("worsening", "increasing"):
        return "\u25b2", "trend-worsening"  # ▲
    elif direction in ("elevated",):
        return "\u25b2", "trend-elevated"
    elif direction in ("stable",):
        return "\u2192", "trend-stable"     # →
    else:
        return "\u2192", "trend-stable"


def build_template_context(segments_data, narratives_data):
    """Build the Jinja2 template context from segments and narratives."""
    location = segments_data.get("location", {})
    overall_score = segments_data.get("overall_score")
    segments = segments_data.get("segments", [])
    priority_actions = segments_data.get("priority_actions", [])
    cross_signal_patterns = segments_data.get("cross_signal_patterns", [])
    why_now = segments_data.get("why_now", [])
    data_gaps = segments_data.get("data_gaps", [])

    # Fill in narratives, using fallbacks where missing
    filled_narratives = {}
    for seg in segments:
        dim = seg.get("dimension", "")
        narrative = (narratives_data.get(dim) or "").strip()
        if not narrative:
            narrative = _fallback_narrative(seg)
        filled_narratives[dim] = narrative
    # Synthesis fallback
    synthesis = (narratives_data.get("synthesis") or "").strip()
    if not synthesis:
        parts = []
        if overall_score is not None:
            parts.append(f"This neighborhood scores {overall_score}/100 overall")
        if cross_signal_patterns:
            titles = [p.get("title", "") for p in cross_signal_patterns[:3] if p.get("title")]
            if titles:
                parts.append("Notable patterns: " + "; ".join(titles))
        synthesis = ". ".join(parts) + "." if parts else "No synthesis available."
    filled_narratives["synthesis"] = synthesis

    # Augment segments with display helpers
    for seg in segments:
        seg["score_color"] = _score_color(seg.get("score"))
        seg["dimension_label"] = DIMENSION_LABELS.get(seg.get("dimension", ""), "Unknown")
        for trend in seg.get("trends", []):
            arrow, css_class = _trend_arrow(trend.get("direction"))
            trend["arrow"] = arrow
            trend["arrow_class"] = css_class
        for metric in seg.get("metrics", []):
            metric["score_color"] = _score_color(None)  # not used for metrics

    generated_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return {
        "location": location,
        "overall_score": overall_score,
        "overall_score_color": _score_color(overall_score),
        "segments": segments,
        "narratives": filled_narratives,
        "priority_actions": priority_actions,
        "cross_signal_patterns": cross_signal_patterns,
        "why_now": why_now,
        "data_gaps": data_gaps,
        "generated_date": generated_date,
        "version": REPORT_VERSION,
    }


def render_html(template_path, context):
    """Render the HTML report using Jinja2."""
    template_dir = str(Path(template_path).parent)
    template_name = Path(template_path).name
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html"]),
    )
    tmpl = env.get_template(template_name)
    return tmpl.render(**context)


def render_markdown(context):
    """Generate a Markdown version of the report (no template needed)."""
    lines = []
    loc = context["location"]
    lines.append(f"# BlockSense NYC — Neighborhood Analysis Report")
    lines.append(f"**{loc.get('name', 'Unknown')}**, {loc.get('borough', '')}")
    lines.append("")
    lines.append(f"**Overall Score: {context.get('overall_score', 'N/A')}/100**")
    lines.append("")

    # Score summary
    scores_line = []
    for seg in context.get("segments", []):
        dim_label = seg.get("dimension_label", seg.get("dimension", "?"))
        score = seg.get("score", "N/A")
        scores_line.append(f"{dim_label}: {score}")
    if scores_line:
        lines.append(" | ".join(scores_line))
        lines.append("")

    # Priority actions
    actions = context.get("priority_actions", [])
    if actions:
        lines.append("## Priority Actions")
        lines.append("")
        for i, action in enumerate(actions, 1):
            act_text = action.get("action", "N/A") if isinstance(action, dict) else str(action)
            lines.append(f"{i}. {act_text}")
        lines.append("")

    # Dimension sections
    for seg in context.get("segments", []):
        dim = seg.get("dimension", "unknown")
        dim_label = seg.get("dimension_label", dim.title())
        score = seg.get("score", "N/A")
        lines.append(f"## {dim_label} ({score}/100)")
        lines.append("")

        narrative = context.get("narratives", {}).get(dim, "")
        if narrative:
            lines.append(narrative)
            lines.append("")

        metrics = seg.get("metrics", [])
        if metrics:
            lines.append("### Metrics")
            lines.append("")
            for m in metrics:
                label = m.get("label", "?")
                value = m.get("value", "?")
                unit = m.get("unit", "")
                annotation = m.get("annotation", "")
                line = f"- **{label}**: {value} {unit}".strip()
                if annotation:
                    line += f" _{annotation}_"
                lines.append(line)
            lines.append("")

        trends = seg.get("trends", [])
        if trends:
            lines.append("### Trends")
            lines.append("")
            for t in trends:
                signal = t.get("signal", "?")
                direction = t.get("direction", "?")
                delta = t.get("delta_pct")
                arrow = t.get("arrow", "")
                line = f"- {arrow} **{signal}**: {direction}"
                if delta is not None:
                    line += f" ({delta}% change)"
                if t.get("is_anomaly"):
                    ctx = t.get("anomaly_context", "")
                    line += f" **[ANOMALY{': ' + ctx if ctx else ''}]**"
                lines.append(line)
            lines.append("")

    # Cross-signal patterns / synthesis
    synthesis = context.get("narratives", {}).get("synthesis", "")
    patterns = context.get("cross_signal_patterns", [])
    if synthesis or patterns:
        lines.append("## Cross-Signal Patterns")
        lines.append("")
        if patterns:
            for p in patterns:
                lines.append(f"- **{p.get('title', '?')}**: {p.get('summary', '')}")
            lines.append("")
        if synthesis:
            lines.append(synthesis)
            lines.append("")

    # Data gaps
    data_gaps = context.get("data_gaps", [])
    if data_gaps:
        lines.append("## Data Notes")
        lines.append("")
        for gap in data_gaps:
            lines.append(f"- {gap}")
        lines.append("")

    lines.append("---")
    lines.append(f"Generated: {context.get('generated_date', 'N/A')} | {context.get('version', '')}")
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Render BlockSense neighborhood analysis report from segments and narratives."
    )
    parser.add_argument(
        "--segments",
        required=True,
        help="Path to segments.json (output of extract_segments.py).",
    )
    parser.add_argument(
        "--narratives",
        required=True,
        help="Path to narratives.json (LLM-generated narratives).",
    )
    parser.add_argument(
        "--template",
        required=True,
        help="Path to the Jinja2 HTML report template.",
    )
    parser.add_argument(
        "--output-html",
        default=None,
        help="Output path for the HTML report. Default: stdout.",
    )
    parser.add_argument(
        "--output-md",
        default=None,
        help="Output path for the Markdown report. Omit to skip Markdown generation.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Check scripts/demo_cache/ for a cached result before running.",
    )
    args = parser.parse_args()

    segments_path = Path(args.segments)
    narratives_path = Path(args.narratives)
    template_path = Path(args.template)

    for p, label in [(segments_path, "segments"), (narratives_path, "narratives"), (template_path, "template")]:
        if not p.exists():
            print(f"Error: {label} file not found: {p}", file=sys.stderr)
            sys.exit(1)

    # Demo cache check
    if args.demo:
        script_dir = Path(__file__).resolve().parent
        cache_dir = script_dir / "demo_cache"
        cache_file = cache_dir / f"{segments_path.name}.report.html"
        if cache_file.exists():
            cached = cache_file.read_text(encoding="utf-8")
            if args.output_html:
                Path(args.output_html).write_text(cached, encoding="utf-8")
                print(f"[demo] Wrote cached HTML report to {args.output_html}", file=sys.stderr)
            else:
                sys.stdout.write(cached)
            # For demo mode, also check for cached Markdown
            cache_md = cache_dir / f"{segments_path.name}.report.md"
            if args.output_md and cache_md.exists():
                Path(args.output_md).write_text(
                    cache_md.read_text(encoding="utf-8"), encoding="utf-8"
                )
                print(f"[demo] Wrote cached Markdown report to {args.output_md}", file=sys.stderr)
            sys.exit(0)

    # Load data
    try:
        segments_data = json.loads(segments_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, Exception) as e:
        print(f"Error reading segments: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        narratives_data = json.loads(narratives_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, Exception) as e:
        print(f"Error reading narratives: {e}", file=sys.stderr)
        sys.exit(1)

    # Build context and render
    context = build_template_context(segments_data, narratives_data)

    # Render HTML
    try:
        html_output = render_html(str(template_path), context)
    except Exception as e:
        print(f"Error rendering HTML template: {e}", file=sys.stderr)
        sys.exit(1)

    if args.output_html:
        Path(args.output_html).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output_html).write_text(html_output, encoding="utf-8")
        print(f"HTML report written to {args.output_html}", file=sys.stderr)
    else:
        sys.stdout.write(html_output)

    # Render Markdown
    if args.output_md:
        md_output = render_markdown(context)
        Path(args.output_md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output_md).write_text(md_output, encoding="utf-8")
        print(f"Markdown report written to {args.output_md}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
