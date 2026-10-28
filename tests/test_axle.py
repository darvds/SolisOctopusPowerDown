import unittest

from tests.harness import App, AXLE_SENSORS, TIME_CID, event, local


def axle_app(config=None, joined=()):
    cfg = {"peak_export": True, "axle_events": True}
    cfg.update(config or {})
    app = App(cfg)
    app.capture_ops()
    app.g["_program"]["unknown"] = False
    app.set_events(joined=joined)
    app.set_axle()
    return app


def kinds(app, now):
    return [(w["kind"], app.fmt_window(w["start"], w["end"])) for w in app.export_windows(now)]


class AxleEventParsingTests(unittest.TestCase):
    def test_export_event(self):
        app = axle_app()
        app.set_axle("export", "17:30", "18:30")
        ev = app.axle_event(local("12:00"))
        self.assertEqual(ev["kind"], "axle_export")
        self.assertEqual(ev["start"], local("17:30"))
        self.assertEqual(ev["event_end"], local("18:30"))
        self.assertEqual(ev["end"], local("18:40"))  # 10 min grace

    def test_import_event(self):
        app = axle_app()
        app.set_axle("import", "13:00", "14:00")
        self.assertEqual(app.axle_event(local("12:00"))["kind"], "axle_import")

    def test_no_event(self):
        app = axle_app()
        self.assertIsNone(app.axle_event(local("12:00")))

    def test_ended_after_grace(self):
        app = axle_app()
        app.set_axle("export", "17:30", "18:30")
        self.assertIsNotNone(app.axle_event(local("18:35")))
        self.assertIsNone(app.axle_event(local("18:40")))

    def test_disabled(self):
        app = axle_app({"axle_events": False})
        app.set_axle("export", "17:30", "18:30")
        self.assertIsNone(app.axle_event(local("12:00")))
        self.assertEqual(app.axle_entities_status(), "disabled")


class AxleWindowTests(unittest.TestCase):
    def test_cuts_peak(self):
        app = axle_app()
        app.set_axle("export", "17:30", "18:30")
        self.assertEqual(kinds(app, local("12:00"))[:3], [
            ("peak", "16:00-17:30"), ("axle_export", "17:30-18:40"), ("peak", "18:40-19:00")])

    def test_axle_beats_power_down(self):
        app = axle_app(joined=[event("18:00", "19:00", eid="101")])
        app.set_axle("export", "17:30", "18:30")
        windows = app.export_windows(local("12:00"))
        self.assertEqual([(w["kind"], app.fmt_window(w["start"], w["end"])) for w in windows[:3]], [
            ("peak", "16:00-17:30"), ("axle_export", "17:30-18:40"), ("power_down", "18:40-19:00")])
        self.assertEqual(windows[2]["id"], "101-1840")
        self.assertEqual(windows[2]["octopoints_per_kwh"], 800)

    def test_import_event_cuts_peak(self):
        app = axle_app()
        app.set_axle("import", "17:00", "18:00")
        self.assertEqual(kinds(app, local("12:00"))[:3], [
            ("peak", "16:00-17:00"), ("axle_import", "17:00-18:10"), ("peak", "18:10-19:00")])


class AxleHoldBackTests(unittest.TestCase):
    def plan_at(self, app, hhmm, soc=100):
        w, later = app.target_window(local(hhmm))
        return app.predict(w, local(hhmm), soc, None, {}, {}, later)

    def test_peak_holds_back_for_axle_export(self):
        app = axle_app()
        app.set_axle("export", "17:30", "18:30")
        p = self.plan_at(app, "15:45")
        self.assertEqual(p["window"], "16:00-17:30")
        # 5 kW over 17:30-18:40, less the 0.5 kW house load.
        self.assertEqual(p["held_for_power_down_kwh"], 5.25)

    def test_power_down_before_axle_holds_back(self):
        app = axle_app({"peak_export": False}, joined=[event("16:00", "17:00")])
        app.set_axle("export", "18:00", "19:00")
        p = self.plan_at(app, "15:50")
        self.assertEqual(p["window_kind"], "power_down")
        self.assertGreater(p["held_for_power_down_kwh"], 0)

    def test_no_hold_back_for_import(self):
        app = axle_app()
        app.set_axle("import", "17:30", "18:30")
        self.assertEqual(self.plan_at(app, "15:45")["held_for_power_down_kwh"], 0)

    def test_no_hold_back_after_cheap_window(self):
        app = axle_app()
        app.set_axle("export", "09:00", "10:00", day=7)
        self.assertEqual(self.plan_at(app, "15:45")["held_for_power_down_kwh"], 0)


class AxleStandAsideTests(unittest.TestCase):
    def test_no_writes_during_event(self):
        app = axle_app({"battery_capacity_kwh": 16.1})
        app.set_axle("export", "17:30", "18:30")
        app.set_soc(100)
        app.at("15:45")
        app.reconcile()
        self.assertEqual(app.ops[-1][0], ("value", TIME_CID, "16:00-17:30"))
        for hhmm in ("17:30", "17:45", "18:30", "18:35"):
            with self.subTest(time=hhmm):
                app.ops.clear()
                app.at(hhmm)
                app.set_soc(60)
                app.reconcile()
                self.assertEqual(app.ops, [])
                self.assertEqual(app.status()[1], "axle_event")
        attrs = app.status()[2]
        self.assertEqual(attrs["axle_event_type"], "export")
        self.assertEqual(attrs["axle_event_end"], local("18:30").isoformat())
        self.assertEqual(app.active(), "off")
        # After the grace period the app takes over again.
        app.at("18:40")
        app.reconcile()
        self.assertNotEqual(app.ops, [])
        self.assertNotEqual(app.status()[1], "axle_event")

    def test_event_announced_after_arming_clears_once(self):
        app = axle_app()
        app.set_soc(100)
        app.at("15:45")
        app.reconcile()
        self.assertEqual(app.ops[-1][0], ("value", TIME_CID, "16:00-19:00"))
        app.ops.clear()
        app.set_axle("export", "17:30", "18:30")
        app.at("17:35")
        app.reconcile()
        self.assertEqual(app.ops, [[("switch", False), ("value", TIME_CID, "00:00-00:00")]])
        app.ops.clear()
        app.at("17:40")
        app.reconcile()
        self.assertEqual(app.ops, [])

    def test_restart_mid_event_writes_nothing(self):
        app = axle_app()
        app.g["_program"]["unknown"] = True
        app.set_axle("export", "17:30", "18:30")
        app.at("17:45")
        app.reconcile("startup")
        self.assertEqual(app.ops, [])
        self.assertTrue(app.g["_program"]["unknown"])

    def test_upcoming_event_still_clears_leftover_slot(self):
        app = axle_app({"peak_export": False})
        app.g["_program"]["unknown"] = True
        app.set_axle("export", "20:00", "21:00")
        app.at("19:05")
        app.reconcile("startup")
        self.assertEqual(app.ops, [[("switch", False), ("value", TIME_CID, "00:00-00:00")]])
        self.assertEqual(app.status()[1], "axle_upcoming")

    def test_import_event_stops_peak_export(self):
        app = axle_app()
        app.set_axle("import", "17:00", "18:00")
        app.set_soc(100)
        app.at("16:30")
        app.reconcile()
        self.assertEqual(app.ops[-1][0], ("value", TIME_CID, "16:00-17:00"))
        app.ops.clear()
        app.at("17:15")
        app.reconcile()
        self.assertEqual(app.ops, [])
        self.assertEqual(app.status()[2]["axle_event_type"], "import")


class AxleEntityTests(unittest.TestCase):
    def test_missing_sensors_warn_and_peak_runs(self):
        app = App({"peak_export": True, "axle_events": True})
        app.capture_ops()
        app.g["_program"]["unknown"] = False
        app.set_soc(100)
        app.at("15:45")
        with self.assertLogs("pyscript.solis_power_down", level="ERROR") as logs:
            app.reconcile()
        self.assertTrue(any("sensor.axle_energy_event_type" in line for line in logs.output))
        attrs = app.status()[2]
        self.assertEqual(attrs["axle_events"], "missing")
        self.assertIn("Axle", attrs["warning"])
        self.assertEqual(attrs["window_kind"], "peak")

    def test_ok_status(self):
        app = axle_app()
        app.reconcile()
        self.assertEqual(app.status()[2]["axle_events"], "ok")

    def test_trigger(self):
        app = axle_app()
        triggers = [(t[0], t[1]) for t in app.triggers]
        self.assertIn(("event_trigger", ("state_changed", f"entity_id in {AXLE_SENSORS!r}")), triggers)
        app.set_axle("export", "17:30", "18:30")
        app.at("17:00")
        app.set_soc(100)
        app.run(app.solis_power_down_axle_changed(entity_id=AXLE_SENSORS[0]))
        self.assertEqual(app.status()[2]["last_reason"], "axle event changed")

    def test_trigger_when_disabled_matches_nothing(self):
        app = App()
        triggers = [(t[0], t[1]) for t in app.triggers]
        self.assertIn(("event_trigger", ("state_changed", "entity_id in []")), triggers)


if __name__ == "__main__":
    unittest.main()
