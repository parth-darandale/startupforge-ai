"""Formal, source-linked report content and DOCX/PDF exports."""

from __future__ import annotations

import io
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.integration.contracts import GenerationResult, TemplateDescriptor
from app.integration.interfaces import ReportsAdapter as ReportsProtocol
from app.modules.intelligence.contracts import ProjectContext
from app.modules.intelligence.errors import IntelligenceError

from .core.artifacts import ArtifactStore


class ContentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReportSection(ContentModel):
    section_id: str = Field(min_length=1, max_length=100)
    heading: str = Field(min_length=1, max_length=200)
    paragraphs: list[str] = Field(min_length=1, max_length=40)
    source_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_kinds: list[str] = Field(default_factory=list)


class ReportReference(ContentModel):
    source_id: str
    title: str
    url: str
    publisher: str | None = None
    retrieved_at: str


class ReportContent(ContentModel):
    title: str = Field(min_length=1, max_length=200)
    sections: list[ReportSection] = Field(min_length=1, max_length=40)
    bibliography: list[ReportReference] = Field(default_factory=list, max_length=100)


class ReportsAdapter(ReportsProtocol):
    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

    def templates(self) -> list[TemplateDescriptor]:
        return [TemplateDescriptor(template_id="evidence-led-report", name="Evidence-led startup report", kind="report",
                                   supported_formats=["docx", "pdf"],
                                   supported_options={"sections": ["Executive summary", "Idea and users",
                                                                      "Market and evidence", "Feasibility",
                                                                      "Risks and gaps", "Sources"]})]

    def validate_options(self, kind: str, options: dict[str, Any]) -> None:
        if kind != "report" or options.get("template_id") != "evidence-led-report":
            raise IntelligenceError("TEMPLATE_NOT_FOUND", "Choose an installed report template.", 422)
        formats = options.get("formats", [])
        if not formats or any(fmt not in {"docx", "pdf"} for fmt in formats):
            raise IntelligenceError("UNSUPPORTED_FORMAT", "Report exports support DOCX and PDF.", 422,
                                    details={"supported_formats": ["docx", "pdf"]})

    def validate_content(self, content: dict[str, Any], context: ProjectContext) -> dict[str, Any]:
        try:
            normalized = ReportContent.model_validate(content).model_dump(mode="json")
        except ValidationError as exc:
            raise IntelligenceError("INVALID_ARTIFACT_CONTENT", "The edited content does not match the report format.", 422,
                                    details={"issues": exc.errors(include_input=False)}) from exc
        source_ids = {source.source_id for source in context.sources}
        evidence_by_id = {item.evidence_id: item for item in context.evidence}
        bibliography_ids = {item["source_id"] for item in normalized["bibliography"]}
        if bibliography_ids - source_ids:
            raise IntelligenceError("INVALID_CONTENT_REFERENCE", "The bibliography contains a source outside this context.", 422)
        for section in normalized["sections"]:
            if set(section["source_ids"]) - source_ids or set(section["evidence_ids"]) - set(evidence_by_id):
                raise IntelligenceError("INVALID_CONTENT_REFERENCE", "Report content references material outside this context.", 422)
            if any(evidence_by_id[evidence_id].source_id not in section["source_ids"] for evidence_id in section["evidence_ids"]):
                raise IntelligenceError("INVALID_CONTENT_REFERENCE", "Each cited evidence item needs its matching source reference.", 422)
            if "supported" in section["claim_kinds"] and not section["evidence_ids"]:
                raise IntelligenceError("UNSOURCED_SUPPORTED_CLAIM", "Supported report findings must keep their evidence references.", 422)
            for paragraph in section["paragraphs"]:
                if re.search(r"(?<![A-Za-z])(?:\$|€|£|₹|INR\s*)?\d[\d,.]*(?:\s*%|\s*(?:million|billion|thousand|crore|lakh))?", paragraph, re.I):
                    if not section["evidence_ids"] and not ({"assumption", "provided", "reviewed"} & set(section["claim_kinds"])):
                        raise IntelligenceError("UNSOURCED_NUMERIC_CLAIM", "Numeric report claims need evidence or an explicit assumption label.", 422)
        return normalized

    def build_content(self, context: ProjectContext) -> dict[str, Any]:
        idea = context.idea
        sections: list[ReportSection] = []

        def add(section_id: str, heading: str, paragraphs: list[str], source_ids: list[str] | None = None,
                evidence_ids: list[str] | None = None, claim_kinds: list[str] | None = None) -> None:
            if paragraphs:
                sections.append(ReportSection(section_id=section_id, heading=heading, paragraphs=paragraphs,
                                              source_ids=sorted(set(source_ids or [])),
                                              evidence_ids=sorted(set(evidence_ids or [])),
                                              claim_kinds=claim_kinds or []))

        add("executive-summary", "Executive summary", [context.final_analysis.summary], claim_kinds=["reviewed"])
        add("idea-and-users", "Idea and target users", [f"Problem: {idea.problem}", f"Proposed solution: {idea.solution}",
             "Target users: " + (", ".join(idea.target_users) if idea.target_users else "Not specified in the submitted idea."),
             f"Domain: {idea.domain}"], claim_kinds=["provided"])
        if idea.assumptions:
            add("founder-assumptions", "Founder assumptions", [f"Assumption: {item}" for item in idea.assumptions], claim_kinds=["assumption"])

        for analysis_section in context.final_analysis.sections:
            paragraphs: list[str] = []
            source_ids: set[str] = set()
            evidence_ids: set[str] = set()
            claim_kinds: set[str] = set()
            for claim in analysis_section.claims:
                label = {"supported": "Supported finding", "assumption": "Unverified assumption", "unknown": "Unknown"}[claim.kind]
                paragraphs.append(f"{label}: {claim.text}")
                evidence_ids.update(claim.evidence_ids)
                claim_kinds.add(claim.kind)
            source_ids.update(e.source_id for e in context.evidence if e.evidence_id in evidence_ids)
            add(f"analysis-{analysis_section.section_id}", analysis_section.heading, paragraphs,
                list(source_ids), list(evidence_ids), sorted(claim_kinds))

        if context.figures:
            paragraphs = [f"{figure.display_value} — {figure.metric}\nQuoted evidence: {figure.excerpt}\nQualification: {figure.qualification}"
                          for figure in context.figures]
            add("sourced-statistics", "Sourced statistics and market figures", paragraphs,
                [figure.source_id for figure in context.figures], [figure.evidence_id for figure in context.figures], ["supported"])

        gaps = list(dict.fromkeys([*context.final_analysis.gaps, *context.critic.unresolved_gaps]))
        add("open-gaps", "Open questions and evidence gaps", gaps or ["No unresolved gaps were recorded by the review."],
            claim_kinds=["unknown"])
        add("review", "Review status", [f"Review status: {context.critic.review_status}.",
             context.critic.revision_summary or "No revision summary was provided."], claim_kinds=["reviewed"])
        bibliography = [ReportReference(source_id=source.source_id, title=source.title, url=source.url,
                                        publisher=source.publisher, retrieved_at=source.retrieved_at.isoformat()).model_dump(mode="json")
                        for source in context.sources]
        return self.validate_content({"title": f"{idea.project_name} — Startup Feasibility Report",
                                      "sections": [section.model_dump(mode="json") for section in sections],
                                      "bibliography": bibliography}, context)

    def render(self, content: dict[str, Any], context: ProjectContext, format: str) -> dict[str, Any]:
        normalized = self.validate_content(content, context)
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized["title"]).strip("-").lower()[:60] or "startup-report"
        if format == "docx":
            data = _render_docx(normalized, context)
            return {"filename": f"{slug}.docx", "format": "docx",
                    "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "data": data}
        if format == "pdf":
            data = _render_pdf(normalized, context)
            return {"filename": f"{slug}.pdf", "format": "pdf", "media_type": "application/pdf", "data": data}
        raise IntelligenceError("UNSUPPORTED_FORMAT", "Report exports support DOCX and PDF.", 422,
                                details={"supported_formats": ["docx", "pdf"]})

    async def generate(self, kind: str, context: ProjectContext, options: dict[str, Any], progress) -> GenerationResult:
        if kind != "report":
            raise IntelligenceError("UNSUPPORTED_ARTIFACT_KIND", "The report module only generates reports.", 422)
        await progress("building_report", 20)
        content = self.build_content(context)
        await progress("rendering_report", 55)
        files = [self.render(content, context, fmt) for fmt in options["formats"]]
        artifact_id, file_ids = self.artifacts.create_artifact(context, "report", content, files)
        await progress("registering_report", 90)
        return GenerationResult(kind="report", context_ref={"project_id": context.project_id,
                                "context_version": context.context_version, "context_hash": context.content_hash},
                                artifact_ids=[artifact_id], file_ids=file_ids)


def _render_docx(content: dict[str, Any], context: ProjectContext) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt, RGBColor

    doc = Document()
    section = doc.sections[0]
    section.top_margin = Inches(0.72); section.bottom_margin = Inches(0.72)
    section.left_margin = Inches(0.82); section.right_margin = Inches(0.82)
    normal = doc.styles["Normal"]
    normal.font.name = "Aptos"; normal.font.size = Pt(10.5); normal.font.color.rgb = RGBColor(37, 53, 59)
    normal.paragraph_format.space_after = Pt(7)
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = title.add_run(content["title"]); run.bold = True; run.font.size = Pt(26); run.font.color.rgb = RGBColor(24, 51, 61)
    meta = doc.add_paragraph(f"Prepared from reviewed project context v{context.context_version} · {context.critic.review_status.replace('_', ' ')}")
    meta.runs[0].italic = True; meta.runs[0].font.color.rgb = RGBColor(27, 111, 102)
    source_index = {source.source_id: i + 1 for i, source in enumerate(context.sources)}
    for item in content["sections"]:
        doc.add_heading(item["heading"], level=1)
        for paragraph in item["paragraphs"]:
            p = doc.add_paragraph(paragraph)
            cited = [source_index[sid] for sid in item["source_ids"] if sid in source_index]
            if cited:
                p.add_run(" " + "".join(f"[{index}]" for index in cited)).font.color.rgb = RGBColor(27, 111, 102)
        if item["evidence_ids"]:
            evidence_line = doc.add_paragraph("Evidence IDs: " + ", ".join(item["evidence_ids"]))
            evidence_line.runs[0].font.size = Pt(8)
            evidence_line.runs[0].font.color.rgb = RGBColor(90, 115, 112)
    if content["bibliography"]:
        doc.add_heading("Sources", level=1)
        for index, source in enumerate(content["bibliography"], start=1):
            paragraph = doc.add_paragraph(style="List Number")
            paragraph.add_run(f"{source['title']} [{source['source_id']}]\n")
            if source.get("publisher"):
                paragraph.add_run(f"{source['publisher']} · ")
            paragraph.add_run(source["url"])
            paragraph.add_run(f"\nRetrieved: {source['retrieved_at']}")
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer.add_run(f"StartupForge · context v{context.context_version} · {context.content_hash[:12]}").font.size = Pt(8)
    output = io.BytesIO(); doc.save(output); return output.getvalue()


def _render_pdf(content: dict[str, Any], context: ProjectContext) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
    from xml.sax.saxutils import escape

    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=19*mm, leftMargin=19*mm, topMargin=18*mm, bottomMargin=18*mm,
                            title=content["title"], author="StartupForge")
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="SFTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=23,
                              leading=28, textColor=colors.HexColor("#18333d"), alignment=TA_LEFT, spaceAfter=8))
    styles.add(ParagraphStyle(name="SFHeading", parent=styles["Heading2"], fontSize=14, leading=18,
                              textColor=colors.HexColor("#1b6f66"), spaceBefore=14, spaceAfter=6))
    styles.add(ParagraphStyle(name="SFBody", parent=styles["BodyText"], fontSize=9.5, leading=14, spaceAfter=7))
    story = [Paragraph(escape(content["title"]), styles["SFTitle"]),
             Paragraph(f"Context v{context.context_version} · Review status: {escape(context.critic.review_status.replace('_', ' '))}", styles["Italic"]),
             Spacer(1, 7*mm)]
    for item in content["sections"]:
        story.append(Paragraph(escape(item["heading"]), styles["SFHeading"]))
        for paragraph in item["paragraphs"]:
            story.append(Paragraph(escape(paragraph).replace("\n", "<br/>"), styles["SFBody"]))
        if item["evidence_ids"]:
            story.append(Paragraph("Evidence IDs: " + escape(", ".join(item["evidence_ids"])), styles["Italic"]))
    if content["bibliography"]:
        story.append(Paragraph("Sources", styles["SFHeading"]))
        for index, source in enumerate(content["bibliography"], start=1):
            text = f"{index}. {source['title']} [{source['source_id']}]<br/>{source.get('publisher') or ''} {source['url']}<br/>Retrieved: {source['retrieved_at']}"
            story.append(Paragraph(escape(text).replace("&lt;br/&gt;", "<br/>"), styles["SFBody"]))
    def footer(canvas, _doc):
        canvas.saveState(); canvas.setFont("Helvetica", 8); canvas.setFillColor(colors.HexColor("#52696d"))
        canvas.drawString(19*mm, 10*mm, f"StartupForge · context v{context.context_version} · {context.content_hash[:12]}")
        canvas.drawRightString(A4[0]-19*mm, 10*mm, str(canvas.getPageNumber())); canvas.restoreState()
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
