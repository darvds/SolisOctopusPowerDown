import unittest

from tests.harness import App, EVENTS, event, local


class SessionTests(unittest.TestCase):
    def test_parses_joined_and_available(self):
        app = App()
        app.set_events(joined=[event("17:00", "18:00", eid="1")],
                       available=[event("17:00", "18:00", eid="1"), event("16:00", "17:00", eid="2", code="PD2")])
        joined, available = app.sessions()
        self.assertEqual([s["id"] for s in joined], ["1"])
        self.assertEqual(joined[0]["start"], local("17:00"))
        self.assertEqual([s["code"] for s in available], ["PD2"])

    def test_target_is_current_or_next_joined(self):
        app = App()
        app.set_events(joined=[event("19:00", "20:00", eid="2"), event("17:00", "18:00", eid="1")])
        app.at("12:00")
        self.assertEqual(app.target_window(local("12:00"))[0]["id"], "1")
        self.assertEqual(app.target_window(local("17:30"))[0]["id"], "1")
        self.assertEqual(app.target_window(local("18:00"))[0]["id"], "2")
        self.assertIsNone(app.target_window(local("20:00"))[0])

    def test_ignores_bad_events(self):
        app = App()
        app.set_events(joined=[{"id": "x", "start": None, "end": None}, {"id": "y", "start": "nope", "end": "nope"}])
        self.assertEqual(app.sessions(), ([], []))

    def test_missing_entity(self):
        app = App()
        self.assertIsNone(app.target_window(local("12:00"))[0])


class AutoJoinTests(unittest.TestCase):
    def test_joins_available_once(self):
        app = App({"notify_service": "notify.mobile_app_phone"})
        app.set_events(available=[event("17:00", "18:00", eid="5", code="PD5")])
        app.run(app.auto_join())
        app.run(app.auto_join())
        self.assertEqual(app.services.joins(), ["PD5"])
        call = app.services.calls[0]
        self.assertEqual(call["target"], {"entity_id": EVENTS})
        # Treated as joined before the integration refreshes.
        self.assertEqual(app.target_window(local("12:00"))[0]["id"], "5")
        self.assertEqual(len(app.services.notifications()), 1)
        self.assertEqual(app.services.notifications()[0]["service"], "mobile_app_phone")

    def test_skips_already_joined_and_past(self):
        app = App()
        app.set_events(joined=[event("17:00", "18:00", eid="5")],
                       available=[event("17:00", "18:00", eid="5"), event("09:00", "10:00", eid="6", code="PD6")])
        app.run(app.auto_join())
        self.assertEqual(app.services.joins(), [])

    def test_retries_after_failure(self):
        app = App()
        app.set_events(available=[event("17:00", "18:00", eid="5", code="PD5")])
        app.services.fail.add(("octopus_energy", "join_octoplus_power_down_session_event"))
        app.run(app.auto_join())
        self.assertIsNone(app.target_window(local("12:00"))[0])
        app.services.fail.clear()
        app.run(app.auto_join())
        self.assertEqual(app.services.joins(), ["PD5", "PD5"])
        self.assertIsNotNone(app.target_window(local("12:00"))[0])

    def test_disabled(self):
        app = App({"auto_join": False})
        app.set_events(available=[event("17:00", "18:00")])
        app.run(app.auto_join())
        self.assertEqual(app.services.calls, [])


if __name__ == "__main__":
    unittest.main()
