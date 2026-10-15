import unittest

from tests.harness import App, EVENTS, event


class TriggerTests(unittest.TestCase):
    def test_registered_triggers_and_services(self):
        app = App()
        kinds = {(t[0], t[1]) for t in app.triggers}
        self.assertIn(("time_trigger", ("startup",)), kinds)
        self.assertIn(("time_trigger", ("cron(*/5 * * * *)",)), kinds)
        self.assertIn(("event_trigger", ("state_changed", f"entity_id == '{EVENTS}'")), kinds)
        self.assertEqual(set(app.service_funcs), {
            "solis_power_down_reconcile", "solis_power_down_clear", "solis_power_down_preview"})

    def test_startup_joins_then_reconciles(self):
        app = App()
        app.capture_ops()
        app.g["log_storage_mode"] = _noop
        app.set_events(available=[event("17:00", "18:00", code="PD9")])
        app.run(app.solis_power_down_startup())
        self.assertIn(60, app.asyncio.sleeps)
        self.assertEqual(app.services.joins(), ["PD9"])
        self.assertEqual(app.status()[1], "planned")
        self.assertEqual(len(app.ops), 1)  # clears the leftover slot

    def test_events_change_replans(self):
        app = App()
        app.capture_ops()
        app.g["_program"]["unknown"] = False
        app.set_events(joined=[event("17:00", "18:00")])
        app.at("16:50")
        app.set_soc(56.25)
        app.run(app.solis_power_down_events_changed(entity_id=EVENTS, old_state=None, new_state=None))
        self.assertEqual(len(app.ops), 1)

    def test_clear_service(self):
        app = App({"sync_inverter_time": False})
        app.cloud.values["5922"] = "1"
        app.run(app.solis_power_down_clear())
        self.assertEqual([w[0] for w in app.cloud.control_writes()], ["5922", "5964"])
        self.assertFalse(app.g["_program"]["unknown"])

    def test_preview_uses_available_session(self):
        app = App()
        app.set_events(available=[event("17:00", "18:00")])
        with self.assertLogs("pyscript.solis_power_down", level="INFO") as logs:
            app.run(app.solis_power_down_preview())
        self.assertTrue(any("predicted_export_kwh" in line for line in logs.output))

    def test_unconfigured_events_entity(self):
        app = App({"power_down_events_entity": None})
        self.assertIn(("event_trigger", ("state_changed", "entity_id == ''")),
                      [(t[0], t[1]) for t in app.triggers])
        kinds = [t[1] for t in app.triggers if t[0] == "state_trigger"]
        self.assertIn(("input_boolean.solis_peak_export_unconfigured",), kinds)

    def test_no_state_trigger_on_event_domain(self):
        # pyscript reads `event` in a state_trigger expression as its own built-in.
        for config in ({}, {"power_down_events_entity": None}):
            app = App(config)
            for kind, args, _ in app.triggers:
                if kind == "state_trigger":
                    for arg in args:
                        self.assertFalse(str(arg).startswith("event."), arg)


async def _noop():
    return None


if __name__ == "__main__":
    unittest.main()
