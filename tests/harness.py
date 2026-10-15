"""
Test harness: loads the pyscript app outside Home Assistant.

pyscript injects globals (pyscript, hass, state, task, service, time_trigger,
state_trigger) into each script, so the app can't be imported normally. The
harness compiles the file into a fresh namespace with fakes for all of those,
plus a fake asyncio (instant sleeps), a fake clock, fake HA services and a
fake SolisCloud.
"""
import asyncio
import json
import logging
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

APP_PATH = Path(__file__).resolve().parents[1] / "pyscript" / "apps" / "solis_power_down.py"
TZ = ZoneInfo("Europe/London")

EVENTS = "event.octopus_energy_a_test_octoplus_power_down_events"
SOC = "sensor.solis_battery_soc"
LOAD = "sensor.solis_house_load"

BASE_CONFIG = {
    "key_id": "test-key",
    "secret": "test-secret",
    "username": "user@example.com",
    "password": "hunter2",
    "plantId": "1234",
    "power_down_events_entity": EVENTS,
    "soc_sensor": SOC,
    "battery_capacity_kwh": 10,
    "floor_soc": 10,
    "reserve_margin": 1.0,
    "default_load_kw": 0.5,
    "battery_voltage": 50,
    "max_discharge_kw": 5,
    "max_discharge_amps": 100,
    "export_limit_kw": 5,
    "discharge_efficiency": 1.0,
    "inverter_timezone": "Europe/London",
    "inter_write_delay": 0,
}

# Duplicated from the app on purpose, so a wrong CID in the app fails the tests.
TIME_CID, CURRENT_CID, SOC_CID, SWITCH_CID = "5964", "5967", "5965", "5922"
CHARGE_SWITCH_CIDS = ["5916", "5917", "5918", "5919", "5920", "5921"]
DISCHARGE_SWITCH_CIDS = ["5922", "5923", "5924", "5925", "5926", "5927"]
SLOT_SWITCH_CIDS = CHARGE_SWITCH_CIDS + DISCHARGE_SWITCH_CIDS


# Keep the app's log output out of the test run (assertLogs still works).
_app_log = logging.getLogger("pyscript.solis_power_down")
_app_log.addHandler(logging.NullHandler())
_app_log.propagate = False


def _install_homeassistant_stub():
    if "homeassistant.helpers.aiohttp_client" in sys.modules:
        return
    ha = types.ModuleType("homeassistant")
    helpers = types.ModuleType("homeassistant.helpers")
    client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    client.async_get_clientsession = lambda hass: None
    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.aiohttp_client"] = client


class FakeState:
    def __init__(self, value, attributes=None):
        self.state = value
        self.attributes = attributes or {}


class FakeAsyncio:
    """Stands in for the asyncio module inside the app: sleeps are instant and recorded."""

    def __init__(self):
        self.sleeps = []

    async def sleep(self, seconds):
        self.sleeps.append(seconds)


class FakeServices:
    def __init__(self):
        self.calls = []
        self.fail = set()

    async def async_call(self, domain, service, data=None, blocking=False, target=None):
        self.calls.append({"domain": domain, "service": service, "data": data or {}, "target": target})
        if (domain, service) in self.fail:
            raise RuntimeError("service failed")

    def joins(self):
        return [c["data"]["event_code"] for c in self.calls
                if c["service"] == "join_octoplus_power_down_session_event"]

    def notifications(self):
        return [c for c in self.calls if c["domain"] == "notify"]


# -----------------------------
# Fake SolisCloud
# -----------------------------
OK_CONTROL = {"code": "0", "data": [{"code": "0"}]}


def storage_inverter(inv_id="111", sn="SN111", name="Solis"):
    return {"id": inv_id, "sn": sn, "name": name, "productModel": "2"}


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status = status
        self._text = body if isinstance(body, str) else json.dumps(body if body is not None else {})

    async def text(self):
        return self._text


class FakeSolisCloud:
    """Records every POST and answers like SolisCloud."""

    def __init__(self):
        self.calls = []
        self.records = [storage_inverter()]
        self.fail_cids = set()
        self.values = {cid: "0" for cid in SLOT_SWITCH_CIDS}
        self.values["636"] = "35"

    async def post(self, url, data=None, headers=None):
        path = url.split(":13333", 1)[1]
        body = json.loads(data)
        self.calls.append({"path": path, "body": body, "headers": headers or {}})
        if path == "/v2/api/login":
            return FakeResponse(200, {"csrfToken": "tok-123"})
        if path == "/v1/api/inverterList":
            return FakeResponse(200, {"data": {"page": {"records": self.records}}})
        if path == "/v2/api/control":
            if str(body["cid"]) in self.fail_cids:
                return FakeResponse(500, {})
            self.values[str(body["cid"])] = str(body["value"])
            return FakeResponse(200, OK_CONTROL)
        if path == "/v2/api/atRead":
            return FakeResponse(200, {"code": "0", "data": {"msg": self.values.get(str(body["cid"]), "")}})
        if path == "/v2/api/atReadBatch":
            items = [{"cid": int(cid), "msg": self.values.get(cid, "")} for cid in body["cids"].split(",")]
            return FakeResponse(200, {"code": "0", "data": [items]})
        return FakeResponse(404, {})

    def control_writes(self):
        """(cid, value, yuanzhi) for every control write in order, excluding the time sync."""
        out = []
        for c in self.calls:
            if c["path"] == "/v2/api/control" and c["body"].get("cid") != "56":
                out.append((c["body"]["cid"], c["body"]["value"], c["body"].get("yuanzhi")))
        return out


# -----------------------------
# The loaded app
# -----------------------------
class App:
    def __init__(self, config=None):
        _install_homeassistant_stub()
        cfg = dict(BASE_CONFIG)
        cfg.update(config or {})
        for key in [k for k, v in cfg.items() if v is None]:
            del cfg[key]

        self.states = {}
        self.state_sets = []
        self.triggers = []
        self.service_funcs = []
        self.asyncio = FakeAsyncio()
        self.cloud = FakeSolisCloud()
        self.services = FakeServices()
        self.ops = None  # set by capture_ops()

        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone="Europe/London"),
            states=types.SimpleNamespace(get=self._get_state),
            services=self.services,
        )
        g = {
            "__name__": "solis_power_down",
            "pyscript": types.SimpleNamespace(app_config=cfg, config={}),
            "hass": hass,
            "state": types.SimpleNamespace(set=self._set_state),
            "task": types.SimpleNamespace(unique=lambda name, kill_me=False: None),
            "service": self._service,
            "time_trigger": self._trigger("time_trigger"),
            "state_trigger": self._trigger("state_trigger"),
            "event_trigger": self._trigger("event_trigger"),
        }
        source = APP_PATH.read_text()
        exec(compile(source, str(APP_PATH), "exec"), g)

        g["asyncio"] = self.asyncio
        g["async_get_clientsession"] = lambda _hass: self.cloud
        self.load_profile = {}

        async def fake_fetch():
            return dict(self.load_profile)

        g["fetch_load_profile"] = fake_fetch
        self.g = g
        self.set_soc(80)
        self.at("12:00")

    # --- pyscript / HA fakes ---
    def _get_state(self, entity_id):
        if entity_id in self.states:
            value, attrs = self.states[entity_id]
            return FakeState(value, attrs)
        return None

    def _set_state(self, entity_id, value, new_attributes=None):
        self.state_sets.append((entity_id, value, new_attributes or {}))

    def _service(self, func):
        self.service_funcs.append(func.__name__)
        return func

    def _trigger(self, kind):
        def decorator_factory(*args, **kwargs):
            def decorator(func):
                self.triggers.append((kind, args, func.__name__))
                return func
            return decorator
        return decorator_factory

    # --- helpers for tests ---
    def __getattr__(self, name):
        g = self.__dict__.get("g")
        if g is not None and name in g:
            return g[name]
        raise AttributeError(name)

    def set_soc(self, soc):
        self.states[SOC] = (str(soc), {})

    def set_events(self, joined=(), available=()):
        self.states[EVENTS] = ("2026-10-06T10:00:00+00:00",
                               {"joined_events": list(joined), "available_events": list(available)})

    def at(self, hhmm, day=6):
        """Freeze the app's clock at 2026-10-<day> HH:MM (Europe/London)."""
        now = local(hhmm, day)
        self.g["now_local"] = lambda: now
        return now

    def capture_ops(self, ok=True):
        """Replace the SolisCloud layer with a recorder of the ops the app applies."""
        self.ops = []

        async def fake_apply(ops):
            if ops:
                self.ops.append(list(ops))
            return ok

        self.g["solis_apply"] = fake_apply
        return self.ops

    def run(self, coro):
        return asyncio.run(coro)

    def reconcile(self, reason="test"):
        return self.run(self.g["reconcile"](reason))

    def status(self):
        sets = [s for s in self.state_sets if s[0] == "sensor.solis_power_down"]
        return sets[-1] if sets else None

    def active(self):
        sets = [s for s in self.state_sets if s[0] == "binary_sensor.solis_power_down_active"]
        return sets[-1][1] if sets else None


def local(hhmm, day=6):
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def event(start, end, eid="101", code="PD101", ppk=800, day=6):
    """A power down event dict as the Octopus integration exposes it (UTC ISO strings)."""
    return {
        "id": eid,
        "code": code,
        "start": local(start, day).astimezone(timezone.utc).isoformat(),
        "end": local(end, day).astimezone(timezone.utc).isoformat(),
        "duration_in_minutes": 60,
        "octopoints_per_kwh": ppk,
    }


def session(start, end, ppk=800, day=6):
    return {"id": "1", "code": "PD1", "start": local(start, day), "end": local(end, day),
            "octopoints_per_kwh": ppk, "joined": True}
