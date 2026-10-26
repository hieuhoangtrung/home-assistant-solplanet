"""Solplanet Battery Dashboard - standalone local web server.

Talks directly to the Solplanet inverter over HTTP (same protocol as the
Home Assistant integration) and exposes a REST API consumed by the SPA.

Usage:
    pip install -r requirements.txt
    python server.py
    # Open http://localhost:8088 in your browser
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
_LOGGER = logging.getLogger("solplanet.dashboard")

# ---------------------------------------------------------------------------
# Paths & config persistence
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
STATIC_DIR = BASE_DIR / "static"
HISTORY_DB = BASE_DIR / "history.db"
HISTORY_INTERVAL = 300  # seconds between persisted samples

DEFAULT_CONFIG: dict[str, Any] = {
    "inverter_ip": "",
    "inverter_port": 8484,
    "latitude": None,
    "longitude": None,
    "owm_api_key": None,
}


def load_config() -> dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            return {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text())}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ---------------------------------------------------------------------------
# Thin HTTP wrapper (mirrors SolplanetClient from the HA integration)
# ---------------------------------------------------------------------------
class InverterClient:
    def __init__(self, ip: str, port: int = 8484, timeout: int = 15) -> None:
        self.base_url = f"http://{ip}:{port}/"
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    def _make_session(self) -> aiohttp.ClientSession:
        # Use a plain TCP connector with SSL disabled - inverter dongles speak
        # plain HTTP only. Without force_close=True some dongles drop persistent
        # connections silently and subsequent requests hang.
        connector = aiohttp.TCPConnector(ssl=False, force_close=True)
        return aiohttp.ClientSession(
            connector=connector,
            timeout=self.timeout,
            connector_owner=True,
        )

    async def get(self, endpoint: str) -> Any:
        async with self._make_session() as session:
            async with session.get(self.base_url + endpoint) as resp:
                resp.raise_for_status()
                content = await resp.read()
                text = content.strip().decode(resp.get_encoding() or "utf-8", "replace")
                return json.loads(text)

    async def post(self, endpoint: str, data: Any) -> Any:
        payload = _to_serialisable(data)
        async with self._make_session() as session:
            async with session.post(self.base_url + endpoint, json=payload) as resp:
                resp.raise_for_status()
                content = await resp.read()
                text = content.strip().decode(resp.get_encoding() or "utf-8", "replace")
                return json.loads(text) if text.strip() else {}


def _to_serialisable(obj: Any) -> Any:
    """Recursively convert dataclasses / objects to plain dicts."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _to_serialisable(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
    if isinstance(obj, dict):
        return {k: _to_serialisable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_serialisable(i) for i in obj]
    return obj


# ---------------------------------------------------------------------------
# Protocol auto-detect helpers
# ---------------------------------------------------------------------------
async def _detect_version(client: InverterClient) -> str:
    """Return 'v2' or 'v1'."""
    try:
        data = await client.get("getdev.cgi?device=2")
        if isinstance(data, dict) and "inv" in data:
            return "v2"
    except Exception:
        pass
    return "v1"


async def _get_inverter_info(client: InverterClient, version: str) -> dict:
    endpoint = "getdev.cgi?device=2" if version == "v2" else "invinfo.cgi"
    data = await client.get(endpoint)
    return data


async def _get_inverter_data(client: InverterClient, version: str, sn: str) -> dict:
    endpoint = (
        f"getdevdata.cgi?device=2&sn={sn}" if version == "v2" else f"invdata.cgi?sn={sn}"
    )
    return await client.get(endpoint)


async def _get_meter_data(client: InverterClient, version: str) -> dict:
    endpoint = "getdevdata.cgi?device=3" if version == "v2" else "emeter.cgi"
    return await client.get(endpoint)


async def _get_battery_data(client: InverterClient, sn: str) -> dict:
    return await client.get(f"getdevdata.cgi?device=4&sn={sn}")


async def _get_battery_info(client: InverterClient, sn: str) -> dict:
    return await client.get(f"getdev.cgi?device=4&sn={sn}")


async def _get_schedule(client: InverterClient) -> dict:
    return await client.get("getdefine.cgi")


# ---------------------------------------------------------------------------
# Schedule helpers (ported from client.py)
# ---------------------------------------------------------------------------
SCHEDULE_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _decode_slot(code: int) -> dict | None:
    if code == 0:
        return None
    discharge_bit = code & 0x1
    duration_bits = (code >> 14) & 0x3
    half_hour_bit = (code >> 17) & 0x1
    hour_bits = code >> 24
    start_h = hour_bits
    start_m = 30 if half_hour_bit else 0
    duration = duration_bits + 1
    end_h = (start_h + duration) % 24
    return {
        "start_hour": start_h,
        "start_minute": start_m,
        "duration": duration,
        "mode": "discharge" if discharge_bit else "charge",
        "start": f"{start_h:02d}:{start_m:02d}",
        "end": f"{end_h:02d}:{start_m:02d}",
    }


def _encode_slot(start: str, duration: int, mode: str) -> int:
    hour, minute = map(int, start.split(":"))
    BASE = 0x3C02
    HOUR = 0x1000000
    HALF = 0x1E0000
    DURATION = 0x3C00
    return (
        BASE
        + (hour * HOUR)
        + ((minute // 30) * HALF)
        + ((duration - 1) * DURATION)
        + (1 if mode == "discharge" else 0)
    )


def _decode_schedule(raw: dict) -> dict:
    result: dict[str, Any] = {"Pin": raw.get("Pin", 0), "Pout": raw.get("Pout", 0), "days": {}}
    for day in SCHEDULE_DAYS:
        slots = []
        for code in raw.get(day, [])[:6]:
            slot = _decode_slot(code)
            if slot:
                slots.append(slot)
        result["days"][day] = slots
    return result


# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------
app = FastAPI(title="Solplanet Dashboard API")
_config: dict[str, Any] = load_config()

# Weather response cache: {"ts": epoch_seconds, "data": {...}}
_WEATHER_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_WEATHER_TTL = 600  # seconds
_geo_cache: dict[str, Any] | None = None
_history_conn: sqlite3.Connection | None = None
_last_history_recorded_at = 0.0


def _get_client() -> InverterClient:
    ip = _config.get("inverter_ip", "")
    if not ip:
        raise HTTPException(status_code=503, detail="Inverter IP not configured")
    return InverterClient(ip, int(_config.get("inverter_port", 80)))


# ---------------------------------------------------------------------------
# Local history persistence
# ---------------------------------------------------------------------------
def _db_init(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            ts      INTEGER PRIMARY KEY,
            ppv     REAL,
            pac     REAL,
            pbat    REAL,
            pgrid   REAL,
            pload   REAL,
            soc     REAL,
            tb      REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON readings(ts)")
    _db_ensure_column(conn, "pload", "REAL")
    conn.commit()
    return conn


def _db_ensure_column(conn: sqlite3.Connection, name: str, col_type: str) -> None:
    cur = conn.execute("PRAGMA table_info(readings)")
    existing = {row[1] for row in cur.fetchall()}
    if name not in existing:
        conn.execute(f"ALTER TABLE readings ADD COLUMN {name} {col_type}")


def _db() -> sqlite3.Connection:
    global _history_conn
    if _history_conn is None:
        _history_conn = _db_init(HISTORY_DB)
    return _history_conn


def _as_number(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _first_number(*values: Any) -> float | None:
    for value in values:
        number = _as_number(value)
        if number is not None:
            return number
    return None


def _estimate_home_load(pv_w: Any, bat_w: Any, grid_w: Any, explicit_w: Any = None) -> float:
    explicit = _as_number(explicit_w)
    if explicit is not None:
        return max(0.0, explicit)

    pv = _as_number(pv_w) or 0.0
    bat = _as_number(bat_w) or 0.0
    grid = _as_number(grid_w) or 0.0
    candidates = [
        pv + bat + grid,
        pv - bat - grid,
        pv - bat + grid,
        pv + bat - grid,
    ]
    positive = [candidate for candidate in candidates if candidate > 0]
    return min(positive) if positive else 0.0


def _history_row_from_live(data: dict[str, Any]) -> dict[str, Any]:
    inverter_data = ((data.get("inverter_data") or [{}])[0] or {}).get("data") or {}
    battery_data = ((data.get("batteries") or [{}])[0] or {}).get("data") or {}
    meter = data.get("meter") or {}

    ppv = _first_number(battery_data.get("ppv"), inverter_data.get("ppv"), inverter_data.get("pac"))
    pac = _first_number(inverter_data.get("pac"))
    pbat = _first_number(battery_data.get("pb"))
    pgrid = _first_number(meter.get("pac"))
    pload = _first_number(
        data.get("pload"),
        data.get("load"),
        data.get("homeLoad"),
        meter.get("pload"),
        meter.get("load"),
        meter.get("homeLoad"),
        meter.get("pcons"),
        inverter_data.get("pload"),
        inverter_data.get("load"),
        inverter_data.get("homeLoad"),
        battery_data.get("pload"),
        battery_data.get("load"),
        battery_data.get("homeLoad"),
    )

    return {
        "ts": int(time.time()),
        "ppv": ppv,
        "pac": pac,
        "pbat": pbat,
        "pgrid": pgrid,
        "pload": _estimate_home_load(ppv, pbat, pgrid, pload),
        "soc": _first_number(battery_data.get("soc")),
        "tb": _first_number(battery_data.get("tb")),
    }


def _record_history_snapshot(data: dict[str, Any]) -> None:
    global _last_history_recorded_at
    now = time.time()
    if now - _last_history_recorded_at < HISTORY_INTERVAL:
        return

    row = _history_row_from_live(data)
    _db().execute(
        "INSERT OR REPLACE INTO readings (ts,ppv,pac,pbat,pgrid,pload,soc,tb) VALUES (?,?,?,?,?,?,?,?)",
        (
            row["ts"],
            row["ppv"],
            row["pac"],
            row["pbat"],
            row["pgrid"],
            row["pload"],
            row["soc"],
            row["tb"],
        ),
    )
    _db().commit()
    _last_history_recorded_at = now


def _history_rows(since_ts: int) -> list[dict[str, Any]]:
    cur = _db().execute(
        "SELECT ts,ppv,pac,pbat,pgrid,pload,soc,tb FROM readings WHERE ts>=? ORDER BY ts",
        (since_ts,),
    )
    cols = ["ts", "ppv", "pac", "pbat", "pgrid", "pload", "soc", "tb"]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _group_rows_by_day(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    sorted_rows = sorted(rows, key=lambda r: r.get("ts") or 0)
    for i, row in enumerate(sorted_rows):
        ts = int(row.get("ts") or 0)
        if not ts:
            continue
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        bucket = grouped.setdefault(day, {
            "day": day,
            "samples": 0,
            "pv_avg": 0,
            "load_avg": 0,
            "grid_import_kwh": 0,
            "grid_export_kwh": 0,
            "battery_charge_kwh": 0,
            "battery_discharge_kwh": 0,
        })
        bucket["samples"] += 1
        bucket["pv_avg"] += float(row.get("ppv") or row.get("pac") or 0)
        bucket["load_avg"] += float(row.get("pload") or 0)
        if i == 0:
            continue
        prev = sorted_rows[i - 1]
        prev_ts = int(prev.get("ts") or ts)
        if time.strftime("%Y-%m-%d", time.localtime(prev_ts)) != day:
            continue
        dt_hours = max(0.0, min((ts - prev_ts) / 3600, 1.0))
        pgrid = float(prev.get("pgrid") or 0)
        pbat = float(prev.get("pbat") or 0)
        if pgrid > 0:
            bucket["grid_import_kwh"] += pgrid * dt_hours / 1000
        elif pgrid < 0:
            bucket["grid_export_kwh"] += abs(pgrid) * dt_hours / 1000
        if pbat > 0:
            bucket["battery_discharge_kwh"] += pbat * dt_hours / 1000
        elif pbat < 0:
            bucket["battery_charge_kwh"] += abs(pbat) * dt_hours / 1000

    for bucket in grouped.values():
        samples = max(1, int(bucket["samples"]))
        bucket["pv_avg"] = bucket["pv_avg"] / samples
        bucket["load_avg"] = bucket["load_avg"] / samples
    return list(grouped.values())


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------
class ConfigPayload(BaseModel):
    inverter_ip: str
    inverter_port: int = 8484
    latitude: float | None = None
    longitude: float | None = None
    owm_api_key: str | None = None


@app.get("/api/config")
async def get_config():
    return _config


@app.post("/api/config")
async def set_config(payload: ConfigPayload):
    global _config
    _config["inverter_ip"] = payload.inverter_ip
    _config["inverter_port"] = payload.inverter_port
    _config["latitude"] = payload.latitude
    _config["longitude"] = payload.longitude
    _config["owm_api_key"] = payload.owm_api_key
    save_config(_config)
    _WEATHER_CACHE["ts"] = 0.0  # invalidate cache so new location applies
    return {"ok": True}


@app.get("/api/ha_context")
async def get_ha_context():
    """Standalone mode has no Home Assistant entity registry."""
    return {
        "available": False,
        "language": None,
        "time_zone": None,
        "unit_system": None,
        "devices": [],
        "entities": {},
        "automations": [],
        "scripts": [],
    }


@app.post("/api/ha_service")
async def call_ha_service():
    """Standalone mode cannot proxy Home Assistant services."""
    raise HTTPException(
        status_code=400,
        detail="Home Assistant service proxy is unavailable in standalone mode",
    )


# ---------------------------------------------------------------------------
# Local network scan
# ---------------------------------------------------------------------------
async def _try_inverter(ip: str, timeout: float = 1.5) -> dict | None:
    """Return basic info if the IP looks like a Solplanet inverter."""
    connector = aiohttp.TCPConnector(ssl=False, force_close=True)
    t = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(connector=connector, timeout=t, connector_owner=True) as s:
        # Try V2 on port 8484 (primary dongle port)
        try:
            async with s.get(f"http://{ip}:8484/getdev.cgi?device=2") as resp:
                if resp.status == 200:
                    data = json.loads(await resp.read())
                    if isinstance(data, dict) and "inv" in data:
                        inv_list = data.get("inv", [])
                        sn = inv_list[0].get("sn", "") if inv_list else ""
                        mod = inv_list[0].get("mod", "") if inv_list else ""
                        return {"ip": ip, "sn": sn, "model": mod, "version": "v2"}
        except Exception:
            pass
        # Try V1 on port 8484
        try:
            async with s.get(f"http://{ip}:8484/invinfo.cgi") as resp:
                if resp.status == 200:
                    data = json.loads(await resp.read())
                    if isinstance(data, dict) and "inv" in data:
                        inv_list = data.get("inv", [])
                        sn = inv_list[0].get("sn", "") if inv_list else ""
                        return {"ip": ip, "sn": sn, "model": "", "version": "v1"}
        except Exception:
            pass
    return None


@app.get("/api/scan")
async def scan_network():
    """Scan the local /24 subnet for Solplanet inverters."""
    import socket

    results = []
    # Detect local IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "192.168.1.1"

    network = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
    hosts = list(network.hosts())

    semaphore = asyncio.Semaphore(50)

    async def check(ip: str):
        async with semaphore:
            result = await _try_inverter(ip)
            if result:
                results.append(result)

    await asyncio.gather(*[check(str(h)) for h in hosts])
    return {"found": results}


# ---------------------------------------------------------------------------
# Live data endpoint - aggregates all device data in one call
# ---------------------------------------------------------------------------
@app.get("/api/data")
async def get_live_data():
    client = _get_client()
    try:
        version = await _detect_version(client)
        inv_info = await _get_inverter_info(client, version)
        inv_list = inv_info.get("inv", [])
        if not inv_list:
            raise HTTPException(status_code=502, detail="No inverter found")

        # Fetch all inverter data concurrently
        inv_sns = [i.get("sn", "") for i in inv_list]

        async def fetch_inv(sn: str):
            try:
                return {"sn": sn, "data": await _get_inverter_data(client, version, sn)}
            except Exception as e:
                return {"sn": sn, "error": str(e)}

        inv_data_list = await asyncio.gather(*[fetch_inv(sn) for sn in inv_sns])

        # Meter
        meter_data: dict = {}
        try:
            meter_data = await _get_meter_data(client, version)
        except Exception:
            pass

        # Battery (V2 only)
        battery_data: list = []
        battery_sns: list[str] = []
        if version == "v2":
            for inv in inv_list:
                if inv.get("stu", 0) == 1 or inv.get("sn", ""):
                    # Try to find battery SNs from battery info
                    try:
                        binfo = await client.get("getdev.cgi?device=4&sn=" + inv.get("sn", ""))
                        bat_sn = binfo.get("sn") or inv.get("sn", "")
                        battery_sns.append(bat_sn)
                    except Exception:
                        pass

            async def fetch_bat(sn: str):
                try:
                    bdata = await _get_battery_data(client, sn)
                    binfo = await _get_battery_info(client, sn)
                    return {"sn": sn, "data": bdata, "info": binfo}
                except Exception as e:
                    return {"sn": sn, "error": str(e)}

            if battery_sns:
                battery_data = await asyncio.gather(*[fetch_bat(sn) for sn in battery_sns])
            battery_data = list(battery_data)

        # Schedule (V2 only)
        schedule: dict = {}
        if version == "v2":
            try:
                raw_sched = await _get_schedule(client)
                schedule = _decode_schedule(raw_sched)
            except Exception:
                pass

        result = {
            "version": version,
            "inverters": inv_list,
            "inverter_data": inv_data_list,
            "meter": meter_data,
            "batteries": battery_data,
            "schedule": schedule,
        }
        _record_history_snapshot(result)
        return result
    except HTTPException:
        raise
    except Exception as e:
        _LOGGER.exception("Error fetching live data")
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/history")
async def get_history(hours: int = 24, group: str | None = None, all_: int = Query(0, alias="all")):
    """Return locally persisted standalone readings."""
    if all_:
        since = 0
    else:
        since = int(time.time()) - max(1, min(hours, 24 * 365)) * 3600
    rows = _history_rows(since)
    if group == "day":
        return _group_rows_by_day(rows)
    return rows


# ---------------------------------------------------------------------------
# Battery control endpoints
# ---------------------------------------------------------------------------
class WorkModePayload(BaseModel):
    sn: str
    type: int
    mod_r: int


@app.post("/api/battery/work_mode")
async def set_work_mode(payload: WorkModePayload):
    client = _get_client()
    try:
        binfo_raw = await client.get(f"getdev.cgi?device=4&sn={payload.sn}")
        req = {
            "key": "bat",
            "value": {
                "type": payload.type,
                "mod_r": payload.mod_r,
                "sn": payload.sn,
                "discharge_max": binfo_raw.get("discharge_max", 20),
                "charge_max": binfo_raw.get("charge_max", 100),
                "muf": binfo_raw.get("battery", {}).get("muf") if isinstance(binfo_raw.get("battery"), dict) else binfo_raw.get("muf"),
                "mod": binfo_raw.get("battery", {}).get("mod") if isinstance(binfo_raw.get("battery"), dict) else binfo_raw.get("mod"),
                "num": binfo_raw.get("battery", {}).get("num") if isinstance(binfo_raw.get("battery"), dict) else binfo_raw.get("num", 1),
            },
        }
        result = await client.post("setting.cgi", req)
        return {"ok": True, "result": result}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


class SocLimitPayload(BaseModel):
    sn: str
    soc_min: int | None = None
    soc_max: int | None = None


@app.post("/api/battery/soc_limits")
async def set_soc_limits(payload: SocLimitPayload):
    client = _get_client()
    try:
        binfo_raw = await client.get(f"getdev.cgi?device=4&sn={payload.sn}")
        bat = binfo_raw.get("battery", binfo_raw)
        req = {
            "key": "bat",
            "value": {
                "type": binfo_raw.get("type", 1),
                "mod_r": binfo_raw.get("mod_r", 1),
                "sn": payload.sn,
                "discharge_max": payload.soc_min if payload.soc_min is not None else binfo_raw.get("discharge_max", 20),
                "charge_max": payload.soc_max if payload.soc_max is not None else binfo_raw.get("charge_max", 100),
                "muf": bat.get("muf") if isinstance(bat, dict) else None,
                "mod": bat.get("mod") if isinstance(bat, dict) else None,
                "num": bat.get("num") if isinstance(bat, dict) else 1,
            },
        }
        result = await client.post("setting.cgi", req)
        return {"ok": True, "result": result}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


class ScheduleSlotModel(BaseModel):
    start: str   # "HH:MM"
    duration: int  # 1-4 hours
    mode: str    # "charge" | "discharge"


class SchedulePayload(BaseModel):
    days: dict[str, list[ScheduleSlotModel]]
    pin: int = 0
    pout: int = 0


@app.get("/api/schedule")
async def get_schedule():
    client = _get_client()
    try:
        raw = await _get_schedule(client)
        return _decode_schedule(raw)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/schedule")
async def set_schedule(payload: SchedulePayload):
    client = _get_client()
    try:
        encoded: dict[str, Any] = {"Pin": payload.pin, "Pout": payload.pout}
        for day, slots in payload.days.items():
            if slots:
                encoded[day] = [_encode_slot(s.start, s.duration, s.mode) for s in slots]
        req = {"key": "timer", "value": encoded}
        result = await client.post("setting.cgi", req)
        return {"ok": True, "result": result}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# Serve SPA
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------
# Map WMO weather codes (Open-Meteo) to our icon/description buckets.
_WMO = {
    0: ("clear", "Clear sky"),
    1: ("mostly_clear", "Mainly clear"),
    2: ("partly_cloudy", "Partly cloudy"),
    3: ("overcast", "Overcast"),
    45: ("fog", "Fog"),
    48: ("fog", "Rime fog"),
    51: ("drizzle", "Light drizzle"),
    53: ("drizzle", "Drizzle"),
    55: ("drizzle", "Dense drizzle"),
    61: ("rain", "Light rain"),
    63: ("rain", "Rain"),
    65: ("rain", "Heavy rain"),
    66: ("rain", "Freezing rain"),
    67: ("rain", "Freezing rain"),
    71: ("snow", "Light snow"),
    73: ("snow", "Snow"),
    75: ("snow", "Heavy snow"),
    77: ("snow", "Snow grains"),
    80: ("rain", "Rain showers"),
    81: ("rain", "Rain showers"),
    82: ("rain", "Violent rain showers"),
    85: ("snow", "Snow showers"),
    86: ("snow", "Snow showers"),
    95: ("thunder", "Thunderstorm"),
    96: ("thunder", "Thunderstorm with hail"),
    99: ("thunder", "Thunderstorm with hail"),
}


def _wmo(code: int) -> tuple[str, str]:
    return _WMO.get(int(code), ("unknown", "Unknown"))


def _owm_condition(main: str, icon: str) -> str:
    m = (main or "").lower()
    mapping = {
        "clear": "clear",
        "clouds": "cloudy",
        "rain": "rain",
        "drizzle": "drizzle",
        "snow": "snow",
        "thunderstorm": "thunder",
        "mist": "fog",
        "fog": "fog",
        "haze": "fog",
        "smoke": "fog",
    }
    cond = mapping.get(m, "unknown")
    # OWM icon "02"/"03"/"04" indicate increasing cloudiness for clear/clouds.
    if cond == "cloudy" and icon.startswith("02"):
        return "partly_cloudy"
    return cond


async def _geolocate(session: aiohttp.ClientSession) -> dict[str, Any] | None:
    """Best-effort IP geolocation (cached for the process lifetime)."""
    global _geo_cache
    if _geo_cache:
        return _geo_cache
    try:
        async with session.get("https://ipapi.co/json/") as resp:
            if resp.status == 200:
                d = await resp.json()
                _geo_cache = {
                    "lat": d.get("latitude"),
                    "lon": d.get("longitude"),
                    "name": ", ".join(x for x in [d.get("city"), d.get("country_name")] if x),
                }
                return _geo_cache
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("IP geolocation failed: %s", err)
    return None


async def _fetch_open_meteo(session, lat, lon, name) -> dict[str, Any]:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
        "weather_code,wind_speed_10m"
        "&hourly=temperature_2m,weather_code"
        "&forecast_days=1&timezone=auto"
    )
    async with session.get(url) as resp:
        resp.raise_for_status()
        d = await resp.json()
    cur = d.get("current", {})
    cond, desc = _wmo(cur.get("weather_code", -1))
    hourly = []
    h = d.get("hourly", {})
    times = h.get("time", [])
    temps = h.get("temperature_2m", [])
    codes = h.get("weather_code", [])
    now_hour = time.strftime("%Y-%m-%dT%H:00")
    start = next((i for i, t in enumerate(times) if t >= now_hour), 0)
    for i in range(start, min(start + 6, len(times))):
        c, _ = _wmo(codes[i])
        hourly.append({"label": times[i][11:16], "temp": temps[i], "condition": c})
    return {
        "available": True,
        "source": "Open-Meteo",
        "location": name or f"{lat:.2f}, {lon:.2f}",
        "temp": cur.get("temperature_2m"),
        "feels_like": cur.get("apparent_temperature"),
        "humidity": cur.get("relative_humidity_2m"),
        "wind": cur.get("wind_speed_10m"),
        "condition": cond,
        "description": desc,
        "hourly": hourly,
    }


async def _fetch_owm(session, lat, lon, name, key) -> dict[str, Any]:
    base = "https://api.openweathermap.org/data/2.5"
    cur_url = f"{base}/weather?lat={lat}&lon={lon}&units=metric&appid={key}"
    fc_url = f"{base}/forecast?lat={lat}&lon={lon}&units=metric&cnt=6&appid={key}"
    async with session.get(cur_url) as resp:
        resp.raise_for_status()
        c = await resp.json()
    w = (c.get("weather") or [{}])[0]
    cond = _owm_condition(w.get("main", ""), w.get("icon", ""))
    hourly = []
    try:
        async with session.get(fc_url) as resp:
            resp.raise_for_status()
            fc = await resp.json()
        for slot in fc.get("list", [])[:6]:
            sw = (slot.get("weather") or [{}])[0]
            hourly.append({
                "label": slot.get("dt_txt", "")[11:16],
                "temp": slot.get("main", {}).get("temp"),
                "condition": _owm_condition(sw.get("main", ""), sw.get("icon", "")),
            })
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("OWM forecast failed: %s", err)
    return {
        "available": True,
        "source": "OpenWeatherMap",
        "location": name or c.get("name") or f"{lat:.2f}, {lon:.2f}",
        "temp": c.get("main", {}).get("temp"),
        "feels_like": c.get("main", {}).get("feels_like"),
        "humidity": c.get("main", {}).get("humidity"),
        "wind": (c.get("wind", {}).get("speed") or 0) * 3.6,  # m/s -> km/h
        "condition": cond,
        "description": w.get("description", "").title(),
        "hourly": hourly,
    }


@app.get("/api/weather")
async def get_weather():
    """Return current weather + short forecast, cached for ~10 minutes."""
    now = time.time()
    if _WEATHER_CACHE["data"] is not None and now - _WEATHER_CACHE["ts"] < _WEATHER_TTL:
        return _WEATHER_CACHE["data"]

    lat = _config.get("latitude")
    lon = _config.get("longitude")
    key = _config.get("owm_api_key")
    name = None

    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if lat is None or lon is None:
                geo = await _geolocate(session)
                if not geo or geo.get("lat") is None:
                    return {"available": False, "reason": "no_location"}
                lat, lon, name = geo["lat"], geo["lon"], geo.get("name")
            if key:
                data = await _fetch_owm(session, lat, lon, name, key)
            else:
                data = await _fetch_open_meteo(session, lat, lon, name)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Weather fetch failed: %s", err)
        # Serve stale cache if we have it, else report unavailable.
        if _WEATHER_CACHE["data"] is not None:
            return _WEATHER_CACHE["data"]
        return {"available": False, "reason": "fetch_error"}

    _WEATHER_CACHE["data"] = data
    _WEATHER_CACHE["ts"] = now
    return data


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/{path:path}")
async def spa_fallback(path: str):
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(STATIC_DIR / "index.html"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8088))
    _LOGGER.info("Starting Solplanet Dashboard on http://0.0.0.0:%d", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
