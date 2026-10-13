# Solis Power Down Export

A [pyscript](https://github.com/custom-components/pyscript) app for Home Assistant that exports from a **Solis** home battery during **Octopus Octoplus Power Down** sessions. It predicts how much the battery can export while still covering the house until the cheap overnight window.

It's a companion to [Solis EV-Follow](https://github.com/darvds/SolisOctopusEVFollow) and shares its SolisCloud code.

## How it works

1. **Join.** It watches the Octopus integration's power down events entity and joins every available session (`auto_join`).
2. **Predict.** For the next joined session it works out:
   - **House load** for each half-hour, using the mean of your load sensor at that time of day over the last `history_days` (from the recorder's long-term statistics).
   - **Solar** from an optional Solcast forecast.
   - **Battery at the session start.** This is the current SOC minus predicted use until then. If the cheap window comes first, it starts from `core_charge_soc` at the window's end.
   - **Evening reserve.** This is `floor_soc` plus the predicted house load (net of solar) from session end until the cheap window, × `reserve_margin`.
   - **Discharge power.** Everything above the reserve, spread over the session, capped by `max_discharge_kw`, `max_discharge_amps` and `export_limit_kw`.
   - **Predicted export, import and octopoints.** It also works out the stop SOC (the reserve as a %).
3. **Arm.** `prepare_minutes` before the start it writes one Solis **discharge slot**: time window, discharge current and stop SOC, then switches the slot on.
4. **Track.** Every 5 minutes during the session it re-plans from the real SOC and remaining time. If the current has drifted by `current_step_amps` or more, it rewrites it.
5. **Clear.** At the end it switches the slot off and resets it to `00:00-00:00`.

**Fail-safe:** the inverter runs the slot by itself. If Home Assistant, pyscript or SolisCloud goes down, discharging still stops at the session end and never goes below the stop SOC.

### Example

The example uses a 10 kWh battery, a 15% floor, `reserve_margin: 1.0`, a 0.5 kW house and a 17:00–18:00 session. The battery is at 56% at 16:45 and the cheap window starts at 23:30:

| | |
|---|---|
| Battery at 17:00 | 5.5 kWh |
| Evening reserve (1.5 + 18:00–23:30 × 0.5 kW) | 4.25 kWh |
| Discharge | 1.25 kW ≈ 25 A, stop at 43% |
| Predicted export | 0.69 kWh (the house uses 0.5 kWh; 5% is conversion loss) |

## Requirements

- Home Assistant with [pyscript](https://github.com/custom-components/pyscript) (HACS).
- [Octopus Energy integration](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy), enrolled in Octoplus, so `event.octopus_energy_<account>_octoplus_power_down_events` exists.
- A Solis hybrid inverter:
  - **6-slot firmware** (HMI ≥ `4B00`)
  - **time-of-use / timed charge-discharge enabled**
  - SolisCloud API access (key ID and secret)
- Sensors for:
  - **Battery SOC** (%)
  - **House load** (W or kW, with `state_class: measurement` so the recorder keeps hourly statistics)
  - **Battery voltage** (optional)

## Installation

1. Copy `pyscript/apps/solis_power_down.py` to `<config>/pyscript/apps/solis_power_down.py`.
2. Merge the block from [`configuration.example.yaml`](configuration.example.yaml) into `configuration.yaml`. Keep a single `pyscript:` block; it can sit next to `solis_ev_follow`.
3. Use the same SolisCloud secrets as EV-Follow (`solis_key_id`, `solis_secret`, `solis_username`, `solis_password`, `solis_plant_id`).
4. Restart Home Assistant.

Start with `diagnostics_only: true`. Run **`pyscript.solis_power_down_preview`** and check the prediction in the log and on `sensor.solis_power_down`. When it looks right, set `diagnostics_only: false` and restart.

`diagnostics_only` only stops inverter writes. Auto-join still happens; set `auto_join: false` to stop that too.

## Configuration

| Option | Default | Description |
|---|---|---|
| `key_id`, `secret`, `username`, `password`, `plantId` | **required** | SolisCloud API credentials |
| `inverter_sn` / `inverter_id` | auto | Only needed if the plant has more than one storage inverter |
| `power_down_events_entity` | **required** | `event.octopus_energy_<account>_octoplus_power_down_events` |
| `auto_join` | `true` | Join every available session |
| `notify_service` | none | e.g. `notify.mobile_app_phone`: messages when a session is joined and when export is armed |
| `soc_sensor` | **required** | Battery SOC (%) |
| `battery_capacity_kwh` | `10.0` | Usable battery capacity |
| `floor_soc` | `15` | Lowest SOC the app will discharge to |
| `core_charge_soc` | `100` | SOC the battery reaches by the end of the cheap window |
| `load_power_sensor` | none | House consumption sensor used for the load history |
| `history_days` | `14` | Days of history to average |
| `default_load_kw` | `0.5` | Load used for hours without history |
| `reserve_margin` | `1.2` | Multiplier on the predicted evening load |
| `solar_forecast_sensors` | none | Solcast sensors with a `detailedForecast` attribute |
| `battery_voltage_sensor` / `battery_voltage` | none / `51.2` | Used to turn kW into discharge amps |
| `max_discharge_kw` | `5.0` | Inverter / battery discharge limit |
| `max_discharge_amps` | `100` | Discharge current limit |
| `export_limit_kw` | `3.68` | Your export limit |
| `discharge_efficiency` | `0.95` | Battery-to-AC efficiency |
| `discharge_slot` | `1` | Solis discharge slot driven by this app (1–6) |
| `core_window` | `23:30-05:30` | Cheap window the reserve has to last until |
| `prepare_minutes` | `15` | How long before the session the slot is written |
| `current_step_amps` | `5` | Minimum change before the current is rewritten mid-session |
| `inverter_timezone` | HA time zone | Time zone the inverter clock and slots use |
| `sync_inverter_time` | `true` | Sync the inverter clock (at most once per 24h) |
| `diagnostics_only` | `false` | Log instead of writing to the inverter |
| `control_retries` | `3` | Retries per SolisCloud write |
| `verify_readback` | `false` | Read each value back after writing |
| `startup_delay_seconds` | `60` | Wait for other integrations after startup |

## Monitoring

- **`sensor.solis_power_down`**
  - **State:** one of the following.
    - `idle`: no session coming up.
    - `planned`: a session is coming but the slot isn't written yet.
    - `armed`: the slot is written.
    - `exporting`: the session is running.
    - `no_spare_energy`: nothing above the reserve to export.
  - **Attributes:** the full prediction (`predicted_export_kwh`, `predicted_house_load_kwh`, `reserve_kwh`, `battery_at_start_kwh`, `discharge_current`, `stop_soc`, `estimated_octopoints`, …), plus what's programmed on the inverter.
- **`binary_sensor.solis_power_down_active`**: `on` during a session.
- **Actions** (Developer Tools → Actions):
  - `pyscript.solis_power_down_reconcile`: join, re-plan and update the slot now.
  - `pyscript.solis_power_down_preview`: log the prediction for the next session without writing.
  - `pyscript.solis_power_down_clear`: switch off and clear the discharge slot.
- **Debug logging:**
  ```yaml
  logger:
    logs:
      pyscript.solis_power_down: debug
  ```

## Notes

- `estimated_octopoints` is a rough estimate: the import avoided × octopoints/kWh. Octopus measures the reduction against its own baseline.
- The exported energy is paid at your export tariff, separately from the Power Down reward.
- EV-Follow could pause grid charging while `binary_sensor.solis_power_down_active` is on. That isn't wired up yet.

## Development

The tests use only the Python standard library (3.9+):

```bash
python3 -m unittest discover -s tests -t .
```

`tests/harness.py` loads the app with fakes for:
- pyscript's globals
- the clock
- HA services
- recorder history
- SolisCloud

The tests cover:
- **Prediction maths:** reserve, export / current / power caps, overnight refill, load history, solar, DST
- **Sessions:** parsing, auto-join and retries
- **The session timeline:** arm, track, clear
- **SolisCloud write order:** values first, switch last; switch off before clearing
- **Diagnostics mode**

## Credits

- [Moondevil-ha/solis-smart-charging](https://github.com/Moondevil-ha/solis-smart-charging) and [stevegal/solis_control](https://github.com/stevegal/solis_control) (MIT): SolisCloud API signing, login and control writes.
- [mkuthan/solis-cloud-control](https://github.com/mkuthan/solis-cloud-control): discharge slot and switch CIDs, and the `yuanzhi` bitmask.
- [BottlecapDave/HomeAssistant-OctopusEnergy](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy): Octoplus Power Down entities and the join action.

Not affiliated with Solis, Octopus Energy or Home Assistant.

## License

[MIT](LICENSE)
