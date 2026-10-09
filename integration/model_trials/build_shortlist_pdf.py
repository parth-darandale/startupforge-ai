"""Generate the concise comparison from the retained local trial records."""
import json
from pathlib import Path
from statistics import median

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output/pdf/StartupForge_AI_Top_Two_Models.pdf"
pdfmetrics.registerFont(TTFont("Arial", "C:/Windows/Fonts/arial.ttf"))
pdfmetrics.registerFont(TTFont("ArialBold", "C:/Windows/Fonts/arialbd.ttf"))
pdfmetrics.registerFontFamily("Arial", normal="Arial", bold="ArialBold")
navy = colors.HexColor("#183345")
teal = colors.HexColor("#256859")
muted = colors.HexColor("#52636D")
styles = {
    "title": ParagraphStyle("title", fontName="ArialBold", fontSize=22, leading=26, textColor=navy),
    "sub": ParagraphStyle("sub", fontName="Arial", fontSize=10, leading=14, textColor=muted),
    "body": ParagraphStyle("body", fontName="Arial", fontSize=9.3, leading=13, textColor=navy),
    "cell": ParagraphStyle("cell", fontName="Arial", fontSize=9, leading=12, textColor=navy),
    "head": ParagraphStyle("head", fontName="ArialBold", fontSize=10, leading=13, textColor=colors.white),
    "note": ParagraphStyle("note", fontName="Arial", fontSize=8.5, leading=11.5, textColor=muted),
}


def p(text, style="body"):
    return Paragraph(text, styles[style])


def trial(name):
    data = json.loads((Path(__file__).parent / name).read_text(encoding="utf-8"))
    assert data["status"] == "completed"
    model = data["models"][0]
    tasks = model["tasks"]
    assert len(tasks) == 7 and all(t.get("checks", {}).get("schema_valid") for t in tasks)
    return tasks


qwen = trial("qwen3_4b_instruct.json")
fin = trial("fin_r1.json")
assert [t["case_id"] for t in qwen] == [t["case_id"] for t in fin]
later = lambda tasks: [t["elapsed_seconds"] for t in tasks[1:]]
timing = lambda tasks: f"{median(later(tasks)):.2f} s median<br/>{min(later(tasks)):.2f}-{max(later(tasks)):.2f} s range"
rows = [
    [p("What we checked", "head"), p("Qwen3 4B Instruct 2507<br/>DEFAULT", "head"), p("Fin-R1<br/>ALTERNATIVE", "head")],
    ["Main use", "Balanced interactive workflow", "Candidate for reviewing and rewriting claims"],
    ["Structured responses", "7/7 completed and schema-valid", "7/7 completed and schema-valid"],
    ["Idea extraction", "Included students; added unprovided features and omitted provenance/constraints", "Added twenty audiences and unprovided features; omitted provenance/constraints"],
    ["Seeded-error critic", "Flagged both false claims and the fabricated citation", "Flagged both false claims; did not diagnose the fabricated citation"],
    ["Revision", "Improved summary; retained false wording under weaker claim labels", "Rewrote the false percentage and profit guarantee as uncertainty"],
    ["Debate replies", "Both obeyed the numbers rule; Pro asserted an unverified competitor limitation", "Pro repeated forbidden numbers and an unverified competitor limitation"],
    ["Fictional cashbook", "Correct: closing cash INR 4,600; net change INR -400", "Named a formula; did not calculate either requested result"],
    ["Citation accuracy", "Some valid IDs attached to unrelated claims", "Some valid IDs attached to unrelated claims"],
    ["First call", f"{qwen[0]['elapsed_seconds']:.2f} s", f"{fin[0]['elapsed_seconds']:.2f} s"],
    ["Six later calls", timing(qwen), timing(fin)],
]
table_data = [[p(value, "cell") if isinstance(value, str) else value for value in row] for row in rows]
table = Table(table_data, colWidths=[109, 211, 211], repeatRows=1, hAlign="LEFT")
table.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), navy),
    ("BACKGROUND", (1, 1), (1, -1), colors.HexColor("#EEF6F2")),
    ("BACKGROUND", (2, 1), (2, -1), colors.HexColor("#FCF8EE")),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("LEFTPADDING", (0, 0), (-1, -1), 9),
    ("RIGHTPADDING", (0, 0), (-1, -1), 9),
    ("TOPPADDING", (0, 0), (-1, -1), 8),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ("LINEBELOW", (0, 0), (-1, 0), 1, navy),
    ("LINEBELOW", (0, 1), (-1, -1), 0.4, colors.HexColor("#DCE5E5")),
]))

story = [p("StartupForge AI", "sub"), Spacer(1, 5), p("The two-model shortlist", "title"),
         Spacer(1, 5), p("Local trials | 8 October 2026 | Both options run without an API key", "sub"),
         Spacer(1, 13), table, Spacer(1, 12),
         p("<b>Decision:</b> Keep Qwen as the default. Fin-R1's observed advantage is rewriting unsupported claims. Neither model consistently established correct citation support."),
         Spacer(1, 8),
         p("<b>Fair comparison:</b> Same seven independent cases, synthetic fixtures and frozen v1 prompts. Q4_K_M weights; context 4,096; output cap 1,024; temperature 0; seed 42; timeout 180 s. One run per case on an i5-12450H, 16 GB RAM and RTX 3050 Laptop 6 GB. Load and output length varied; timing is not a controlled speed ranking.", "note"),
         Spacer(1, 7),
         p("<b>Evidence limit:</b> The newer Qwen app workflow completed debates and saving, but retained zero sources or market figures for the Indian textbook example. Fin-R1 was tested on the baseline cases, not that complete app workflow. A model switch does not supply missing market evidence.", "note"),
         Spacer(1, 7),
         p("<b>Trial records:</b> integration/model_trials/qwen3_4b_instruct.json and fin_r1.json.<br/>The concise Markdown comparison and archived full audit are in the same folder.", "note")]


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#DCE5E5"))
    canvas.line(32, 29, A4[0] - 32, 29)
    canvas.setFont("Arial", 8)
    canvas.setFillColor(muted)
    canvas.drawString(32, 17, "Member 1 | Idea intelligence | Trial observations, not market validation")
    canvas.drawRightString(A4[0] - 32, 17, str(doc.page))
    canvas.restoreState()


OUT.parent.mkdir(parents=True, exist_ok=True)
doc = SimpleDocTemplate(str(OUT), pagesize=A4, rightMargin=32, leftMargin=32,
                        topMargin=30, bottomMargin=40, title="StartupForge AI - Top Two Models",
                        author="StartupForge AI Team")
doc.build(story, onFirstPage=footer, onLaterPages=footer)
print(OUT)
