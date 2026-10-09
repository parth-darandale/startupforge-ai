from app.modules.intelligence.business_evidence import filter_business_documents, filter_business_evidence
from app.modules.intelligence.contracts import EvidenceItem, IdeaSpec, ResearchDocument, ResearchOptions, Source


def idea(solution='A marketplace for textbook resale among university students in India.'):
    return IdeaSpec(original_text='Students buy and sell used textbooks.', project_name='CampusLoop',
                    problem='Textbooks are expensive.', solution=solution, domain='Education', target_users=['Students in India'])


def document(content, title='Report', kind='web'):
    return ResearchDocument(source=Source(source_id=title, title=title, url='https://example.org/report', kind=kind), content=content)


def test_region_audience_domain_and_feed_title_cannot_qualify_unrelated_source():
    docs = [document('India education funding supports students and university enrolment with 80 million children.', 'Textbook resale India')]
    retained, warnings, gaps = filter_business_documents(docs, idea(), ResearchOptions(region='India'))
    assert retained == [] and warnings and gaps


def test_downloaded_metadata_title_alone_cannot_qualify_unrelated_body():
    doc = document('Source page title: Textbook resale\nPage-declared publication date: 2025-02-01\n\nA broad education report describes student populations.')
    assert not filter_business_documents([doc], idea(), ResearchOptions())[0]


def test_topic_documents_and_only_relevant_passages_are_retained_with_exact_ids():
    doc = document('Textbook resale involves matching secondhand coursebooks to course requirements.')
    assert filter_business_documents([doc], idea(), ResearchOptions())[0] == [doc]
    relevant = EvidenceItem(evidence_id='keep', source_id='report', excerpt=doc.content)
    irrelevant = EvidenceItem(evidence_id='drop', source_id='report', excerpt='India enrolled 80 million students in higher education.')
    retained, warnings, gaps = filter_business_evidence([irrelevant, relevant], idea(), ResearchOptions())
    assert retained == [relevant] and retained[0].evidence_id == 'keep' and warnings and gaps


def test_specific_solar_solution_does_not_retain_only_broad_solar_word():
    structured = idea('Compare rooftop solar installation quotes for small factory owners.')
    docs = [document('Solar investment is popular among consumers.', 'Broad solar'),
            document('Rooftop solar installation is increasing at industrial factories.', 'Specific rooftop')]
    assert [d.source.title for d in filter_business_documents(docs, structured, ResearchOptions())[0]] == ['Specific rooftop']


def test_topic_phrase_in_project_name_does_not_erase_solution_terms():
    structured = idea('Compare rooftop solar installation quotes for factory owners.')
    structured.project_name = 'Solar Rooftop'
    doc = document('Industrial factories compare rooftop solar installation options.')
    assert filter_business_documents([doc], structured, ResearchOptions())[0] == [doc]


def test_fixtures_do_not_receive_live_topic_filtering_and_empty_topic_is_explicit():
    doc = document('Synthetic demonstration statistics.', kind='fixture')
    assert filter_business_documents([doc], idea(), ResearchOptions())[0] == [doc]
    structured = idea('An app to help people connect online.')
    docs, warnings, gaps = filter_business_documents([document('People connect online.')], structured, ResearchOptions())
    assert not docs and any('distinctive research topic' in gap for gap in gaps)
