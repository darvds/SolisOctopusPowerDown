import unittest

from tests.harness import App, EVENTS, TIME_CID, local


def real_joined_events():
    """Shaped like the Octopus integration's joined_events attribute (15 Oct 2026)."""
    return [
        {"id": 6911, "start": "2026-10-12T18:00:00+01:00", "end": "2026-10-12T19:00:00+01:00",
         "duration_in_minutes": 60, "rewarded_octopoints": None, "octopoints_per_kwh": 73,
         "target_regions": ["6", "7", "8", "9", "10", "11", "12"]},
        {"id": 7110, "start": "2026-10-15T18:00:00+01:00", "end": "2026-10-15T19:00:00+01:00",
         "duration_in_minutes": 60, "rewarded_octopoints": None, "octopoints_per_kwh": 75,
         "target_regions": ["4", "6", "7", "8", "9", "10", "11", "12"]},
        {"id": 1324, "start": "2024-12-11T18:00:00+00:00", "end": "2024-12-11T19:00:00+00:00",
         "duration_in_minutes": 60, "rewarded_octopoints": 200, "octopoints_per_kwh": 480,
         "target_regions": []},
    ]


def peak_app(config=None):
    cfg = {"peak_export": True, "battery_capacity_kwh": 16.1}
    cfg.update(config or {})
    app = App(cfg)
    app.capture_ops()
    app.g["_program"]["unknown"] = False
    return app


class EventsEntityStatusTests(unittest.TestCase):
    def test_missing_entity_warns_but_peak_still_runs(self):
        app = peak_app()
        app.at("15:45", day=15)
        app.set_soc(100)
        with self.assertLogs("pyscript.solis_power_down", level="ERROR") as logs:
            app.reconcile()
        self.assertTrue(any(EVENTS in line for line in logs.output))
        attrs = app.status()[2]
        self.assertEqual(attrs["power_down_events"], "missing")
        self.assertIn(EVENTS, attrs["warning"])
        self.assertEqual(attrs["window_kind"], "peak")
        self.assertEqual(app.ops[0][0], ("value", TIME_CID, "16:00-19:00"))

    def test_missing_entity_logged_once_an_hour(self):
        app = peak_app()
        with self.assertLogs("pyscript.solis_power_down", level="ERROR") as logs:
            app.reconcile()
            app.reconcile()
            app.g["_flags"]["events_warned_at"] -= __import__("datetime").timedelta(hours=2)
            app.reconcile()
        errors = [line for line in logs.output if line.startswith("ERROR")]
        self.assertEqual(len(errors), 2)

    def test_not_configured(self):
        app = peak_app({"power_down_events_entity": None})
        app.reconcile()
        self.assertEqual(app.status()[2]["power_down_events"], "not_configured")

    def test_present(self):
        app = peak_app()
        app.set_events(joined=real_joined_events())
        app.reconcile()
        attrs = app.status()[2]
        self.assertEqual(attrs["power_down_events"], "ok")
        self.assertNotIn("warning", attrs)

    def test_missing_entity_skips_auto_join(self):
        app = App()
        app.run(app.auto_join())
        self.assertEqual(app.services.calls, [])


class RealEventsTests(unittest.TestCase):
    def test_15_october_split(self):
        app = peak_app()
        app.set_events(joined=real_joined_events())
        windows = app.export_windows(local("15:45", day=15))
        self.assertEqual(
            [(w["kind"], app.fmt_window(w["start"], w["end"])) for w in windows[:2]],
            [("peak", "16:00-18:00"), ("power_down", "18:00-19:00")])

        app.at("17:50", day=15)
        app.set_soc(55)
        app.reconcile()
        attrs = app.status()[2]
        self.assertEqual(attrs["window"], "16:00-18:00")
        self.assertGreater(attrs["held_for_power_down_kwh"], 0)

        app.at("18:00", day=15)
        app.reconcile()
        attrs = app.status()[2]
        self.assertEqual(attrs["window_kind"], "power_down")
        self.assertEqual(attrs["octopoints_per_kwh"], 75)
        self.assertEqual(app.status()[1], "exporting")
        self.assertEqual(app.active(), "on")


if __name__ == "__main__":
    unittest.main()
