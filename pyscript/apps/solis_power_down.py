"""
Solis Power Down Export (pyscript app)

Exports from the Solis battery during Octopus Octoplus "Power Down" sessions
and, optionally, every day in a peak export window (e.g. 16:00-19:00), sized
so the battery still covers the house until the cheap overnight window.

How it works
------------
- Watches the Octopus Energy integration's power down events entity. Any
  available session is joined automatically (optional), and joined sessions
  are planned and exported.
- Optionally adds a daily peak export window. Where a Power Down session
  overlaps it, the peak window is split around the session, and battery is
  held back during earlier peak time so the Power Down can export at full
  power (Power Down first).
- For the next window it predicts:
    * house load per half-hour, from the recorder's hourly statistics for the
      load sensor over the last N days (same time of day),
    * solar, from an optional Solcast forecast,
    * the battery at session start (current SOC, or a full battery after the
      overnight window if that comes first, minus predicted net load),
    * the reserve the house needs from session end until the cheap window,
    * the discharge power that empties the rest during the session, capped by
      the inverter, battery and export limit,
    * and from that, how much will be exported.
- A few minutes before the session it writes one Solis discharge slot (time,
  current, stop SOC, switch on). The inverter then starts and stops on its
  own, so if HA / pyscript / SolisCloud dies the battery still stops at the
  session end and never goes below the reserve SOC.
- During the session the plan is redone every 5 minutes from the real SOC and
  the discharge current is adjusted if it has drifted.
- Afterwards the slot is switched off and cleared.

Only the configured discharge slot is touched, so it can run alongside the
Solis EV-Follow app (which only uses charge slots).

Requires 6-slot Solis firmware (HMI >= 4B00) with time-of-use (timed
charge/discharge) enabled, and the Octopus Energy integration with Octoplus.
"""
import json
import re
import math
import asyncio
import logging
import hashlib
import hmac
import base64
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from zoneinfo import ZoneInfo
from homeassistant.helpers.aiohttp_client import async_get_clientsession

log = logging.getLogger("pyscript.solis_power_down")
log.setLevel(logging.DEBUG)

VERSION = "1.1.0"

# -----------------------------
# SolisCloud endpoints / constants
# -----------------------------
BASE_URL = "https://www.soliscloud.com:13333"
VERB = "POST"
LOGIN_URL = "/v2/api/login"
CONTROL_URL = "/v2/api/control"
AT_READ_URL = "/v2/api/atRead"
AT_READ_BATCH_URL = "/v2/api/atReadBatch"
INVERTER_LIST_URL = "/v1/api/inverterList"

# 6-slot firmware (HMI >= 4B00) discharge slot CIDs, slot 1..6
DISCHARGE_TIME_CIDS = ["5964", "5968", "5972", "5976", "5980", "5987"]
DISCHARGE_CURRENT_CIDS = ["5967", "5971", "5975", "5979", "5983", "5986"]
DISCHARGE_SOC_CIDS = ["5965", "5969", "5973", "5977", "5981", "5984"]
STORAGE_MODE_CID = "636"

# Per-slot on/off switches ("1"/"0"). Writes must include "yuanzhi": a bitmask
# of all 12 switch states before the change, bit = index in SLOT_SWITCH_CIDS
# (charge slots 1-6 -> bits 0-5, discharge slots 1-6 -> bits 6-11).
CHARGE_SWITCH_CIDS = ["5916", "5917", "5918", "5919", "5920", "5921"]
DISCHARGE_SWITCH_CIDS = ["5922", "5923", "5924", "5925", "5926", "5927"]
SLOT_SWITCH_CIDS = CHARGE_SWITCH_CIDS + DISCHARGE_SWITCH_CIDS
EMPTY_SLOT = "00:00-00:00"

STATUS_SENSOR = "sensor.solis_power_down"
ACTIVE_SENSOR = "binary_sensor.solis_power_down_active"
JOIN_DOMAIN = "octopus_energy"
JOIN_SERVICE = "join_octoplus_power_down_session_event"

# -----------------------------
# Configuration (from configuration.yaml -> pyscript: apps: solis_power_down:)
# -----------------------------
try:
    # Set when this file lives in <config>/pyscript/apps/
    CFG = pyscript.app_config or {}
except Exception:
    # Fallback if the file was placed directly in <config>/pyscript/
    CFG = (pyscript.config.get("apps") or {}).get("solis_power_down") or {}
    log.warning("Not loaded as a pyscript app; move this file to <config>/pyscript/apps/")


def _cfg_bool(key, default):
    return str(CFG.get(key, default)).lower() in ("true", "1", "yes", "on")


def _cfg_list(key):
    value = CFG.get(key) or []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


EVENTS_ENTITY = str(CFG.get("power_down_events_entity", ""))
AUTO_JOIN = _cfg_bool("auto_join", True)

SOC_SENSOR = str(CFG.get("soc_sensor", ""))
BATTERY_CAPACITY_KWH = float(CFG.get("battery_capacity_kwh", 10.0))
FLOOR_SOC = int(CFG.get("floor_soc", 15))
RESERVE_MARGIN = float(CFG.get("reserve_margin", 1.2))
CORE_CHARGE_SOC = int(CFG.get("core_charge_soc", 100))

LOAD_POWER_SENSOR = str(CFG.get("load_power_sensor", ""))
HISTORY_DAYS = int(CFG.get("history_days", 14))
DEFAULT_LOAD_KW = float(CFG.get("default_load_kw", 0.5))
SOLAR_FORECAST_SENSORS = _cfg_list("solar_forecast_sensors")

BATTERY_VOLTAGE_SENSOR = str(CFG.get("battery_voltage_sensor", ""))
BATTERY_VOLTAGE = float(CFG.get("battery_voltage", 51.2))
MAX_DISCHARGE_KW = float(CFG.get("max_discharge_kw", 5.0))
MAX_DISCHARGE_AMPS = int(CFG.get("max_discharge_amps", 100))
EXPORT_LIMIT_KW = float(CFG.get("export_limit_kw", 3.68))
DISCHARGE_EFFICIENCY = float(CFG.get("discharge_efficiency", 0.95))

DISCHARGE_SLOT = int(CFG.get("discharge_slot", 1))
CORE_WINDOW = str(CFG.get("core_window", "23:30-05:30"))
PREPARE_MINUTES = int(CFG.get("prepare_minutes", 15))
CURRENT_STEP_AMPS = int(CFG.get("current_step_amps", 5))
STARTUP_DELAY_SECONDS = int(CFG.get("startup_delay_seconds", 60))
NOTIFY_SERVICE = str(CFG.get("notify_service", ""))

PEAK_EXPORT = _cfg_bool("peak_export", False)
PEAK_WINDOW = str(CFG.get("peak_window", "16:00-19:00"))
PEAK_EXPORT_RATE = float(CFG.get("peak_export_rate", 16))
PEAK_SWITCH = str(CFG.get("peak_export_switch", ""))
NOTIFY_PEAK = _cfg_bool("notify_peak", False)

INVERTER_TIMEZONE = str(CFG.get("inverter_timezone", hass.config.time_zone or "UTC"))
SYNC_INVERTER_TIME = _cfg_bool("sync_inverter_time", True)

DIAGNOSTICS_ONLY = _cfg_bool("diagnostics_only", False)
CONTROL_RETRIES = int(CFG.get("control_retries", 3))
CONTROL_DELAY = float(CFG.get("control_delay", 0.5))
INTER_WRITE_DELAY = float(CFG.get("inter_write_delay", 0.25))
VERIFY_READBACK = _cfg_bool("verify_readback", False)

try:
    INV_TZ = ZoneInfo(INVERTER_TIMEZONE)
except Exception as e:
    log.warning("Invalid inverter_timezone '%s' (%s), falling back to UTC", INVERTER_TIMEZONE, e)
    INV_TZ = timezone.utc

# -----------------------------
# Runtime state (dicts so functions can mutate without `global`)
# -----------------------------
# What we last wrote to the discharge slot. "unknown" means we don't know
# what's on the inverter (e.g. after a restart) and must write it.
_program = {"window": EMPTY_SLOT, "current": None, "soc": None, "enabled": False, "unknown": True}
_cache = {"inverter_id": None, "inverter_sn": None, "last_time_sync": None,
          "load_profile": None, "load_profile_at": None}
_flags = {"busy": False, "last_reason": None, "last_write_ok": None}
# Sessions we joined ourselves (id -> session), so we act on them even before
# the integration refreshes joined_events, and which sessions we've announced.
_joined = {"by_us": {}, "notified": set()}
_last = {"plan": None, "session": None}


# -----------------------------
# SolisCloud auth helpers (from solis_ev_follow)
# -----------------------------
def digest(body: str) -> str:
    return base64.b64encode(hashlib.md5(body.encode("utf-8")).digest()).decode("utf-8")


def passwordEncode(password: str) -> str:
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def prepare_header(config, body: str, canonicalized_resource: str):
    content_md5 = digest(body)
    content_type = "application/json"
    now = datetime.now(timezone.utc)
    date = now.strftime("%a, %d %b %Y %H:%M:%S GMT")
    encrypt_str = (
        VERB + "\n" + content_md5 + "\n" + content_type + "\n" + date + "\n" + canonicalized_resource
    )
    hmac_obj = hmac.new(
        config["secret"].encode("utf-8"),
        msg=encrypt_str.encode("utf-8"),
        digestmod=hashlib.sha1,
    )
    sign = base64.b64encode(hmac_obj.digest())
    authorization = "API " + str(config["key_id"]) + ":" + sign.decode("utf-8")
    return {
        "Content-MD5": content_md5,
        "Content-Type": content_type,
        "Date": date,
        "Authorization": authorization,
    }


def _clean_json_text(text: str) -> str:
    # SolisCloud sometimes returns JSON with trailing commas.
    return re.sub(r'("(?:\\?.)*?")|,\s*([}\]])', r"\1\2", text)


# -----------------------------
# SolisCloud I/O helpers (from solis_ev_follow)
# -----------------------------
async def solis_post(session, url_path, body_dict, token=None):
    body = json.dumps(body_dict, separators=(",", ":"))
    headers = prepare_header(CFG, body, url_path)
    if token:
        headers["token"] = token
    return await session.post(BASE_URL + url_path, data=body, headers=headers)


async def resp_json(resp):
    text = await resp.text()
    return json.loads(_clean_json_text(text))


async def solis_login(session):
    body = {"userInfo": str(CFG["username"]), "password": passwordEncode(str(CFG["password"]))}
    resp = await solis_post(session, LOGIN_URL, body)
    if resp.status != HTTPStatus.OK:
        log.error("Login failed with status %s", resp.status)
        return None
    data = await resp_json(resp)
    token = data.get("csrfToken")
    if not token:
        log.error("Login succeeded but csrfToken missing: %s", data)
    return token


async def get_inverter(session):
    """Return (inverter_id, inverter_sn), cached after the first lookup."""
    if _cache["inverter_id"] and _cache["inverter_sn"]:
        return _cache["inverter_id"], _cache["inverter_sn"]

    resp = await solis_post(session, INVERTER_LIST_URL, {"stationId": str(CFG["plantId"])})
    if resp.status != HTTPStatus.OK:
        log.error("inverterList failed status %s", resp.status)
        return None, None
    data = await resp_json(resp)
    records = (data.get("data") or {}).get("page", {}).get("records", []) or []
    if not records:
        log.error("No inverters returned from inverterList")
        return None, None

    cfg_sn = str(CFG.get("inverter_sn", "")).strip()
    cfg_id = str(CFG.get("inverter_id", "")).strip()
    if cfg_sn.lower() in ("unknown", "unavailable", "none"):
        cfg_sn = ""
    if cfg_id.lower() in ("unknown", "unavailable", "none"):
        cfg_id = ""

    chosen = None
    if cfg_sn or cfg_id:
        for r in records:
            if (cfg_sn and str(r.get("sn")) == cfg_sn) or (cfg_id and str(r.get("id")) == cfg_id):
                chosen = r
                break
    else:
        storage_records = []
        for r in records:
            if str(r.get("productModel")) == "2":
                storage_records.append(r)
        if len(storage_records) == 1:
            chosen = storage_records[0]
        elif len(storage_records) == 0 and len(records) == 1:
            chosen = records[0]

    if not chosen:
        log.error("Could not pick an inverter; set inverter_sn or inverter_id. Available:")
        for r in records:
            log.error("  - ID: %s, SN: %s, Name: %s, ProductModel: %s",
                      r.get("id"), r.get("sn"), r.get("name"), r.get("productModel"))
        return None, None

    _cache["inverter_id"] = chosen.get("id")
    _cache["inverter_sn"] = chosen.get("sn")
    log.info("Using inverter ID=%s SN=%s Name=%s",
             _cache["inverter_id"], _cache["inverter_sn"], chosen.get("name"))
    return _cache["inverter_id"], _cache["inverter_sn"]


async def get_control_value(session, token, inverter_sn, cid):
    """Read one CID. Returns its value as a string, or None."""
    for attempt in range(1, CONTROL_RETRIES + 1):
        r = await solis_post(session, AT_READ_URL,
                             {"inverterSn": str(inverter_sn), "cid": str(cid)}, token=token)
        if r.status != HTTPStatus.OK:
            await asyncio.sleep(0.2)
            continue
        try:
            data = await resp_json(r)
            payload = data.get("data")
            if isinstance(payload, list) and payload:
                payload = payload[0]
            if str(data.get("code")) == "0" and isinstance(payload, dict) and "msg" in payload:
                return str(payload["msg"])
        except Exception as e:
            log.warning("AT_READ cid=%s attempt %s parse error: %s", cid, attempt, e)
        await asyncio.sleep(0.2)
    return None


async def write_control(session, token, inverter_sn, cid, value, old_value=None):
    body = {"inverterSn": str(inverter_sn), "cid": str(cid), "value": str(value)}
    if old_value is not None:
        body["yuanzhi"] = str(old_value)
    last_text = None
    for attempt in range(1, CONTROL_RETRIES + 1):
        r = await solis_post(session, CONTROL_URL, body, token=token)
        try:
            last_text = await r.text()
        except Exception:
            last_text = None

        if r.status != HTTPStatus.OK:
            log.warning("CONTROL cid=%s attempt %s/%s http=%s", cid, attempt, CONTROL_RETRIES, r.status)
            await asyncio.sleep(0.3)
            continue
        try:
            data = json.loads(_clean_json_text(last_text or ""))
            payload = data.get("data") or []
            if str(data.get("code")) == "0" and payload and str(payload[0].get("code")) == "0":
                if VERIFY_READBACK:
                    await asyncio.sleep(CONTROL_DELAY)
                    rb = await get_control_value(session, token, inverter_sn, cid)
                    if rb is not None and rb != str(value):
                        log.warning("Readback cid=%s: wrote '%s' but read '%s'", cid, value, rb)
                    else:
                        log.debug("Readback cid=%s: %s", cid, rb)
                return True
        except Exception as e:
            log.warning("CONTROL cid=%s attempt %s/%s parse error: %s", cid, attempt, CONTROL_RETRIES, e)
        await asyncio.sleep(0.3)

    log.error("CONTROL cid=%s failed after %s attempts. Last response: %s", cid, CONTROL_RETRIES, last_text)
    return False


async def read_slot_switches(session, token, inverter_sn):
    """Return {cid: "0"/"1"} for the 12 slot switches; unreadable ones are left out."""
    states = {}
    try:
        r = await solis_post(session, AT_READ_BATCH_URL,
                             {"inverterSn": str(inverter_sn), "cids": ",".join(SLOT_SWITCH_CIDS)},
                             token=token)
        if r.status == HTTPStatus.OK:
            data = await resp_json(r)
            if str(data.get("code")) == "0":
                for outer in data.get("data") or []:
                    items = outer if isinstance(outer, list) else [outer]
                    for item in items:
                        if isinstance(item, dict) and "cid" in item and "msg" in item:
                            states[str(item["cid"])] = str(item["msg"])
        else:
            log.warning("atReadBatch for slot switches failed http=%s", r.status)
    except Exception as e:
        log.warning("atReadBatch for slot switches failed: %s", e)

    for cid in SLOT_SWITCH_CIDS:
        if cid not in states:
            value = await get_control_value(session, token, inverter_sn, cid)
            if value is not None:
                states[cid] = value
    log.debug("Slot switch states: %s", states)
    return states


def slot_switch_mask(states):
    """The "yuanzhi" bitmask for the current switch states, or None if any is unknown."""
    mask = 0
    for bit, cid in enumerate(SLOT_SWITCH_CIDS):
        if cid not in states:
            return None
        if states[cid] == "1":
            mask |= 1 << bit
    return str(mask)


async def set_slot_switch(session, token, inverter_sn, states, cid, enabled):
    """Switch a slot on/off if it isn't already. Updates `states` on success."""
    want = "1" if enabled else "0"
    if states.get(cid) == want:
        return True
    old_value = slot_switch_mask(states)
    if old_value is None:
        log.warning("Slot switch states unknown; writing CID %s without yuanzhi", cid)
    log.info("%s discharge slot %s (CID %s)", "Enabling" if enabled else "Disabling", DISCHARGE_SLOT, cid)
    ok = await write_control(session, token, inverter_sn, cid, want, old_value=old_value)
    if ok:
        states[cid] = want
    return ok


async def sync_inverter_time(session, token, inverter_id):
    value = datetime.now(INV_TZ).strftime("%Y-%m-%d %H:%M:%S")
    body = f'{{"inverterId":"{inverter_id}","cid":"56","value":"{value}"}}'
    headers = prepare_header(CFG, body, CONTROL_URL)
    headers["token"] = token
    try:
        resp = await session.post(BASE_URL + CONTROL_URL, data=body, headers=headers)
        if resp.status == HTTPStatus.OK:
            data = await resp_json(resp)
            if str(data.get("code")) == "0":
                log.info("Synced inverter time to %s (%s)", value, INVERTER_TIMEZONE)
                _cache["last_time_sync"] = datetime.now(timezone.utc)
                return
            log.warning("Time sync returned code=%s msg=%s", data.get("code"), data.get("msg"))
        else:
            log.warning("Time sync HTTP status: %s", resp.status)
    except Exception as e:
        log.error("Time sync failed: %s", e)


async def solis_connect():
    """Log in and pick the inverter. Returns (session, token, inverter_id, inverter_sn) or None."""
    session = async_get_clientsession(hass)
    token = await solis_login(session)
    if not token:
        return None
    inverter_id, inverter_sn = await get_inverter(session)
    if not inverter_sn:
        return None
    return session, token, inverter_id, inverter_sn


async def solis_apply(ops):
    """
    Apply discharge slot changes in order. `ops` is a list of
    ("value", cid, value) and ("switch", enabled). Returns True if all succeeded.
    """
    if not ops:
        return True
    if DIAGNOSTICS_ONLY:
        log.warning("DIAGNOSTICS MODE: would write %s", ops)
        return True

    conn = await solis_connect()
    if conn is None:
        return False
    session, token, inverter_id, inverter_sn = conn

    if SYNC_INVERTER_TIME:
        last = _cache["last_time_sync"]
        if last is None or datetime.now(timezone.utc) - last > timedelta(hours=24):
            await sync_inverter_time(session, token, inverter_id)

    switches = None
    for op in ops:
        if op[0] == "switch" and switches is None:
            switches = await read_slot_switches(session, token, inverter_sn)

    ok = True
    switch_cid = DISCHARGE_SWITCH_CIDS[DISCHARGE_SLOT - 1]
    for op in ops:
        if op[0] == "switch":
            if not await set_slot_switch(session, token, inverter_sn, switches, switch_cid, op[1]):
                ok = False
                # Never switch on a slot whose settings didn't all land.
                break
        else:
            log.info("Writing discharge slot %s CID %s = %s", DISCHARGE_SLOT, op[1], op[2])
            if not await write_control(session, token, inverter_sn, op[1], op[2]):
                ok = False
                break
        await asyncio.sleep(INTER_WRITE_DELAY)
    return ok


async def log_storage_mode():
    """Read the storage mode once so the log shows whether TOU is likely enabled."""
    if DIAGNOSTICS_ONLY:
        return
    try:
        conn = await solis_connect()
        if conn is None:
            return
        session, token, _, inverter_sn = conn
        value = await get_control_value(session, token, inverter_sn, STORAGE_MODE_CID)
        log.info("Inverter storage mode (CID %s) = %s. Time-of-use / timed charge-discharge "
                 "must be enabled for discharge slots to work.", STORAGE_MODE_CID, value)
    except Exception as e:
        log.warning("Could not read storage mode: %s", e)


# -----------------------------
# Time helpers
# -----------------------------
def _parse_window(text):
    start_s, end_s = text.split("-")
    return (datetime.strptime(start_s.strip(), "%H:%M").time(),
            datetime.strptime(end_s.strip(), "%H:%M").time())


CORE_START, CORE_END = _parse_window(CORE_WINDOW)
PEAK_START, PEAK_END = _parse_window(PEAK_WINDOW)


def now_local():
    return datetime.now(INV_TZ)


def to_local(dt):
    return dt.astimezone(INV_TZ)


def floor_half_hour(dt):
    return dt.replace(minute=(dt.minute // 30) * 30, second=0, microsecond=0)


def parse_dt(value):
    """Datetime or ISO string -> tz-aware datetime (naive is taken as UTC), or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def periods(start, end):
    """
    Split [start, end) into pieces on half-hour boundaries.
    Returns [(piece_start_utc, hours)]. Works in UTC so DST changes are exact.
    """
    out = []
    t = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    while t < end_utc:
        nxt = min(floor_half_hour(t) + timedelta(minutes=30), end_utc)
        out.append((t, (nxt - t).total_seconds() / 3600.0))
        t = nxt
    return out


def _at_local_time(day_dt, t):
    """`day_dt`'s date at local time `t` (DST-aware)."""
    local = to_local(day_dt)
    return datetime(local.year, local.month, local.day, t.hour, t.minute, tzinfo=INV_TZ)


def next_core_start(after):
    """The first core-window start at or after `after`."""
    for days in range(0, 3):
        candidate = _at_local_time(after + timedelta(days=days), CORE_START)
        if candidate >= after:
            return candidate
    return after


def in_core_window(dt):
    t = to_local(dt).time()
    if CORE_START <= CORE_END:
        return CORE_START <= t < CORE_END
    return t >= CORE_START or t < CORE_END


def last_core_end_between(after, before):
    """The latest core-window end in (after, before], or None."""
    best = None
    for days in range(-1, 3):
        candidate = _at_local_time(before - timedelta(days=days), CORE_END)
        if after < candidate <= before and (best is None or candidate > best):
            best = candidate
    return best


def fmt_window(start, end):
    return f"{to_local(start).strftime('%H:%M')}-{to_local(end).strftime('%H:%M')}"


# -----------------------------
# HA state helpers
# -----------------------------
def _state_obj(entity_id):
    if not entity_id:
        return None
    return hass.states.get(entity_id)


def _float_state(entity_id):
    s = _state_obj(entity_id)
    if s is None:
        return None
    try:
        return float(s.state)
    except (TypeError, ValueError):
        return None


def _attrs(entity_id):
    s = _state_obj(entity_id)
    if s is None:
        return {}
    return dict(getattr(s, "attributes", None) or {})


# -----------------------------
# Sessions
# -----------------------------
def _session_from_event(ev, joined):
    start = parse_dt(ev.get("start"))
    end = parse_dt(ev.get("end"))
    if start is None or end is None or end <= start:
        return None
    try:
        ppk = float(ev.get("octopoints_per_kwh") or 0)
    except (TypeError, ValueError):
        ppk = 0.0
    return {
        "id": str(ev.get("id")),
        "code": ev.get("code"),
        "start": start,
        "end": end,
        "octopoints_per_kwh": ppk,
        "joined": joined,
    }


def sessions():
    """(joined, available) sessions from the power down events entity."""
    attrs = _attrs(EVENTS_ENTITY)
    joined = []
    seen = set()
    for ev in attrs.get("joined_events") or []:
        s = _session_from_event(ev, True)
        if s is not None:
            joined.append(s)
            seen.add(s["id"])
    for sid, s in _joined["by_us"].items():
        if sid not in seen:
            joined.append(s)
            seen.add(sid)

    available = []
    for ev in attrs.get("available_events") or []:
        s = _session_from_event(ev, False)
        if s is not None and s["id"] not in seen:
            available.append(s)
    return joined, available


def peak_switch_on():
    if not PEAK_SWITCH:
        return True
    s = hass.states.get(PEAK_SWITCH)
    return s is None or s.state != "off"


def peak_windows(now, honour_switch=True):
    """Daily peak export windows (yesterday to tomorrow) that haven't ended."""
    if not PEAK_EXPORT or (honour_switch and not peak_switch_on()):
        return []
    out = []
    for days in (-1, 0, 1):
        day = now + timedelta(days=days)
        start = _at_local_time(day, PEAK_START)
        end = _at_local_time(day, PEAK_END)
        if end <= start:
            end = _at_local_time(day + timedelta(days=1), PEAK_END)
        if end > now:
            out.append({"id": f"peak-{to_local(start).strftime('%Y-%m-%d')}", "kind": "peak",
                        "start": start, "end": end})
    return out


def export_rate_at(t):
    """The peak export rate (p/kWh) if `t` is in a peak window, else None."""
    for w in peak_windows(t, honour_switch=False):
        if w["start"] <= t < w["end"]:
            return PEAK_EXPORT_RATE
    return None


def export_windows(now):
    """
    Joined Power Down sessions and daily peak windows that haven't ended, as
    non-overlapping segments sorted by start. Peak windows are cut around
    Power Down sessions.
    """
    joined, _ = sessions()
    # Every joined session cuts the peak window, even one that has just ended,
    # so the rest of the peak window keeps its own start time.
    power_downs = []
    segments = []
    for s in joined:
        w = dict(s)
        w["kind"] = "power_down"
        power_downs.append(w)
        if w["end"] > now:
            segments.append(w)

    for p in peak_windows(now):
        pieces = [(p["start"], p["end"])]
        for d in power_downs:
            cut = []
            for a, b in pieces:
                if d["end"] <= a or d["start"] >= b:
                    cut.append((a, b))
                    continue
                if a < d["start"]:
                    cut.append((a, d["start"]))
                if d["end"] < b:
                    cut.append((d["end"], b))
            pieces = cut
        for a, b in pieces:
            if b > now:
                segments.append({"id": f"{p['id']}-{to_local(a).strftime('%H%M')}", "kind": "peak",
                                 "start": a, "end": b, "octopoints_per_kwh": 0.0})

    ordered = sorted([(w["start"].timestamp(), i) for i, w in enumerate(segments)])
    return [segments[i] for _, i in ordered]


def target_window(now):
    """
    (window, later): the export window running now or else the next one, and
    the windows after it that come before the next cheap window.
    """
    windows = export_windows(now)
    for i, w in enumerate(windows):
        if w["end"] > now:
            cutoff = next_core_start(w["end"])
            later = [x for x in windows[i + 1:] if x["start"] < cutoff]
            return w, later
    return None, []


async def auto_join():
    if not AUTO_JOIN or not EVENTS_ENTITY:
        return
    now = now_local()
    _, available = sessions()
    for s in available:
        if s["end"] <= now or not s["code"]:
            continue
        log.info("Joining Power Down session %s-%s (code %s, %s octopoints/kWh)",
                 s["start"].isoformat(), s["end"].isoformat(), s["code"], s["octopoints_per_kwh"])
        try:
            await hass.services.async_call(
                JOIN_DOMAIN, JOIN_SERVICE, {"event_code": s["code"]},
                blocking=True, target={"entity_id": EVENTS_ENTITY},
            )
        except Exception as e:
            log.error("Failed to join Power Down session %s: %s (will retry)", s["code"], e)
            continue
        s["joined"] = True
        _joined["by_us"][s["id"]] = s
        await notify(
            "Joined Octopus Power Down",
            f"{to_local(s['start']).strftime('%a %d %b')} {fmt_window(s['start'], s['end'])}, "
            f"{s['octopoints_per_kwh']:g} octopoints/kWh. The battery will export during it.",
        )


async def notify(title, message):
    if not NOTIFY_SERVICE:
        return
    domain, _, name = NOTIFY_SERVICE.partition(".")
    if not name:
        domain, name = "notify", domain
    try:
        await hass.services.async_call(domain, name, {"title": title, "message": message}, blocking=False)
    except Exception as e:
        log.warning("Notify via %s failed: %s", NOTIFY_SERVICE, e)


# -----------------------------
# Forecast inputs
# -----------------------------
async def fetch_load_profile():
    """
    {local hour: mean kW} for the load sensor over the last HISTORY_DAYS, from
    the recorder's hourly long-term statistics. Empty if unavailable.
    """
    if not LOAD_POWER_SENSOR:
        return {}
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=HISTORY_DAYS)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, {LOAD_POWER_SENSOR}, "hour", None, {"mean"}
    )
    unit = str(_attrs(LOAD_POWER_SENSOR).get("unit_of_measurement", "W"))
    scale = 1.0 if unit.lower() == "kw" else 0.001
    return build_load_profile(stats.get(LOAD_POWER_SENSOR) or [], scale)


def build_load_profile(rows, scale):
    sums = {}
    counts = {}
    for row in rows:
        mean = row.get("mean")
        start = row.get("start")
        if mean is None or start is None:
            continue
        if isinstance(start, (int, float)):
            start = datetime.fromtimestamp(start, tz=timezone.utc)
        start = parse_dt(start)
        hour = to_local(start).hour
        sums[hour] = sums.get(hour, 0.0) + max(0.0, float(mean)) * scale
        counts[hour] = counts.get(hour, 0) + 1
    profile = {}
    for hour in sums:
        profile[hour] = sums[hour] / counts[hour]
    return profile


async def ensure_load_profile():
    """Refresh the load profile at most every 6 hours."""
    at = _cache["load_profile_at"]
    if _cache["load_profile"] is not None and at is not None \
            and datetime.now(timezone.utc) - at < timedelta(hours=6):
        return _cache["load_profile"]
    try:
        profile = await fetch_load_profile()
        if LOAD_POWER_SENSOR and not profile:
            log.warning("No recorder statistics for %s; using default_load_kw=%s",
                        LOAD_POWER_SENSOR, DEFAULT_LOAD_KW)
    except Exception as e:
        log.warning("Load history unavailable (%s); using default_load_kw=%s", e, DEFAULT_LOAD_KW)
        profile = {}
    _cache["load_profile"] = profile
    _cache["load_profile_at"] = datetime.now(timezone.utc)
    return profile


def solar_forecast():
    """{half-hour start (UTC): kW} from Solcast-style detailedForecast attributes."""
    out = {}
    for entity in SOLAR_FORECAST_SENSORS:
        for item in _attrs(entity).get("detailedForecast") or []:
            start = parse_dt(item.get("period_start"))
            try:
                kw = float(item.get("pv_estimate") or 0)
            except (TypeError, ValueError):
                continue
            if start is not None:
                out[floor_half_hour(start.astimezone(timezone.utc))] = max(0.0, kw)
    return out


# -----------------------------
# Prediction
# -----------------------------
def predict(session, now, soc, voltage, load_profile, solar, later=None):
    """
    Plan the export for the window `session`. All energies in kWh, powers in kW.
    `soc` is the battery SOC now (%), `load_profile` {local hour: kW},
    `solar` {UTC half-hour: kW}, `later` the windows after it and before the
    cheap window (battery is held back for Power Down ones).
    """
    cap = BATTERY_CAPACITY_KWH

    def load_kw(t):
        return load_profile.get(to_local(t).hour, DEFAULT_LOAD_KW)

    def solar_kw(t):
        return solar.get(floor_half_hour(t.astimezone(timezone.utc)), 0.0)

    def net_drain(a, b):
        total = 0.0
        for t, h in periods(a, b):
            total += max(0.0, load_kw(t) - solar_kw(t)) * h
        return total

    volts = voltage if voltage and voltage > 0 else BATTERY_VOLTAGE

    def max_battery_kw(a, b):
        """The most the battery can usefully give in [a, b): inverter, current and export caps."""
        hrs = 0.0
        house = 0.0
        for t, h in periods(a, b):
            hrs += h
            house += (load_kw(t) - solar_kw(t)) * h
        if hrs <= 0:
            return 0.0, 0.0
        export_cap = max(0.0, (EXPORT_LIMIT_KW + house / hrs) / DISCHARGE_EFFICIENCY)
        return min(MAX_DISCHARGE_KW, MAX_DISCHARGE_AMPS * volts / 1000.0, export_cap), hrs

    start = max(session["start"], now)
    end = session["end"]

    # Battery at the start: from now, or from a full battery at the end of
    # the overnight window if one comes first.
    base_time, base_soc = now, soc
    core_end = last_core_end_between(now, start)
    if core_end is not None:
        base_time, base_soc = core_end, max(soc, CORE_CHARGE_SOC)
    battery_at_start = max(0.0, min(cap, base_soc / 100.0 * cap - net_drain(base_time, start)))

    # Reserve: the floor, plus the house from session end until the cheap window.
    floor_kwh = FLOOR_SOC / 100.0 * cap
    if in_core_window(end):
        evening_kwh = 0.0
    else:
        evening_kwh = net_drain(end, next_core_start(end))
    # Power Down first: keep what later Power Down sessions can export. Their
    # house load is already in the evening reserve.
    held_kwh = 0.0
    for w in later or []:
        if w.get("kind") == "power_down":
            kw, hrs = max_battery_kw(max(w["start"], end), w["end"])
            held_kwh += max(0.0, kw * hrs - net_drain(max(w["start"], end), w["end"]))
    reserve_kwh = min(cap, floor_kwh + RESERVE_MARGIN * evening_kwh + held_kwh)

    session_periods = periods(start, end)
    hours = 0.0
    load_kwh = 0.0
    solar_kwh = 0.0
    for t, h in session_periods:
        hours += h
        load_kwh += load_kw(t) * h
        solar_kwh += solar_kw(t) * h

    spare_kwh = max(0.0, battery_at_start - reserve_kwh)
    if hours > 0:
        # Battery power that would push exactly the export limit out.
        export_cap_kw = max(0.0, (EXPORT_LIMIT_KW + (load_kwh - solar_kwh) / hours) / DISCHARGE_EFFICIENCY)
        power_kw = min(spare_kwh / hours, MAX_DISCHARGE_KW, export_cap_kw)
    else:
        power_kw = 0.0

    current = 0
    if power_kw > 0:
        current = min(MAX_DISCHARGE_AMPS, int(math.ceil(power_kw * 1000.0 / volts - 1e-6)))
    power_kw = min(power_kw, current * volts / 1000.0)

    export_kwh = 0.0
    import_kwh = 0.0
    value_pence = 0.0
    for t, h in session_periods:
        net = power_kw * DISCHARGE_EFFICIENCY + solar_kw(t) - load_kw(t)
        exported = min(EXPORT_LIMIT_KW, max(0.0, net)) * h
        export_kwh += exported
        import_kwh += max(0.0, -net) * h
        rate = export_rate_at(t)
        if rate is not None:
            value_pence += exported * rate

    stop_soc = int(math.ceil(reserve_kwh / cap * 100.0 - 1e-6)) if cap > 0 else 100
    stop_soc = max(FLOOR_SOC, min(100, stop_soc))
    avoided_kwh = max(0.0, load_kwh - import_kwh)

    return {
        "session_start": session["start"].isoformat(),
        "session_end": session["end"].isoformat(),
        "window": fmt_window(session["start"], session["end"]),
        "window_kind": session.get("kind", "power_down"),
        "window_id": session.get("id"),
        "hours": round(hours, 3),
        "battery_at_start_kwh": round(battery_at_start, 2),
        "reserve_kwh": round(reserve_kwh, 2),
        "held_for_power_down_kwh": round(held_kwh, 2),
        "evening_load_kwh": round(evening_kwh, 2),
        "spare_kwh": round(spare_kwh, 2),
        "battery_used_kwh": round(power_kw * hours, 2),
        "discharge_kw": round(power_kw, 2),
        "discharge_current": current,
        "stop_soc": stop_soc,
        "predicted_house_load_kwh": round(load_kwh, 2),
        "predicted_solar_kwh": round(solar_kwh, 2),
        "predicted_export_kwh": round(export_kwh, 2),
        "predicted_import_kwh": round(import_kwh, 2),
        "estimated_octopoints": int(round(avoided_kwh * session.get("octopoints_per_kwh", 0))),
        "estimated_export_value_pence": round(value_pence, 1),
    }


async def plan_for(session, now, later=None):
    soc = _float_state(SOC_SENSOR)
    if soc is None:
        log.warning("SOC sensor '%s' unavailable; can't plan", SOC_SENSOR)
        return None
    voltage = _float_state(BATTERY_VOLTAGE_SENSOR)
    profile = await ensure_load_profile()
    return predict(session, now, soc, voltage, profile, solar_forecast(), later)


# -----------------------------
# Reconcile: make the discharge slot match the plan
# -----------------------------
def desired_program(session, plan, now):
    """What the discharge slot should hold right now."""
    if session is None or plan is None:
        return {"window": EMPTY_SLOT, "current": None, "soc": None, "enabled": False}
    if now < session["start"] - timedelta(minutes=PREPARE_MINUTES) or now >= session["end"]:
        return {"window": EMPTY_SLOT, "current": None, "soc": None, "enabled": False}
    if plan["discharge_current"] <= 0:
        return {"window": EMPTY_SLOT, "current": None, "soc": None, "enabled": False}
    return {
        "window": fmt_window(session["start"], session["end"]),
        "current": plan["discharge_current"],
        "soc": plan["stop_soc"],
        "enabled": True,
    }


def ops_for(want):
    """The writes that take the inverter from `_program` to `want`, in a safe order."""
    have = _program
    unknown = have["unknown"]
    slot = DISCHARGE_SLOT - 1
    ops = []

    if not want["enabled"]:
        if unknown or have["enabled"]:
            ops.append(("switch", False))
        if unknown or have["window"] != EMPTY_SLOT:
            ops.append(("value", DISCHARGE_TIME_CIDS[slot], EMPTY_SLOT))
        return ops

    fresh = unknown or not have["enabled"] or have["window"] != want["window"]
    if fresh:
        ops.append(("value", DISCHARGE_TIME_CIDS[slot], want["window"]))
    if fresh or have["current"] is None or abs(want["current"] - have["current"]) >= CURRENT_STEP_AMPS:
        ops.append(("value", DISCHARGE_CURRENT_CIDS[slot], str(want["current"])))
    if fresh or want["soc"] != have["soc"]:
        ops.append(("value", DISCHARGE_SOC_CIDS[slot], str(want["soc"])))
    if fresh:
        ops.append(("switch", True))
    return ops


def _apply_ops_to_program(ops):
    slot = DISCHARGE_SLOT - 1
    for op in ops:
        if op[0] == "switch":
            _program["enabled"] = op[1]
        elif op[1] == DISCHARGE_TIME_CIDS[slot]:
            _program["window"] = op[2]
        elif op[1] == DISCHARGE_CURRENT_CIDS[slot]:
            _program["current"] = int(op[2])
        elif op[1] == DISCHARGE_SOC_CIDS[slot]:
            _program["soc"] = int(op[2])
    if not _program["enabled"] and _program["window"] == EMPTY_SLOT:
        _program["current"] = None
        _program["soc"] = None
    _program["unknown"] = False


async def reconcile(reason):
    # Serialise: pyscript runs on the event loop, so check-and-set is atomic.
    while _flags["busy"]:
        await asyncio.sleep(1)
    _flags["busy"] = True
    try:
        now = now_local()
        session, later = target_window(now)
        plan = None
        if session is not None:
            plan = await plan_for(session, now, later)
        want = desired_program(session, plan, now)
        ops = ops_for(want)

        if ops:
            log.info("[%s] discharge slot %s -> %s (current=%s soc=%s) via %s",
                     reason, DISCHARGE_SLOT, want["window"], want["current"], want["soc"], ops)
            ok = await solis_apply(ops)
            _flags["last_write_ok"] = ok
            if ok:
                _apply_ops_to_program(ops)
                is_power_down = session is not None and session["kind"] == "power_down"
                if want["enabled"] and session["id"] not in _joined["notified"] \
                        and (is_power_down or NOTIFY_PEAK):
                    _joined["notified"].add(session["id"])
                    await notify(
                        "Power Down export planned" if is_power_down else "Peak export planned",
                        f"{plan['window']}: exporting ~{plan['predicted_export_kwh']} kWh at "
                        f"{plan['discharge_current']} A, stopping at {plan['stop_soc']}% "
                        f"({plan['reserve_kwh']} kWh kept for the evening).",
                    )
            else:
                # Partial writes leave the inverter in an unknown state; rewrite it all next time.
                _program["unknown"] = True
                log.error("Failed to write discharge slot; will retry on next check")
        else:
            log.debug("[%s] no change (slot=%s)", reason, _program["window"])

        _last["plan"] = plan
        _last["session"] = session
        _flags["last_reason"] = reason
        publish_status(session, plan, now)
    finally:
        _flags["busy"] = False


def publish_status(session, plan, now):
    running = session is not None and session["start"] <= now < session["end"]
    active = running and session["kind"] == "power_down"
    if session is None:
        status = "idle"
    elif plan is not None and plan["discharge_current"] <= 0:
        status = "no_spare_energy"
    elif running and _program["enabled"]:
        status = "exporting"
    elif _program["enabled"]:
        status = "armed"
    else:
        status = "planned"

    attrs = {
        "session_id": session["id"] if session else None,
        "session_start": session["start"].isoformat() if session else None,
        "session_end": session["end"].isoformat() if session else None,
        "octopoints_per_kwh": session["octopoints_per_kwh"] if session else None,
        "discharge_slot": DISCHARGE_SLOT,
        "programmed_window": _program["window"],
        "programmed_current": _program["current"],
        "programmed_stop_soc": _program["soc"],
        "slot_enabled": _program["enabled"],
        "slot_state_unknown": _program["unknown"],
        "last_reason": _flags["last_reason"],
        "last_write_ok": _flags["last_write_ok"],
        "diagnostics_only": DIAGNOSTICS_ONLY,
        "version": VERSION,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "friendly_name": "Solis Power Down",
        "icon": "mdi:transmission-tower-export",
    }
    if plan:
        for key, value in plan.items():
            if key not in ("session_start", "session_end"):
                attrs[key] = value
    state.set(STATUS_SENSOR, status, new_attributes=attrs)
    state.set(ACTIVE_SENSOR, "on" if active else "off", new_attributes={
        "friendly_name": "Solis Power Down active",
        "icon": "mdi:transmission-tower-export",
    })


# -----------------------------
# Triggers
# -----------------------------
@time_trigger("startup")
async def solis_power_down_startup():
    log.info("=== Solis Power Down v%s starting (discharge slot %s, tz %s, diagnostics_only=%s, "
             "peak_export=%s %s) ===", VERSION, DISCHARGE_SLOT, INVERTER_TIMEZONE, DIAGNOSTICS_ONLY,
             PEAK_EXPORT, PEAK_WINDOW if PEAK_EXPORT else "")
    if not 1 <= DISCHARGE_SLOT <= 6:
        log.error("discharge_slot must be 1-6, not %s", DISCHARGE_SLOT)
        return
    if not EVENTS_ENTITY or hass.states.get(EVENTS_ENTITY) is None:
        log.warning("Power down events entity '%s' not found yet - check power_down_events_entity",
                    EVENTS_ENTITY)
    if not SOC_SENSOR:
        log.error("soc_sensor not configured - can't plan exports")
    # Let the Octopus / Solis integrations finish loading.
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    await log_storage_mode()
    await auto_join()
    # _program starts unknown, so this also clears any leftover slot.
    await reconcile("startup")


@state_trigger(f"{EVENTS_ENTITY or 'event.solis_power_down_unconfigured'}.*")
async def solis_power_down_events_changed(var_name=None, value=None):
    task.unique("solis_power_down_events", kill_me=True)
    await auto_join()
    await reconcile("events changed")


@state_trigger(f"{PEAK_SWITCH or 'input_boolean.solis_peak_export_unconfigured'}")
async def solis_power_down_peak_switch_changed(var_name=None, value=None):
    log.info("Peak export switch %s is now %s", var_name, value)
    await reconcile("peak switch")


@time_trigger("cron(*/5 * * * *)")
async def solis_power_down_periodic():
    await reconcile("periodic")


@time_trigger("cron(7 * * * *)")
async def solis_power_down_join_sweep():
    await auto_join()


# -----------------------------
# Services (for testing from Developer Tools -> Actions)
# -----------------------------
@service
async def solis_power_down_reconcile():
    """Re-plan the next export window and update the Solis discharge slot now."""
    await auto_join()
    await reconcile("manual")


@service
async def solis_power_down_clear():
    """Switch off and clear the discharge slot (it is rewritten if a session is still due)."""
    _program["unknown"] = True
    ok = await solis_apply(ops_for({"window": EMPTY_SLOT, "current": None, "soc": None, "enabled": False}))
    if ok:
        _program.update({"window": EMPTY_SLOT, "current": None, "soc": None,
                         "enabled": False, "unknown": False})


@service
async def solis_power_down_preview():
    """Log the export prediction for the next window (or available session) without writing."""
    now = now_local()
    session, later = target_window(now)
    if session is None:
        _, available = sessions()
        for s in available:
            if s["end"] > now and (session is None or s["start"] < session["start"]):
                session = s
    if session is None:
        log.info("Preview: no upcoming export window")
        return
    plan = await plan_for(session, now, later)
    log.info("Preview for %s: %s", fmt_window(session["start"], session["end"]), plan)
