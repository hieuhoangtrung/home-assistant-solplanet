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
import sys
from pathlib import Path
from typing import Any

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Request
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

DEFAULT_CONFIG: dict[str, Any] = {
    "inverter_ip": "",
    "inverter_port": 8484,
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


def _get_client() -> InverterClient:
    ip = _config.get("inverter_ip", "")
    if not ip:
        raise HTTPException(status_code=503, detail="Inverter IP not configured")
    return InverterClient(ip, int(_config.get("inverter_port", 80)))


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------
class ConfigPayload(BaseModel):
    inverter_ip: str
    inverter_port: int = 8484


@app.get("/api/config")
async def get_config():
    return _config


@app.post("/api/config")
async def set_config(payload: ConfigPayload):
    global _config
    _config["inverter_ip"] = payload.inverter_ip
    _config["inverter_port"] = payload.inverter_port
    save_config(_config)
    return {"ok": True}


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

        return {
            "version": version,
            "inverters": inv_list,
            "inverter_data": inv_data_list,
            "meter": meter_data,
            "batteries": battery_data,
            "schedule": schedule,
        }
    except HTTPException:
        raise
    except Exception as e:
        _LOGGER.exception("Error fetching live data")
        raise HTTPException(status_code=502, detail=str(e))


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


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/{path:path}")
async def spa_fallback(path: str):
    return FileResponse(str(STATIC_DIR / "index.html"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8088))
    _LOGGER.info("Starting Solplanet Dashboard on http://0.0.0.0:%d", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
