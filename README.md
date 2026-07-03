# Gmail Job Application Tracker

This project checks the configured Gmail account for job-application submission and status-update emails, including messages that are already read or archived. It never sends, deletes, archives, labels, or otherwise changes Gmail.

## What it does

On the first successful run, the tracker:

1. Authenticates directly to the configured Gmail account with Google's read-only Gmail permission.
2. Searches matching email across the mailbox's full history to learn recurring applicant-tracking-system senders and subject formats.
3. Records only application events from the most recent 30 days.
4. Creates an append-only `application_events.csv`.
5. Treats `applied_jobs.csv` as the canonical application ledger, preserving
   self-reported applications while adding Gmail statuses.
6. Links applications to matching rows in `job_leads.csv`, bringing over the
   lead score, priority, pay, posting links, fit notes, commute, and risk fields.
7. Generates a normalized `application_key` and merges duplicate application
   rows without discarding the self-reported application date or the newest
   Gmail status.
8. Marks uncertain employer/title matches for manual review instead of guessing.

Later runs search from the previous successful sync with a three-day overlap. Gmail message IDs prevent duplicate events.

Read, archived, and Trash mail are included because searches use `in:anywhere`
and `include_trash` defaults to `true`. TSent mail and Spam are
excluded in both the Gmail query and a second message-label check. Set
`"include_trash": false` in `gmail_tracker_config.json` only if deleted
application updates should be ignored.

## Privacy and safety

- Gmail access uses only `gmail.readonly`.
- The script verifies that OAuth authenticated the exact configured account; it stops if a different account was selected.
- OAuth files are stored under `.gmail_job_tracker/`, which is excluded by `.gitignore`.
- Raw email bodies are never written to disk.
- Event output stores only the sender, subject, date, parsed status, short matched phrase, and Gmail message ID/link.
- The script does not apply to jobs or respond to employers.

## Files

- `gmail_job_tracker.py` — tracker and parser.
- `gmail_tracker_config.example.json` — optional configuration template.
- `job_leads.csv` — researched lead catalog; read by the tracker and left intact.
- `applied_jobs.csv` — canonical combined ledger: self-reported application
  facts, Gmail status, and linked lead research.
- `application_events.csv` — append-only parsed email-event history; created on first successful run.
- `.gmail_job_tracker/learned_patterns.json` — full-history parsing summary, without message bodies.
- `.gmail_job_tracker/state.json` — last successful run and initial-run boundary.
- `run_gmail_tracker.ps1` — Windows wrapper suitable for Task Scheduler.
- `tests/test_gmail_job_tracker.py` — offline parser and boundary tests.

## One-time Gmail setup

Google requires a small local OAuth application:

1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Create or select a project.
3. Enable the **Gmail API**.
4. Open **Google Auth Platform** and configure the consent screen.
5. Open **Google Auth Platform → Audience**.
6. Leave the publishing status as **Testing** and the user type as **External**.
7. Under **Test users**, click **Add users**, enter the Gmail address you
   want to track, and save. Merely using that address as the support/developer
   email does not add it as a test user.
8. Create an OAuth client with application type **Desktop app**.
9. Download the client JSON.
10. Create `.gmail_job_tracker` inside this project.
11. Save the downloaded file as `.gmail_job_tracker/credentials.json`.

The implementation follows Google's [Python Gmail quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python) and requests only the read-only scope.

Do not email, upload, or commit `credentials.json` or `token.json`.

## Install on Windows

Open PowerShell in this folder:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install ruff
```

Python 3.10.7 or newer is required by Google's current quickstart.

If the Python launcher reports that 3.11 is unavailable, first run `py -0p` and
replace `-3.11` with an installed version that is 3.10.7 or newer. The virtual
environment must be created successfully before either `.venv` command will
exist.

## Optional configuration

The script will prompt for the Gmail address the first time you run it if the
config file does not already set one. If you prefer to set it up explicitly
before syncing, create a config file and run `--setup` once:

```powershell
Copy-Item .\gmail_tracker_config.example.json .\gmail_tracker_config.json
.\.venv\Scripts\python.exe .\gmail_job_tracker.py --setup
```

Use the config file to customize limits or paths. Keep `initial_lookback_days`
at `30` for the requested first-run boundary.

`home_location` is an optional free-form field for your own reference and
future workflow use. The tracker stores it in config but does not calculate
commute distance. Commute values in the CSV outputs are copied from the
`commute_minutes_from_home` column in `job_leads.csv`.

## First run

Test parsing without changing CSV files:

```powershell
.\.venv\Scripts\python.exe .\gmail_job_tracker.py --dry-run
```

If this is the first run and no account is saved yet, the script prompts for
the Gmail address to track and an optional home location before it starts the
Gmail authorization flow. A browser opens for Google authorization. Select that
same account. The script stops with an account-mismatch error if another
account is selected.

If Google shows `Error 403: access_denied` and says the app is available only
to developer-approved testers, return to **Google Auth Platform → Audience** and
add the exact Gmail address under **Test users**. Wait a minute, close the failed
authorization tab, and run the dry run again. Google documents that External
apps in Testing mode are limited to accounts explicitly placed on this
allowlist.

Then perform the first recorded sync:

```powershell
.\.venv\Scripts\python.exe .\gmail_job_tracker.py
```

The full-history phase learns patterns only. The first `application_events.csv` remains limited to the newest 30 days.

## Normal updates

```powershell
.\run_gmail_tracker.ps1
```

The wrapper appends run output to `logs/gmail-job-tracker.log`.

To update the saved Gmail account or optional home location without running a
sync:

```powershell
.\.venv\Scripts\python.exe .\gmail_job_tracker.py --setup
```

To rebuild parsing patterns after encountering a new applicant-tracking system:

```powershell
.\.venv\Scripts\python.exe .\gmail_job_tracker.py --relearn
```

`--relearn` changes the learned sender/subject summary; it does not backfill old events outside the original 30-day initial boundary.

## Automate with Windows Task Scheduler

Run the tracker manually once so OAuth is complete. Then:

1. Open **Task Scheduler** and choose **Create Basic Task**.
2. Choose a daily trigger, such as 9:00 AM.
3. Choose **Start a program**.
4. Program/script: `powershell.exe`
5. Arguments:

   ```text
   -NoProfile -ExecutionPolicy Bypass -File "C:\Path\To\gmail_job_tracker\run_gmail_tracker.ps1"
   ```

6. Start in:

   ```text
   C:\Path\To\gmail_job_tracker
   ```

The task should run under your Windows account so it can read the local OAuth token.

## Statuses recognized

- Application received
- Application viewed
- Under review
- Interview
- Assessment/action required
- Offer
- Rejected
- Withdrawn/cancelled
- Generic application update

Job alerts and recommendations are ignored unless the message also contains a strong application-status phrase.

## Manual-review behavior

An event is marked for review when:

- the employer or title could not be extracted;
- parser confidence is below 0.75;
- only the employer, not the job title, matched `applied_jobs.csv`; or
- multiple applications could plausibly match the same email.

In those cases, the email remains in `application_events.csv`, but the tracker does not invent a company/title or overwrite an ambiguous application.

## Linking and duplicate prevention

The tracker normalizes punctuation, capitalization, company suffixes such as
`LLC`/`Inc.`, and schedule qualifiers such as `(Part-Time)` before comparing
jobs. Matching follows this order:

1. company and title;
2. a unique title-only match when an email omits the employer;
3. a unique company-only match when an email omits the title.

Every canonical row receives an `application_key` based on normalized company
and title. Duplicate rows with the same key are merged. A self-reported
`date_applied` and source are preferred, while the newest timestamped Gmail
status wins. `tracking_sources` shows whether a row came from self-reporting,
Gmail, `job_leads.csv`, or a combination.

Lead research is copied into dedicated `lead_*` and fit/risk columns in
`applied_jobs.csv`; the tracker does not add application-status columns to or
otherwise rewrite `job_leads.csv`. This includes `commute_minutes_from_home`,
which is copied through from lead data rather than computed by the tracker.

## Run tests

The tests do not access Gmail:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

They cover receipts, rejections, interviews, viewed applications, job-alert rejection, the 30-day initial query, and conservative updates to an existing application.

## Run Ruff

Ruff checks the repo with a conservative baseline focused on import/runtime
errors and invalid syntax:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
```

The config lives in `pyproject.toml`.

## GitHub Actions CI

`.github/workflows/ci.yml` runs on every GitHub push and uses two jobs:

- `ruff` installs project dependencies plus Ruff and runs `ruff check .`
- `tests` installs project dependencies and runs `python -m unittest discover -s tests -v`

## Resetting OAuth or tracker state

- To authorize a different Gmail account, delete `.gmail_job_tracker/token.json` and rerun manually.
- To relearn patterns without changing the 30-day boundary, use `--relearn`.
- Do not delete `.gmail_job_tracker/state.json` casually. Removing it makes the next run behave like a new initial sync.

## Limits

- Email wording varies, so uncertain messages require review.
- Gmail search finds candidate messages; the parser determines whether each is an actual application event.
