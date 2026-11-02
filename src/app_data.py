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

DEVELOPMENT_FIELDS = ['development_id', 'document_id', 'jurisdiction', 'regulator', 'asset_class',
                      'lifecycle_stage', 'obligation_summary', 'risk_rating', 'confidence',
                      'review_status', 'evidence_chunk_ids']
CHUNK_FIELDS = ['chunk_id', 'document_id', 'text', 'citation']


def regulatory_frames(data):
    """A publication preview is not an inferred obligation or reviewed finding."""
    chunks, developments = [], []
    by_document = {}
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
        developments.append(dict(development_id=doc['document_id'] + ':publication',
            document_id=doc['document_id'], jurisdiction=doc['jurisdiction'], regulator=doc['regulator'],
            asset_class=', '.join(doc['asset_classes']) or 'unknown',
            lifecycle_stage=doc['lifecycle_stage'] or 'unknown',
            obligation_summary=html.escape('Publication: ' + (doc['title'] or 'Untitled')),
            risk_rating='unknown', confidence=None, review_status='not_reviewed',
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
    try:
        snapshots = market_snapshot(root)
    except (ValueError, OSError) as exc:
        snapshots = []
        issues.append(f'Market data unavailable: {exc}')
    for snapshot in snapshots:
        if snapshot['status'] != 'available':
            issues.append(f"{snapshot['source_id']}: {snapshot['status']}. {snapshot.get('error') or ''}")
    documents, developments, chunks = regulatory_frames(data)
    return documents, developments, chunks, market_frame(snapshots), issues


def load_app_data(root):
    # Retain the existing background collection without adding UI controls.
    collection_service(str(root))
    return load_frames(str(root))
