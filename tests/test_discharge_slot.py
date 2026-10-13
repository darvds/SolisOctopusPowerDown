import unittest

from tests.harness import (App, CURRENT_CID, SOC_CID, SWITCH_CID, TIME_CID, event)

ARM = [("value", TIME_CID, "17:00-18:00"), ("value", CURRENT_CID, "35"),
       ("value", SOC_CID, "38"), ("switch", True)]
CLEAR = [("switch", False), ("value", TIME_CID, "00:00-00:00")]


def armed_app(config=None):
    app = App(config)
    app.capture_ops()
    app.set_events(joined=[event("17:00", "18:00")])
    return app


class SessionScenarioTests(unittest.TestCase):
    def test_afternoon_session(self):
        app = armed_app()
        app.g["_program"]["unknown"] = False
        steps = [
            # time,  soc, expected ops,                           status
            ("12:00", 80, [], "planned"),
            ("16:40", 56.25, [], "planned"),
            ("16:45", 56.25, [ARM], "armed"),
            ("16:50", 56.25, [], "armed"),
            ("17:00", 56.25, [], "exporting"),
            # Discharging on plan: 5.5 - 0.5 h x 1.75 = 4.625 kWh, still ~35 A.
            ("17:30", 46.25, [], "exporting"),
            # Running hot (SOC lower than planned): current drops by >= 5 A.
            ("17:30", 42, [[("value", CURRENT_CID, "18")]], "exporting"),
            ("18:00", 38, [CLEAR], "idle"),
            ("18:05", 38, [], "idle"),
        ]
        for hhmm, soc, expected, status in steps:
            with self.subTest(time=hhmm, soc=soc):
                app.ops.clear()
                app.at(hhmm)
                app.set_soc(soc)
                app.reconcile()
                self.assertEqual(app.ops, expected)
                self.assertEqual(app.status()[1], status)

    def test_active_sensor(self):
        app = armed_app()
        app.at("16:50")
        app.reconcile()
        self.assertEqual(app.active(), "off")
        app.at("17:10")
        app.reconcile()
        self.assertEqual(app.active(), "on")

    def test_small_current_changes_are_not_written(self):
        app = armed_app()
        app.at("16:45")
        app.set_soc(56.25)
        app.reconcile()
        app.ops.clear()
        app.set_soc(56)  # a few amps less
        app.reconcile()
        self.assertEqual(app.ops, [])
        self.assertEqual(app.status()[2]["programmed_current"], 35)

    def test_startup_clears_leftover_slot(self):
        app = App()
        app.capture_ops()
        app.reconcile("startup")
        self.assertEqual(app.ops, [CLEAR])
        app.ops.clear()
        app.reconcile()
        self.assertEqual(app.ops, [])

    def test_startup_mid_session_writes_everything(self):
        app = armed_app()
        app.at("17:30")
        app.set_soc(46.25)
        app.reconcile("startup")
        self.assertEqual(app.ops, [[("value", TIME_CID, "17:00-18:00"), ("value", CURRENT_CID, "35"),
                                    ("value", SOC_CID, "38"), ("switch", True)]])

    def test_no_spare_energy_leaves_slot_off(self):
        app = armed_app()
        app.g["_program"]["unknown"] = False
        app.at("16:50")
        app.set_soc(30)
        app.reconcile()
        self.assertEqual(app.ops, [])
        self.assertEqual(app.status()[1], "no_spare_energy")

    def test_failed_write_is_retried_in_full(self):
        app = armed_app()
        app.g["_program"]["unknown"] = False
        app.capture_ops(ok=False)
        app.at("16:45")
        app.set_soc(56.25)
        app.reconcile()
        self.assertTrue(app.g["_program"]["unknown"])
        app.capture_ops(ok=True)
        app.reconcile()
        self.assertEqual(app.ops, [ARM])

    def test_status_attributes(self):
        app = armed_app()
        app.at("16:45")
        app.set_soc(56.25)
        app.reconcile()
        attrs = app.status()[2]
        self.assertEqual(attrs["predicted_export_kwh"], 1.25)
        self.assertEqual(attrs["reserve_kwh"], 3.75)
        self.assertEqual(attrs["programmed_window"], "17:00-18:00")
        self.assertEqual(attrs["octopoints_per_kwh"], 800)

    def test_notifies_once_when_armed(self):
        app = armed_app({"notify_service": "notify.phone"})
        app.at("16:45")
        app.set_soc(56.25)
        app.reconcile()
        app.at("17:30")
        app.set_soc(42)
        app.reconcile()
        notes = app.services.notifications()
        self.assertEqual(len(notes), 1)
        self.assertIn("1.25 kWh", notes[0]["data"]["message"])

    def test_other_discharge_slot(self):
        app = armed_app({"discharge_slot": 2})
        app.at("16:45")
        app.set_soc(56.25)
        app.reconcile()
        self.assertEqual([op[1] for op in app.ops[0][:3]], ["5968", "5971", "5969"])


class SolisCloudTests(unittest.TestCase):
    """The real solis_apply against the fake SolisCloud."""

    def test_arm_writes_in_order_with_yuanzhi(self):
        app = App({"sync_inverter_time": False})
        app.cloud.values["5916"] = "1"  # charge slot 1 (core window) is on
        ok = app.run(app.solis_apply(ARM))
        self.assertTrue(ok)
        self.assertEqual(app.cloud.control_writes(), [
            (TIME_CID, "17:00-18:00", None),
            (CURRENT_CID, "35", None),
            (SOC_CID, "38", None),
            (SWITCH_CID, "1", "1"),
        ])

    def test_clear_switches_off_first(self):
        app = App({"sync_inverter_time": False})
        app.cloud.values[SWITCH_CID] = "1"
        app.run(app.solis_apply(CLEAR))
        self.assertEqual(app.cloud.control_writes(), [
            (SWITCH_CID, "0", str(1 << 6)),
            (TIME_CID, "00:00-00:00", None),
        ])

    def test_failure_stops_before_switching_on(self):
        app = App({"sync_inverter_time": False, "control_retries": 1})
        app.cloud.fail_cids.add(CURRENT_CID)
        ok = app.run(app.solis_apply(ARM))
        self.assertFalse(ok)
        cids = [w[0] for w in app.cloud.control_writes()]
        self.assertNotIn(SWITCH_CID, cids)

    def test_diagnostics_only_writes_nothing(self):
        app = App({"diagnostics_only": True})
        self.assertTrue(app.run(app.solis_apply(ARM)))
        self.assertEqual(app.cloud.calls, [])

    def test_time_sync_daily(self):
        app = App()
        app.run(app.solis_apply(CLEAR))
        app.run(app.solis_apply(CLEAR))
        syncs = [c for c in app.cloud.calls if c["body"].get("cid") == "56"]
        self.assertEqual(len(syncs), 1)


if __name__ == "__main__":
    unittest.main()
