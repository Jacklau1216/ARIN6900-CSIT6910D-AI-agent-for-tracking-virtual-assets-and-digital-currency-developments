"""Bounded, approved official publications for the three additional pilot regions."""
import io
import importlib.metadata
import json
import re
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from pypdf import PdfReader

from collection_service import file_lock
from virtual_asset_agent.cleaning import clean_pages, chunk_pages
from virtual_asset_agent.common import PipelineError, identity, sha256, utc_now, write_immutable, json_bytes
from virtual_asset_agent.fetching import Fetcher
from virtual_asset_agent.pipeline import RunLog
from virtual_asset_agent.storage import Repository, init_database

RULES = {
    'stablecoin': r'\bstablecoins?\b',
    'cryptocurrency': r'\bcryptoassets?\b|\bcryptoasset custody\b',
    'tokenized_security': r'\btokenised\s+(?:securities|bonds)\b|\bDLT financial instruments\b',
    'tokenized_deposit': r'\btokenised\s+(?:bonds\s+and\s+)?deposits\b',
}


def parse(content, source):
    title = source['title']
    if source['media_type'] == 'application/pdf':
        if not content.startswith(b'%PDF'):
            raise PipelineError('Expected PDF bytes')
        pdf = PdfReader(io.BytesIO(content))
        if pdf.is_encrypted or not 1 <= len(pdf.pages) <= 150:
            raise PipelineError('Unsupported PDF')
        pages = [p.extract_text() or '' for p in pdf.pages]
        cover = ' '.join(pages[0].split())
        if title not in cover or source['date_raw'] not in cover:
            raise PipelineError('Verified title/date not found on cover')
        date_raw = source['date_raw']
        locator = {'page': 1}
        body_locator = {'physical_pages': len(pages)}
    else:
        soup = BeautifulSoup(content, 'html.parser')
        heading = soup.select_one(source['title_selector'])
        if heading is None or heading.get_text(' ', strip=True) != title:
            raise PipelineError('Official title missing or changed; review source configuration')
        nodes = soup.select(source['body_selector'])
        if not nodes:
            raise PipelineError('Article body missing')
        for node in nodes:
            for bad in node.select('script, style, form, nav'):
                bad.decompose()
        pages = ['\n\n'.join(node.get_text('\n', strip=True) for node in nodes)]
        date_node = soup.select_one(source['date_selector'])
        if date_node is None:
            raise PipelineError('Publication date missing')
        date_raw = date_node.get_text(' ', strip=True)
        locator = {'selector': source['date_selector']}
        body_locator = {'selector': source['body_selector']}
    text, ranges = clean_pages(pages)
    if len(text) < 300:
        raise PipelineError('Insufficient substantive text')
    published = datetime.strptime(date_raw, source['date_format']).date().isoformat()
    if published > utc_now()[:10]:
        raise PipelineError('Future publication date')
    if source['media_type'] == 'text/html':
        ranges[0]['page'] = None
    evidence = {'title': {'raw_value': title, 'locator': {'page': 1} if source['media_type'] == 'application/pdf'
                         else {'selector': source['title_selector']}},
                'published_at': {'raw_value': date_raw, 'locator': locator},
                'body': {'locator': body_locator},
                'language': {'reason': 'not_extracted'},
                'document_type': {'reason': 'not_classified'},
                'updated_at': {'reason': 'not_extracted'}, 'effective_at': {'reason': 'not_extracted'}}
    return pages, text, ranges, published, evidence


def ingest_one(root, source, fetcher_factory=Fetcher):
    root = Path(root)
    if not source.get('enabled'):
        raise PipelineError('Requested regional source is disabled')
    log = RunLog(root, source['source_id'])
    fetcher = fetcher_factory(source, log)
    repository = None
    stage = 'download'
    try:
        download = fetcher.get(source['url'], source['media_type'])
        digest = sha256(download.content)
        suffix = 'pdf' if source['media_type'] == 'application/pdf' else 'html'
        raw_path = f'data/raw/{digest}.{suffix}'
        write_immutable(root / raw_path, download.content)
        stage = 'parsing'
        pages, text, ranges, published, evidence = parse(download.content, source)
        manifest = {'dependencies': {name: importlib.metadata.version(name) for name in ('beautifulsoup4', 'pypdf')}, 'code': sha256(Path(__file__).read_bytes()), 'source': source,
                    'schema': sha256((root / 'db/regional_schema.sql').read_bytes()),
                    'cleaner': sha256(Path(__import__('virtual_asset_agent.cleaning', fromlist=['']).__file__).read_bytes())}
        pipeline_version = sha256(json_bytes(manifest))
        write_immutable(root / f'data/processed/{pipeline_version}/manifest.json', json_bytes(manifest))
        repository = Repository(root / 'data/ingestion/regional.sqlite')
        repository.sync_sources({source['source_id']: source})
        version = repository.register_raw(source['source_id'], source['url'], download, digest, raw_path, None)
        eid = identity('extraction', version['version_id'], pipeline_version)
        chunks = chunk_pages(text, ranges, eid, 1800)
        assets = []
        for asset, pattern in RULES.items():
            hit = next((c for c in chunks if re.search(pattern, c['text'], re.I)), None)
            if hit:
                assets.append(dict(extraction_id=eid, asset_class=asset, rule_id='regional-explicit-terms-v1',
                                   evidence_chunk_id=hit['chunk_id']))
        if not assets:
            raise PipelineError('No evidenced asset classification')
        extraction = dict(extraction_id=eid, version_id=version['version_id'], pipeline_version=pipeline_version,
                          processed_at=utc_now(), title=source['title'], language=None, published_at=published,
                          updated_at=None, effective_at=None, document_type='other', lifecycle_stage='unknown',
                          raw_text='\n\n'.join(pages), clean_text=text, metadata_evidence=json_bytes(evidence).decode())
        stage = 'persistence'
        created = repository.save_result(extraction, chunks, assets)
        result = dict(source_id=source['source_id'], source_url=source['url'], status='created' if created else 'reused',
                      chunks=len(chunks), asset_classes=[a['asset_class'] for a in assets], published_at=published)
        log(stage, 'success', source['url'], result=result)
        return result
    except Exception as exc:
        # Source boundary: retain diagnostics, then let the caller isolate failures.
        log(stage, 'failure', source['url'], error_type=type(exc).__name__, message=str(exc))
        raise
    finally:
        fetcher.close()
        if repository:
            repository.close()


def collect_regions(root):
    root = Path(root)
    with file_lock(root / 'data/ingestion/regional.lock'):
        path = root / 'data/ingestion/regional.sqlite'
        if not path.exists():
            init_database(path, root / 'db/regional_schema.sql')
        sources = json.loads((root / 'config/regional_sources.json').read_text(encoding='utf-8'))['sources']
        results = []
        for source in sources:
            if not source.get('enabled'):
                continue
            try:
                results.append(ingest_one(root, source))
            except Exception as exc:
                results.append(dict(source_id=source['source_id'], source_url=source['url'], status='failed',
                                    error_type=type(exc).__name__, message=str(exc)))
        return results


if __name__ == '__main__':
    print(json.dumps(collect_regions(Path(__file__).resolve().parents[1]), indent=2))
