"""Standalone retrieval-augmented regulatory analysis.

This module is deliberately separate from Streamlit. It retrieves evidence from
the existing read-only SQLite corpus, asks an analyst model for a structured
assessment, asks a critic model to verify the assessment against the retrieved
evidence, and applies a transparent risk rubric. The default retriever is a
dependency-free lexical retriever so the prototype can run without a vector
database; the retrieved chunks are still suitable for replacing with embeddings
later.

Example (from the repository root)::

    $env:PYTHONPATH = "$PWD/src"
    python -m virtual_asset_agent.rag --db data/ingestion/hkma.sqlite \
        --question "What are the stablecoin reserve obligations?"

The module does not import Streamlit and does not write to the ingestion DB.
Embeddings are held in memory for the current process; a production vector
store can be added later without changing the tool contracts.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .agent_workflow import _structured_prompt

try:
    from langchain_core.documents import Document
    from langchain_core.embeddings import Embeddings
    from langchain_core.tools import StructuredTool, tool
except ImportError as exc:  # pragma: no cover - dependency is optional at import time
    Document = None
    Embeddings = Any
    StructuredTool = None
    tool = None
    _LANGCHAIN_IMPORT_ERROR = exc
else:
    _LANGCHAIN_IMPORT_ERROR = None

try:
    from langchain_ollama import OllamaEmbeddings
except ImportError:  # pragma: no cover - optional provider
    OllamaEmbeddings = None

try:
    from langchain_openai import OpenAIEmbeddings
except ImportError:  # pragma: no cover - optional provider
    OpenAIEmbeddings = None


RiskRating = Literal['low', 'medium', 'high', 'critical', 'unknown']


@dataclass(frozen=True)
class Evidence:
    """A retrieved chunk with enough provenance to support an answer."""

    chunk_id: str
    document_id: str
    text: str
    title: str
    jurisdiction: str
    regulator: str
    source_url: str
    page_start: int | None
    section: str | None
    score: float

    @property
    def citation(self) -> str:
        location = f", p. {self.page_start}" if self.page_start else ''
        return f"{self.title} ({self.jurisdiction}, {self.regulator}{location}) [{self.chunk_id}]"


class AnalystAssessment(BaseModel):
    executive_summary: str = Field(description='Two to four sentences grounded only in the evidence.')
    key_findings: list[str] = Field(default_factory=list)
    impact_tags: list[str] = Field(default_factory=list)
    affected_actors: list[str] = Field(default_factory=list)
    obligations: list[str] = Field(default_factory=list)
    risk_factors: list[str] = Field(default_factory=list)
    risk_rating: RiskRating = 'unknown'
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_chunk_ids: list[str] = Field(default_factory=list)


class CriticAssessment(BaseModel):
    claim_status: Literal['supported', 'partly_supported', 'unsupported', 'insufficient_evidence'] = 'insufficient_evidence'
    unsupported_claims: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    citation_errors: list[str] = Field(default_factory=list)
    corrections: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class RiskAssessment(BaseModel):
    rating: RiskRating
    score: int = Field(ge=0, le=100)
    dimensions: dict[str, int] = Field(default_factory=dict)
    rationale: list[str] = Field(default_factory=list)
    escalation_triggers: list[str] = Field(default_factory=list)


class RAGResult(BaseModel):
    question: str
    evidence: list[dict[str, Any]]
    analyst: AnalystAssessment
    critic: CriticAssessment
    risk: RiskAssessment


def build_embeddings() -> Embeddings:
    """Build a LangChain embedding model from the existing AI settings.

    ``AI_EMBEDDING_PROVIDER`` may be ``ollama`` or ``openai_compatible``.
    OpenAI-compatible endpoints must expose an embeddings API.
    """
    if _LANGCHAIN_IMPORT_ERROR is not None:
        raise ImportError('Install langchain-core and a LangChain embedding provider.') from _LANGCHAIN_IMPORT_ERROR
    from .agent_workflow import get_agent_settings

    settings = get_agent_settings()
    provider = os.getenv('AI_EMBEDDING_PROVIDER', settings['provider']).strip().lower()
    model = os.getenv('AI_EMBEDDING_MODEL', 'nomic-embed-text')
    if provider == 'ollama':
        if OllamaEmbeddings is None:
            raise ImportError('Install langchain-ollama to use Ollama embeddings.')
        base_url = os.getenv('OLLAMA_BASE_URL', 'http://localhost:11434')
        return OllamaEmbeddings(model=model, base_url=base_url)
    if provider == 'openai_compatible':
        if OpenAIEmbeddings is None:
            raise ImportError('Install langchain-openai to use OpenAI-compatible embeddings.')
        return OpenAIEmbeddings(
            model=model,
            base_url=settings['openai_base_url'],
            api_key=settings['openai_api_key'],
        )
    raise ValueError("AI_EMBEDDING_PROVIDER must be 'ollama' or 'openai_compatible'")


def _tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9_-]{1,}", value.casefold())


def retrieve(db_path: str | Path, question: str, limit: int = 6, jurisdiction: str | None = None) -> list[Evidence]:
    """Retrieve chunks with deterministic BM25-like lexical scoring.

    This is intentionally conservative: it ranks evidence but never treats a
    keyword match as a confidence or legal-risk score.
    """
    if not question or not question.strip():
        raise ValueError('Question must not be empty')
    if limit < 1 or limit > 50:
        raise ValueError('limit must be between 1 and 50')
    query_terms = Counter(_tokens(question))
    if not query_terms:
        return []
    with sqlite3.connect(Path(db_path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT c.chunk_id, d.document_id, c.text, e.title, s.jurisdiction,
                      s.regulator, d.source_url, c.page_start, c.section
                 FROM chunks c
                 JOIN extractions e ON e.extraction_id = c.extraction_id
                 JOIN document_versions v ON v.version_id = e.version_id
                 JOIN documents d ON d.document_id = v.document_id
                 JOIN sources s ON s.source_id = d.source_id
                WHERE (? IS NULL OR s.jurisdiction = ?)
                ORDER BY e.published_at DESC, c.ordinal ASC""",
            (jurisdiction, jurisdiction),
        ).fetchall()
    scored: list[Evidence] = []
    for row in rows:
        terms = Counter(_tokens(row['text']))
        score = sum(min(3, terms[term]) * weight for term, weight in query_terms.items())
        if score <= 0:
            continue
        scored.append(Evidence(**dict(row), score=float(score)))
    return sorted(scored, key=lambda item: (-item.score, item.document_id, item.chunk_id))[:limit]


def load_evidence(db_path: str | Path, jurisdiction: str | None = None) -> list[Evidence]:
    """Load all ready evidence rows for embedding without ranking them."""
    with sqlite3.connect(Path(db_path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT c.chunk_id, d.document_id, c.text, e.title, s.jurisdiction,
                      s.regulator, d.source_url, c.page_start, c.section
                 FROM chunks c
                 JOIN extractions e ON e.extraction_id = c.extraction_id
                 JOIN document_versions v ON v.version_id = e.version_id
                 JOIN documents d ON d.document_id = v.document_id
                 JOIN sources s ON s.source_id = d.source_id
                WHERE (? IS NULL OR s.jurisdiction = ?)
                ORDER BY e.published_at DESC, c.ordinal ASC""",
            (jurisdiction, jurisdiction),
        ).fetchall()
    return [Evidence(**dict(row), score=0.0) for row in rows]


class InMemoryVectorIndex:
    """Small LangChain-compatible embedding index for RAG experiments.

    It embeds each corpus chunk once, retrieves a broad candidate set by cosine
    similarity, then reranks candidates using both semantic similarity and the
    lexical score. It intentionally does not persist vectors or modify SQLite.
    """

    def __init__(self, evidence: list[Evidence], embeddings: Embeddings):
        if _LANGCHAIN_IMPORT_ERROR is not None or Document is None:
            raise ImportError('Install langchain-core before creating the RAG index.') from _LANGCHAIN_IMPORT_ERROR
        if not evidence:
            raise ValueError('Cannot index an empty evidence set')
        self.evidence = evidence
        self.embeddings = embeddings
        self.documents = [Document(page_content=item.text, metadata=asdict(item)) for item in evidence]
        self.vectors = embeddings.embed_documents([document.page_content for document in self.documents])

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
        return sum(a * b for a, b in zip(left, right)) / denominator if denominator else 0.0

    def search(self, question: str, limit: int = 6, candidate_limit: int = 20) -> list[Evidence]:
        if not question.strip():
            raise ValueError('Question must not be empty')
        if limit < 1 or limit > candidate_limit:
            raise ValueError('limit must be between 1 and candidate_limit')
        query_vector = self.embeddings.embed_query(question)
        semantic = [(self._cosine(query_vector, vector), index) for index, vector in enumerate(self.vectors)]
        candidates = sorted(semantic, reverse=True)[:candidate_limit]
        lexical_terms = Counter(_tokens(question))
        reranked = []
        for semantic_score, index in candidates:
            item = self.evidence[index]
            term_counts = Counter(_tokens(item.text))
            lexical_score = sum(min(3, term_counts[term]) * weight for term, weight in lexical_terms.items())
            combined_score = (0.75 * semantic_score) + (0.25 * min(1.0, lexical_score / 10.0))
            reranked.append((combined_score, item))
        return [Evidence(**{**asdict(item), 'score': score})
                for score, item in sorted(reranked, key=lambda pair: (-pair[0], pair[1].chunk_id))[:limit]]


def build_index(db_path: str | Path, embeddings: Embeddings | None = None,
                jurisdiction: str | None = None) -> InMemoryVectorIndex:
    """Load corpus evidence and create an in-memory embedding index."""
    if embeddings is None:
        embeddings = build_embeddings()
    evidence = load_evidence(db_path, jurisdiction=jurisdiction)
    if not evidence:
        raise ValueError('No evidence found to embed in the selected corpus')
    return InMemoryVectorIndex(evidence, embeddings)


def create_rag_tools(index: InMemoryVectorIndex) -> list[StructuredTool]:
    """Expose retrieval and reranking as LangChain tools for an agent."""
    if _LANGCHAIN_IMPORT_ERROR is not None or tool is None:
        raise ImportError('Install langchain-core to create RAG tools.') from _LANGCHAIN_IMPORT_ERROR

    @tool
    def retrieve_regulatory_evidence(question: str, limit: int = 6) -> str:
        """Retrieve and rerank source chunks for a regulatory question."""
        results = index.search(question, limit=limit)
        return json.dumps([asdict(item) | {'citation': item.citation} for item in results], ensure_ascii=False)

    return [retrieve_regulatory_evidence]


def _evidence_text(evidence: list[Evidence]) -> str:
    return '\n\n'.join(f"[{item.chunk_id}] {item.citation}\n{item.text}" for item in evidence)


def evaluate_risk(analyst: AnalystAssessment, evidence: list[Evidence]) -> RiskAssessment:
    """Apply an auditable 0-100 regulatory-impact rubric.

    Dimensions are scored independently: legal force (0-30), operational
    burden (0-25), scope/exposure (0-20), enforcement proximity (0-15), and
    evidence quality (0-10). Ratings are low 0-24, medium 25-49, high 50-74,
    and critical 75-100. A model rating cannot override the calculated score.
    """
    text = ' '.join(analyst.risk_factors + analyst.obligations + analyst.key_findings).casefold()
    dimensions = {
        'legal_force': min(30, 30 if any(x in text for x in ('law', 'legislation', 'mandatory', 'prohibit', 'enforce')) else 10),
        'operational_burden': min(25, 25 if any(x in text for x in ('reserve', 'custody', 'segregation', 'report', 'audit', 'licen')) else 8),
        'scope_exposure': min(20, 20 if any(x in text for x in ('all firms', 'market-wide', 'cross-border', 'systemic')) else 8),
        'enforcement_proximity': min(15, 15 if any(x in text for x in ('effective', 'deadline', 'enforcement', 'sanction')) else 4),
        'evidence_quality': min(10, 10 if len(evidence) >= 2 and analyst.evidence_chunk_ids else 3),
    }
    score = sum(dimensions.values())
    rating: RiskRating = 'critical' if score >= 75 else 'high' if score >= 50 else 'medium' if score >= 25 else 'low'
    triggers = []
    if dimensions['legal_force'] >= 30:
        triggers.append('Binding or prohibitive language requires legal/compliance review.')
    if dimensions['enforcement_proximity'] >= 15:
        triggers.append('An effective date, deadline, or enforcement mechanism is indicated.')
    if dimensions['scope_exposure'] >= 20:
        triggers.append('Broad or cross-border exposure may require coordinated remediation.')
    return RiskAssessment(rating=rating, score=score, dimensions=dimensions,
                          rationale=analyst.risk_factors or ['No explicit risk factors were extracted.'],
                          escalation_triggers=triggers)


def run_rag(db_path: str | Path, question: str, limit: int = 6, jurisdiction: str | None = None) -> RAGResult:
    """Retrieve evidence and execute analyst -> critic -> risk evaluation."""
    evidence = retrieve(db_path, question, limit=limit, jurisdiction=jurisdiction)
    if not evidence:
        raise ValueError('No matching evidence was found in the selected corpus')
    context = _evidence_text(evidence)
    analyst = _structured_prompt(
        f"""You are the analyst in a regulatory intelligence workflow.
Answer the question only from the evidence below. Distinguish enacted rules,
proposals, guidance, and observations. Do not invent obligations. Every finding
and obligation must cite one or more supplied chunk IDs.
Question: {question}
Evidence:\n{context}
Return the requested structured fields. Risk factors must describe observable
regulatory or operational consequences, not investment advice.""", AnalystAssessment)
    critic = _structured_prompt(
        f"""You are the critic for a regulatory intelligence workflow.
Check the analyst assessment against the supplied evidence. Flag claims that
are unsupported, overstate legal force, confuse proposal with enacted law, or
use the wrong citation. Do not add facts not present in the evidence.
Analyst assessment:\n{analyst.model_dump_json()}
Evidence:\n{context}""", CriticAssessment)
    risk = evaluate_risk(analyst, evidence)
    if critic.claim_status in {'unsupported', 'insufficient_evidence'}:
        risk.rating = 'unknown'
        risk.rationale.append('Critic found insufficient support for the analyst claims; manual review is required.')
    return RAGResult(question=question, evidence=[asdict(item) for item in evidence],
                     analyst=analyst, critic=critic, risk=risk)


def main() -> None:
    parser = argparse.ArgumentParser(description='Standalone regulatory RAG prototype')
    parser.add_argument('--db', required=True, help='SQLite database under data/ingestion')
    parser.add_argument('--question', required=True)
    parser.add_argument('--limit', type=int, default=6)
    parser.add_argument('--jurisdiction')
    args = parser.parse_args()
    print(json.dumps(run_rag(args.db, args.question, args.limit, args.jurisdiction).model_dump(), indent=2))


if __name__ == '__main__':
    main()
