# Context

## Core terms

- `Config`: runtime settings for Gmail auth, local state, and CSV file paths.
- `ParsedMessage`: one Gmail message normalized into a possible job-application event.
- `learned_patterns`: the saved summary of recurring sender domains and subject patterns.
- `state`: saved sync metadata, including the last successful sync time.
- `applied_jobs.csv`: the deduplicated application ledger.
- `application_events.csv`: the append-only event history.
- `job_leads.csv`: the lead catalog used to enrich application rows.

## Flow vocabulary

- `initial sync`: the first run, or a recovery run treated like the first run, which uses the recent lookback window.
- `incremental sync`: later runs, which start from the last successful sync with an overlap window.
- `candidate message`: a Gmail message returned by the search query before parsing.
- `status event`: a parsed message that matches a job-application status pattern.
- `needs review`: a parsed message whose title/company extraction or confidence is not strong enough to trust automatically.
- `learned sender domain`: a sender domain discovered during the historical learning pass and reused to sharpen future queries.
- `setup`: the interactive first-run path that asks for the Gmail address and saves it to the config file.

## Control flow

- `main()` parses CLI flags and dispatches to `run_sync()` or `run_diagnostic()`.
- `--setup` runs the interactive config-save path without starting a sync.
- `run_sync()` is the main pipeline:
  - load config and state
  - authenticate Gmail
  - learn or reload sender patterns
  - build the query
  - list candidate message IDs
  - fetch and parse each message
  - update application rows
  - link lead data
  - deduplicate rows
  - append event rows
  - save local outputs unless `--dry-run`
- `run_diagnostic()` is a no-write inspection path for a small sample of messages.
