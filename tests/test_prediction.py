import unittest
from datetime import timezone

from tests.harness import App, local, session


def plan(app, sess, now, soc=80, voltage=None, profile=None, solar=None):
    return app.predict(sess, now, soc, voltage, profile or {}, solar or {})


class PredictionTests(unittest.TestCase):
    """10 kWh battery, 10% floor, 0.5 kW house, 50 V, 23:30 cheap window (see harness)."""

    def test_keeps_evening_reserve_and_exports_the_rest(self):
        app = App()
        p = plan(app, session("17:00", "18:00"), local("12:00"))
        # 8 kWh now - 5 h x 0.5 kW before the session
        self.assertEqual(p["battery_at_start_kwh"], 5.5)
        # 1 kWh floor + 18:00-23:30 at 0.5 kW
        self.assertEqual(p["evening_load_kwh"], 2.75)
        self.assertEqual(p["reserve_kwh"], 3.75)
        self.assertEqual(p["discharge_kw"], 1.75)
        self.assertEqual(p["discharge_current"], 35)
        self.assertEqual(p["stop_soc"], 38)
        self.assertEqual(p["predicted_house_load_kwh"], 0.5)
        self.assertEqual(p["predicted_export_kwh"], 1.25)
        self.assertEqual(p["predicted_import_kwh"], 0)
        self.assertEqual(p["estimated_octopoints"], 400)
        self.assertEqual(p["window"], "17:00-18:00")

    def test_reserve_margin_scales_evening_load(self):
        app = App({"reserve_margin": 1.2})
        p = plan(app, session("17:00", "18:00"), local("12:00"))
        self.assertEqual(p["reserve_kwh"], 4.3)

    def test_capped_by_export_limit(self):
        app = App({"export_limit_kw": 1.0})
        p = plan(app, session("17:00", "18:00"), local("12:00"))
        self.assertEqual(p["discharge_kw"], 1.5)
        self.assertEqual(p["discharge_current"], 30)
        self.assertEqual(p["predicted_export_kwh"], 1.0)

    def test_capped_by_max_current(self):
        app = App({"max_discharge_amps": 20})
        p = plan(app, session("17:00", "18:00"), local("12:00"))
        self.assertEqual(p["discharge_current"], 20)
        self.assertEqual(p["discharge_kw"], 1.0)
        self.assertEqual(p["predicted_export_kwh"], 0.5)

    def test_capped_by_max_power(self):
        app = App({"max_discharge_kw": 1.2})
        p = plan(app, session("17:00", "18:00"), local("12:00"), soc=100)
        self.assertEqual(p["discharge_current"], 24)

    def test_voltage_sensor_used_for_current(self):
        app = App()
        p = plan(app, session("17:00", "18:00"), local("12:00"), voltage=35.0)
        self.assertEqual(p["discharge_current"], 50)

    def test_nothing_to_spare(self):
        app = App()
        p = plan(app, session("17:00", "18:00"), local("12:00"), soc=40)
        self.assertEqual(p["discharge_current"], 0)
        self.assertEqual(p["predicted_export_kwh"], 0)
        self.assertEqual(p["predicted_import_kwh"], 0.5)
        self.assertEqual(p["estimated_octopoints"], 0)

    def test_overnight_window_before_session_assumes_full_battery(self):
        app = App()
        sess = session("17:00", "18:00", day=6)
        p = plan(app, sess, local("22:00", day=5), soc=30)
        # Full at 05:30, then 11.5 h x 0.5 kW
        self.assertEqual(p["battery_at_start_kwh"], 4.25)
        self.assertEqual(p["discharge_current"], 10)

    def test_load_profile_from_history(self):
        app = App()
        p = plan(app, session("17:00", "18:00"), local("12:00"), profile={17: 2.0})
        self.assertEqual(p["predicted_house_load_kwh"], 2.0)
        # Battery gives 1.75 kW, house needs 2 kW: a little import, nothing exported.
        self.assertEqual(p["predicted_export_kwh"], 0)
        self.assertEqual(p["predicted_import_kwh"], 0.25)
        self.assertEqual(p["estimated_octopoints"], 1400)

    def test_solar_forecast_adds_to_export(self):
        app = App()
        solar = {local("17:00").astimezone(timezone.utc): 1.0}
        p = plan(app, session("17:00", "18:00"), local("12:00"), solar=solar)
        self.assertEqual(p["predicted_solar_kwh"], 0.5)
        self.assertEqual(p["predicted_export_kwh"], 1.75)

    def test_session_ending_in_cheap_window_keeps_only_floor(self):
        app = App()
        p = plan(app, session("23:00", "23:45"), local("22:00"), soc=50)
        self.assertEqual(p["reserve_kwh"], 1.0)
        self.assertEqual(p["stop_soc"], 10)

    def test_replan_mid_session_uses_remaining_time(self):
        app = App()
        p = plan(app, session("17:00", "18:00"), local("17:30"), soc=40)
        self.assertEqual(p["hours"], 0.5)
        self.assertEqual(p["battery_at_start_kwh"], 4.0)
        self.assertEqual(p["discharge_kw"], 0.5)
        self.assertEqual(p["discharge_current"], 10)

    def test_dst_change_overnight(self):
        # Clocks go back on 2026-10-25: 23:30 -> 05:30 is 7 real hours.
        app = App()
        p = plan(app, session("17:00", "18:00", day=25), local("22:00", day=24), soc=30)
        # Full at 05:30 on the 25th, then 11.5 h of 0.5 kW
        self.assertEqual(p["battery_at_start_kwh"], 4.25)


class LoadProfileTests(unittest.TestCase):
    def test_hourly_means_averaged_per_local_hour(self):
        app = App()
        rows = [
            {"start": local("17:00", day=1).timestamp(), "mean": 1000},
            {"start": local("17:00", day=2).timestamp(), "mean": 3000},
            {"start": local("18:00", day=2), "mean": 500},
            {"start": local("19:00", day=2), "mean": None},
        ]
        self.assertEqual(app.build_load_profile(rows, 0.001), {17: 2.0, 18: 0.5})

    def test_missing_history_falls_back(self):
        app = App()

        async def broken():
            raise RuntimeError("no recorder")

        app.g["fetch_load_profile"] = broken
        self.assertEqual(app.run(app.ensure_load_profile()), {})


if __name__ == "__main__":
    unittest.main()
