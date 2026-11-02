"""App-owned collection scheduling; no Streamlit calls in worker threads."""

import atexit
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import requests

from virtual_asset_agent.common import PipelineError
from virtual_asset_agent.config import load_sources
from virtual_asset_agent.discovery import ingest_discovered
from virtual_asset_agent.fetching import Fetcher, SourceAccessError
from virtual_asset_agent.pipeline import RunLog, ingest
from virtual_asset_agent.reader import Reader
from virtual_asset_agent.storage import init_database

LOG = logging.getLogger(__name__)
DEFAULTS = {'enabled': True, 'interval_hours': 24, 'request_id': None}


class CollectionBusy(Exception):
    """Another process or thread owns the collection lock."""


@contextmanager
def file_lock(path):
    """OS-released lock: stale lock files do not block recovery after a crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise CollectionBusy('Collection is already running') from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CollectionBusy('Collection is already running') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def read_json(path, default):
    if not path.exists():
        return dict(default)
    result = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(result, dict):
        raise ValueError(f'Expected a JSON object in {path.name}')
    return result


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def collect_once(root, db_path):
    """Reuse bounded ingestion and one rate-limited session per enabled source."""
    sources = load_sources(root / 'config/sources.yaml')
    if not db_path.exists():
        init_database(db_path, root / 'db/schema.sql')
    with Reader(db_path):  # Fail explicitly on old schemas; never silently migrate.
        pass
    results, remaining, stopped = [], 0, False
    for source_id, source in sources.items():
        if not source['enabled']:
            continue
        log = RunLog(root, source_id)
        shared = Fetcher(source, log)

        class BorrowedFetcher:
            def __init__(self, source, logger):
                pass

            def get(self, url, expected_type):
                return shared.get(url, expected_type)

            def close(self):
                pass

        try:
            seen = set()
            if source.get('discovery', {}).get('enabled'):
                batch = ingest_discovered(root, db_path, sources, source_id,
                    limit=source['discovery']['max_batch'], fetcher_factory=BorrowedFetcher)
                results.extend(batch['results'])
                seen.update(item['source_url'] for item in batch['results'])
                remaining += batch['remaining']
                if batch['stopped_for_source_access']:
                    stopped = True
                    continue
            for url in source['scope']['target_urls']:
                if url in seen:
                    continue
                try:
                    results.append(ingest(root, db_path, sources, source_id,
                        document_url=url, fetcher_factory=BorrowedFetcher))
                except (PipelineError, requests.RequestException, sqlite3.Error, OSError, ValueError) as exc:
                    results.append({'source_id': source_id, 'source_url': url, 'status': 'failed',
                                    'error_type': type(exc).__name__, 'message': str(exc)})
                    if isinstance(exc, SourceAccessError):
                        stopped = True
                        break
        except (PipelineError, requests.RequestException, sqlite3.Error, OSError, ValueError) as exc:
            results.append({'source_id': source_id, 'source_url': source['discovery_url'],
                            'status': 'failed', 'error_type': type(exc).__name__, 'message': str(exc)})
            stopped = stopped or isinstance(exc, SourceAccessError)
        finally:
            shared.close()
    if (root / 'config/regional_sources.json').exists():
        from regional_ingestion import collect_regions
        results.extend(collect_regions(root))
    counts = {key: sum(item['status'] == key for item in results) for key in ('created', 'reused', 'failed')}
    return {'status': 'partial_failure' if counts['failed'] else 'success',
            'counts': counts, 'remaining': remaining, 'stopped_for_source_access': stopped,
            'results': [{k: item[k] for k in ('source_url', 'status', 'change_type', 'error_type', 'message')
                         if k in item} for item in results]}


class CollectionService:
    def __init__(self, root, runner=collect_once, clock=time.time):
        self.root = Path(root).resolve()
        self.directory = self.root / 'data/ingestion/automation'
        self.db_path = self.root / 'data/ingestion/hkma.sqlite'
        self.settings_path = self.directory / 'settings.json'
        self.state_path = self.directory / 'state.json'
        self.runner, self.clock = runner, clock
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread = None
        self.worker_error = None

    def settings(self):
        value = read_json(self.settings_path, DEFAULTS)
        if (type(value.get('enabled')) is not bool or type(value.get('interval_hours')) is not int
                or not 1 <= value['interval_hours'] <= 168
                or value.get('request_id') is not None and not isinstance(value['request_id'], str)):
            raise ValueError('Invalid collection settings; interval must be 1..168 hours')
        return value

    def configure(self, enabled, interval_hours):
        if type(enabled) is not bool or type(interval_hours) is not int or not 1 <= interval_hours <= 168:
            raise ValueError('Interval must be 1..168 hours')
        with file_lock(self.directory / 'settings.lock'):
            settings = self.settings()
            settings.update(enabled=enabled, interval_hours=interval_hours)
            save_json(self.settings_path, settings)
        self._wake.set()

    def request_run(self):
        with file_lock(self.directory / 'settings.lock'):
            settings = self.settings()
            settings['request_id'] = uuid.uuid4().hex
            save_json(self.settings_path, settings)
        self._wake.set()

    def snapshot(self):
        settings = self.settings()
        state = read_json(self.state_path, {'status': 'not_started'})
        next_due = None
        if settings['enabled'] and state['status'] != 'running':
            next_due = state.get('finished_epoch', self.clock() - settings['interval_hours'] * 3600)
            next_due = timestamp(next_due + settings['interval_hours'] * 3600)
        return {'settings': settings, 'state': state, 'next_due': next_due,
                'worker_error': self.worker_error,
                'worker_alive': self._thread is not None and self._thread.is_alive()}

    def tick(self):
        """One due check, with cross-process exclusion and persisted deadlines."""
        try:
            with file_lock(self.directory / 'run.lock'):
                settings = self.settings()
                prior = read_json(self.state_path, {})
                requested = settings.get('request_id') != prior.get('handled_request_id')
                due = (settings['enabled'] and (prior.get('status') == 'running'
                    or self.clock() >= prior.get('finished_epoch', float('-inf')) + settings['interval_hours'] * 3600))
                if not requested and not due:
                    return False
                started = self.clock()
                state = {'status': 'running', 'started_at': timestamp(started),
                         'handled_request_id': settings.get('request_id'),
                         'trigger': 'manual' if requested else 'scheduled',
                         'recovered_interrupted_run': prior.get('status') == 'running'}
                save_json(self.state_path, state)
                try:
                    state.update(self.runner(self.root, self.db_path))
                except Exception as exc:
                    # Worker boundary: retain diagnostics and continue at the next scheduled interval.
                    LOG.exception('Collection cycle failed')
                    state.update(status='failed', error_type=type(exc).__name__, message=str(exc))
                state.update(finished_epoch=self.clock(), finished_at=timestamp(self.clock()))
                save_json(self.state_path, state)
                return True
        except CollectionBusy:
            return False

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name='hkma-collection', daemon=True)
            self._thread.start()
            atexit.register(self.stop)
        return self

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
                self.worker_error = None
            except Exception as exc:
                LOG.exception('Collection scheduler failed')
                self.worker_error = f'{type(exc).__name__}: {exc}'
            self._wake.wait(5)
            self._wake.clear()
