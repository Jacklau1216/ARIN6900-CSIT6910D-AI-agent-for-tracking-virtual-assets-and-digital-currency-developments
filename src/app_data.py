"""Real-data adapters for the teammate's original DataFrame interfaces."""

import html
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import streamlit as st

from dashboard_data import load_corpus
from live_documents import collection_service
from market_data import market_snapshot
from virtual_asset_agent.agent_workflow import analyze_regulatory_text

DEVELOPMENT_FIELDS = ['development_id', 'document_id', 'jurisdiction', 'regulator', 'asset_class',
                      'lifecycle_stage', 'obligation_summary', 'risk_rating', 'confidence',
                      'review_status', 'evidence_chunk_ids']
CHUNK_FIELDS = ['chunk_id', 'document_id', 'text', 'citation']
VALID_RISK_RATINGS = {'low', 'medium', 'high', 'critical', 'unknown'}


def _normalise_risk(risk_rating):
    if isinstance(risk_rating, str):
        risk = risk_rating.strip().lower()
        if risk in VALID_RISK_RATINGS:
            return risk
    return 'unknown'


def _normalise_confidence(confidence):
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        return None
    value = max(0.0, min(1.0, value))
    return value


def ensure_assessment_store(db_path):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS regulatory_assessments (
                document_id TEXT PRIMARY KEY,
                title TEXT,
                jurisdiction TEXT,
                asset_class TEXT,
                raw_text TEXT,
                primary_legal_source TEXT,
                risk_rating TEXT NOT NULL DEFAULT 'unknown',
                confidence REAL NOT NULL DEFAULT 0.0,
                review_status TEXT NOT NULL DEFAULT 'agent_reviewed',
                executive_summary TEXT,
                impact_tags TEXT,
                assessed_at TEXT,
                source_citations TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_regulatory_assessments_jurisdiction "
            "ON regulatory_assessments(jurisdiction)"
        )


def load_assessment_map(db_path):
    path = Path(db_path)
    if not path.exists():
        return {}
    try:
        with sqlite3.connect(path) as conn:
            rows = conn.execute(
                'SELECT document_id, risk_rating, confidence, review_status, executive_summary, impact_tags '
                'FROM regulatory_assessments'
            ).fetchall()
    except sqlite3.Error:
        return {}
    return {
        row[0]: {
            'risk_rating': _normalise_risk(row[1]),
            'confidence': _normalise_confidence(row[2]),
            'review_status': row[3] or 'agent_reviewed',
            'executive_summary': row[4] or '',
            'impact_tags': (row[5] or '').split(',') if row[5] else [],
        }
        for row in rows
    }


def upsert_document_assessment(db_path, document_id, title, jurisdiction, asset_class, raw_text,
                              primary_legal_source=None):
    analysis = analyze_regulatory_text(
        raw_text=raw_text or '',
        title=title or '',
        jurisdiction=jurisdiction or 'unknown',
        asset_class=asset_class or 'unknown',
        primary_legal_source=primary_legal_source or raw_text or '',
    )
    risk_rating = _normalise_risk(analysis.get('risk_rating', 'unknown'))
    confidence = _normalise_confidence(analysis.get('confidence', 0.0))
    if confidence is None:
        confidence = 0.0
    impact_tags = analysis.get('impact_tags') or []
    citations = analysis.get('critic_review', {}).get('supporting_evidence') or []
    row = {
        'document_id': document_id,
        'title': title or '',
        'jurisdiction': jurisdiction or 'unknown',
        'asset_class': asset_class or 'unknown',
        'raw_text': (raw_text or '')[:12000],
        'primary_legal_source': (primary_legal_source or raw_text or '')[:12000],
        'risk_rating': risk_rating,
        'confidence': confidence,
        'review_status': 'agent_reviewed',
        'executive_summary': (analysis.get('executive_summary') or '').strip()[:2000],
        'impact_tags': ','.join(str(tag).strip() for tag in impact_tags if str(tag).strip())[:1000],
        'assessed_at': pd.Timestamp.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'source_citations': '|'.join(str(item).strip() for item in citations if str(item).strip())[:2000],
    }
    with sqlite3.connect(Path(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO regulatory_assessments (
                document_id, title, jurisdiction, asset_class, raw_text, primary_legal_source,
                risk_rating, confidence, review_status, executive_summary, impact_tags,
                assessed_at, source_citations
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id)
            DO UPDATE SET
                title=excluded.title,
                jurisdiction=excluded.jurisdiction,
                asset_class=excluded.asset_class,
                raw_text=excluded.raw_text,
                primary_legal_source=excluded.primary_legal_source,
                risk_rating=excluded.risk_rating,
                confidence=excluded.confidence,
                review_status=excluded.review_status,
                executive_summary=excluded.executive_summary,
                impact_tags=excluded.impact_tags,
                assessed_at=excluded.assessed_at,
                source_citations=excluded.source_citations
            """,
            (
                row['document_id'], row['title'], row['jurisdiction'], row['asset_class'], row['raw_text'],
                row['primary_legal_source'], row['risk_rating'], row['confidence'], row['review_status'],
                row['executive_summary'], row['impact_tags'], row['assessed_at'], row['source_citations'],
            ),
        )
    return row


def refresh_document_assessments(root, data):
    db_path = Path(root) / 'data/ingestion/regulatory_assessments.sqlite'
    ensure_assessment_store(db_path)
    existing = load_assessment_map(db_path)
    by_document = {}
    for chunk in data.get('chunks', []):
        citation = chunk.get('citation') or {}
        doc_id = citation.get('document_id') or chunk.get('document_id')
        if not doc_id:
            continue
        by_document.setdefault(doc_id, []).append((chunk.get('text') or '').strip())
    for doc in data.get('documents', []):
        if doc.get('state') != 'ready':
            continue
        doc_id = doc.get('document_id')
        current = existing.get(doc_id) or {}
        risk_rating = current.get('risk_rating')
        confidence = current.get('confidence')
        if risk_rating not in (None, 'unknown') and confidence is not None:
            continue
        raw_text = '\n'.join(text for text in by_document.get(doc_id, []) if text)
        if not raw_text.strip():
            continue
        upsert_document_assessment(
            db_path=db_path,
            document_id=doc_id,
            title=doc.get('title') or '',
            jurisdiction=doc.get('jurisdiction') or 'unknown',
            asset_class=', '.join(doc.get('asset_classes') or []) or 'unknown',
            raw_text=raw_text,
            primary_legal_source=raw_text,
        )
    return load_assessment_map(db_path)


def regulatory_frames(data, assessments=None):
    """A publication preview is not an inferred obligation or reviewed finding."""
    chunks, developments = [], []
    by_document = {}
    assessments = assessments or {}
    for chunk in data['chunks']:
        citation = chunk['citation']
        doc_id = citation['document_id']
        by_document.setdefault(doc_id, []).append(chunk['chunk_id'])
        location = f"p. {chunk['page_start']}" if chunk['page_start'] else 'HTML body'
        label = f"{citation['source_name']} | {citation['title']} | {location} | {chunk['chunk_id']}"
        chunks.append(dict(chunk_id=chunk['chunk_id'], document_id=doc_id,
                           text=html.escape(chunk['text']),
                           citation=SimpleNamespace(label=html.escape(label),
                                                    url=html.escape(citation['source_url'], quote=True))))
    for doc in data['documents']:
        if doc['state'] != 'ready':
            continue
        assessment = assessments.get(doc['document_id']) or {}
        risk_rating = _normalise_risk(assessment.get('risk_rating', 'unknown'))
        confidence = _normalise_confidence(assessment.get('confidence'))
        review_status = assessment.get('review_status', 'agent_reviewed' if assessment else 'not_reviewed')
        if not assessment:
            risk_rating = 'unknown'
            confidence = None
            review_status = 'not_reviewed'
        developments.append(dict(development_id=doc['document_id'] + ':publication',
            document_id=doc['document_id'], jurisdiction=doc['jurisdiction'], regulator=doc['regulator'],
            asset_class=', '.join(doc['asset_classes']) or 'unknown',
            lifecycle_stage=doc['lifecycle_stage'] or 'unknown',
            obligation_summary=html.escape('Publication: ' + (doc['title'] or 'Untitled')),
            risk_rating=risk_rating, confidence=confidence, review_status=review_status,
            evidence_chunk_ids=by_document.get(doc['document_id'], [])))
    return (pd.DataFrame(data['documents']), pd.DataFrame(developments, columns=DEVELOPMENT_FIELDS),
            pd.DataFrame(chunks, columns=CHUNK_FIELDS))


def market_frame(snapshots):
    """Keep the original three-card interface, with explicit real instruments and units."""
    rows = []
    specifications = [
        ('coingecko_bitcoin', 'BTC', 'cryptocurrency', 'market_cap', 'USD'),
        ('defillama_stablecoins', 'USDC', 'stablecoin', 'circulating_amount', 'USD at peg'),
        ('defillama_rwa', None, 'rwa', 'protocol_tvl', 'USD')]
    for source_id, instrument, asset_class, metric, unit in specifications:
        snapshot = next((s for s in snapshots if s['source_id'] == source_id), {})
        matches = [r for r in snapshot.get('rows', []) if r['metric'] == metric
                   and (instrument is None or r['instrument'] == instrument)]
        match = max(matches, key=lambda r: r['value']) if matches else {}
        rows.append(dict(instrument=html.escape(match.get('instrument', instrument or 'RWA protocol unavailable')),
            asset_class=asset_class, metric=metric, value=match.get('value', float('nan')),
            currency=unit, observed_at=match.get('observed_at'), collected_at=match.get('collected_at'),
            source_url=match.get('source_url', snapshot.get('source_url')),
            provider=match.get('provider', snapshot.get('provider')),
            data_quality=snapshot.get('status', 'unavailable'),
            content_sha256=match.get('content_sha256'), raw_path=match.get('raw_path'),
            json_pointer=match.get('json_pointer')))
    return pd.DataFrame(rows)


def assessment(value):
    return 'Not assessed' if pd.isna(value) else f'{value:.0%}'


@st.cache_data(ttl=60, show_spinner=False)
def load_frames(root):
    root = Path(root)
    issues = []
    data = {'documents': [], 'chunks': []}
    path = root / 'data/ingestion/hkma.sqlite'
    try:
        if path.exists():
            data = load_corpus(path)
        else:
            issues.append('Waiting for the first real regulatory collection.')
    except (ValueError, OSError, sqlite3.Error) as exc:
        issues.append(f'Regulatory data unavailable: {exc}')
    regional = root / 'data/ingestion/regional.sqlite'
    if regional.exists():
        try:
            extra = load_corpus(regional)
            data['documents'].extend(extra['documents'])
            data['chunks'].extend(extra['chunks'])
        except (ValueError, OSError, sqlite3.Error) as exc:
            issues.append(f'Regional data unavailable: {exc}')
    assessment_map = refresh_document_assessments(root, data)
    try:
        snapshots = market_snapshot(root)
    except (ValueError, OSError) as exc:
        snapshots = []
        issues.append(f'Market data unavailable: {exc}')
    for snapshot in snapshots:
        if snapshot['status'] != 'available':
            issues.append(f"{snapshot['source_id']}: {snapshot['status']}. {snapshot.get('error') or ''}")
    documents, developments, chunks = regulatory_frames(data, assessment_map)
    return documents, developments, chunks, market_frame(snapshots), issues


def load_app_data(root):
    # Retain the existing background collection without adding UI controls.
    collection_service(str(root))
    return load_frames(str(root))
