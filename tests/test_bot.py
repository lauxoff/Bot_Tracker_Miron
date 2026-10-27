import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

os.environ["BOT_TOKEN"] = "123456789:TESTTOKEN"
os.environ["ALLOWED_USER_IDS"] = "1"
os.environ["ADMIN_USER_IDS"] = "1"

import bot


class FeedingInputTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_database_path = bot.DATABASE_PATH
        bot.DATABASE_PATH = os.path.join(self.temp_dir.name, "tracker.db")
        bot.init_db()

    def tearDown(self):
        bot.DATABASE_PATH = self.old_database_path
        self.temp_dir.cleanup()

    def test_accepts_only_a_single_valid_amount(self):
        for text, expected in (
            ("120", 120),
            (" 180 мл ", 180),
            ("90 ML", 90),
            ("500", 500),
        ):
            with self.subTest(text=text):
                self.assertEqual(bot.parse_milliliters(text), expected)

    def test_rejects_invalid_or_ambiguous_amounts(self):
        for text in ("", "9", "501", "1000 мл", "-120", "120.5", "120 мл потом", "съел 120 мл"):
            with self.subTest(text=text):
                self.assertIsNone(bot.parse_milliliters(text))

    def test_custom_amount_prompt_is_scoped_and_expires(self):
        bot.set_waiting_for_ml(chat_id=42, user_id=7)
        self.assertEqual(bot.get_waiting_user_id(42), "7")

        expired_request = {
            "user_id": 7,
            "requested_at": bot.get_now_timestamp() - bot.ML_INPUT_TIMEOUT_SECONDS - 1,
        }
        bot.set_current_state(bot.waiting_key(42), json.dumps(expired_request))

        self.assertIsNone(bot.get_waiting_user_id(42))
        self.assertIsNone(bot.get_current_state(bot.waiting_key(42)))


class EventReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_database_path = bot.DATABASE_PATH
        bot.DATABASE_PATH = os.path.join(self.temp_dir.name, "tracker.db")
        bot.init_db()

    def tearDown(self):
        bot.DATABASE_PATH = self.old_database_path
        self.temp_dir.cleanup()

    def test_overnight_sleep_counts_only_minutes_in_today(self):
        today_start, _ = bot.today_bounds()
        bot.log_event(
            "sleep",
            start_time=today_start - 4 * 60 * 60,
            end_time=today_start + 2 * 60 * 60,
            details="360",
        )
        events = bot.get_today_summary()
        report = bot.format_today_summary(events, now_ts=today_start + 3 * 60 * 60)

        self.assertIn("Всего сна: <b>2ч 0мин</b>", report)
        self.assertNotIn("продолжается", report)

    def test_active_sleep_is_included_in_today_report(self):
        today_start, _ = bot.today_bounds()
        bot.set_current_state("sleep_start", str(today_start + 60 * 60))

        events = bot.get_today_summary()
        report = bot.format_today_summary(events, now_ts=today_start + 150 * 60)

        self.assertIn("Всего сна: <b>1ч 30мин</b>", report)
        self.assertIn("продолжается", report)

    def test_yesterdays_instant_events_are_not_repeated_in_todays_report(self):
        today_start, _ = bot.today_bounds()
        bot.log_event("feeding", start_time=today_start - 60, details="90")

        self.assertEqual(bot.get_today_summary(), [])

    def test_reset_preserves_old_history_and_active_state(self):
        today_start, tomorrow_start = bot.today_bounds()
        bot.log_event("feeding", start_time=today_start - 60, details="90")
        bot.log_event("feeding", start_time=today_start + 60, details="120")
        bot.log_event(
            "sleep",
            start_time=today_start - 120 * 60,
            end_time=today_start + 120 * 60,
            details="240",
        )
        bot.set_current_state("sleep_start", str(today_start + 30))
        bot.set_waiting_for_ml(chat_id=10, user_id=1)

        bot.clear_today_events_in_db()

        with bot.database_connection() as conn:
            events = conn.execute(
                "SELECT event_type, start_time, end_time, details FROM events"
            ).fetchall()
        self.assertEqual(len(events), 2)
        prior_feeding = next(event for event in events if event[0] == "feeding")
        prior_sleep = next(event for event in events if event[0] == "sleep")
        self.assertLess(bot.parse_stored_time(str(prior_feeding[1])), today_start)
        self.assertEqual(bot.parse_stored_time(str(prior_sleep[2])), today_start)
        self.assertEqual(prior_sleep[3], "120")
        self.assertIsNotNone(bot.get_current_state("sleep_start"))
        self.assertIsNotNone(bot.get_current_state("waiting_for_ml:10"))
        self.assertGreater(tomorrow_start, today_start)

    def test_today_bounds_follow_configured_timezone(self):
        now = bot.TIMEZONE.localize(datetime(2026, 10, 15, 12, 0))
        start, end = bot.today_bounds(now)
        self.assertEqual(datetime.fromtimestamp(start, bot.TIMEZONE).date(), now.date())
        self.assertEqual(datetime.fromtimestamp(end, bot.TIMEZONE).date(), now.date() + timedelta(days=1))


class ActivityTransitionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_database_path = bot.DATABASE_PATH
        bot.DATABASE_PATH = os.path.join(self.temp_dir.name, "tracker.db")
        bot.init_db()

    def tearDown(self):
        bot.DATABASE_PATH = self.old_database_path
        self.temp_dir.cleanup()

    def test_only_one_activity_can_start_when_commands_arrive_concurrently(self):
        barrier = threading.Barrier(2)

        def start(activity):
            barrier.wait()
            return bot.start_activity(activity, start_time=1000)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(start, ("sleep", "walk")))

        self.assertEqual(results.count(None), 1)
        self.assertEqual(len([result for result in results if result is not None]), 1)
        with bot.database_connection() as conn:
            active_states = conn.execute(
                "SELECT key FROM state WHERE key IN ('sleep_start', 'walk_start')"
            ).fetchall()
        self.assertEqual(len(active_states), 1)

    def test_finishing_an_activity_is_atomic_and_idempotent(self):
        bot.start_activity("sleep", start_time=1000)

        self.assertEqual(bot.finish_activity("sleep", end_time=1300), (1000.0, 5))
        self.assertIsNone(bot.finish_activity("sleep", end_time=1300))
        with bot.database_connection() as conn:
            events = conn.execute(
                "SELECT event_type, start_time, end_time, details FROM events"
            ).fetchall()
        self.assertEqual(events, [("sleep", 1000.0, 1300, "5")])
        self.assertIsNone(bot.get_current_state("sleep_start"))

    def test_invalid_start_time_does_not_clear_activity_or_create_event(self):
        bot.set_current_state("sleep_start", "invalid")

        with self.assertRaises(ValueError):
            bot.finish_activity("sleep", end_time=1300)

        self.assertEqual(bot.get_current_state("sleep_start"), "invalid")
        with bot.database_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
