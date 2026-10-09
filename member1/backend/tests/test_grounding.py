import pytest

from app.modules.intelligence.contracts import (
    AnalysisReport, Claim, CritiqueReport, CriticIssue, EvidenceItem,
)
from app.modules.intelligence.grounding import ExtractedIdea, ground_idea, quotation_issue
from app.modules.intelligence.pipeline import IntelligenceService


IDEA = ("CampusLoop lets college students exchange used textbooks. "
        "Pilot at one college and validate demand before charging.")
PASSAGE = "In India in 2024, a survey of 80 college students reported textbook spending of ₹2,500 per semester."


def extracted(**changes):
    data = dict(
        original_text=IDEA, project_name="CampusLoop", problem="Students face unsafe listings.",
        solution="A verified marketplace with staff accounts, payments, delivery and AI search.",
        target_users=["Students", "Faculty", "Staff"], domain="Education", assumptions=[],
        constraints=["Charge a subscription immediately."],
        field_origins={field: "provided" for field in
                       ("project_name", "problem", "solution", "target_users", "domain", "assumptions", "constraints")},
        source_quotes={
            "project_name": ["CampusLoop"], "solution": ["college students exchange used textbooks"],
            "target_users": ["college students"],
            "constraints": ["Pilot at one college", "validate demand before charging"],
        },
    )
    data.update(changes)
    return ExtractedIdea(**data)


def evidence(text=PASSAGE, identifier="ev_real"):
    return EvidenceItem(evidence_id=identifier, source_id="src_real", excerpt=text)


def report(text, refs=None, kind="supported"):
    return AnalysisReport(summary="Invented guaranteed market growth.", sections=[{
        "section_id": "market", "heading": "Market assessment", "claims": [{
            "claim_id": "c_market", "text": text, "kind": kind,
            "evidence_ids": ["ev_real"] if refs is None else refs,
        }],
    }])


def test_authentic_input_quote_cannot_launder_invented_features_or_users():
    result = ground_idea(extracted(), IDEA)
    assert result.solution == "college students exchange used textbooks"
    assert result.target_users == ["college students"]
    assert result.constraints == ["Pilot at one college", "validate demand before charging"]
    assert result.field_origins["solution"] == "provided"
    assert result.field_origins["problem"] == "unknown"
    assert "unsafe" not in result.problem
    assert all(word not in result.solution for word in ("verified", "staff", "payments", "delivery", "AI"))


def test_fabricated_or_case_changed_input_quotes_do_not_establish_provenance():
    result = ground_idea(extracted(source_quotes={
        "solution": ["College students exchange used textbooks"],
        "target_users": ["faculty"], "constraints": ["Charge immediately"],
    }), IDEA)
    assert result.solution == IDEA
    assert result.target_users == []
    assert result.constraints == []
    assert result.field_origins["target_users"] == "unknown"
    assert result.original_text == IDEA


def test_missing_quote_map_is_conservative_and_preserves_full_original():
    result = ground_idea(extracted(source_quotes={}), IDEA)
    assert result.solution == IDEA
    assert result.target_users == []
    assert result.project_name == "Startup concept"
    assert result.domain == "Unspecified domain"
    assert result.field_origins["problem"] == "unknown"


def test_live_sunquote_continuation_preserves_the_original_product_definition():
    submitted = ("SunQuote is a rooftop solar quote comparison service for small factory owners in India. "
                 "It would collect installer quotations and help compare costs. "
                 "Start with a pilot in one industrial area and test demand before charging customers.")
    result = ground_idea(extracted(original_text=submitted, source_quotes={
        "project_name": ["SunQuote"],
        "solution": ["It would collect installer quotations and help compare costs."],
        "target_users": ["small factory owners in India"],
        "constraints": ["Start with a pilot in one industrial area and test demand before charging customers."],
    }), submitted)
    assert result.solution == submitted
    assert result.project_name == "SunQuote"
    assert result.target_users == ["small factory owners in India"]
    assert result.constraints == ["Start with a pilot in one industrial area and test demand before charging customers."]
    assert result.field_origins["solution"] == "provided"
    assert result.field_origins["problem"] == "unknown"


@pytest.mark.parametrize("pronoun", ["It", "They", "This", "That", "These", "Those"])
def test_anaphoric_solution_quotes_use_bounded_input_without_resolving_the_referent(pronoun):
    definition = "A rooftop solar quotation comparison service is proposed. "
    continuation = f"{pronoun} would help compare installer quotations."
    submitted = definition + continuation + " " + "Additional provided context. " * 140
    result = ground_idea(extracted(original_text=submitted, source_quotes={"solution": [continuation]}), submitted)
    assert result.solution == submitted[:3000]
    assert result.original_text == submitted
    assert len(result.solution) == 3000


def test_self_contained_solution_definition_with_follow_on_quotes_remains_exact():
    definition = "SunQuote compares rooftop solar installer quotations."
    continuation = "It would help factory owners compare costs."
    submitted = f"{definition} {continuation} Start with a limited pilot."
    result = ground_idea(extracted(original_text=submitted,
                                  source_quotes={"solution": [definition, continuation]}), submitted)
    assert result.solution == f"{definition}\n{continuation}"
    assert "Start with" not in result.solution
    assert result.field_origins["solution"] == "provided"


def test_generated_name_and_domain_are_explicitly_inferred():
    result = ground_idea(extracted(source_quotes={}, field_origins={
        "project_name": "inferred", "domain": "inferred",
    }), IDEA)
    assert result.project_name == "CampusLoop"
    assert result.domain == "Education"
    assert result.field_origins["project_name"] == "inferred"
    assert result.field_origins["domain"] == "inferred"


@pytest.mark.parametrize("text", [
    "The startup has a ₹2,500 million addressable market.",
    PASSAGE.replace("₹2,500", "₹25,000"),
    PASSAGE.replace("per semester", "per month"),
    PASSAGE.replace("In India", "Globally"),
    PASSAGE.replace("80 college students", "80 million college students"),
    "a survey of 80 college students reported textbook spending of ₹2,500 per semester.",
])
def test_known_citation_does_not_support_paraphrase_number_unit_or_population_mismatch(text):
    final, messages = IntelligenceService._guard_final(report(text), {"ev_real"}, [evidence()])
    claim = final.sections[0].claims[0]
    assert claim.kind == "unknown"
    assert claim.evidence_ids == []
    assert "₹" not in claim.text
    assert messages


def test_supported_quote_reports_source_words_and_preserves_qualifiers():
    final, messages = IntelligenceService._guard_final(report(PASSAGE), {"ev_real"}, [evidence()])
    claim = final.sections[0].claims[0]
    assert claim.kind == "supported"
    assert claim.text == f"Source excerpt: “{PASSAGE}”"
    assert claim.evidence_ids == ["ev_real"]
    assert messages == []


def test_unrelated_known_reference_is_rejected_even_beside_a_good_reference():
    documents = [evidence(), evidence("Public libraries provide book lending services.", "ev_other")]
    final, _ = IntelligenceService._guard_final(report(PASSAGE, ["ev_real", "ev_other"]),
                                                {"ev_real", "ev_other"}, documents)
    assert final.sections[0].claims[0].kind == "unknown"


def test_literal_second_sentence_can_be_quoted_but_mid_sentence_statistic_cannot():
    source = evidence("The survey was local. " + PASSAGE + " National applicability remains untested.")
    assert quotation_issue(Claim(claim_id="c", text=PASSAGE, kind="supported", evidence_ids=["ev_real"]),
                           {"ev_real": source}) is None
    assert quotation_issue(Claim(claim_id="c", text="₹2,500 per semester.", kind="supported", evidence_ids=["ev_real"]),
                           {"ev_real": source})


@pytest.mark.parametrize("kind,prefix", [("supported", ""), ("unknown", ""),
                                        ("assumption", "Unverified assumption: ")])
def test_unchanged_critic_rejected_claim_is_rewritten_even_if_its_badge_changes(kind, prefix):
    original = report(PASSAGE)
    review = CritiqueReport(review_status="blocked", issues=[CriticIssue(
        issue_id="critic_scope", claim_id="c_market", severity="high",
        reason="The local survey does not establish a national startup market.", evidence_ids=["ev_real"],
    )])
    final, messages = IntelligenceService._guard_final(report(prefix + PASSAGE, kind=kind), {"ev_real"},
                                                       [evidence()], review, original)
    claim = final.sections[0].claims[0]
    assert claim.kind == "unknown"
    assert claim.evidence_ids == []
    assert "₹2,500" not in claim.text
    assert "critic's concern" in claim.text
    assert messages


def test_literal_guard_issue_is_sent_to_critic_revision_without_changing_initial():
    initial = report("This startup is guaranteed to sell 80 million textbooks.")
    review = CritiqueReport(review_status="ready")
    IntelligenceService._merge_reference_issues(review, initial, {"ev_real"}, [evidence()])
    assert initial.sections[0].claims[0].kind == "supported"
    assert review.review_status == "ready_with_gaps"
    assert review.issues[0].claim_id == "c_market"
    assert review.issues[0].issue_id.startswith("guard_quotation_")
    assert "literal quotation" in review.issues[0].reason


def test_corrected_literal_quotation_resolves_only_deterministic_traceability_issue():
    initial = report("This startup is guaranteed to sell 80 million textbooks.")
    review = CritiqueReport(review_status="ready")
    IntelligenceService._merge_reference_issues(review, initial, {"ev_real"}, [evidence()])
    corrected, _ = IntelligenceService._guard_final(report(PASSAGE), {"ev_real"}, [evidence()], review, initial)
    IntelligenceService._resolve_critic(review, corrected, {"ev_real"}, 1, [evidence()])
    assert corrected.sections[0].claims[0].kind == "supported"
    assert review.review_status == "ready"
    assert "traceability was restored" in review.issues[0].resolution
    assert "does not establish" in review.issues[0].resolution


def test_literal_quote_does_not_resolve_an_independent_semantic_reviewer_concern():
    review = CritiqueReport(review_status="blocked", issues=[CriticIssue(
        issue_id="reviewer_population", claim_id="c_market", severity="high",
        reason="This historical survey does not cover the proposed customer population.",
    )])
    final, _ = IntelligenceService._guard_final(report(PASSAGE), {"ev_real"}, [evidence()])
    IntelligenceService._resolve_critic(review, final, {"ev_real"}, 1, [evidence()])
    assert review.issues[0].resolution is None
    assert review.review_status == "blocked"


def test_model_cannot_label_a_semantic_concern_as_an_auto_resolvable_guard_issue():
    review = CritiqueReport(review_status="blocked", issues=[CriticIssue(
        issue_id="guard_quotation_fake", claim_id="c_market", severity="high",
        reason="The source population differs from the proposed users.",
    )])
    normalized = IntelligenceService._normalize_critic(review, report(PASSAGE), {"ev_real"}, [])
    assert normalized.issues[0].issue_id.startswith("critic_")
    IntelligenceService._resolve_critic(normalized, report(PASSAGE), {"ev_real"}, 1, [evidence()])
    assert normalized.issues[0].resolution is None
    assert normalized.review_status == "blocked"


def test_exact_issue_ids_recover_campus_review_claims_and_resolve_unknowns():
    initial = AnalysisReport(summary="Initial review", sections=[{
        "section_id": "validation", "heading": "Validation", "claims": [
            {"claim_id": f"cf_{index}", "text": f"Initial conclusion {index}.",
             "kind": "assumption", "evidence_ids": []}
            for index in range(1, 6)
        ],
    }])
    final = initial.model_copy(deep=True)
    final.gaps = ["Customer demand still requires a campus pilot."]
    for claim in final.sections[0].claims:
        claim.kind = "unknown"
        claim.text = "Customer validation remains unknown."
    review = CritiqueReport(review_status="blocked", issues=[
        CriticIssue(issue_id=f"cf_{index}", severity="high", reason="Validation is missing.")
        for index in range(1, 6)
    ], unresolved_gaps=["No direct market-size evidence was found."])
    original_review = review.model_dump()
    warnings = []

    normalized = IntelligenceService._normalize_critic(review, initial, set(), warnings)
    IntelligenceService._resolve_critic(normalized, final, set(), 1, [])

    assert [issue.claim_id for issue in normalized.issues] == [f"cf_{index}" for index in range(1, 6)]
    assert all(issue.resolution and "unknown" in issue.resolution for issue in normalized.issues)
    assert normalized.review_status == "ready_with_gaps"
    assert set(normalized.unresolved_gaps) >= {
        "Customer demand still requires a campus pilot.", "No direct market-size evidence was found.",
    }
    assert warnings
    assert review.model_dump() == original_review


def test_recovered_exact_claim_reference_does_not_resolve_semantic_quote_concern():
    initial = report("This startup's demand is validated worldwide.")
    review = CritiqueReport(review_status="blocked", issues=[CriticIssue(
        issue_id="c_market", severity="high",
        reason="A historical local survey does not validate this startup's current customers.",
    )])
    normalized = IntelligenceService._normalize_critic(review, initial, {"ev_real"}, [])
    final, _ = IntelligenceService._guard_final(report(PASSAGE), {"ev_real"}, [evidence()], normalized, initial)
    IntelligenceService._resolve_critic(normalized, final, {"ev_real"}, 1, [evidence()])

    assert normalized.issues[0].claim_id == "c_market"
    assert final.sections[0].claims[0].kind == "supported"
    assert normalized.issues[0].resolution is None
    assert normalized.review_status == "blocked"


def test_unmatched_general_high_review_issue_still_blocks_unknown_report():
    review = CritiqueReport(review_status="blocked", issues=[CriticIssue(
        issue_id="review_integrity", severity="high", reason="A general integrity concern remains unresolved.",
    )])
    final = report("Market demand remains unknown.", refs=[], kind="unknown")
    normalized = IntelligenceService._normalize_critic(review, final, set(), [])
    IntelligenceService._resolve_critic(normalized, final, set(), 1, [])

    assert normalized.issues[0].claim_id is None
    assert normalized.issues[0].resolution is None
    assert normalized.review_status == "blocked"


@pytest.mark.parametrize("issue_id,claim_id,expected", [
    ("c_market", "c_other", "c_other"),
    ("c_market", "missing_claim", None),
    ("c_market_suffix", None, None),
    ("C_MARKET", None, None),
    (" c_market", None, None),
])
def test_claim_reference_recovery_respects_explicit_ids_and_requires_exact_match(issue_id, claim_id, expected):
    initial = report(PASSAGE)
    initial.sections[0].claims.append(Claim(claim_id="c_other", text="Demand remains unknown.", kind="unknown"))
    review = CritiqueReport(review_status="blocked", issues=[CriticIssue(
        issue_id=issue_id, claim_id=claim_id, severity="high", reason="Review concern.",
    )])

    normalized = IntelligenceService._normalize_critic(review, initial, {"ev_real"}, [])

    assert normalized.issues[0].claim_id == expected


def test_exact_claim_reference_is_recovered_before_reserved_issue_id_is_renamed():
    initial = report(PASSAGE)
    initial.sections[0].claims[0].claim_id = "guard_quotation_market"
    review = CritiqueReport(review_status="blocked", issues=[CriticIssue(
        issue_id="guard_quotation_market", severity="high",
        reason="The source population differs from the proposed users.",
    )])

    normalized = IntelligenceService._normalize_critic(review, initial, {"ev_real"}, [])
    IntelligenceService._resolve_critic(normalized, initial, {"ev_real"}, 1, [evidence()])

    assert normalized.issues[0].claim_id == "guard_quotation_market"
    assert normalized.issues[0].issue_id.startswith("critic_")
    assert normalized.issues[0].resolution is None
    assert normalized.review_status == "blocked"
