"""Render an AI-selected slide composition as editable, native PowerPoint objects.

The default model supplies theme, layout and copy. AutoPresent's Python output
is statically translated to validated primitives; generated code is never run.
Long source passages remain complete in notes, with an explicit visible notice
when the available slide area requires a shorter excerpt.
"""

from __future__ import annotations

import io
import math
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import ImageFont
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt


@dataclass(frozen=True)
class Palette:
    paper: str
    ink: str
    accent: str
    dark: str
    muted: str
    rule: str


PALETTES = {
    "midnight": Palette("F5F6FA", "182640", "5266CB", "15223B", "566378", "CED4E1"),
    "ocean": Palette("F3F8F8", "163A43", "087F8C", "103840", "516A70", "C6D8DB"),
    "forest": Palette("F6F7F0", "213F32", "477843", "193D31", "59695A", "CFD8C8"),
    "ember": Palette("FCF7F1", "4B3029", "B94F2E", "462B25", "7C655D", "DFCEC2"),
}

_WHITE = "FFFFFF"
_PALE = "E3E9EE"
_NOTICE = "Full details in notes."
_FONT = "Arial"
_WIDTH = 13.333333
_HEIGHT = 7.5
_LEFT = 0.78
_RIGHT = 12.55
_BOTTOM = 6.63


def _get(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


@lru_cache(maxsize=96)
def _font(size: int, bold: bool = False) -> Any:
    # Pillow is already required by python-pptx. Metrics use the same Arial face
    # assigned to the exported text, with a conservative width allowance.
    filename = "arialbd.ttf" if bold else "arial.ttf"
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / filename
    for candidate in (str(windows), filename, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(candidate, size=round(size * 96 / 72))
        except OSError:
            continue
    return ImageFont.load_default()


def _text_width(text: str, size: int, bold: bool = False) -> float:
    face = _font(size, bold)
    if hasattr(face, "getlength"):
        return float(face.getlength(text)) / 96
    return float(face.getbbox(text)[2]) / 96


def _wrap(text: str, width: float, size: int, bold: bool = False) -> list[str]:
    limit = max(0.20, width - 0.13) * 0.95
    lines: list[str] = []
    for paragraph in str(text).replace("\r", "").split("\n"):
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}" if current else word
            if _text_width(candidate, size, bold) <= limit:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            # Break very long URLs and unspaced input without allowing an
            # invisible overflow beyond the textbox's right edge.
            while _text_width(word, size, bold) > limit and len(word) > 1:
                end = 1
                while end < len(word) and _text_width(word[:end + 1], size, bold) <= limit:
                    end += 1
                lines.append(word[:end])
                word = word[end:]
            current = word
        lines.append(current)
    return lines or [""]


def _height(text: str, width: float, size: int, bold: bool = False) -> float:
    return len(_wrap(text, width, size, bold)) * size / 72 * 1.18 + 0.04


def _fit(text: str, width: float, height: float, preferred: int, minimum: int,
         bold: bool = False) -> tuple[list[str], int, bool]:
    for size in range(preferred, minimum - 1, -1):
        if _height(text, width, size, bold) <= height:
            return _wrap(text, width, size, bold), size, False
    # Shorten at a word boundary only after trying every permitted font size.
    # The original block is always copied to notes by _notes().
    words = text.split()
    low, high = 0, len(words)
    while low < high:
        mid = (low + high + 1) // 2
        excerpt = " ".join(words[:mid]).rstrip(" ,;:") + " … " + _NOTICE
        if _height(excerpt, width, minimum, bold) <= height:
            low = mid
        else:
            high = mid - 1
    excerpt = " ".join(words[:low]).rstrip(" ,;:") + " … " + _NOTICE
    # A cell can be intentionally as short as one line. Keep the notice
    # visible, even if a pathological individual input exceeds that space.
    if _height(excerpt, width, minimum, bold) > height:
        excerpt = _NOTICE
    return _wrap(excerpt.strip(), width, minimum, bold), minimum, True


def _rgb(value: str) -> RGBColor:
    return RGBColor.from_string(value)


class _DeckRenderer:
    def __init__(self, content: dict[str, Any], context: Any):
        self.content = content
        self.context = context
        design = content.get("design") or {}
        self.palette = PALETTES.get(design.get("theme", "midnight"), PALETTES["midnight"])
        self.sources = {_get(source, "source_id"): source for source in _get(context, "sources", [])}
        self.source_numbers = {source_id: index + 1 for index, source_id in enumerate(self.sources)}
        self.figures = {_get(figure, "figure_id"): figure for figure in _get(context, "figures", [])}
        self.evidence = {_get(item, "evidence_id"): item for item in _get(context, "evidence", [])}
        self.deck = Presentation()
        self.deck.slide_width = Inches(_WIDTH)
        self.deck.slide_height = Inches(_HEIGHT)
        self.deck.core_properties.title = content["title"]
        self.deck.core_properties.subject = "Startup investment presentation"
        self.deck.core_properties.author = "StartupForge"

    def text(self, slide: Any, text: str, x: float, y: float, width: float, height: float,
             size: int = 23, minimum: int = 17, color: str | None = None,
             bold: bool = False, align: Any = PP_ALIGN.LEFT,
             hyperlink: str | None = None) -> bool:
        lines, actual_size, shortened = _fit(str(text), width, height, size, minimum, bold)
        box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(width), Inches(height))
        frame = box.text_frame
        frame.clear()
        frame.word_wrap = False
        frame.margin_left = frame.margin_right = Inches(0.015)
        frame.margin_top = frame.margin_bottom = Inches(0.005)
        frame.vertical_anchor = MSO_ANCHOR.TOP
        paragraph = frame.paragraphs[0]
        paragraph.alignment = align
        paragraph.space_before = paragraph.space_after = Pt(0)
        paragraph.line_spacing = Pt(actual_size * 1.18)
        for index, line in enumerate(lines):
            if index:
                paragraph.add_line_break()
            run = paragraph.add_run()
            run.text = line
            run.font.name = _FONT
            run.font.size = Pt(actual_size)
            run.font.bold = bold
            run.font.color.rgb = _rgb(color or self.palette.ink)
            if hyperlink:
                run.hyperlink.address = hyperlink
        return shortened

    def rule(self, slide: Any, x: float, y: float, width: float,
             color: str | None = None, weight: float = 0.8) -> None:
        connector = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT,
                                               Inches(x), Inches(y), Inches(x + width), Inches(y))
        connector.line.color.rgb = _rgb(color or self.palette.rule)
        connector.line.width = Pt(weight)

    def vertical_rule(self, slide: Any, x: float, y: float, height: float) -> None:
        connector = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT,
                                               Inches(x), Inches(y), Inches(x), Inches(y + height))
        connector.line.color.rgb = _rgb(self.palette.rule)
        connector.line.width = Pt(0.8)

    def block_text(self, block: dict[str, Any]) -> str:
        text = str(block.get("text", ""))
        kind = block.get("claim_kind")
        prefix = {"assumption": "Assumption: ", "unknown": "Open question: ",
                  "warning": "Caution: "}.get(kind, "")
        if kind == "recommendation":
            action = text.split(":", 1)[-1].strip()
            is_plan = re.match(r"(?:ask|assign|build|check|choose|collect|compare|confirm|define|design|develop|explore|gather|identify|launch|map|measure|offer|partner|pilot|plan|prioritize|record|recruit|review|run|select|set|test|track|validate)\b", action, re.I)
            conditional = re.search(r"\b(?:could|may|might|would|whether|hypothesis|assumption)\b|\?", text, re.I)
            if not is_plan and not conditional:
                prefix = "Hypothesis: "
        if prefix and not text.lower().startswith(prefix.lower().rstrip()):
            text = prefix + text
        refs = [f"[{self.source_numbers[sid]}]" for sid in block.get("source_ids", [])
                if sid in self.source_numbers]
        if refs and not re.match(r"^\[\d+\]", text):
            text += " " + " ".join(refs)
        return text

    def header(self, slide: Any, title: str, dark: bool = False) -> float:
        color = _WHITE if dark else self.palette.ink
        size = 36 if len(title) < 70 else 32
        height = max(0.72, _height(title, _RIGHT - _LEFT, size, True))
        self.text(slide, title, _LEFT, 0.47, _RIGHT - _LEFT, height,
                  size=size, minimum=32, color=color, bold=True)
        top = 0.47 + height + 0.32
        self.rule(slide, _LEFT, top - 0.13, _RIGHT - _LEFT,
                  self.palette.accent if not dark else _PALE, 1.5)
        return top + 0.13

    def footer(self, slide: Any, data: dict[str, Any], index: int, dark: bool = False) -> None:
        source_ids = list(dict.fromkeys([
            *data.get("source_ids", []),
            *(sid for block in data.get("body_blocks", []) for sid in block.get("source_ids", [])),
        ]))
        source_ids.sort(key=lambda sid: self.source_numbers.get(sid, 9999))
        references = [f"[{self.source_numbers[sid]}]" for sid in source_ids if sid in self.sources]
        color = _PALE if dark else self.palette.muted
        if references:
            self.text(slide, "Sources " + ", ".join(references), _LEFT, 7.02, 10.8, 0.24,
                      size=10, minimum=10, color=color)
        self.text(slide, f"{index + 1:02d}", 11.93, 7.00, 0.60, 0.27,
                  size=11, minimum=11, color=color, align=PP_ALIGN.RIGHT)

    def notes(self, slide: Any, data: dict[str, Any]) -> None:
        passages = [data.get("speaker_notes", "")]
        source_ids = list(data.get("source_ids", []))
        evidence_ids = list(data.get("evidence_ids", []))
        for index, block in enumerate(data.get("body_blocks", []), 1):
            # Exact saved wording is retained even when a shorter excerpt is
            # required for a legible slide. No source qualification is lost.
            passages.append(f"{index}. {block.get('text', '')}")
            source_ids.extend(block.get("source_ids", []))
            evidence_ids.extend(block.get("evidence_ids", []))
            if block.get("figure_id") in self.figures:
                figure = self.figures[block["figure_id"]]
                passages.append(f"Quoted evidence: {_get(figure, 'excerpt', '')}\n"
                                f"Qualification: {_get(figure, 'qualification', '')}")
        for source_id in dict.fromkeys(source_ids):
            source = self.sources.get(source_id)
            if source:
                passages.append(f"[{self.source_numbers[source_id]}] {_get(source, 'title')}\n"
                                f"{_get(source, 'url')}\nSource ID: {source_id}")
        for evidence_id in dict.fromkeys(evidence_ids):
            evidence = self.evidence.get(evidence_id)
            if evidence:
                passages.append(f"Evidence {evidence_id}\n{_get(evidence, 'excerpt', '')}")
        slide.notes_slide.notes_text_frame.text = "\n\n".join(str(part) for part in passages if part)

    def stack(self, slide: Any, blocks: list[dict[str, Any]], x: float, y: float,
              width: float, height: float, size: int = 23, color: str | None = None,
              separators: bool = False) -> None:
        if not blocks:
            return
        gap = 0.22 if len(blocks) <= 4 else 0.12
        available = height - gap * (len(blocks) - 1)
        texts = [self.block_text(block) for block in blocks]
        # Allocate by actual wrapped line lengths rather than identical cards.
        desired = [max(0.46, _height(text, width, size)) for text in texts]
        if sum(desired) > available:
            desired = [max(0.43, _height(text, width, 17)) for text in texts]
        if sum(desired) > available:
            # Reserve at least two readable lines for every distinct block.
            minimum = min(0.59, available / len(blocks))
            remaining = max(0, available - minimum * len(blocks))
            weights = [math.sqrt(max(1, len(text))) for text in texts]
            desired = [minimum + remaining * weight / sum(weights) for weight in weights]
        elif len(blocks) <= 4:
            # More air between short points; never stretch the text itself.
            gap += min(0.25, (available - sum(desired)) / max(1, len(blocks)))
        for index, (text, block, block_height) in enumerate(zip(texts, blocks, desired)):
            self.text(slide, text, x, y, width, block_height, size=size, minimum=17,
                      color=color or (self.palette.muted if block.get("type") == "gap" else self.palette.ink))
            y += block_height
            if separators and index != len(blocks) - 1:
                self.rule(slide, x, y + gap / 2, width)
            y += gap

    def columns(self, slide: Any, blocks: list[dict[str, Any]], top: float,
                labels: list[str] | None = None, comparison: bool = False) -> None:
        width = 5.45
        right = 7.10
        if labels:
            for label, x in zip(labels[:2], (_LEFT, right)):
                self.text(slide, label, x, top, width, 0.57, size=23, minimum=20,
                          bold=True, color=self.palette.accent)
            top += 0.75
        self.vertical_rule(slide, 6.65, top, _BOTTOM - top)
        midpoint = math.ceil(len(blocks) / 2)
        self.stack(slide, blocks[:midpoint], _LEFT, top, width, _BOTTOM - top,
                   size=22, separators=comparison)
        self.stack(slide, blocks[midpoint:], right, top, width, _BOTTOM - top,
                   size=22, separators=comparison)

    def cover(self, slide: Any, data: dict[str, Any], blocks: list[dict[str, Any]]) -> None:
        # The cover presents the proposed venture; strategy assertions on later
        # slides retain their explicit hypothesis classification.
        blocks = [{**block, "claim_kind": "provided"} for block in blocks]
        title = str(data["title"])
        title_height = max(0.95, _height(title, 11.5, 44, True))
        title_height = min(2.4, title_height)
        y = 1.15 if len(blocks) <= 2 else 0.72
        self.text(slide, title, _LEFT, y, 11.65, title_height,
                  size=44, minimum=32, color=_WHITE, bold=True)
        self.rule(slide, _LEFT, y + title_height + 0.26, 3.2, self.palette.accent, 3)
        top = y + title_height + 0.67
        if len(blocks) <= 3:
            self.stack(slide, blocks, _LEFT, top, 10.9, _BOTTOM - top, size=27, color=_PALE)
        else:
            # A cover with many edited blocks still preserves each point.
            midpoint = math.ceil(len(blocks) / 2)
            for x, items in ((_LEFT, blocks[:midpoint]), (7.10, blocks[midpoint:])):
                self.stack(slide, items, x, top, 5.45, _BOTTOM - top, size=22, color=_PALE)

    def statement(self, slide: Any, data: dict[str, Any], blocks: list[dict[str, Any]], top: float) -> None:
        if not blocks:
            return
        focus = min(max(0, int(data.get("emphasis_index", 0))), len(blocks) - 1)
        lead = blocks[focus]
        remaining = blocks[:focus] + blocks[focus + 1:]
        if len(blocks) > 7:
            self.columns(slide, blocks, top)
            return
        lead_height = min(2.0, max(1.1, _height(self.block_text(lead), 11.6, 31, True)))
        lead_height = min(lead_height, (_BOTTOM - top) * (0.50 if remaining else 0.85))
        self.text(slide, self.block_text(lead), _LEFT, top, 11.65, lead_height,
                  size=31, minimum=24, color=self.palette.accent, bold=True)
        top += lead_height + 0.35
        if remaining:
            if len(remaining) <= 3:
                self.stack(slide, remaining, _LEFT, top, 11.6, _BOTTOM - top, size=23)
            else:
                self.columns(slide, remaining, top)

    def process(self, slide: Any, blocks: list[dict[str, Any]], top: float) -> None:
        if not blocks:
            return
        context_blocks = [block for block in blocks if block.get("claim_kind") != "recommendation"]
        steps = [block for block in blocks if block.get("claim_kind") == "recommendation"]
        if context_blocks and steps:
            # A supplied product description or constraint is context, not a
            # numbered step in the proposed workflow.
            intro_height = min(1.45, 0.66 * len(context_blocks))
            self.stack(slide, context_blocks, _LEFT, top, _RIGHT - _LEFT, intro_height, size=21)
            top += intro_height + 0.22
            blocks = steps
        count = len(blocks)
        # Native numbered process diagrams use whitespace, an alignment line,
        # and editable text. No raster, fake charts, or dashboard panels.
        if count <= 4:
            gap = 0.38
            width = (_RIGHT - _LEFT - gap * (count - 1)) / count
            line_y = top + 0.82
            self.rule(slide, _LEFT, line_y, _RIGHT - _LEFT, self.palette.accent, 1.6)
            for index, block in enumerate(blocks):
                x = _LEFT + index * (width + gap)
                self.text(slide, f"{index + 1:02d}", x, top, width, 0.65,
                          size=34, minimum=30, bold=True, color=self.palette.accent)
                self.text(slide, self.block_text(block), x, line_y + 0.35,
                          width, _BOTTOM - line_y - 0.35, size=24, minimum=17)
        else:
            rows = math.ceil(count / 2)
            step_height = (_BOTTOM - top) / rows
            for index, block in enumerate(blocks):
                column, row = divmod(index, rows)
                x = _LEFT + column * 6.28
                y = top + row * step_height
                self.text(slide, f"{index + 1:02d}", x, y, 0.54, 0.58,
                          size=25, minimum=23, bold=True, color=self.palette.accent)
                self.text(slide, self.block_text(block), x + 0.72, y, 4.75,
                          step_height - 0.12, size=21, minimum=17)
                if row < rows - 1:
                    self.vertical_rule(slide, x + 0.22, y + 0.58, max(0.05, step_height - 0.66))

    def scorecard(self, slide: Any, blocks: list[dict[str, Any]], top: float) -> None:
        # Rows are appropriate for assessments, milestones, and business-model
        # components, without implying invented numerical scores.
        if len(blocks) > 6:
            self.columns(slide, blocks, top, comparison=True)
            return
        if not blocks:
            return
        row_height = (_BOTTOM - top) / len(blocks)
        for index, block in enumerate(blocks):
            y = top + index * row_height
            text = self.block_text(block)
            parts = text.split(":", 1)
            split_heading = len(parts) == 2 and 2 <= len(parts[0]) <= 42
            if split_heading:
                self.text(slide, parts[0], _LEFT, y + 0.10, 3.1, row_height - 0.24,
                          size=23, minimum=17, color=self.palette.accent, bold=True)
                self.text(slide, parts[1].strip(), 4.15, y + 0.10, 8.35, row_height - 0.24,
                          size=23, minimum=17)
            else:
                self.text(slide, text, _LEFT, y + 0.08, 11.7, row_height - 0.20,
                          size=25 if len(blocks) <= 3 else 22, minimum=17)
            if index != len(blocks) - 1:
                self.rule(slide, _LEFT, y + row_height - 0.04, _RIGHT - _LEFT)

    def evidence_layout(self, slide: Any, data: dict[str, Any], blocks: list[dict[str, Any]], top: float) -> None:
        if not blocks:
            return
        focus = min(max(0, int(data.get("emphasis_index", 0))), len(blocks) - 1)
        figure = self.figures.get(blocks[focus].get("figure_id"))
        if not figure or len(blocks) > 7:
            self.statement(slide, data, blocks, top)
            return
        remaining = blocks[:focus] + blocks[focus + 1:]
        width = 6.05 if remaining else 11.6
        value = str(_get(figure, "display_value", ""))
        metric = str(_get(figure, "metric", ""))
        qualification = str(_get(figure, "qualification", ""))
        excerpt = str(_get(figure, "excerpt", ""))
        self.text(slide, value, _LEFT, top, width, 0.88,
                  size=48, minimum=32, bold=True, color=self.palette.accent)
        self.text(slide, metric, _LEFT, top + 1.05, width, 0.82,
                  size=25, minimum=20, bold=True)
        bottom = _BOTTOM
        quote_top = top + 2.08
        qual_height = min(1.05, max(0.42, _height(qualification, width, 17))) if qualification else 0
        quote_height = max(0.52, bottom - quote_top - qual_height - 0.20)
        self.text(slide, excerpt, _LEFT, quote_top, width, quote_height,
                  size=20, minimum=17)
        if qualification:
            self.text(slide, "Qualification: " + qualification, _LEFT,
                      bottom - qual_height, width, qual_height, size=17, minimum=17,
                      color=self.palette.muted)
        if remaining:
            self.vertical_rule(slide, 7.13, top, _BOTTOM - top)
            self.stack(slide, remaining, 7.58, top + 0.02, 4.95, _BOTTOM - top, size=22)

    def closing(self, slide: Any, data: dict[str, Any], blocks: list[dict[str, Any]], top: float) -> None:
        # Source records remain linked editable type, arranged as a bibliography
        # rather than repeating full URLs in tiny footer text.
        if not blocks:
            return
        is_bibliography = sum(bool(re.match(r"^\[\d+\]", str(b.get("text", ""))))
                              for b in blocks) >= max(1, math.ceil(len(blocks) / 2))
        if not is_bibliography:
            if len(blocks) <= 4:
                self.stack(slide, blocks, _LEFT, top + 0.1, 11.6, _BOTTOM - top - 0.1, size=26)
            else:
                self.columns(slide, blocks, top)
            return
        columns = 2 if len(blocks) > 4 else 1
        rows = math.ceil(len(blocks) / columns)
        row_height = (_BOTTOM - top) / rows
        width = 5.42 if columns == 2 else 11.6
        for index, block in enumerate(blocks):
            column, row = divmod(index, rows)
            x = _LEFT + column * 6.30
            y = top + row * row_height
            refs = block.get("source_ids", [])
            source = self.sources.get(refs[0]) if refs else None
            text = str(block.get("text", ""))
            link = str(_get(source, "url", "")) if source else None
            self.text(slide, text, x, y + 0.06, width, row_height - 0.15,
                      size=20, minimum=17, hyperlink=link)
            if row < rows - 1:
                self.rule(slide, x, y + row_height - 0.04, width)

    def render(self) -> bytes:
        for index, data in enumerate(self.content["slides"]):
            slide = self.deck.slides.add_slide(self.deck.slide_layouts[6])
            if data.get("generated_elements") is not None:
                self.autopresent_slide(slide, data)
                self.footer(slide, data, index)
                self.notes(slide, data)
                continue
            layout = str(data.get("layout", "auto"))
            if layout == "auto":
                layout = "cover" if index == 0 else "split" if len(data.get("body_blocks", [])) > 5 else "statement"
            dark = layout == "cover"
            slide.background.fill.solid()
            slide.background.fill.fore_color.rgb = _rgb(self.palette.dark if dark else self.palette.paper)
            blocks = data.get("body_blocks", [])
            if dark:
                self.cover(slide, data, blocks)
            else:
                top = self.header(slide, str(data["title"]))
                labels = data.get("visual_labels", [])
                if layout == "statement":
                    self.statement(slide, data, blocks, top)
                elif layout == "process":
                    self.process(slide, blocks, top)
                elif layout == "comparison":
                    self.columns(slide, blocks, top, labels, comparison=True)
                elif layout == "scorecard":
                    self.scorecard(slide, blocks, top)
                elif layout == "evidence":
                    self.evidence_layout(slide, data, blocks, top)
                elif layout == "closing":
                    self.closing(slide, data, blocks, top)
                else:
                    self.columns(slide, blocks, top, labels)
            self.footer(slide, data, index, dark)
            self.notes(slide, data)
        output = io.BytesIO()
        self.deck.save(output)
        return output.getvalue()

    def autopresent_slide(self, slide: Any, data: dict[str, Any]) -> None:
        """Render only validated geometry; all text comes from canonical blocks."""
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = _rgb("FFFFFF")
        elements = data["generated_elements"]
        # Decorations form the back layer regardless of generated call order.
        for item in elements:
            if item["type"] != "shape":
                continue
            x, y, w, h = (item[key] for key in ("x", "y", "width", "height"))
            if item.get("shape") == "line":
                shape = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT,
                    Inches(x), Inches(y), Inches(x + w), Inches(y + h))
                shape.line.color.rgb = _rgb(item.get("line_color") or self.palette.accent)
                shape.line.width = Pt(1.5)
                continue
            shape = slide.shapes.add_shape(MSO_SHAPE.OVAL if item.get("shape") == "ellipse" else MSO_SHAPE.RECTANGLE,
                                          Inches(x), Inches(y), Inches(w), Inches(h))
            if item.get("fill"):
                shape.fill.solid()
                shape.fill.fore_color.rgb = _rgb(item["fill"])
            else:
                shape.fill.background()
            if item.get("line_color"):
                shape.line.color.rgb = _rgb(item["line_color"])
            else:
                shape.line.fill.background()
        for item in elements:
            if item["type"] != "text":
                continue
            key = item["key"]
            blocks = [] if key == "TITLE" else [data["body_blocks"][int(part[6:])] for part in key.split("+")]
            references = sum(bool(block.get("source_ids")) and bool(re.match(r"^\[\d+\]", block["text"]))
                             for block in blocks)
            if len(blocks) > 1 and references >= max(1, math.ceil(len(blocks) / 2)):
                # Keep each bibliography item individually clickable, even when
                # the model placed several references in one shared text area.
                columns = 2 if len(blocks) > 4 and item["width"] >= 8 else 1
                rows = math.ceil(len(blocks) / columns)
                cell_width = item["width"] / columns
                cell_height = item["height"] / rows
                for block_index, block in enumerate(blocks):
                    column, row = divmod(block_index, rows)
                    source = self.sources.get(block["source_ids"][0]) if block.get("source_ids") else None
                    self.text(slide, self.block_text(block), item["x"] + column * cell_width,
                              item["y"] + row * cell_height, cell_width - 0.18, cell_height - 0.10,
                              size=min(20, round(item["font_size"])), minimum=17, color=item["color"],
                              hyperlink=str(_get(source, "url")) if source else None)
                continue
            text = data["title"] if key == "TITLE" else "\n\n".join(self.block_text(block) for block in blocks)
            minimum = 28 if key == "TITLE" else 17
            source = self.sources.get(blocks[0]["source_ids"][0]) if len(blocks) == 1 and references else None
            self.text(slide, text, item["x"], item["y"], item["width"], item["height"],
                      size=max(minimum, round(item["font_size"])), minimum=minimum,
                      bold=item["bold"], color=item["color"],
                      align={"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}[item["align"]],
                      hyperlink=str(_get(source, "url")) if source else None)


def render_designed_pptx(content: dict[str, Any], context: Any) -> bytes:
    """Export validated slide data with its selected theme and native layouts.

    Supported themes: midnight, ocean, forest, ember. Supported slide layouts:
    auto, cover, statement, split, process, comparison, scorecard, evidence,
    closing. All original block text, source URLs, and evidence excerpts are
    retained in notes, and no continuation slides change the requested count.
    """
    return _DeckRenderer(content, context).render()
