import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gmail_job_tracker import (
    APPLIED_COLUMNS,
    Config,
    ParsedMessage,
    canonical_application_key,
    classify_status,
    deduplicate_application_rows,
    link_leads_to_applications,
    load_config,
    parse_message,
    query_for_initial,
    resolve_runtime_config,
    save_config,
    warn_if_stale_sync,
    update_application_rows,
)


def encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def message(subject: str, body: str, sender: str = "Indeed <no-reply@indeed.com>"):
    return {
        "id": "abc123",
        "threadId": "thread123",
        "internalDate": "1782662400000",
        "labelIds": ["CATEGORY_UPDATES"],
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "Date", "value": "Sun, 28 Jun 2026 08:00:00 -0700"},
            ],
            "parts": [{"mimeType": "text/plain", "body": {"data": encoded(body)}}],
        },
    }


class ParserTests(unittest.TestCase):
    def test_application_receipt(self):
        parsed = parse_message(
            message(
                "Application received - Operations Analyst at JW Fulfillment",
                "Thank you for applying for the Operations Analyst position at JW Fulfillment.",
            )
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.status, "application_received")
        self.assertEqual(parsed.title, "Operations Analyst")
        self.assertEqual(parsed.company, "JW Fulfillment")

    def test_rejection_beats_generic_thank_you(self):
        parsed = parse_message(
            message(
                "Update on your application for Office Assistant at Example Co",
                "Thank you for applying. We have decided not to proceed with your application.",
            )
        )
        self.assertEqual(parsed.status, "rejected")

    def test_interview(self):
        parsed = parse_message(
            message(
                "Interview invitation",
                "We would like to interview you for the Administrative Assistant position at Example Co.",
                "Example Co Recruiting <recruiting@example.com>",
            )
        )
        self.assertEqual(parsed.status, "interview")
        self.assertEqual(parsed.company, "Example Co")

    def test_viewed(self):
        status, _ = classify_status(
            "Your application was viewed", "The employer viewed your application."
        )
        self.assertEqual(status, "viewed")

    def test_job_alert_is_ignored(self):
        parsed = parse_message(
            message(
                "Job alert: administrative assistant",
                "Recommended jobs you may be interested in. Apply now.",
            )
        )
        self.assertIsNone(parsed)

    def test_initial_query_has_hard_30_day_window(self):
        query = query_for_initial(30)
        self.assertIn("in:anywhere", query)
        self.assertIn("-in:sent", query)
        self.assertIn("-in:spam", query)
        self.assertNotIn("-in:spam -in:trash -in:sent", query)
        self.assertIn("newer_than:30d", query)

    def test_indeed_subject_extracts_title(self):
        parsed = parse_message(
            message(
                "Indeed Application: Administrative/QuickBooks Assistant",
                "Your application has been submitted. Good luck!",
            )
        )
        self.assertEqual(parsed.title, "Administrative/QuickBooks Assistant")
        self.assertEqual(parsed.company, "Unknown")

    def test_learned_sender_expands_future_search(self):
        query = query_for_initial(
            30,
            {"sender_domains": {"notifications.example-ats.com": 4, "one-off.test": 1}},
        )
        self.assertIn("from:notifications.example-ats.com", query)
        self.assertNotIn("from:one-off.test", query)

    def test_applied_columns_use_generic_commute_field(self):
        self.assertIn("commute_minutes_from_home", APPLIED_COLUMNS)
        self.assertNotIn("commute_minutes_from_fontana", APPLIED_COLUMNS)

    def test_existing_application_is_updated_without_losing_date(self):
        row = {column: "Unknown" for column in APPLIED_COLUMNS}
        row.update(
            {
                "date_applied": "2026-06-28",
                "title": "Job (Part-Time)",
                "company": "Company",
                "status": "Applied",
            }
        )
        event = ParsedMessage(
            message_id="m1",
            thread_id="t1",
            email_date="2026-07-02T12:00:00+00:00",
            sender="Company <jobs@job.com>",
            sender_domain="mtsac.edu",
            subject="Your application is under review",
            status="under_review",
            title="Job (Part-Time)",
            company="Company",
            confidence=0.95,
            needs_review=False,
            gmail_url="https://mail.google.com/mail/u/0/#all/m1",
            labels="CATEGORY_UPDATES",
            evidence="application is under review",
        )
        updated, matches = update_application_rows([row], [event])
        self.assertEqual(updated[0]["date_applied"], "2026-06-28")
        self.assertEqual(updated[0]["status"], "Under review")
        self.assertEqual(updated[0]["response_signal"], "Under review")
        self.assertEqual(updated[0]["status_updated_at"], "2026-07-02T12:00:00+00:00")
        self.assertEqual(
            updated[0]["next_action"],
            "Monitor for interview or assessment instructions.",
        )
        self.assertEqual(matches["m1"], "company+title")

    def test_rejection_updates_response_signal_and_follow_up_fields(self):
        row = {column: "Unknown" for column in APPLIED_COLUMNS}
        row.update(
            {
                "date_applied": "2026-06-28",
                "title": "Office Assistant",
                "company": "Example Co",
                "status": "Applied",
                "response_signal": "Unknown",
                "next_action": "Unknown",
            }
        )
        event = ParsedMessage(
            message_id="m2",
            thread_id="t2",
            email_date="2026-07-03T12:00:00+00:00",
            sender="Example Co <jobs@example.com>",
            sender_domain="example.com",
            subject="Update on your application for Office Assistant at Example Co",
            status="rejected",
            title="Office Assistant",
            company="Example Co",
            confidence=0.94,
            needs_review=False,
            gmail_url="https://mail.google.com/mail/u/0/#all/m2",
            labels="CATEGORY_UPDATES",
            evidence="decided not to proceed",
        )
        updated, matches = update_application_rows([row], [event])
        self.assertEqual(updated[0]["status"], "Rejected")
        self.assertEqual(updated[0]["response_signal"], "Rejected")
        self.assertEqual(updated[0]["status_updated_at"], "2026-07-03T12:00:00+00:00")
        self.assertEqual(
            updated[0]["next_action"],
            "Close the application and retain the employer/role history.",
        )
        self.assertEqual(updated[0]["gmail_message_id"], "m2")
        self.assertEqual(matches["m2"], "company+title")

    def test_self_reported_application_links_to_job_lead(self):
        application = {column: "Unknown" for column in APPLIED_COLUMNS}
        application.update(
            {
                "date_applied": "2026-06-28",
                "title": "Job (Part-Time)",
                "company": "Company",
                "source": "Self-reported",
                "status": "Applied",
                "notes": "Application confirmed manually.",
            }
        )
        lead = {
            "date_found": "2026-06-28",
            "priority": "A",
            "score": "88",
            "title": "Job",
            "company": "Company",
            "location": "Johnsville, CA",
            "posting_url": "https://example.test/mtsac",
            "commute_minutes_from_home": "10-20 estimated",
            "pay_rate": "$30,000-$400,376/year",
            "job_family": "Example",
        }
        linked = link_leads_to_applications([application], [lead])
        self.assertEqual(linked[0]["lead_priority"], "A")
        self.assertEqual(linked[0]["lead_score"], "88")
        self.assertEqual(linked[0]["posting_url"], "https://example.test/mtsac")
        self.assertEqual(linked[0]["commute_minutes_from_home"], "10-20 estimated")
        self.assertIn("Self-reported", linked[0]["tracking_sources"])
        self.assertIn("Job leads", linked[0]["tracking_sources"])

    def test_deduplicate_keeps_self_report_and_latest_gmail_status(self):
        self_reported = {column: "Unknown" for column in APPLIED_COLUMNS}
        self_reported.update(
            {
                "date_applied": "2026-06-28",
                "title": "Office Assistant",
                "company": "Example LLC",
                "source": "Indeed",
                "status": "Applied",
            }
        )
        gmail = {column: "Unknown" for column in APPLIED_COLUMNS}
        gmail.update(
            {
                "date_applied": "Unknown",
                "title": "Office Assistant",
                "company": "Example",
                "source": "Gmail",
                "status": "Interview",
                "status_updated_at": "2026-07-01T12:00:00+00:00",
                "gmail_message_id": "m2",
            }
        )
        deduped = deduplicate_application_rows([self_reported, gmail])
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["date_applied"], "2026-06-28")
        self.assertEqual(deduped[0]["status"], "Interview")
        self.assertEqual(deduped[0]["gmail_message_id"], "m2")
        self.assertEqual(
            deduped[0]["application_key"],
            canonical_application_key("Office Assistant", "Example LLC"),
        )

    def test_config_round_trip_preserves_home_location(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "gmail_tracker_config.json"
            original = Config(account="user@example.com", home_location="Riverside, CA")
            save_config(path, original)
            loaded = load_config(path)
        self.assertEqual(loaded.account, "user@example.com")
        self.assertEqual(loaded.home_location, "Riverside, CA")

    def test_setup_reuses_existing_home_location_when_enter_pressed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "gmail_tracker_config.json"
            config = Config(account="user@example.com", home_location="Riverside, CA")
            with (
                patch("builtins.input", side_effect=["", ""]),
                patch("sys.stdin.isatty", return_value=True),
            ):
                resolved = resolve_runtime_config(config, path, force_prompt=True)
        self.assertEqual(resolved.account, "user@example.com")
        self.assertEqual(resolved.home_location, "Riverside, CA")

    def test_setup_allows_blank_home_location(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "gmail_tracker_config.json"
            config = Config()
            with (
                patch("builtins.input", side_effect=["user@example.com", ""]),
                patch("sys.stdin.isatty", return_value=True),
            ):
                resolved = resolve_runtime_config(config, path, force_prompt=True)
        self.assertEqual(resolved.account, "user@example.com")
        self.assertEqual(resolved.home_location, "")


class DocumentationEncodingTests(unittest.TestCase):
    def test_readme_and_samples_remain_clean_utf8(self):
        repo_root = Path(__file__).resolve().parents[1]
        replacement = chr(0xFFFD)
        mojibake_a = chr(0x00E2)
        mojibake_c = chr(0x00C3)
        em_dash = chr(0x2014)
        arrow = chr(0x2192)
        files = [
            repo_root / "README.md",
            repo_root / "sample_data" / "README.md",
            repo_root / "sample_data" / "application_events.sample.csv",
            repo_root / "sample_data" / "applied_jobs.after.sample.csv",
            repo_root / "sample_data" / "applied_jobs.before.sample.csv",
            repo_root / "sample_data" / "job_leads.sample.csv",
        ]

        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(replacement, text, path.name)
            self.assertNotIn(mojibake_a, text, path.name)
            self.assertNotIn(mojibake_c, text, path.name)

        readme = (repo_root / "README.md").read_text(encoding="utf-8")
        files_section = readme.split("## Files", 1)[1].split("## One-time Gmail setup", 1)[0]
        setup_section = readme.split("## One-time Gmail setup", 1)[1].split("## Install on Windows", 1)[0]
        first_run_section = readme.split("## First run", 1)[1].split("## Normal updates", 1)[0]

        self.assertIn(f"`gmail_job_tracker.py` {em_dash} tracker and parser.", files_section)
        self.assertIn(
            f"`run_gmail_tracker.ps1` {em_dash} Windows wrapper suitable for Task Scheduler.",
            files_section,
        )
        self.assertIn(f"Open **Google Auth Platform {arrow} Audience**.", setup_section)
        self.assertIn("If Google shows `Error 403: access_denied`", first_run_section)


class ScheduledRunGuardTests(unittest.TestCase):
    def test_wrapper_creates_log_file_before_launching_python(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = (repo_root / "run_gmail_tracker.ps1").read_text(encoding="utf-8")
        touch_line = 'New-Item -ItemType File -Path $logFile -Force | Out-Null'
        launch_line = '& $python ".\\gmail_job_tracker.py" *>&1 |'
        self.assertIn(touch_line, script)
        self.assertIn(launch_line, script)
        self.assertLess(script.index(touch_line), script.index(launch_line))

    def test_warns_when_last_successful_sync_is_stale(self):
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            warn_if_stale_sync(
                {"last_successful_sync": "2026-06-29T12:00:00+00:00"},
                stale_after_days=3,
            )
        warning = stderr.getvalue()
        self.assertIn("Warning: last successful sync was", warning)
        self.assertIn("2026-06-29", warning)


if __name__ == "__main__":
    unittest.main()
