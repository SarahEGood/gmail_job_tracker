# Sample Data

This directory contains sanitized examples of the jobsearch CSV structure.

## Files

- `job_leads.sample.csv`: fictional lead records that mirror the lead catalog columns.
- `application_events.sample.csv`: fictional append-only Gmail event rows.
- `applied_jobs.before.sample.csv`: a snapshot before Gmail/lead reconciliation.
- `applied_jobs.after.sample.csv`: a snapshot after reconciliation.

## What the samples illustrate

The `applied_jobs` snapshots are meant to show a few common tracker outcomes:

- a self-reported application that stays unchanged
- a row updated by a Gmail status message
- a row enriched from a matching job lead
- a row marked for manual review because the signal is not complete
- a row that changes to `Rejected`
- a Gmail-only application that gets added after email parsing

## Safety notes

- All employers, people, URLs, message IDs, and dates here are fictional.
- The sample values are safe to publish publicly.
- Real names, real contact info, and real account details are intentionally absent.
