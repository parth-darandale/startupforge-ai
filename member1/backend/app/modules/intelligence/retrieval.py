"""Chunk and rank research text with traceable evidence IDs.

The default is lexical TF-IDF cosine retrieval, labelled `keyword`. FastEmbed is
an optional real semantic embedding backend, loaded only when explicitly selected.
The bounded request corpus is ranked in memory; it is not a persistent vector DB.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from .contracts import EvidenceItem, ResearchDocument
from .errors import IntelligenceError
from .settings import Settings

CHUNK_WORDS = 350
OVERLAP_WORDS = 50
MAX_EVIDENCE = 12
STOPWORDS = frozenset("a an and are as at be been but by can for from has have in is it its of on or that the their this to was were will with".split())


class Retriever(Protocol):
    method: str

    async def retrieve(self, documents: list[ResearchDocument], query: str,
                       limit: int = MAX_EVIDENCE) -> list[EvidenceItem]: ...


@dataclass(frozen=True)
class TextChunk:
    evidence_id: str
    source_id: str
    text: str
    locator: str


def chunk_documents(documents: list[ResearchDocument]) -> list[TextChunk]:
    chunks: dict[str, TextChunk] = {}
    for document in documents:
        # Span slicing preserves punctuation and line breaks from source text.
        words = list(re.finditer(r"\S+", document.content))
        for start in range(0, len(words), CHUNK_WORDS - OVERLAP_WORDS):
            end = min(start + CHUNK_WORDS, len(words))
            text = document.content[words[start].start():words[end - 1].end()]
            identifier = f"{document.source.source_id}\0{start}\0{text}"
            evidence_id = "ev_" + hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:20]
            chunks[evidence_id] = TextChunk(
                evidence_id, document.source.source_id, text, f"words {start + 1}-{end}"
            )
            if end == len(words):
                break
    return list(chunks.values())


def _tokens(text: str) -> list[str]:
    return [word for word in re.findall(r"\b\w{2,}\b", text.lower()) if word not in STOPWORDS]


def _select(chunks: list[TextChunk], scores: list[float], limit: int) -> list[EvidenceItem]:
    limit = min(MAX_EVIDENCE, max(0, limit))
    ranked = sorted(
        ((chunk, score) for chunk, score in zip(chunks, scores) if math.isfinite(score) and score > 0),
        key=lambda item: (-item[1], item[0].evidence_id),
    )
    selected: list[tuple[TextChunk, float]] = []
    sources: set[str] = set()
    # First include the highest ranked relevant chunk from each source to avoid
    # filling the entire evidence budget with overlapping excerpts from one page.
    for chunk, score in ranked:
        if len(selected) >= limit:
            break
        if chunk.source_id not in sources:
            selected.append((chunk, score))
            sources.add(chunk.source_id)
    selected_ids = {chunk.evidence_id for chunk, _ in selected}
    for chunk, score in ranked:
        if len(selected) >= limit:
            break
        if chunk.evidence_id not in selected_ids:
            selected.append((chunk, score))
            selected_ids.add(chunk.evidence_id)
    selected.sort(key=lambda item: (-item[1], item[0].evidence_id))
    return [
        EvidenceItem(evidence_id=chunk.evidence_id, source_id=chunk.source_id,
                     excerpt=chunk.text, locator=chunk.locator, relevance_score=round(score, 6))
        for chunk, score in selected
    ]


class KeywordRetriever:
    method = "keyword"

    async def retrieve(self, documents: list[ResearchDocument], query: str,
                       limit: int = MAX_EVIDENCE) -> list[EvidenceItem]:
        chunks = chunk_documents(documents)
        query_counts = Counter(_tokens(query))
        if not chunks or not query_counts or limit <= 0:
            return []
        counts = [Counter(_tokens(chunk.text)) for chunk in chunks]
        document_frequency: Counter = Counter()
        for count in counts:
            document_frequency.update(count.keys())
        idf = {term: math.log((1 + len(chunks)) / (1 + frequency)) + 1
               for term, frequency in document_frequency.items()}
        query_vector = {term: count * idf.get(term, math.log(1 + len(chunks)) + 1)
                        for term, count in query_counts.items()}
        query_norm = math.sqrt(sum(weight * weight for weight in query_vector.values()))
        scores = []
        for count in counts:
            vector = {term: frequency * idf[term] for term, frequency in count.items()}
            norm = math.sqrt(sum(weight * weight for weight in vector.values()))
            numerator = sum(weight * vector.get(term, 0.0) for term, weight in query_vector.items())
            scores.append(numerator / (query_norm * norm) if norm else 0.0)
        return _select(chunks, scores, limit)


class FastEmbedRetriever:
    method = "semantic_fastembed"

    def __init__(self, settings: Settings):
        if not settings.embedding_model.strip():
            raise IntelligenceError("PROVIDER_CONFIGURATION", "Set EMBEDDING_MODEL before using FastEmbed.", status_code=503)
        self.settings = settings
        self._model = None
        self._lock = threading.Lock()

    def _scores(self, chunks: list[TextChunk], query: str) -> list[float]:
        with self._lock:
            if self._model is None:
                try:
                    from fastembed import TextEmbedding
                except ImportError:
                    raise IntelligenceError(
                        "EMBEDDINGS_NOT_INSTALLED", "Install requirements-semantic.txt to enable FastEmbed.", status_code=503
                    ) from None
                self._model = TextEmbedding(model_name=self.settings.embedding_model, threads=2)
            vectors = list(self._model.embed([query, *(chunk.text for chunk in chunks)]))
        if len(vectors) != len(chunks) + 1:
            raise IntelligenceError("EMBEDDINGS_INVALID_RESPONSE", "The embedding model returned an invalid result.")
        query_vector = vectors[0]
        query_norm = math.sqrt(sum(float(value) ** 2 for value in query_vector))
        scores: list[float] = []
        for vector in vectors[1:]:
            if len(vector) != len(query_vector):
                raise IntelligenceError("EMBEDDINGS_INVALID_RESPONSE", "The embedding vectors have different sizes.")
            norm = math.sqrt(sum(float(value) ** 2 for value in vector))
            dot = sum(float(left) * float(right) for left, right in zip(query_vector, vector))
            scores.append(dot / (query_norm * norm) if query_norm and norm else 0.0)
        return scores

    async def retrieve(self, documents: list[ResearchDocument], query: str,
                       limit: int = MAX_EVIDENCE) -> list[EvidenceItem]:
        chunks = chunk_documents(documents)
        if not chunks or not query.strip() or limit <= 0:
            return []
        try:
            scores = await asyncio.wait_for(
                asyncio.to_thread(self._scores, chunks, query), timeout=self.settings.provider_timeout
            )
        except asyncio.TimeoutError:
            raise IntelligenceError("EMBEDDINGS_TIMEOUT", "The embedding model timed out. Its first run may need to download model weights.", retryable=True) from None
        except IntelligenceError:
            raise
        except Exception:
            raise IntelligenceError("EMBEDDINGS_UNAVAILABLE", "The embedding model could not be loaded or run. Check its name and local runtime.", status_code=503) from None
        return _select(chunks, scores, limit)


def make_retriever(settings: Settings) -> Retriever:
    if settings.retrieval_provider == "keyword":
        return KeywordRetriever()
    if settings.retrieval_provider == "fastembed":
        return FastEmbedRetriever(settings)
    raise IntelligenceError("PROVIDER_CONFIGURATION", "RETRIEVAL_PROVIDER must be keyword or fastembed.", status_code=503)
