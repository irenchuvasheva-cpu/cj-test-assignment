"""Тесты текущего поведения. После ваших правок они должны остаться зелёными."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from digest import (
    MAX_JOBS_PER_RUN,
    after_send,
    filter_pending_jobs,
    job_uids,
    normalize_company_name,
    plan_digest,
    role_key,
)

SAMPLE = json.loads(
    (Path(__file__).resolve().parent / "jobs_sample.json").read_text(encoding="utf-8")
)["jobs"]


def uids(jobs):
    return [j["job_uid"] for j in jobs]


class NormalizeTests(unittest.TestCase):
    def test_ltd_and_tld_collapse(self):
        self.assertEqual(normalize_company_name("Revolut Ltd"), "revolut")
        self.assertEqual(normalize_company_name("Revolut"), "revolut")
        self.assertEqual(normalize_company_name("Booking.com"), "booking")
        self.assertEqual(normalize_company_name("Booking"), "booking")
        self.assertEqual(normalize_company_name("Remote.com"), "remote")


class FilterTests(unittest.TestCase):
    def test_skip_already_sent_and_empty_uid(self):
        jobs = SAMPLE + [{"job_uid": "", "company": "X", "title": "Y"}]
        pending = filter_pending_jobs(jobs, already_sent={"stripe-1"})
        self.assertNotIn("stripe-1", uids(pending))
        self.assertNotIn("", uids(pending))

    def test_skip_hidden(self):
        pending = filter_pending_jobs(SAMPLE, already_sent=set(), hidden={"remote-1"})
        self.assertNotIn("remote-1", uids(pending))

    def test_role_reroll_uses_exact_title(self):
        recent = {role_key("ASOS", "People Partner")}
        pending = filter_pending_jobs(SAMPLE, already_sent=set(), recent_roles=recent)
        self.assertNotIn("asos-new", uids(pending))
        # другой title той же компании не режется
        self.assertTrue(any(j["company"].startswith("Revolut") for j in pending))


class PlanTests(unittest.TestCase):
    def test_paid_groups_revolut_as_one_message(self):
        plan = plan_digest(SAMPLE, is_paid=True)
        revolut = next(g for g in plan.groups if normalize_company_name(g[0]["company"]) == "revolut")
        self.assertEqual(len(revolut), 3)
        self.assertGreaterEqual(len(plan.groups), 4)
        self.assertEqual(plan.leftover_jobs, [])
        self.assertIn("rev-1", plan.mark_sent)
        self.assertIn("rev-2", plan.mark_sent)

    def test_free_gets_only_first_company_rest_leftover_not_marked(self):
        plan = plan_digest(SAMPLE, is_paid=False)
        self.assertEqual(len(plan.groups), 1)
        first_uids = set()
        for job in plan.groups[0]:
            first_uids.update(job_uids(job))
        leftover_uids = set()
        for job in plan.leftover_jobs:
            leftover_uids.update(job_uids(job))
        self.assertTrue(leftover_uids)
        self.assertTrue(first_uids.isdisjoint(leftover_uids))
        self.assertEqual(set(plan.mark_sent), first_uids)
        self.assertFalse(leftover_uids & set(plan.mark_sent))

    def test_merge_same_title_different_cities(self):
        plan = plan_digest(SAMPLE, is_paid=True)
        booking = next(
            g for g in plan.groups if normalize_company_name(g[0]["company"]) == "booking"
        )
        self.assertEqual(len(booking), 1)
        job = booking[0]
        self.assertEqual(set(job_uids(job)), {"book-ams", "book-ber"})
        self.assertIn("Amsterdam", job["location"])
        self.assertIn("Berlin", job["location"])
        self.assertIn("book-ams", plan.mark_sent)
        self.assertIn("book-ber", plan.mark_sent)

    def test_truncate_puts_tail_in_leftover(self):
        plan = plan_digest(SAMPLE, is_paid=True, max_jobs=3)
        sent_count = sum(len(g) for g in plan.groups)
        self.assertEqual(sent_count, 3)
        self.assertTrue(plan.leftover_jobs)
        leftover_uids = {u for job in plan.leftover_jobs for u in job_uids(job)}
        self.assertTrue(leftover_uids)
        self.assertFalse(leftover_uids & set(plan.mark_sent))

    def test_default_max_jobs_cap_exists(self):
        self.assertEqual(MAX_JOBS_PER_RUN, 100)

    def test_after_send_all_delivered_keeps_plan(self):
        plan = plan_digest(SAMPLE, is_paid=True)
        commit = after_send(plan, [True] * len(plan.groups))
        self.assertEqual(commit.mark_sent, plan.mark_sent)

    def test_after_send_delivered_length_must_match(self):
        plan = plan_digest(SAMPLE, is_paid=True)
        with self.assertRaises(ValueError):
            after_send(plan, [True])


if __name__ == "__main__":
    unittest.main()
