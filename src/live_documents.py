"""English-only real-document UI backed by short-lived read-only queries."""

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

from collection_service import CollectionBusy, CollectionService
from virtual_asset_agent.reader import Reader


@st.cache_resource
def collection_service(root):
    return CollectionService(root).start()


def local_time(value):
    if not value:
        return 'Not available'
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(
        timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S HKT')


def original_bytes(root, document):
    raw = Path(document['raw_path'])
    resolved = (root / raw).resolve()
    if raw.is_absolute() or not resolved.is_relative_to((root / 'data/raw').resolve()):
        raise ValueError('Invalid original-file path')
    data = resolved.read_bytes()
    if hashlib.sha256(data).hexdigest() != document['content_sha256']:
        raise ValueError('Original-file hash does not match the stored evidence')
    return data


def list_documents(db_path):
    documents = []
    with Reader(db_path) as reader:
        while True:
            page = reader.list_documents(limit=100, offset=len(documents))
            documents.extend(page)
            if len(page) < 100:
                return documents


@st.fragment(run_every='5s')
def collection_panel(service):
    try:
        status = service.snapshot()
    except (ValueError, OSError) as exc:
        st.error(f'Collection settings or status unavailable: {exc}')
        return
    settings, state = status['settings'], status['state']
    st.subheader('Automatic collection')
    st.caption('HKMA stablecoin documents. Runs while this Streamlit server is running, '
               'including after you close the browser. Times are shown in Hong Kong time.')
    left, middle, right = st.columns(3)
    left.metric('Schedule', f"Every {settings['interval_hours']} hours" if settings['enabled'] else 'Paused')
    middle.metric('Last run', state['status'].replace('_', ' ').title())
    right.write('**Next scheduled check**')
    right.write('After the current run' if state['status'] == 'running' else
                local_time(status['next_due']) if settings['enabled'] else 'Paused')
    if state.get('started_at'):
        st.caption(f"Started: {local_time(state['started_at'])} | Finished: {local_time(state.get('finished_at'))}")
    if status['worker_error']:
        st.error(status['worker_error'])
    if state.get('message'):
        st.error(f"{state.get('error_type', 'Collection error')}: {state['message']}")
    if state.get('counts'):
        counts = state['counts']
        st.write(f"Latest run: {counts['created']} new processing results, {counts['reused']} reused, {counts['failed']} failed.")
    if state.get('remaining'):
        st.warning(f"{state['remaining']} discovered documents were not attempted within this bounded batch.")
    if state.get('stopped_for_source_access'):
        st.warning('HKMA requested that access stop. Further requests in that source run were cancelled.')
    with st.expander('Collection settings'):
        with st.form('collection-settings'):
            enabled = st.checkbox('Enable automatic collection', value=settings['enabled'])
            interval = st.number_input('Interval (hours)', min_value=1, max_value=168,
                                       value=settings['interval_hours'], step=1)
            if st.form_submit_button('Save schedule'):
                try:
                    service.configure(enabled, int(interval))
                    st.success('Schedule saved. An in-progress run finishes before pausing.')
                except (CollectionBusy, ValueError, OSError) as exc:
                    st.error(f'Unable to save schedule: {exc}')
        st.caption('The first run is due immediately. Later runs are scheduled from completion. '
                   'Settings survive restarts. MAS is not enabled.')
    if st.button('Collect now', disabled=state['status'] == 'running'):
        try:
            service.request_run()
            st.success('Collection requested. This panel refreshes automatically.')
        except (CollectionBusy, ValueError, OSError) as exc:
            st.error(f'Unable to request collection: {exc}')
    if state.get('results'):
        with st.expander('Latest collection results'):
            st.dataframe(state['results'], use_container_width=True, hide_index=True)


@st.fragment(run_every='15s')
def document_panel(root, db_path, documents_override=None):
    st.subheader('Collected documents')
    if not db_path.exists():
        st.info('Waiting for the first collection to initialize the real database.')
        return
    try:
        documents = list_documents(db_path) if documents_override is None else documents_override
        a, b, c = st.columns(3)
        a.metric('Real documents', len(documents))
        b.metric('Current evidence chunks', sum(d['chunk_count'] for d in documents))
        c.metric('Sources', len({d['source_id'] for d in documents}))
        query = st.text_input('Filter titles', placeholder='e.g. guideline, licensing, consultation')
        formats = st.multiselect('Formats', ['HTML', 'PDF'], default=['HTML', 'PDF'])
        selected = [d for d in documents if query.casefold() in (d['title'] or d['source_url']).casefold()
                    and ('PDF' if d['media_type'] == 'application/pdf' else 'HTML') in formats]
        if not selected:
            st.info('No documents match the current filters. Existing results remain available after a collection failure.')
            return
        st.dataframe([{'Title': d['title'] or 'Awaiting successful parsing',
                       'Published': d['published_at'] or 'Unknown', 'Source': d['regulator'],
                       'Format': 'PDF' if d['media_type'] == 'application/pdf' else 'HTML',
                       'Last observed': local_time(d['last_observed_at']), 'State': d['state']}
                      for d in selected], use_container_width=True, hide_index=True)
        lookup = {d['document_id']: d for d in selected}
        chosen = st.selectbox('Read a document', list(lookup), format_func=lambda key:
                             lookup[key]['title'] or lookup[key]['source_url'])
        document = lookup[chosen]
        st.write('**' + (document['title'] or 'Document awaiting parsing') + '**')
        st.link_button('Open official source', document['source_url'])
        st.caption(f"Publication date: {document['published_at'] or 'Unknown'} | "
                   f"Evidence collected: {local_time(document['evidence_collected_at'])}")
        try:
            data = original_bytes(root, document)
            st.download_button('Download verified original', data,
                file_name=Path(document['raw_path']).name, mime=document['media_type'])
        except (OSError, ValueError) as exc:
            st.warning(f'Original download unavailable: {exc}')
        if document['state'] != 'ready':
            st.warning('The latest registered original has no successful extraction. No older text is substituted.')
            return
        with Reader(db_path) as reader:
            extraction = reader.get_extraction(document['extraction_id'], include_text=True)
            st.text_area('Full extracted text', value=extraction['clean_text'], height=450,
                         disabled=True, key='body-' + extraction['extraction_id'])
            with st.expander('Evidence chunks and provenance'):
                chunk_page = st.number_input('Chunk page (20 per page)', min_value=1,
                    max_value=max(1, (document['chunk_count'] + 19) // 20), value=1,
                    key='chunk-page-' + extraction['extraction_id'])
                for chunk in reader.list_chunks(extraction['extraction_id'], limit=20, offset=(chunk_page - 1) * 20):
                    locator = f"PDF page {chunk['page_start']}" if chunk['page_start'] else 'HTML body'
                    st.caption(f"{locator} | Characters {chunk['char_start']}:{chunk['char_end']} | {chunk['chunk_id']}")
                    st.text(chunk['text'])
                st.json({'source_url': document['source_url'], 'content_sha256': document['content_sha256'],
                         'version_id': document['version_id'], 'extraction_id': extraction['extraction_id'],
                         'metadata_evidence': extraction['metadata_evidence']})
    except (ValueError, OSError, sqlite3.Error) as exc:
        st.error(f'Unable to read the real database: {exc}')


def render_live(root):
    st.markdown('''<style>
        [data-testid="stTextArea"] textarea:disabled {
            -webkit-text-fill-color: #e5e7eb; color: #e5e7eb; opacity: 1;
        }
    </style>''', unsafe_allow_html=True)
    st.title('HKMA Regulatory Documents')
    st.caption('Real official publications, extracted text and traceable originals. '
               'The document list refreshes every 15 seconds.')
    try:
        service = collection_service(str(root))
        collection_panel(service)
    except (CollectionBusy, ValueError, OSError) as exc:
        st.error(f'Collection service unavailable: {exc}')
    st.divider()
    document_panel(root, root / 'data/ingestion/hkma.sqlite')
