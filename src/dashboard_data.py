"""Read current real evidence for the dashboard without changing ingestion schemas."""

import re

from virtual_asset_agent.reader import Reader

STOPWORDS = set('a an and are as at be by do does for from how i in is it of on or that the this to what when where which with'.split())


def load_corpus(path):
    documents, chunks = [], []
    with Reader(path) as reader:
        while True:
            page = reader.list_documents(limit=100, offset=len(documents))
            documents.extend(page)
            if len(page) < 100:
                break
        for document in documents:
            if document['state'] != 'ready':
                continue
            for offset in range(0, document['chunk_count'], 100):
                chunks.extend(reader.list_chunks(document['extraction_id'], limit=100, offset=offset))
    return {'documents': documents, 'chunks': chunks}


def search_evidence(corpus, question, limit=5, jurisdiction=None):
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 500:
        raise ValueError('Enter a question of 1 to 500 characters')
    terms = set(re.findall(r'\w+', question.casefold())) - STOPWORDS
    if not terms:
        return []
    allowed = {d['document_id'] for d in corpus['documents']
               if jurisdiction is None or d['jurisdiction'] == jurisdiction}
    matches = []
    for chunk in corpus['chunks']:
        if chunk['citation']['document_id'] not in allowed:
            continue
        words = set(re.findall(r'\w+', chunk['text'].casefold()))
        matched = sorted(terms & words)
        if matched:
            matches.append(dict(chunk, matched_terms=matched, match_count=len(matched)))
    # Match counts rank evidence; they are not confidence or a legal risk score.
    return sorted(matches, key=lambda c: (-c['match_count'], c['citation']['document_id'], c['ordinal']))[:limit]
