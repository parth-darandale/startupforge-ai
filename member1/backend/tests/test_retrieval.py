import sys

import pytest

from app.modules.intelligence.contracts import ResearchDocument, Source
from app.modules.intelligence.errors import IntelligenceError
from app.modules.intelligence.retrieval import FastEmbedRetriever, KeywordRetriever, chunk_documents, make_retriever
from app.modules.intelligence.settings import Settings


def document(source_id, content):
    return ResearchDocument(source=Source(source_id=source_id, title=source_id, url=f"https://example.org/{source_id}"), content=content)


def test_chunks_preserve_original_text_and_word_overlap():
    text = "\n".join("word" + str(i) for i in range(700))
    chunks = chunk_documents([document("source1", text)])
    assert [len(chunk.text.split()) for chunk in chunks] == [350, 350, 100]
    assert chunks[0].text.split()[-50:] == chunks[1].text.split()[:50]
    assert chunks[0].text in text
    assert chunks[1].locator == "words 301-650"
    assert all(chunk.source_id == "source1" for chunk in chunks)


def test_evidence_ids_are_stable_unique_and_duplicates_are_removed():
    first = document("source1", "customer research " * 600)
    second = document("source2", first.content)
    chunks = chunk_documents([first, second, first])
    assert len({chunk.evidence_id for chunk in chunks}) == len(chunks)
    assert chunks == chunk_documents([first, second])
    assert len({chunk.source_id for chunk in chunks}) == 2
    modified = chunk_documents([document("source1", "different content")])
    assert modified[0].evidence_id != chunks[0].evidence_id


@pytest.mark.asyncio
async def test_keyword_retrieval_selects_relevant_sources_and_preserves_citations():
    docs = [
        document("customers", "Customer discovery research interviews validate product assumptions."),
        document("irrelevant", "Chocolate cake recipes cocoa butter flour baking oven."),
        document("prototype", "Product prototype research tests with customer interviews."),
    ]
    evidence = await KeywordRetriever().retrieve(docs, "customer interviews product", limit=2)
    assert len(evidence) == 2
    assert {item.source_id for item in evidence} == {"customers", "prototype"}
    assert all(item.excerpt in next(doc.content for doc in docs if doc.source.source_id == item.source_id) for item in evidence)
    assert all(0 < item.relevance_score <= 1 for item in evidence)
    assert all(item.locator.startswith("words ") for item in evidence)


@pytest.mark.asyncio
async def test_retrieval_caps_evidence_and_prioritizes_source_diversity():
    docs = [document("large", "product research customer " * 2200), document("small", "product research customer")]
    evidence = await KeywordRetriever().retrieve(docs, "product research customer", limit=100)
    assert len(evidence) == 12
    assert len({item.evidence_id for item in evidence}) == 12
    assert "small" in {item.source_id for item in evidence}
    assert len(await KeywordRetriever().retrieve(docs, "product research customer", limit=1)) == 1


@pytest.mark.asyncio
async def test_empty_and_no_overlap_produce_no_fake_evidence():
    retriever = KeywordRetriever()
    assert await retriever.retrieve([], "customer") == []
    assert await retriever.retrieve([document("s", "   ")], "customer") == []
    assert await retriever.retrieve([document("s", "cake recipes")], "customer") == []
    assert await retriever.retrieve([document("s", "customer")], "the and") == []
    assert await retriever.retrieve([document("s", "customer")], "customer", limit=0) == []


@pytest.mark.asyncio
async def test_fastembed_adapter_ranks_real_model_vectors_without_optional_dependency():
    class ModelStub:
        def embed(self, texts):
            assert texts == ["customer", "customer interviews", "baking cakes"]
            return iter([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]])

    retriever = FastEmbedRetriever(Settings())
    retriever._model = ModelStub()
    evidence = await retriever.retrieve([document("customer", "customer interviews"), document("cake", "baking cakes")], "customer")
    assert retriever.method == "semantic_fastembed"
    assert len(evidence) == 1
    assert evidence[0].source_id == "customer"
    assert evidence[0].relevance_score > 0.99


@pytest.mark.asyncio
async def test_missing_fastembed_has_actionable_configuration_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)
    with pytest.raises(IntelligenceError) as error:
        await FastEmbedRetriever(Settings()).retrieve([document("s", "customer research")], "customer")
    assert error.value.code == "EMBEDDINGS_NOT_INSTALLED"
    assert error.value.status_code == 503


def test_retrieval_factory_labels_keyword_honestly():
    assert make_retriever(Settings()).method == "keyword"
    assert make_retriever(Settings(retrieval_provider="fastembed")).method == "semantic_fastembed"
    with pytest.raises(IntelligenceError):
        make_retriever(Settings(retrieval_provider="unknown"))
