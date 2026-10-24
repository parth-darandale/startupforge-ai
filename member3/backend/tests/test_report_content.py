import json
import io
from pathlib import Path

import pytest

from app.modules.intelligence.contracts import AnalysisSection, Claim, CriticIssue, ProjectContext
from app.modules.intelligence.errors import IntelligenceError
from startupforge_member3.reports import ReportsAdapter
from pypdf import PdfReader


FIXTURE = Path(__file__).resolve().parents[3] / "integration" / "fixtures" / "project_context.demo.json"


def _context() -> ProjectContext:
    return ProjectContext.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_feasibility_report_expands_into_a_grounded_validation_plan():
    context = _context()
    content = ReportsAdapter(artifacts=None).build_content(context)
    sections = {section["section_id"]: section for section in content["sections"]}

    assert {
        "product-concept", "customer-validation", "market-scope", "business-model",
        "pilot-plan", "risks-and-constraints", "research-scope", "next-steps", "decision",
    } <= sections.keys()
    assert sections["market-scope"]["claim_kinds"] == ["reviewed", "recommendation"]
    assert any("do not establish the size" in paragraph for paragraph in sections["market-scope"]["paragraphs"])
    assert any("proposed ways to resolve" in paragraph for paragraph in sections["next-steps"]["paragraphs"])
    assert any("demonstration using synthetic fixture sources" in paragraph for paragraph in sections["research-scope"]["paragraphs"])
    assert set(sections["sourced-statistics"]["evidence_ids"]) == {figure.evidence_id for figure in context.figures}


def test_report_recommendations_cover_the_open_questions_without_claiming_results():
    context = _context()
    content = ReportsAdapter(artifacts=None).build_content(context)
    sections = {section["section_id"]: section for section in content["sections"]}
    actions = " ".join(sections["next-steps"]["paragraphs"]).lower()

    assert "recommendations, not completed research" in actions
    assert "current customer price expectations" in actions
    assert "target group" in actions
    assert "goes beyond stated interest" in actions
    assert sections["decision"]["claim_kinds"] == ["reviewed", "recommendation"]


def test_generic_unknown_claims_are_qualified_by_their_report_section():
    context = _context()
    context.final_analysis.sections.append(AnalysisSection(
        section_id="test-alternatives",
        heading="Alternatives",
        claims=[
            Claim(claim_id="test-alt-source", text="This conclusion remains unknown; the cited material does not directly establish it for the submitted startup.", kind="unknown"),
            Claim(claim_id="test-alt-review", text="This conclusion remains unknown; the critic's concern requires further evidence or a validation test.", kind="unknown"),
        ],
    ))
    content = ReportsAdapter(artifacts=None).build_content(context)
    section = next(item for item in content["sections"] if item["section_id"] == "analysis-test-alternatives")

    assert section["paragraphs"] == [
        "Unknown: Available sources do not establish how the proposed offer compares with alternatives used by intended customers.",
        "Unknown: The review concern remains open; compare the proposed offer with alternatives using evidence from intended customers.",
    ]


def test_pdf_export_preserves_the_rupee_symbol_in_source_titles():
    context = _context()
    adapter = ReportsAdapter(artifacts=None)
    content = adapter.build_content(context)
    content["bibliography"][0]["title"] = "Budget meal plan at ₹150 a day"
    pdf = adapter.render(content, context, "pdf")["data"]
    extracted = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)

    assert "₹150" in extracted


def test_report_recommendations_do_not_deny_existing_customer_financial_or_pilot_evidence():
    context = _context()
    evidence = context.evidence[0]
    recorded_result = "The project pilot recorded completed customer tasks, paid orders, delivery costs, and a retention threshold."
    evidence.excerpt = recorded_result
    context.final_analysis.sections.append(AnalysisSection(
        section_id="project-pilot", heading="Recorded project pilot",
        claims=[Claim(claim_id="recorded-pilot", text=recorded_result, kind="supported",
                      evidence_ids=[evidence.evidence_id])],
    ))

    content = ReportsAdapter(artifacts=None).build_content(context)
    sections = {section["section_id"]: section for section in content["sections"]}
    report_text = " ".join(paragraph for section in content["sections"] for paragraph in section["paragraphs"])

    assert recorded_result in report_text
    assert "contains no customer test results" not in report_text
    assert "does not establish project-specific prices" not in report_text
    assert "contains no measured pilot outcomes" not in report_text
    assert "existing customer test results" in " ".join(sections["customer-validation"]["paragraphs"])
    assert "any recorded prices, costs, revenue, or margins" in " ".join(sections["business-model"]["paragraphs"])
    assert "any existing pilot evidence" in " ".join(sections["pilot-plan"]["paragraphs"])


@pytest.mark.parametrize("issue_count", [5, 85])
def test_report_preserves_many_constraints_issues_and_resolutions_within_paragraph_limit(issue_count):
    context = _context()
    context.idea.constraints = [f"Recorded constraint {index}" for index in range(30)]
    context.critic.issues = [CriticIssue(
        issue_id=f"issue-{index}", severity="medium", reason=f"Recorded issue {index}.",
        resolution=f"Recorded resolution {index}.",
    ) for index in range(issue_count)]

    content = ReportsAdapter(artifacts=None).build_content(context)
    section = next(item for item in content["sections"] if item["section_id"] == "risks-and-constraints")
    risk_text = "\n".join(section["paragraphs"])

    assert len(section["paragraphs"]) <= 40
    for constraint in context.idea.constraints:
        assert constraint in risk_text
    for issue in context.critic.issues:
        assert f"{issue.reason}\nResolution recorded: {issue.resolution}" in risk_text


def test_recommendation_label_does_not_allow_unsourced_numeric_claims():
    context = _context()
    adapter = ReportsAdapter(artifacts=None)
    content = adapter.build_content(context)
    section = next(item for item in content["sections"] if item["section_id"] == "next-steps")
    section["paragraphs"].append("The market has 50 million customers.")

    with pytest.raises(IntelligenceError) as exc:
        adapter.validate_content(content, context)

    assert exc.value.code == "UNSOURCED_NUMERIC_CLAIM"
