import base64
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from gmail_job_tracker import (
    APPLIED_COLUMNS,
    ParsedMessage,
    canonical_application_key,
    classify_status,
    deduplicate_application_rows,
    link_leads_to_applications,
    parse_message,
    query_for_initial,
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

    def test_existing_application_is_updated_without_losing_date(self):
        row = {column: "Unknown" for column in APPLIED_COLUMNS}
        row.update(
            {
                "date_applied": "2026-06-28",
                "title": "Administrative Specialist II (Part-Time)",
                "company": "Mt. San Antonio College",
                "status": "Applied",
            }
        )
        event = ParsedMessage(
            message_id="m1",
            thread_id="t1",
            email_date="2026-07-02T12:00:00+00:00",
            sender="Mt. SAC <jobs@mtsac.edu>",
            sender_domain="mtsac.edu",
            subject="Your application is under review",
            status="under_review",
            title="Administrative Specialist II (Part-Time)",
            company="Mt. San Antonio College",
            confidence=0.95,
            needs_review=False,
            gmail_url="https://mail.google.com/mail/u/0/#all/m1",
            labels="CATEGORY_UPDATES",
            evidence="application is under review",
        )
        updated, matches = update_application_rows([row], [event])
        self.assertEqual(updated[0]["date_applied"], "2026-06-28")
        self.assertEqual(updated[0]["status"], "Under review")
        self.assertEqual(matches["m1"], "company+title")

    def test_self_reported_application_links_to_job_lead(self):
        application = {column: "Unknown" for column in APPLIED_COLUMNS}
        application.update(
            {
                "date_applied": "2026-06-28",
                "title": "Administrative Specialist II (Part-Time)",
                "company": "Mt. San Antonio College",
                "source": "Self-reported",
                "status": "Applied",
                "notes": "Application confirmed directly by Sarah.",
            }
        )
        lead = {
            "date_found": "2026-06-28",
            "priority": "A",
            "score": "88",
            "title": "Administrative Specialist II (Part-Time)",
            "company": "Mt. San Antonio College",
            "location": "Walnut, CA",
            "posting_url": "https://example.test/mtsac",
            "pay_rate": "$30,852-$39,376/year",
            "job_family": "Education operations",
        }
        linked = link_leads_to_applications([application], [lead])
        self.assertEqual(linked[0]["lead_priority"], "A")
        self.assertEqual(linked[0]["lead_score"], "88")
        self.assertEqual(linked[0]["posting_url"], "https://example.test/mtsac")
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


if __name__ == "__main__":
    unittest.main()
