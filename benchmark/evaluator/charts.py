"""Standalone, accessible SVG charts for the measured benchmark study."""

from html import escape
from math import ceil
from pathlib import Path

from benchmark.evaluator.evaluate import POLICIES

COLORS = ("#1d365a", "#a94c23", "#31756a", "#78488e")


def svg(title, width, height, elements):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">'
            f'<title>{escape(title)}</title><rect width="{width}" height="{height}" rx="10" fill="#fffef8"/>'
            '<g font-family="system-ui, sans-serif" font-size="14" fill="#18312a">'
            f'{elements}</g></svg>')


def text(x, y, label, *, anchor="start", size=14, weight="normal", fill="#18312a"):
    return (f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" font-weight="{weight}" fill="{fill}">'
            f'{escape(str(label))}</text>')


def shade(ratio, low, high):
    channels = (round(a + (b - a) * ratio) for a, b in zip(low, high, strict=True))
    return "#" + "".join(f"{channel:02x}" for channel in channels)


def heatmap(matrix):
    if not matrix:
        return ""
    rows = list(dict.fromkeys(cell["model"] for cell in matrix))
    columns = list(dict.fromkeys(cell["context"] for cell in matrix))
    cells = {(cell["model"], cell["context"]): cell for cell in matrix}
    maximum = max((cell["average_tokens"] or 0 for cell in matrix), default=1) or 1
    elements = []
    for group, title in enumerate(("Verified pass rate", "Average total tokens")):
        offset = 18 + 184 * group
        elements.append(text(24, offset + 24, title, size=20, weight="650"))
        for index, tier in enumerate(columns):
            elements.append(text(315 + 168 * index, offset + 51, tier, anchor="middle", weight="650"))
        for model_index, model in enumerate(rows):
            y = offset + 63 + 52 * model_index
            elements.append(text(24, y + 30, model.removeprefix("opencode/"), weight="600"))
            for index, tier in enumerate(columns):
                cell = cells[model, tier]
                avg = cell["average_tokens"]
                rate = cell["success_rate"]
                if not cell["tasks"]:
                    fill, label, ink = "#e9ebe5", "Missing", "#52635b"
                elif group == 0:
                    fill = shade(rate, (248, 216, 195), (22, 103, 78))
                    label, ink = f'{rate:.0%} ({cell["successes"]}/{cell["tasks"]})', "white" if rate > .64 else "#18312a"
                else:
                    fill = shade(avg / maximum, (242, 245, 244), (29, 74, 110))
                    label, ink = f"{avg:,.0f}", "white" if avg / maximum > .60 else "#18312a"
                x = 240 + 168 * index
                elements.append(f'<rect x="{x}" y="{y}" width="150" height="45" rx="6" fill="{fill}"/>')
                elements.append(text(x + 75, y + 28, label, anchor="middle", weight="650", fill=ink))
    return svg("Verified task success and mean total tokens by model and context tier", 756, 380, "".join(elements))


def tokens_vs_success(policies):
    points = [(name, row) for name, row in policies.items() if row["average_tokens"] is not None and row["success_rate"] is not None]
    elements = [text(24, 31, "Tokens versus verified success", size=20, weight="650")]
    if not points:
        elements.append(text(24, 66, "Complete held-out task grids are required to plot policy results."))
        return svg("Tokens versus verified success", 756, 102, "".join(elements))
    x_axis, y_axis, width, height = 82, 226, 629, 156
    maximum = max(row["average_tokens"] for _, row in points)
    scale = max(4000, ceil(maximum / 4000) * 4000)
    for index in range(5):
        y = y_axis - height * index / 4
        x = x_axis + width * index / 4
        elements.append(f'<path d="M{x_axis} {y}H{x_axis + width}" stroke="#dfe6dd"/>')
        elements.append(text(x_axis - 14, y + 4, f"{index * 25}%", anchor="end", size=12))
        elements.append(text(x, y_axis + 20, f"{scale * index / 4:,.0f}", anchor="middle", size=12))
    elements.append(f'<path d="M{x_axis} {y_axis - height}V{y_axis}H{x_axis + width}" stroke="#738578" fill="none"/>')
    for index, (name, row) in enumerate(points):
        x = x_axis + width * row["average_tokens"] / scale
        y = y_axis - height * row["success_rate"]
        color = COLORS[index]
        label = f'{POLICIES[name]} · {row["average_tokens"]:,.0f} tokens · {row["success_rate"]:.1%}'
        elements.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{13 - 2 * index}" fill="none" stroke="{color}" stroke-width="2.5">'
                        f'<title>{escape(label)}</title></circle>')
        legend_x = 65 + 168 * index
        elements.append(f'<circle cx="{legend_x}" cy="292" r="5" fill="{color}"/>')
        elements.append(text(legend_x + 12, 297, POLICIES[name]))
    elements.append(text(397, 271, "Mean total tokens per evaluated task", anchor="middle", size=12))
    return svg("Tokens versus verified success for held-out routing policies", 756, 326, "".join(elements))


def export_charts(evaluation, directory: Path):
    heat = heatmap(evaluation["matrix"])
    efficiency = tokens_vs_success(evaluation["policies"])
    (directory / "model-context-heatmap.svg").write_text(heat, encoding="utf-8")
    (directory / "tokens-vs-success.svg").write_text(efficiency, encoding="utf-8")

