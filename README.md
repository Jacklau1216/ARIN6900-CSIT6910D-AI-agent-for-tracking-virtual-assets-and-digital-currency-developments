# Virtual Asset Intelligence

A Streamlit prototype for tracking regulatory and market developments in virtual
assets and digital currencies. The current data pilot covers Hong Kong,
Singapore, the European Union and the United Kingdom, within the broader
multi-jurisdiction project described in [the proposal](proposal.txt).

The original interface now reads real regulatory evidence from SQLite and
public market APIs. The fictional JSONL/CSV fixtures are preserved for reference
and are not loaded by the current app.

## Current capabilities

| Region | Publisher | Supported collection |
|---|---|---|
| Hong Kong | HKMA | Bounded stablecoin topic-page discovery and configured PDFs |
| Singapore | MAS | One configured tokenisation PDF hosted by the Singapore Government Press Centre |
| European Union | ESMA | One configured DLT Pilot Regime article |
| United Kingdom | FCA | One configured stablecoin and crypto custody article |

- Raw bytes, SHA-256 hashes, source URLs, collection times and evidence locations
  are preserved. Identical bytes and processing versions reuse stored results.
- The app combines `data/ingestion/hkma.sqlite` and
  `data/ingestion/regional.sqlite`. Each publication has one current display row;
  historical versions are retained.
- Market providers supply BTC market capitalization, USDC circulation and an
  individual RWA protocol's TVL. These are different metrics, not market shares.
- Regulatory records currently show `Publication: <title>` previews, not extracted
  obligations or assessed impacts. Risk, confidence and review remain unassessed.
- Keyword evidence lookup is available. No LLM, Agent, RAG or generated answers
  are included in this version.

## Setup and run

Use Python 3.10 or later with SQLite 3.38 or later. Run commands from the repository
root. Network access is required for initial collection and market requests.

### 1. Create and activate a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Windows Command Prompt:

```bat
python -m venv .venv
.venv\Scripts\activate.bat
```

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

If activation is restricted, use the virtual environment's Python executable
explicitly (for example, `.\.venv\Scripts\python.exe` on Windows).

### 2. Install the complete application dependencies

```bash
python -m pip install -r requirements-app.txt
```

This includes both the original UI requirements and `requirements-ingestion.txt`.
Installing only `requirements.txt` is insufficient for the current app.

### 3. Launch the UI

```bash
python -m streamlit run src/app.py --server.address 127.0.0.1 --server.port 8503
```

Open [the local app](http://127.0.0.1:8503/). Opening the app starts its collection
worker. On a fresh checkout it creates the databases and starts the first
collection; the initial view may be empty until collection succeeds. Reload or
interact with the page after the 60-second data cache expires to see new records.
The repository does not include the developer's collected database snapshot.

The existing sidebar provides:

- **Overview:** publication counts, evidence counts and regulatory records.
- **Regulatory Monitor:** jurisdiction and asset filters with source evidence.
- **Market Signals:** real API observations with provenance and data quality.
- **Ask the Analyst:** literal keyword matching over collected evidence.

The default collection interval is 24 hours, measured from completion of the
previous run. Settings and status persist under `data/ingestion/automation/`.
The Streamlit server and computer must stay running; this is not an OS scheduler.
MAS, ESMA and FCA check only their configured publications, not new article lists.
Market requests use a separate cache (normally 15 minutes); a static page does
not refresh itself. Stop the server with `Ctrl+C`.

### Optional manual collection

Set the module path for the current shell:

```powershell
# PowerShell
$env:PYTHONPATH = "$PWD/src"
```

```bash
# macOS/Linux
export PYTHONPATH="$PWD/src"
```

For a fresh HKMA database, initialize once, then collect a configured document
or a bounded batch of discovered articles:

```bash
python -m virtual_asset_agent init-db
python -m virtual_asset_agent ingest
python -m virtual_asset_agent ingest-discovered --limit 10
```

Skip `init-db` when the database already exists; it never overwrites data.
For an older schema-v1 database, back it up before running
`python -m virtual_asset_agent migrate-db`. Avoid manual HKMA ingestion while
its background collection is running.

Collect the three additional regions with:

```bash
python -m regional_ingestion
```

This command initializes its separate database when absent. Check the per-source
JSON statuses for failures. Logs are stored locally under `logs/`.

## Limitations and data handling

- Coverage is a bounded pilot, not complete regional or historical coverage.
  Source layouts, availability and rate limits can affect collection.
- MAS collection uses an official government-hosted MAS publication. The disabled
  MAS placeholder in `config/sources.yaml` is separate from the enabled PDF in
  `config/regional_sources.json`.
- FCA HTML can change scripts and sharing attributes without changing its article
  text. Such responses retain new raw versions and processing results; they do
  not create duplicate publication rows. Regional HTML volatility normalization
  is not implemented.
- Unknown dates and classifications remain missing or explicitly unknown. There
  is no OCR, complete table reconstruction, legal-effect inference or model review.
- API failures may produce missing or stale market observations. Values are not
  replaced with fictional fixtures.
- Runtime files under `data/raw/`, `data/processed/`, `data/discovery/`,
  `data/ingestion/`, `data/market/` and `logs/` are local artifacts. Do not include
  them in source commits. The current `.gitignore` does not exclude all of them;
  stage files explicitly instead of using `git add .`.

## Preserved demo data

The original fictional fixtures remain under [`data/canonical/`](data/canonical):

- `documents.jsonl`: publication metadata and provenance. Key fields: `document_id`, `title`, `source_url`, `source_type`, `jurisdiction`, `regulator`, `publication_date`, `retrieved_at`, `asset_classes`, `lifecycle_stage`, `text_path`, and `content_sha256`.
- `chunks.jsonl`: citation-preserving evidence excerpts. Key fields: `chunk_id`, `document_id`, `text`, `page`, `section`, `is_table`, `citation`, and `retrieval_terms`.
- `developments.jsonl`: normalized regulatory findings. Key fields: `development_id`, `document_id`, `jurisdiction`, `regulator`, `asset_class`, `lifecycle_stage`, `affected_actors`, `obligation_summary`, `impact_tags`, `risk_rating`, `evidence_chunk_ids`, `confidence`, and `review_status`.
- `market_observations.csv`: market indicators. Columns: `observation_id`, `instrument`, `asset_class`, `venue`, `metric`, `value`, `currency`, `observed_at`, `source_url`, `provider`, and `data_quality`.

JSONL uses one JSON object per line. Dates and timestamps use ISO 8601; machine timestamps are UTC. Raw source files should remain immutable, and every generated claim should reference one or more `chunk_id` values.

## Project files

- [`src/app.py`](src/app.py): original Streamlit interface connected to real data.
- [`src/app_data.py`](src/app_data.py): adapters for the original DataFrame interfaces.
- [`src/dashboard_data.py`](src/dashboard_data.py): read-only corpus access.
- [`src/live_documents.py`](src/live_documents.py): cached collection service and document helpers.
- [`src/collection_service.py`](src/collection_service.py): app-owned collection worker.
- [`src/market_data.py`](src/market_data.py): public market data clients and caching.
- [`src/regional_ingestion.py`](src/regional_ingestion.py): three additional regional publications.
- [`src/virtual_asset_agent/`](src/virtual_asset_agent): HKMA pipeline and shared evidence storage.
- [`config/sources.yaml`](config/sources.yaml): HKMA source configuration.
- [`config/regional_sources.json`](config/regional_sources.json): regional targets and parsing configuration.
- [`config/market_sources.json`](config/market_sources.json): market provider configuration.
- [`db/schema.sql`](db/schema.sql) and [`db/regional_schema.sql`](db/regional_schema.sql): SQLite schemas.
- [`requirements-app.txt`](requirements-app.txt): complete application dependency entry point.
- [`data/canonical/`](data/canonical): preserved fictional demo fixtures.
- [`proposal.txt`](proposal.txt): original project proposal.
