"""Context-grounded pitch and poster generation, with editable structured output."""

from __future__ import annotations

import io
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.integration.contracts import GenerationResult, TemplateDescriptor
from app.integration.interfaces import VisualStudioAdapter as VisualStudioProtocol
from app.modules.intelligence.contracts import ProjectContext
from app.modules.intelligence.errors import IntelligenceError
from startupforge_member3.core.artifacts import ArtifactStore


class ContentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BodyBlock(ContentModel):
    type: Literal["text", "claim", "figure", "gap"] = "text"
    text: str = Field(min_length=1, max_length=3000)
    source_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_kind: Literal["provided", "supported", "assumption", "unknown", "warning"] | None = None
    claim_id: str | None = None
    figure_id: str | None = None


class DeckSlide(ContentModel):
    slide_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    body_blocks: list[BodyBlock] = Field(default_factory=list, max_length=12)
    speaker_notes: str = Field(default="", max_length=4000)
    source_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class PitchDeckContent(ContentModel):
    title: str = Field(min_length=1, max_length=200)
    tone: Literal["professional", "bold", "academic"] = "professional"
    slides: list[DeckSlide] = Field(min_length=1, max_length=30)


class PosterSection(ContentModel):
    section_id: str = Field(min_length=1, max_length=80)
    heading: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)
    source_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_kind: Literal["provided", "supported", "assumption", "unknown", "warning"] | None = None
    figure_id: str | None = None


class PosterContent(ContentModel):
    title: str = Field(min_length=1, max_length=200)
    sections: list[PosterSection] = Field(min_length=1, max_length=20)


class VisualStudioAdapter(VisualStudioProtocol):
    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

    def templates(self, kind: Literal["pitch_deck", "poster"] | None = None) -> list[TemplateDescriptor]:
        templates = [
            TemplateDescriptor(template_id="investor-brief", name="Investor pitch", kind="pitch_deck",
                               supported_formats=["pptx"],
                               supported_options={"slide_count": {"minimum": 5, "maximum": 12, "default": 8},
                                                  "tone": {"values": ["professional", "bold", "academic"], "default": "professional"}}),
            TemplateDescriptor(template_id="evidence-one-pager", name="Evidence one pager", kind="poster",
                               supported_formats=["txt"],
                               supported_options={"audience": {"maximum_characters": 500, "default": "Potential customers and reviewers"}}),
        ]
        return [item for item in templates if kind is None or item.kind == kind]

    def validate_options(self, kind: str, options: dict[str, Any]) -> None:
        if kind == "pitch_deck":
            if options.get("template_id") != "investor-brief":
                raise IntelligenceError("TEMPLATE_NOT_FOUND", "Choose an installed pitch deck template.", 422)
            if options.get("tone") not in {"professional", "bold", "academic"}:
                raise IntelligenceError("INVALID_OPTION", "Choose professional, bold or academic tone.", 422)
            if not 5 <= int(options.get("slide_count", 0)) <= 12:
                raise IntelligenceError("INVALID_OPTION", "Slide count must be from 5 to 12.", 422)
        elif kind == "poster":
            if options.get("template_id") != "evidence-one-pager":
                raise IntelligenceError("TEMPLATE_NOT_FOUND", "Choose an installed poster template.", 422)
            if not str(options.get("audience", "")).strip() or len(options["audience"]) > 500:
                raise IntelligenceError("INVALID_OPTION", "Poster audience must contain 1 to 500 characters.", 422)
        else:
            raise IntelligenceError("UNSUPPORTED_ARTIFACT_KIND", "This module only generates pitch decks and posters.", 422)

    def validate_content(self, kind: str, content: dict[str, Any], context: ProjectContext) -> dict[str, Any]:
        model = PitchDeckContent if kind == "pitch_deck" else PosterContent if kind == "poster" else None
        if model is None:
            raise IntelligenceError("UNSUPPORTED_ARTIFACT_KIND", "This module cannot edit that artifact type.", 422)
        try:
            normalized = model.model_validate(content).model_dump(mode="json")
        except ValidationError as exc:
            raise IntelligenceError("INVALID_ARTIFACT_CONTENT", "The edited content does not match this artifact type.", 422,
                                    details={"issues": exc.errors(include_input=False)}) from exc
        self._validate_references(kind, normalized, context)
        return normalized

    def _validate_references(self, kind: str, content: dict[str, Any], context: ProjectContext) -> None:
        source_ids = {source.source_id for source in context.sources}
        evidence_by_id = {item.evidence_id: item for item in context.evidence}
        evidence_ids = set(evidence_by_id)
        for section in content.get("slides", content.get("sections", [])):
            refs_source = section.get("source_ids", [])
            refs_evidence = section.get("evidence_ids", [])
            for block in section.get("body_blocks", []):
                refs_source = [*refs_source, *block.get("source_ids", [])]
                refs_evidence = [*refs_evidence, *block.get("evidence_ids", [])]
                if block.get("claim_kind") == "supported" and not block.get("evidence_ids"):
                    raise IntelligenceError("UNSOURCED_SUPPORTED_CLAIM", "Supported claims must retain evidence references.", 422)
                if block.get("type") == "figure":
                    figure = next((item for item in context.figures if item.figure_id == block.get("figure_id")), None)
                    if figure is None or figure.display_value not in block.get("text", "") or figure.excerpt not in block.get("text", ""):
                        raise IntelligenceError("FIGURE_CONTEXT_MISMATCH", "Figure text must preserve the exact quoted value and excerpt.", 422)
                claim_id = block.get("claim_id")
                if claim_id:
                    claim = next((claim for analysis_section in context.final_analysis.sections
                                  for claim in analysis_section.claims if claim.claim_id == claim_id), None)
                    if claim is None or block.get("text") != claim.text or block.get("claim_kind") != claim.kind:
                        raise IntelligenceError("CLAIM_CONTEXT_MISMATCH", "Reviewed claims must remain tied to their original reviewed wording.", 422)
            if any(ref not in source_ids for ref in refs_source) or any(ref not in evidence_ids for ref in refs_evidence):
                raise IntelligenceError("INVALID_CONTENT_REFERENCE", "Output content references evidence outside the selected project context.", 422)
            if any(evidence_by_id[ref].source_id not in refs_source for ref in refs_evidence):
                raise IntelligenceError("INVALID_CONTENT_REFERENCE", "Every evidence reference must include its matching source reference.", 422)
        figure_by_id = {figure.figure_id: figure for figure in context.figures}
        if kind == "pitch_deck":
            blocks = [block for slide in content["slides"] for block in slide["body_blocks"]]
        else:
            blocks = content["sections"]
        # Keep statistic cards exact, and require ordinary numeric claims to retain a
        # selected-context citation. Founder-provided numbers remain explicitly labelled.
        for block in blocks:
            text = str(block.get("text", block.get("body", "")))
            block_kind = block.get("claim_kind")
            block_type = block.get("type")
            if block_type == "figure" or block.get("figure_id"):
                figure = figure_by_id.get(block.get("figure_id"))
                if figure is None or figure.display_value not in text or figure.excerpt not in text:
                    raise IntelligenceError("FIGURE_CONTEXT_MISMATCH", "Figure text must preserve the exact quoted value and excerpt.", 422)
                continue
            if block_kind in {"provided", "assumption", "unknown", "warning"}:
                continue
            references = block.get("evidence_ids", [])
            if re.search(r"(?<![A-Za-z])(?:\$|€|£|₹|INR\s*)?\d[\d,.]*(?:\s*%|\s*(?:million|billion|thousand|crore|lakh))?", text, re.I) and not references:
                raise IntelligenceError("UNSOURCED_NUMERIC_CLAIM", "Numeric claims need evidence from this context.", 422)

    def _claim_groups(self, context: ProjectContext) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for section in context.final_analysis.sections:
            for claim in section.claims:
                source_ids = sorted({
                    evidence.source_id for evidence in context.evidence if evidence.evidence_id in claim.evidence_ids
                })
                groups.setdefault(section.heading, []).append({
                    "text": claim.text, "kind": claim.kind, "claim_id": claim.claim_id,
                    "source_ids": source_ids, "evidence_ids": list(claim.evidence_ids),
                })
        return groups

    def build_deck_content(self, context: ProjectContext, options: dict[str, Any]) -> dict[str, Any]:
        idea = context.idea
        groupings = self._claim_groups(context)
        supported_blocks = []
        risk_blocks = []
        for heading, claims in groupings.items():
            target = risk_blocks if any(word in heading.lower() for word in ("risk", "gap", "assumption")) else supported_blocks
            for claim in claims:
                target.append(BodyBlock(type="claim", text=claim["text"], source_ids=claim["source_ids"],
                                        evidence_ids=claim["evidence_ids"], claim_kind=claim["kind"], claim_id=claim["claim_id"]))
        figure_blocks = [BodyBlock(type="figure", text=f"{figure.display_value} — {figure.metric}\nQuoted evidence: {figure.excerpt}\nQualification: {figure.qualification}",
                                   source_ids=[figure.source_id], evidence_ids=[figure.evidence_id], claim_kind="supported",
                                   figure_id=figure.figure_id)
                         for figure in context.figures[:3]]
        gaps = list(dict.fromkeys([*context.final_analysis.gaps, *context.critic.unresolved_gaps]))
        slides: list[DeckSlide] = []

        def add(title: str, blocks: list[BodyBlock], note: str = "") -> None:
            source_ids = sorted({source for block in blocks for source in block.source_ids})
            evidence_ids = sorted({evidence for block in blocks for evidence in block.evidence_ids})
            slides.append(DeckSlide(slide_id=f"slide-{len(slides) + 1:02d}", title=title, body_blocks=blocks,
                                    speaker_notes=note, source_ids=source_ids, evidence_ids=evidence_ids))

        add(idea.project_name, [BodyBlock(type="text", text="Startup pitch | reviewed project context", claim_kind="provided"),
                                BodyBlock(type="text", text=f"Domain: {idea.domain}", claim_kind="provided")],
            "Use the selected saved context version. Facts and figures are sourced in later slides.")
        add("The problem", [BodyBlock(type="text", text=idea.problem, claim_kind="provided"),
                            BodyBlock(type="text", text=f"Designed for: {', '.join(idea.target_users) or 'Target users are not specified.'}", claim_kind="provided")])
        add("The proposed solution", [BodyBlock(type="text", text=idea.solution, claim_kind="provided"),
                                       *[BodyBlock(type="text", text=f"Assumption to validate: {item}", claim_kind="assumption") for item in idea.assumptions[:3]]])
        if supported_blocks:
            add("What the research supports", [BodyBlock(type="text", text=context.final_analysis.summary, claim_kind="provided"),
                                                *supported_blocks[:3]],
                "Claims are copied from the reviewed analysis and retain source and evidence identifiers.")
        if figure_blocks:
            add("Sourced figures", figure_blocks,
                "Values are reproduced as quoted in the selected context. Read each qualification before using it.")
        remaining = [b for b in supported_blocks[3:]]
        if remaining:
            add("Additional findings", remaining[:4])
        if risk_blocks:
            add("Risks and assumptions", risk_blocks[:4])
        if gaps:
            add("Open questions", [BodyBlock(type="gap", text=item, claim_kind="unknown") for item in gaps[:4]])
        closing = [BodyBlock(type="text", text="Validate the riskiest assumption with target users and primary sources.", claim_kind="unknown"),
                   BodyBlock(type="text", text="Keep founder assumptions separate from sourced findings.", claim_kind="provided")]
        closing.extend(BodyBlock(type="gap", text=item, claim_kind="unknown") for item in gaps[:3])
        add("Next steps and open questions", closing)
        count = int(options["slide_count"])
        selected = [*slides[:count - 1], slides[-1]] if len(slides) > count else slides[:]
        while len(selected) < count:
            selected.insert(-1, DeckSlide(slide_id=f"appendix-{len(selected):02d}", title="Review note",
                body_blocks=[BodyBlock(type="gap", text="No additional reviewed finding is available for this slide.", claim_kind="unknown")],
                speaker_notes="This slide intentionally leaves unsupported content blank."))
        return self.validate_content("pitch_deck", {"title": idea.project_name, "tone": options["tone"],
                                                       "slides": [s.model_dump(mode="json") for s in selected]}, context)

    def build_poster_content(self, context: ProjectContext, options: dict[str, Any]) -> dict[str, Any]:
        idea = context.idea
        sections: list[PosterSection] = [
            PosterSection(section_id="problem", heading="Problem", body=idea.problem, claim_kind="provided"),
            PosterSection(section_id="solution", heading="Solution", body=idea.solution, claim_kind="provided"),
            PosterSection(section_id="audience", heading="Audience", body=", ".join(idea.target_users) or "Target users are not specified.", claim_kind="provided"),
        ]
        for heading, claims in self._claim_groups(context).items():
            entries = []
            sources: set[str] = set()
            evidence: set[str] = set()
            kinds: set[str] = set()
            for claim in claims[:5]:
                label = {"supported": "Supported", "assumption": "Assumption", "unknown": "Unknown"}[claim["kind"]]
                entries.append(f"[{label}] {claim['text']}")
                sources.update(claim["source_ids"])
                evidence.update(claim["evidence_ids"])
                kinds.add(claim["kind"])
            if entries:
                sections.append(PosterSection(section_id=f"finding-{len(sections) + 1}", heading=heading,
                                              body="\n".join(entries), source_ids=sorted(sources), evidence_ids=sorted(evidence),
                                              claim_kind="supported" if "supported" in kinds else "assumption" if "assumption" in kinds else "unknown"))
        for figure in context.figures[:6]:
            sections.append(PosterSection(section_id=f"figure-{figure.figure_id}", heading=figure.metric[:120],
                                          body=f"{figure.display_value}\n{figure.excerpt}\nQualification: {figure.qualification}",
                                          source_ids=[figure.source_id], evidence_ids=[figure.evidence_id], claim_kind="supported",
                                          figure_id=figure.figure_id))
        gaps = list(dict.fromkeys([*context.final_analysis.gaps, *context.critic.unresolved_gaps]))
        if gaps:
            sections.append(PosterSection(section_id="gaps", heading="Open questions", body="\n".join(gaps[:8]), claim_kind="unknown"))
        sections.append(PosterSection(section_id="source-note", heading="Evidence and review", body=(
            f"Review status: {context.critic.review_status}. Prepared for {options['audience']}. "
            "Verify cited sources before external publication."), claim_kind="provided"))
        return self.validate_content("poster", {"title": idea.project_name, "sections": [s.model_dump(mode="json") for s in sections]}, context)

    def render(self, kind: str, content: dict[str, Any], context: ProjectContext, format: str) -> dict[str, Any]:
        normalized = self.validate_content(kind, content, context)
        if kind == "pitch_deck" and format == "pptx":
            return {"filename": f"{_slug(normalized['title'])}-pitch.pptx", "format": "pptx",
                    "media_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    "data": _render_pptx(normalized)}
        if kind == "poster" and format == "txt":
            return {"filename": f"{_slug(normalized['title'])}-poster.txt", "format": "txt",
                    "media_type": "text/plain; charset=utf-8", "data": _render_poster_txt(normalized, context)}
        raise IntelligenceError("UNSUPPORTED_FORMAT", "This visual studio template does not support that export format.", 422)

    async def generate(self, kind: str, context: ProjectContext, options: dict[str, Any], progress) -> GenerationResult:
        await progress("building_editable_content", 25)
        content = self.build_deck_content(context, options) if kind == "pitch_deck" else self.build_poster_content(context, options)
        await progress("rendering_export", 65)
        output = self.render(kind, content, context, "pptx" if kind == "pitch_deck" else "txt")
        artifact_id, file_ids = self.artifacts.create_artifact(context, kind, content, [output])
        await progress("registering_artifact", 90)
        return GenerationResult(kind=kind, context_ref={"project_id": context.project_id,
                                "context_version": context.context_version, "context_hash": context.content_hash},
                                artifact_ids=[artifact_id], file_ids=file_ids)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug[:60] or "startupforge"


def _render_poster_txt(content: dict[str, Any], context: ProjectContext) -> bytes:
    sources = {source.source_id: source for source in context.sources}
    lines = [content["title"], "=" * min(72, len(content["title"])), ""]
    for section in content["sections"]:
        lines.extend([section["heading"], "-" * min(48, len(section["heading"])), section["body"], ""])
        for source_id in section["source_ids"]:
            source = sources.get(source_id)
            if source:
                lines.append(f"Source: {source.title} — {source.url} (ID: {source_id})")
        if section["evidence_ids"]:
            lines.append("Evidence IDs: " + ", ".join(section["evidence_ids"]))
        if section["source_ids"]:
            lines.append("")
    lines.append(f"Context version: {context.context_version} | Review: {context.critic.review_status}")
    return "\n".join(lines).encode("utf-8")


def _render_pptx(content: dict[str, Any]) -> bytes:
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR
    from pptx.util import Inches, Pt

    deck = Presentation()
    deck.slide_width = Inches(13.333)
    deck.slide_height = Inches(7.5)
    blank = deck.slide_layouts[6]
    tone = content.get("tone", "professional")
    navy = RGBColor(39, 43, 74) if tone == "academic" else RGBColor(38, 44, 52) if tone == "bold" else RGBColor(24, 51, 61)
    teal = RGBColor(53, 75, 139) if tone == "academic" else RGBColor(221, 112, 59) if tone == "bold" else RGBColor(27, 111, 102)
    muted = RGBColor(86, 105, 108)
    for index, slide_data in enumerate(content["slides"]):
        slide = deck.slides.add_slide(blank)
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = RGBColor(249, 250, 247)
        bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), Inches(0.16), Inches(7.5))
        bar.fill.solid(); bar.fill.fore_color.rgb = teal; bar.line.fill.background()
        title = slide.shapes.add_textbox(Inches(0.75), Inches(0.5), Inches(11.8), Inches(0.85)).text_frame
        title.clear(); title.word_wrap = True
        title.vertical_anchor = MSO_ANCHOR.MIDDLE
        p = title.paragraphs[0]; p.text = slide_data["title"]; p.font.name = "Aptos Display"; p.font.size = Pt(27); p.font.bold = True; p.font.color.rgb = navy
        body = slide.shapes.add_textbox(Inches(0.95), Inches(1.65), Inches(11.25), Inches(4.55)).text_frame
        body.clear(); body.word_wrap = True
        blocks = slide_data["body_blocks"]
        for b_index, block in enumerate(blocks[:9]):
            paragraph = body.paragraphs[0] if b_index == 0 else body.add_paragraph()
            prefix = "•  " if block["type"] in {"claim", "gap", "figure"} else ""
            label = {"supported": "Supported: ", "assumption": "Assumption: ", "unknown": "Open: ", "warning": "Review: "}.get(block.get("claim_kind"), "")
            paragraph.text = f"{prefix}{label}{block['text']}"
            size = 18 if len(block["text"]) < 150 else 15 if len(block["text"]) < 350 else 12
            paragraph.font.name = "Aptos"; paragraph.font.size = Pt(size)
            paragraph.font.color.rgb = muted if block["type"] in {"gap"} else navy
            paragraph.space_after = Pt(15)
        source_ids = sorted(set(slide_data["source_ids"]))
        evidence_ids = sorted(set(slide_data["evidence_ids"]))
        if source_ids or evidence_ids:
            citation = slide.shapes.add_textbox(Inches(0.95), Inches(6.32), Inches(11.1), Inches(0.48)).text_frame
            citation.word_wrap = True
            citation.text = "Sources: " + (", ".join(source_ids) or "—") + "   Evidence: " + (", ".join(evidence_ids) or "—")
            citation.paragraphs[0].font.name = "Aptos"; citation.paragraphs[0].font.size = Pt(8); citation.paragraphs[0].font.color.rgb = muted
        footer = slide.shapes.add_textbox(Inches(0.95), Inches(7.02), Inches(11.1), Inches(0.25)).text_frame
        footer.text = f"STARTUPFORGE  •  {index + 1:02d} / {len(content['slides']):02d}"
        footer.paragraphs[0].font.name = "Aptos"; footer.paragraphs[0].font.size = Pt(9); footer.paragraphs[0].font.color.rgb = teal
    output = io.BytesIO()
    deck.save(output)
    return output.getvalue()
