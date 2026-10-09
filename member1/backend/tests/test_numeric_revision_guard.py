"""Live revision cannot retain invented statistics by weakening the claim badge."""

import pytest

from app.modules.intelligence.contracts import AnalysisReport, CriticIssue, CritiqueReport, EvidenceItem
from app.modules.intelligence.pipeline import IntelligenceService


PASSAGE = "Rooftop solar additions reached 1,850 MW in August 2026, compared to 973 MW in August 2025."


def report(text, kind="unknown", refs=None):
    return AnalysisReport(summary="Market review", sections=[{
        "section_id": "market", "heading": "Market assessment", "claims": [{
            "claim_id": "cpf_1", "text": text, "kind": kind, "evidence_ids": refs or [],
        }],
    }])


def evidence():
    return EvidenceItem(evidence_id="ev_solar", source_id="src_solar", excerpt=PASSAGE)


def test_live_paraphrase_under_unknown_cannot_retain_numbers_or_invent_a_future_date():
    initial = report(PASSAGE, "supported", ["ev_solar"])
    critic = CritiqueReport(review_status="blocked", issues=[CriticIssue(
        issue_id="reviewer_scope", severity="high", claim_id="cpf_1",
        reason="These capacity figures do not establish factory owner demand.", evidence_ids=["ev_solar"],
    )])
    revision = report(
        "Rooftop solar additions reached 1,850 MW in August 2026, compared to 973 MW in August 2025. "
        "However, cumulative additions by August 2027 are extrapolated and not independently verified.",
        "unknown", ["ev_solar"],
    )
    revised, messages = IntelligenceService._guard_final(revision, {"ev_solar"}, [evidence()], critic, initial)
    claim = revised.sections[0].claims[0]
    assert claim.kind == "unknown"
    assert claim.evidence_ids == []
    assert all(number not in claim.text for number in ("1,850", "973", "2026", "2027"))
    assert "figure cards and evidence" in claim.text
    assert any("numerical assertion" in message for message in messages)
    assert initial.sections[0].claims[0].text == PASSAGE
    assert "2027" in revision.sections[0].claims[0].text


def test_live_assumption_badge_cannot_retain_a_paraphrased_market_share():
    revision = report(
        "Rooftop solar accounts for 54% of monthly solar additions compared to 41% for ground-mounted projects. "
        "The claim is not a complete literal quotation and its scope remains unverified.",
        "assumption", ["ev_solar"],
    )
    revised, _ = IntelligenceService._guard_final(revision, {"ev_solar"}, [evidence()])
    claim = revised.sections[0].claims[0]
    assert claim.kind == "unknown"
    assert "54%" not in claim.text
    assert "41%" not in claim.text
    assert claim.evidence_ids == []


@pytest.mark.parametrize("kind", ["unknown", "assumption"])
@pytest.mark.parametrize("text", [
    "The startup will earn $50 million in annual revenue.",
    "Five million customers might adopt the service.",
    "Fifty-four percent of factory owners may subscribe.",
])
def test_live_empty_evidence_still_rejects_numeric_assertions(kind, text):
    revised, messages = IntelligenceService._guard_final(report(text, kind), set(), [])
    claim = revised.sections[0].claims[0]
    assert claim.kind == "unknown"
    assert claim.text != text
    assert claim.evidence_ids == []
    assert messages


def test_live_complete_supported_quotation_keeps_numbers_and_source_reference():
    revised, messages = IntelligenceService._guard_final(report(PASSAGE, "supported", ["ev_solar"]),
                                                         {"ev_solar"}, [evidence()])
    claim = revised.sections[0].claims[0]
    assert claim.kind == "supported"
    assert claim.text == f"Source excerpt: \u201c{PASSAGE}\u201d"
    assert claim.evidence_ids == ["ev_solar"]
    assert messages == []


@pytest.mark.parametrize("kind", ["unknown", "assumption"])
def test_live_nonnumeric_hypotheses_remain_visible(kind):
    text = "Factory owners may value a transparent quote comparison service; customer interviews are still required."
    revised, messages = IntelligenceService._guard_final(report(text, kind), set(), [])
    assert revised.sections[0].claims[0].kind == kind
    assert revised.sections[0].claims[0].text == text
    assert messages == []


@pytest.mark.parametrize("kind", ["unknown", "assumption"])
def test_demo_reference_validation_retains_existing_numeric_fixture_behavior(kind):
    text = "Synthetic fixture assumes 54% adoption for a classroom example."
    revised, messages = IntelligenceService._guard_final(report(text, kind), set(), None)
    assert revised.sections[0].claims[0].text == text
    assert revised.sections[0].claims[0].kind == kind
    assert messages == []
