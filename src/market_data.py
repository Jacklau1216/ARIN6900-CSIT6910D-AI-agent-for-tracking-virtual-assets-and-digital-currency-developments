"""Public market APIs with auditable snapshots, bounded requests and stale fallback."""

import hashlib
import json
import logging
import math
import time
from datetime import datetime
from pathlib import Path

import requests

from collection_service import CollectionBusy, file_lock, read_json, save_json, timestamp

LOG = logging.getLogger(__name__)
MAX_BYTES = 20 * 1024 * 1024


def number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def normalize(payload, source):
    """Keep provider metrics distinct; absent values are never converted to zero."""
    rows = []

    def add(asset_id, instrument, asset_class, metric, value, unit, locator, updated=None):
        if not number(value):
            return
        rows.append(dict(asset_id=str(asset_id), instrument=str(instrument), asset_class=asset_class,
                         metric=metric, value=value, unit=unit, provider=source['provider'],
                         source_id=source['id'], source_url=source['url'],
                         observed_at=updated, json_pointer=locator))

    if source['id'] == 'defillama_stablecoins':
        if not isinstance(payload, dict) or not isinstance(payload.get('peggedAssets'), list):
            raise ValueError('Expected peggedAssets array')
        seen = set()
        for i, asset in enumerate(payload['peggedAssets']):
            if not isinstance(asset, dict):
                raise ValueError('Invalid stablecoin record')
            if asset.get('pegType') != 'peggedUSD':
                continue
            aid = asset.get('id')
            if not aid or not asset.get('symbol') or aid in seen:
                raise ValueError('Missing or duplicate stablecoin identity')
            seen.add(aid)
            circulation = asset.get('circulating')
            if not isinstance(circulation, dict):
                raise ValueError('Missing circulating object')
            add(aid, asset['symbol'], 'stablecoin', 'circulating_amount',
                circulation.get('peggedUSD'), 'USD at peg', f'/peggedAssets/{i}/circulating/peggedUSD')
            add(aid, asset['symbol'], 'stablecoin', 'price', asset.get('price'), 'USD',
                f'/peggedAssets/{i}/price')
    elif source['id'] == 'coingecko_bitcoin':
        if (not isinstance(payload, list) or len(payload) != 1
                or not isinstance(payload[0], dict) or payload[0].get('id') != 'bitcoin'):
            raise ValueError('Expected one Bitcoin market record')
        asset = payload[0]
        updated = asset.get('last_updated')
        if updated is not None:
            parsed = datetime.fromisoformat(updated.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError('Provider update timestamp must include a timezone')
        for field, metric in [('market_cap', 'market_cap'), ('current_price', 'price'),
                              ('total_volume', 'volume_24h')]:
            add('bitcoin', 'BTC', 'cryptocurrency', metric, asset.get(field), 'USD', '/0/' + field, updated)
    elif source['id'] == 'defillama_rwa':
        if not isinstance(payload, list):
            raise ValueError('Expected protocol array')
        seen = set()
        for i, asset in enumerate(payload):
            if not isinstance(asset, dict):
                raise ValueError('Invalid protocol record')
            if asset.get('category') != 'RWA':
                continue
            aid = asset.get('id')
            if not aid or not asset.get('name') or aid in seen:
                raise ValueError('Missing or duplicate protocol identity')
            seen.add(aid)
            add(aid, asset['name'], 'rwa', 'protocol_tvl', asset.get('tvl'), 'USD', f'/{i}/tvl')
    else:
        raise ValueError('Unsupported market source')
    if not rows:
        raise ValueError('No valid market observations; refusing to replace a good snapshot')
    return rows


def fetch(source):
    with requests.Session() as session:
        session.trust_env = source['trust_env']
        session.headers.update({'User-Agent': 'VirtualAssetResearch/0.2 (academic dashboard)',
                                'Accept': 'application/json'})
        for attempt in range(2):
            try:
                with session.get(source['url'], timeout=(5, 20), stream=True, allow_redirects=False) as response:
                    if response.status_code >= 500 and attempt == 0:
                        time.sleep(1)
                        continue
                    response.raise_for_status()
                    if response.status_code != 200:
                        raise ValueError(f'Unexpected HTTP status {response.status_code}')
                    data = bytearray()
                    for chunk in response.iter_content(65536):
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise ValueError('Market response exceeds size limit')
                    return bytes(data)
            except (requests.Timeout, requests.ConnectionError):
                if attempt:
                    raise
                time.sleep(1)
    raise ValueError('No market response')


def _saved(root, state, source):
    if not state.get('snapshot'):
        return []
    path = (root / state['snapshot']).resolve()
    if not path.is_relative_to((root / 'data/market/raw').resolve()):
        raise ValueError('Invalid market snapshot path')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != state['content_sha256']:
        raise ValueError('Market snapshot hash mismatch')
    rows = normalize(json.loads(raw), source)
    for row in rows:
        row.update(collected_at=state['collected_at'], content_sha256=state['content_sha256'],
                   raw_path=state['snapshot'])
    return rows


def source_snapshot(root, source, refresh_seconds=900, retry_seconds=60, fetcher=fetch, now=None):
    root = Path(root).resolve()
    now = time.time() if now is None else now
    directory = root / 'data/market'
    state_path = directory / (source['id'] + '.json')
    state = {}
    try:
        with file_lock(directory / (source['id'] + '.lock')):
            state = read_json(state_path, {})
            last_try = state.get('last_attempt_epoch', float('-inf'))
            wait = retry_seconds if state.get('error') else refresh_seconds
            if now >= last_try + wait:
                try:
                    raw = fetcher(source)
                    normalize(json.loads(raw), source)  # Validate before publishing.
                    digest = hashlib.sha256(raw).hexdigest()
                    path = directory / 'raw' / (digest + '.json')
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if not path.exists():
                        # Content-addressed originals are never replaced.
                        with path.open('xb') as handle:
                            handle.write(raw)
                    state = dict(snapshot=path.relative_to(root).as_posix(), content_sha256=digest,
                                 collected_at=timestamp(now), collected_epoch=now, error=None)
                except (requests.RequestException, ValueError, OSError) as exc:
                    LOG.warning('source=%s url=%s stage=market_collection error_type=%s error=%s',
                                source['id'], source['url'], type(exc).__name__, exc)
                    state['error'] = f'{type(exc).__name__}: {exc}'
                state['last_attempt_epoch'] = now
                save_json(state_path, state)
            rows = _saved(root, state, source)
    except CollectionBusy:
        state = read_json(state_path, {})
        rows = _saved(root, state, source)
    stale = bool(state.get('error')) or now >= state.get('collected_epoch', float('-inf')) + refresh_seconds
    return dict(source_id=source['id'], provider=source['provider'], source_url=source['url'],
                status='unavailable' if not rows else 'stale' if stale else 'available',
                error=state.get('error'), collected_at=state.get('collected_at'), rows=rows)


def market_snapshot(root):
    config = json.loads((Path(root) / 'config/market_sources.json').read_text(encoding='utf-8'))
    results = []
    for source in config['sources']:
        try:
            result = source_snapshot(root, source, config['refresh_seconds'], config['failure_retry_seconds'])
        except (ValueError, OSError) as exc:
            result = dict(source_id=source['id'], provider=source['provider'], source_url=source['url'],
                          status='unavailable', error=f'{type(exc).__name__}: {exc}', collected_at=None, rows=[])
        results.append(result)
    return results
