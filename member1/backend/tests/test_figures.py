"""Quoted quantities must remain traceable through the shared context contract."""

import pytest
from pydantic import ValidationError

from app.modules.intelligence.contracts import (
    AnalysisReport,
    AnalysisResult,
    ContextDraft,
    CritiqueReport,
    EvidenceItem,
    ExecutionInfo,
    IdeaSpec,
    ResearchOptions,
    Source,
)
from app.modules.intelligence.figures import extract_figures


def source(source_id="source1", kind="fixture"):
    return Source(source_id=source_id, title="Synthetic exercise", url=f"https://example.org/{source_id}", kind=kind)


def evidence(excerpt, evidence_id="evidence1", source_id="source1"):
    return EvidenceItem(evidence_id=evidence_id, source_id=source_id, excerpt=excerpt)


@pytest.fixture
def valid_context():
    material = evidence("Synthetic sample: 12 simulated interview records include 7 simulated respondents. The invented budget is INR 5000.")
    sources = [source()]
    analysis = AnalysisReport(summary="This is a teaching exercise with unverified market demand.", sections=[])
    return ContextDraft(
        idea=IdeaSpec(original_text="An app to evaluate a student startup idea.", project_name="Student app",
                      problem="Unvalidated assumptions", solution="Test a prototype", domain="Education"),
        sources=sources,
        evidence=[material],
        initial_analysis=analysis,
        final_analysis=analysis,
        critic=CritiqueReport(review_status="ready_with_gaps", unresolved_gaps=["No live market data."]),
        execution=ExecutionInfo(mode="demo", llm_provider="demo", search_provider="fixture", retrieval_method="keyword"),
        figures=extract_figures([material], sources),
    )


def test_simulated_statistics_and_currency_preserve_exact_quotations():
    first = "Synthetic sample: 12 simulated interview records include 7 simulated respondents."
    second = "The invented pilot budget is INR5000."
    material = evidence(first + " " + second)
    figures = extract_figures([material], [source()])
    assert [figure.display_value for figure in figures] == [
        "12 simulated interview records", "7 simulated respondents", "INR5000",
    ]
    assert [figure.excerpt for figure in figures] == [first, first, second]
    assert all(figure.evidence_id == material.evidence_id for figure in figures)
    assert all(figure.source_id == material.source_id for figure in figures)
    assert all(figure.display_value in figure.excerpt for figure in figures)
    assert all("Synthetic" in figure.qualification and "not real market data" in figure.qualification for figure in figures)


@pytest.mark.parametrize("value", [
    "14.5%", "25 percent", "USD 1.25 billion", "INR 5,000", "₹5000", "$10 million",
    "100 active users", "40 paying customers", "30 surveyed participants", "8 households",
])
def test_percentages_currencies_and_population_counts_are_quoted_exactly(value):
    text = f"The source reports {value} within its defined study scope."
    figures = extract_figures([evidence(text)], [source(kind="web")])
    assert len(figures) == 1
    assert figures[0].display_value == value
    assert figures[0].excerpt == text
    assert "date, geography and methodology" in figures[0].qualification
    assert "Synthetic" not in figures[0].qualification


@pytest.mark.parametrize("value", ["-12 users", "INR -5000"])
def test_negative_quantities_keep_the_source_sign_and_never_become_positive_quotes(value):
    text = f"The hypothetical net change is {value}."
    figures = extract_figures([evidence(text)], [source()])
    assert len(figures) == 1
    assert figures[0].display_value == value
    assert "-" in figures[0].display_value
    assert figures[0].excerpt == text


def test_overlapping_evidence_deduplicates_same_quantity_and_sentence():
    text = "Synthetic data includes 12 respondents."
    excerpts = [evidence(text, "first"), evidence(text, "overlapping")]
    first = extract_figures(excerpts, [source()])
    assert len(first) == 1
    assert first[0].evidence_id == "first"
    assert first == extract_figures(excerpts, [source()])


def test_different_sources_keep_separate_unique_figure_ids():
    text = "A study reports 50 percent."
    excerpts = [evidence(text, "first", "source1"), evidence(text, "second", "source2")]
    figures = extract_figures(excerpts, [source("source1", "web"), source("source2", "web")])
    assert len(figures) == 2
    assert len({figure.figure_id for figure in figures}) == 2
    assert {(figure.source_id, figure.evidence_id) for figure in figures} == {("source1", "first"), ("source2", "second")}


def test_missing_source_and_plain_numbers_do_not_produce_unlinked_figures():
    excerpts = [
        evidence("The exercise has 12 respondents.", "missing", "unknown-source"),
        evidence("Software version 1.0 was mentioned on page 20 in 2026.", "version"),
    ]
    assert extract_figures(excerpts, [source()]) == []
    assert extract_figures([], [source()]) == []


def test_figure_extraction_is_bounded_to_24_traceable_items():
    text = " ".join(f"Study {index} reports {index + 1} percent." for index in range(40))
    figures = extract_figures([evidence(text)], [source(kind="web")])
    assert len(figures) == 24
    assert len({figure.figure_id for figure in figures}) == 24
    assert all(figure.excerpt in text for figure in figures)


def test_context_figure_json_roundtrip_retains_all_source_and_evidence_references(valid_context):
    roundtrip = ContextDraft.model_validate_json(valid_context.model_dump_json())
    assert roundtrip.figures == valid_context.figures
    assert len(roundtrip.figures) == 3
    assert all(figure.source_id == roundtrip.evidence[0].source_id for figure in roundtrip.figures)


@pytest.mark.parametrize("change,reason", [
    ({"source_id": "missing-source"}, "unknown evidence or source"),
    ({"evidence_id": "missing-evidence"}, "unknown evidence or source"),
    ({"excerpt": "12 simulated interview records from a different source."}, "quoted from its evidence"),
    ({"display_value": "500 invented respondents"}, "quoted from its evidence"),
    ({"excerpt": ""}, "quoted from its evidence"),
    ({"display_value": ""}, "quoted from its evidence"),
])
def test_context_rejects_broken_or_fabricated_figure_lineage(valid_context, change, reason):
    payload = valid_context.model_dump()
    payload["figures"][0].update(change)
    with pytest.raises(ValidationError, match=reason):
        ContextDraft.model_validate(payload)


def test_context_rejects_cross_source_evidence_even_when_both_sources_exist(valid_context):
    payload = valid_context.model_dump()
    payload["sources"].append(source("other-source").model_dump())
    payload["figures"][0]["source_id"] = "other-source"
    with pytest.raises(ValidationError, match="source must match its cited evidence"):
        ContextDraft.model_validate(payload)


def test_context_rejects_duplicate_figure_ids(valid_context):
    payload = valid_context.model_dump()
    payload["figures"][1]["figure_id"] = payload["figures"][0]["figure_id"]
    with pytest.raises(ValidationError, match="Figure IDs must be unique"):
        ContextDraft.model_validate(payload)


def test_analysis_result_figures_must_equal_shared_context_figures(valid_context):
    result = AnalysisResult(
        analysis_id="analysis1", project_id="project1", context_draft=valid_context,
        stages=["research", "retrieval", "analysis", "critic"], figures=valid_context.figures,
    )
    assert result.figures == result.context_draft.figures
    payload = result.model_dump()
    payload["figures"] = []
    with pytest.raises(ValidationError, match="Analysis figures must match the context draft"):
        AnalysisResult.model_validate(payload)


def solar_idea():
    return IdeaSpec(original_text='Compare rooftop solar quotations for Indian factory owners.',
                    project_name='Solar Rooftop', problem='Owners need comparable installations.',
                    solution='Compare rooftop solar installation quotes for factory owners.', domain='Energy')


@pytest.mark.parametrize('value', ['15 GW', '12.9 MW', '3kW', '100 installations', '1,000 businesses'])
def test_business_units_and_counts_keep_exact_quoted_values(value):
    figures = extract_figures([evidence(f'The rooftop solar report describes {value}.')], [source(kind='web')])
    assert [figure.display_value for figure in figures] == [value]


@pytest.mark.parametrize('value', ['2–5 kW', '10-20 MW', '2 kW–5 kW', '$10–$20 million', '15 to 20 percent', '45 per cent',
                                  'between 2 and 5 kW', '2 MW to 5 MW', 'between $10 and $20 million',
                                  'between 2 MW and 5 MW', '10 percent–20%', 'INR 500–₹1,000'])
def test_ranges_and_per_cent_do_not_become_a_single_range_endpoint(value):
    text = f'The rooftop solar market report describes {value} within its defined scope.'
    figures = extract_figures([evidence(text)], [source(kind='web')], idea=solar_idea())
    assert [figure.display_value for figure in figures] == [value]
    assert figures[0].excerpt == text


@pytest.mark.parametrize('sentence,values', [
    ('Rooftop solar costs $10 million and 20 percent of costs are taxes.', ['$10 million', '20 percent']),
    ('Rooftop solar has 10 percent and 20 users.', ['10 percent', '20 users']),
])
def test_independent_and_quantities_never_become_a_mixed_unit_range(sentence, values):
    figures = extract_figures([evidence(sentence)], [source(kind='web')], idea=solar_idea())
    assert [figure.display_value for figure in figures] == values
    assert all(figure.excerpt == sentence for figure in figures)


@pytest.mark.parametrize('value', ['$10 million–20 percent', '$10–€20 million', '10 percent–20 users',
                                  'between 10 percent and 20 users', '2 kW–5 GW', '2 tons to 5 kW',
                                  '2 kW to 5 tons'])
def test_incompatible_or_unsupported_ranges_never_become_lone_endpoints(value):
    sentence = f'Rooftop solar reports {value} in its source scope.'
    assert extract_figures([evidence(sentence)], [source(kind='web')], idea=solar_idea()) == []


def test_indian_rupee_abbreviation_does_not_split_the_quantity_sentence():
    sentence = 'Rooftop solar costs Rs. 500 per installation.'
    figures = extract_figures([evidence(sentence)], [source(kind='web')], idea=solar_idea())
    assert [figure.display_value for figure in figures] == ['Rs. 500']
    assert figures[0].excerpt == sentence


def test_reviewed_topic_omits_unrelated_statistics_in_same_relevant_passage():
    text = ('India rooftop solar installations reached 15 GW in 2025. '
            'A broad education initiative enrolled 80 million students. '
            'A healthcare project received USD 75 million.')
    figures = extract_figures([evidence(text)], [source(kind='web')], idea=solar_idea(), options=ResearchOptions(region='India'))
    assert [figure.display_value for figure in figures] == ['15 GW']
    assert '2025' in figures[0].qualification and 'India' in figures[0].qualification
    assert 'not a calculated TAM/SAM/SOM' in figures[0].qualification


def test_direct_topic_sentence_retains_its_quantity_without_inheriting_other_sentence():
    text = 'A broad report reviewed infrastructure. Rooftop solar reached 15 GW in India in 2025.'
    figures = extract_figures([evidence(text)], [source(kind='web')], idea=solar_idea(), options=ResearchOptions(region='India'))
    assert len(figures) == 1 and figures[0].excerpt == 'Rooftop solar reached 15 GW in India in 2025.'
    assert figures[0].display_value == '15 GW'


@pytest.mark.parametrize('continuation', ['These students numbered 80 million.',
                                        'This healthcare project received USD 75 million.',
                                        'These systems reached 15 GW in 2025.'])
def test_pronoun_continuation_cannot_inherit_topic_from_previous_sentence(continuation):
    text = 'Rooftop solar installations increased. ' + continuation
    assert extract_figures([evidence(text)], [source(kind='web')], idea=solar_idea()) == []


def test_following_paragraph_cannot_inherit_topic_silently():
    text = 'Rooftop solar installations increased.\n\nThese students numbered 80 million.'
    assert not extract_figures([evidence(text)], [source(kind='web')], idea=solar_idea())


def test_quote_scope_unknowns_and_forecasts_are_explicit_without_invented_dates():
    text = 'Rooftop solar systems could reach 15 GW.'
    figures = extract_figures([evidence(text)], [source(kind='web')], idea=solar_idea(), options=ResearchOptions(region='India'))
    qualification = figures[0].qualification
    assert 'Measurement period is not stated' in qualification
    assert 'Geography is not stated' in qualification
    assert 'does not establish applicability' in qualification
    assert 'prospective claim' in qualification


def test_target_customer_check_is_a_caution_not_a_claim_of_applicability():
    structured = solar_idea()
    structured.target_users = ['Indian factory owners']
    figures = extract_figures([evidence('India residential rooftop solar reached 15 GW in 2025.')],
                              [source(kind='web')], idea=structured)
    assert 'Check quoted population against intended customers: Indian factory owners.' in figures[0].qualification
