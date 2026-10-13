import unittest

from tests.harness import App, CURRENT_CID, SOC_CID, TIME_CID, event, local

SWITCH = "input_boolean.solis_peak_export"
CLEAR = [("switch", False), ("value", TIME_CID, "00:00-00:00")]


def arm(window, current, soc):
    return [("value", TIME_CID, window), ("value", CURRENT_CID, str(current)),
            ("value", SOC_CID, str(soc)), ("switch", True)]


def peak_app(config=None, joined=()):
    cfg = {"peak_export": True}
    cfg.update(config or {})
    app = App(cfg)
    app.capture_ops()
    app.g["_program"]["unknown"] = False
    app.set_events(joined=joined)
    return app


def kinds(app, now):
    return [(w["kind"], app.fmt_window(w["start"], w["end"])) for w in app.export_windows(now)]


class PeakWindowTests(unittest.TestCase):
    def test_off_by_default(self):
        app = App()
        self.assertEqual(app.export_windows(local("12:00")), [])

    def test_daily_windows_today_and_tomorrow(self):
        app = peak_app()
        ws = app.export_windows(local("12:00"))
        self.assertEqual([(w["start"], w["end"]) for w in ws],
                         [(local("16:00"), local("19:00")), (local("16:00", day=7), local("19:00", day=7))])
        self.assertEqual(ws[0]["id"], "peak-2026-10-06-1600")
        # After today's window, only tomorrow's is left.
        self.assertEqual(len(app.export_windows(local("19:00"))), 1)

    def test_power_down_inside_peak_splits_it(self):
        app = peak_app(joined=[event("17:00", "18:00")])
        self.assertEqual(kinds(app, local("12:00"))[:3], [
            ("peak", "16:00-17:00"), ("power_down", "17:00-18:00"), ("peak", "18:00-19:00")])

    def test_power_down_overlapping_edge(self):
        app = peak_app(joined=[event("18:30", "19:30")])
        self.assertEqual(kinds(app, local("12:00"))[:2], [
            ("peak", "16:00-18:30"), ("power_down", "18:30-19:30")])

    def test_switch_off_skips_peak_but_not_power_down(self):
        app = peak_app({"peak_export_switch": SWITCH}, joined=[event("17:00", "18:00")])
        app.states[SWITCH] = ("off", {})
        self.assertEqual(kinds(app, local("12:00")), [("power_down", "17:00-18:00")])
        app.states[SWITCH] = ("on", {})
        self.assertEqual(len(app.export_windows(local("12:00"))), 4)

    def test_dst_day(self):
        app = peak_app()
        w, _ = app.target_window(local("12:00", day=25))
        self.assertEqual(app.fmt_window(w["start"], w["end"]), "16:00-19:00")
        p = app.predict(w, local("12:00", day=25), 100, None, {}, {})
        self.assertEqual(p["hours"], 3.0)


class PeakPredictionTests(unittest.TestCase):
    def test_peak_only(self):
        app = peak_app()
        w, later = app.target_window(local("15:45"))
        p = app.predict(w, local("15:45"), 100, None, {}, {}, later)
        # 9.875 kWh at 16:00, keep 1 + 19:00-23:30 x 0.5 kW
        self.assertEqual(p["window_kind"], "peak")
        self.assertEqual(p["reserve_kwh"], 3.25)
        self.assertEqual(p["held_for_power_down_kwh"], 0)
        self.assertEqual(p["discharge_current"], 45)
        self.assertEqual(p["stop_soc"], 33)
        self.assertEqual(p["predicted_export_kwh"], 5.13)
        self.assertEqual(p["estimated_export_value_pence"], 82.0)
        self.assertEqual(p["estimated_octopoints"], 0)

    def test_holds_back_for_power_down_inside_peak(self):
        app = peak_app(joined=[event("17:00", "18:00")])
        w, later = app.target_window(local("15:45"))
        p = app.predict(w, local("15:45"), 100, None, {}, {}, later)
        self.assertEqual(p["window"], "16:00-17:00")
        # Power Down can take 5 kW for an hour; 0.5 kWh of that is house load.
        self.assertEqual(p["held_for_power_down_kwh"], 4.5)
        self.assertEqual(p["reserve_kwh"], 8.75)
        self.assertEqual(p["discharge_current"], 23)

    def test_holds_back_for_later_power_down(self):
        app = peak_app(joined=[event("20:00", "21:00")])
        w, later = app.target_window(local("15:45"))
        p = app.predict(w, local("15:45"), 100, None, {}, {}, later)
        self.assertEqual(p["window"], "16:00-19:00")
        self.assertEqual(p["held_for_power_down_kwh"], 4.5)
        self.assertEqual(p["reserve_kwh"], 7.75)

    def test_no_hold_back_for_power_down_after_cheap_window(self):
        app = peak_app(joined=[event("09:00", "10:00", day=7)])
        w, later = app.target_window(local("15:45"))
        p = app.predict(w, local("15:45"), 100, None, {}, {}, later)
        self.assertEqual(p["held_for_power_down_kwh"], 0)

    def test_power_down_in_peak_earns_peak_rate(self):
        app = peak_app(joined=[event("17:00", "18:00")])
        w, later = app.target_window(local("17:00"))
        p = app.predict(w, local("17:00"), 88, None, {}, {}, later)
        self.assertEqual(p["window_kind"], "power_down")
        # 8.8 kWh, keep 1 + 18:00-23:30 x 0.5 kW: 5 kWh spare, capped at 5 kW.
        self.assertEqual(p["discharge_current"], 100)
        self.assertGreater(p["estimated_export_value_pence"], 0)
        self.assertEqual(p["estimated_octopoints"], 400)


class MinExportTests(unittest.TestCase):
    """Little spare energy: forcing a slot below the house load would import at peak."""

    def setUp(self):
        # Your 13 Oct case: ~0.9 kW house, 1.2 kWh spare for the rest of the peak.
        self.app = peak_app({"default_load_kw": 0.9})
        self.app.at("16:25")

    def test_low_export_rate_is_not_armed(self):
        self.app.set_soc(63)
        self.app.reconcile()
        p = self.app.status()[2]
        self.assertGreater(p["discharge_current"], 0)
        self.assertLess(p["predicted_export_kw"], 0.2)
        self.assertEqual(self.app.ops, [])
        self.assertEqual(self.app.status()[1], "no_spare_energy")

    def test_useful_export_rate_is_armed(self):
        self.app.set_soc(100)
        self.app.reconcile()
        self.assertGreaterEqual(self.app.status()[2]["predicted_export_kw"], 0.2)
        self.assertEqual(self.app.status()[1], "exporting")

    def test_armed_slot_cleared_when_rate_drops(self):
        self.app.set_soc(100)
        self.app.reconcile()
        self.app.ops.clear()
        self.app.at("17:30")
        self.app.set_soc(64)  # house used more than predicted
        self.app.reconcile()
        self.assertEqual(self.app.ops, [CLEAR])

    def test_threshold_configurable(self):
        app = peak_app({"default_load_kw": 0.9, "min_export_kw": 0})
        app.at("16:25")
        app.set_soc(63)
        app.reconcile()
        self.assertEqual(app.status()[1], "exporting")


class PeakScenarioTests(unittest.TestCase):
    def run_steps(self, app, steps):
        for hhmm, soc, expected, status in steps:
            with self.subTest(time=hhmm):
                app.ops.clear()
                app.at(hhmm)
                app.set_soc(soc)
                app.reconcile()
                self.assertEqual(app.ops, expected)
                self.assertEqual(app.status()[1], status)

    def test_peak_only_day(self):
        app = peak_app()
        self.run_steps(app, [
            ("15:30", 100, [], "planned"),
            ("15:45", 100, [arm("16:00-19:00", 45, 33)], "armed"),
            ("16:30", 87, [], "exporting"),
            ("19:00", 33, [CLEAR], "no_spare_energy"),  # tomorrow: daytime use leaves too little
        ])
        self.assertEqual(app.active(), "off")
        self.assertEqual(app.status()[2]["window_kind"], "peak")

    def test_power_down_inside_peak(self):
        app = peak_app(joined=[event("17:00", "18:00")])
        self.run_steps(app, [
            ("15:45", 100, [arm("16:00-17:00", 23, 88)], "armed"),
            ("16:30", 94, [], "exporting"),
            ("17:00", 88, [arm("17:00-18:00", 100, 38)], "exporting"),
            ("18:00", 43, [arm("18:00-19:00", 21, 33)], "exporting"),
            ("19:00", 33, [CLEAR], "no_spare_energy"),
        ])

    def test_active_sensor_only_for_power_down(self):
        app = peak_app(joined=[event("17:00", "18:00")])
        app.at("16:30")
        app.reconcile()
        self.assertEqual(app.active(), "off")
        app.at("17:30")
        app.reconcile()
        self.assertEqual(app.active(), "on")

    def test_switch_off_mid_window_clears(self):
        app = peak_app({"peak_export_switch": SWITCH})
        app.states[SWITCH] = ("on", {})
        app.at("16:30")
        app.set_soc(90)
        app.reconcile()
        self.assertEqual(app.status()[1], "exporting")
        app.ops.clear()
        app.states[SWITCH] = ("off", {})
        app.run(app.solis_power_down_peak_switch_changed(var_name=SWITCH, value="off"))
        self.assertEqual(app.ops, [CLEAR])
        self.assertEqual(app.status()[1], "idle")

    def test_peak_notifications_off_by_default(self):
        app = peak_app({"notify_service": "notify.phone"})
        app.at("15:45")
        app.set_soc(100)
        app.reconcile()
        self.assertEqual(app.services.notifications(), [])

    def test_peak_notifications_when_enabled(self):
        app = peak_app({"notify_service": "notify.phone", "notify_peak": True})
        app.at("15:45")
        app.set_soc(100)
        app.reconcile()
        notes = app.services.notifications()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["data"]["title"], "Peak export planned")


if __name__ == "__main__":
    unittest.main()
