"""NammaFlow Python full-stack demo server.

Run with: python app.py
Then open: http://localhost:3000
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "3000"))
FLEET_TOKEN = os.environ.get("DEMO_FLEET_TOKEN", "nammaflow-demo-only")
RECALCULATION_SECONDS = 30

SIGNAL_OFFSETS = (
    (0.0, 0.0),
    (0.0015, 0.0017),
    (-0.0016, 0.0013),
    (0.0013, -0.0016),
    (-0.0015, -0.0014),
    (0.0027, 0.0002),
)

OSM_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

LOCK = threading.RLock()
NEXT_RECALCULATION_AT = time.time() + RECALCULATION_SECONDS
AREA_NETWORKS: dict[str, dict] = {}
OSM_ROAD_CACHE: dict[tuple[float, float], tuple[float, list[dict]]] = {}

SIGNALS = [
    {
        "id": "N-04",
        "name": "Brigade × St. Marks",
        "road": "Brigade Road",
        "densityPercent": 28,
        "phase": "GREEN",
        "remainingSeconds": 18,
    },
    {
        "id": "N-07",
        "name": "Richmond × Hospital Link",
        "road": "Richmond Road",
        "densityPercent": 81,
        "phase": "RED",
        "remainingSeconds": 32,
    },
    {
        "id": "N-09",
        "name": "Cubbon × MG Road",
        "road": "Cubbon Road",
        "densityPercent": 24,
        "phase": "GREEN",
        "remainingSeconds": 41,
    },
]

STATE = {
    "averageWaitSeconds": 42,
    "vehiclesPerMinute": 186,
    "activeIntersections": 12,
    "densities": [
        {"label": "Low", "percent": 24},
        {"label": "Moderate", "percent": 57},
        {"label": "High", "percent": 81},
    ],
    "emergency": {"active": False},
    "updatedAt": datetime.now(timezone.utc).isoformat(),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def congestion_label(percent: int) -> str:
    if percent >= 70:
        return "High"
    if percent >= 45:
        return "Moderate"
    return "Low"


def phase_for(percent: int) -> str:
    if percent >= 70:
        return "RED"
    if percent >= 45:
        return "AMBER"
    return "GREEN"


def set_phase(signal: dict, phase: str, duration: int) -> None:
    signal["phase"] = phase
    signal["remainingSeconds"] = duration


def recalculate_traffic() -> None:
    """Refresh simulated corridor and loaded area estimates."""
    global NEXT_RECALCULATION_AT

    with LOCK:
        levels = [
            max(10, min(95, item["percent"] + random.randint(-12, 12)))
            for item in STATE["densities"]
        ]
        STATE["densities"] = [
            {"label": congestion_label(percent), "percent": percent}
            for percent in levels
        ]

        for index, signal in enumerate(SIGNALS):
            signal["densityPercent"] = levels[index]
            signal["remainingSeconds"] = max(
                12, min(60, 55 - round(levels[index] * 0.34))
            )
            set_phase(
                signal,
                "RED" if index == 1 else "GREEN",
                signal["remainingSeconds"],
            )

        average_density = sum(levels) / len(levels)
        STATE["averageWaitSeconds"] = 30 + math.floor(average_density * 0.18)
        STATE["vehiclesPerMinute"] = 150 + math.floor(sum(levels) * 0.55)

        if STATE["emergency"].get("active"):
            emergency = STATE["emergency"]
            emergency["etaMinutes"] = max(
                1, round(5 - emergency["speedKph"] / 25)
            )
            emergency["estimatedAt"] = now_iso()

            for index, signal in enumerate(SIGNALS):
                set_phase(
                    signal,
                    "AMBER" if index == 0 else "RED",
                    4 if index == 0 else 20,
                )

        timestamp = now_iso()
        STATE["updatedAt"] = timestamp

        for network in AREA_NETWORKS.values():
            for signal in network["signals"]:
                percent = max(
                    8, min(96, signal["densityPercent"] + random.randint(-12, 12))
                )
                signal["densityPercent"] = percent
                signal["averageWaitSeconds"] = round(18 + percent * 0.55)
                signal["congestion"] = congestion_label(percent)
                signal["phase"] = phase_for(percent)
                signal["updatedAt"] = timestamp

            network["updatedAt"] = timestamp

        NEXT_RECALCULATION_AT = time.time() + RECALCULATION_SECONDS


def automatic_recalculation_loop() -> None:
    while True:
        with LOCK:
            remaining = NEXT_RECALCULATION_AT - time.time()

        if remaining <= 0:
            recalculate_traffic()
        else:
            time.sleep(min(1, remaining))


def corridor_snapshot() -> dict:
    with LOCK:
        highest_density = max(
            STATE["densities"], key=lambda item: item["percent"]
        )

        return {
            "metrics": {
                "averageWaitSeconds": STATE["averageWaitSeconds"],
                "vehiclesPerMinute": STATE["vehiclesPerMinute"],
                "activeIntersections": STATE["activeIntersections"],
                "densityLabel": highest_density["label"],
            },
            "signals": [signal.copy() for signal in SIGNALS],
            "densities": [item.copy() for item in STATE["densities"]],
            "emergency": STATE["emergency"].copy(),
            "updatedAt": STATE["updatedAt"],
            "recalculationIntervalSeconds": RECALCULATION_SECONDS,
            "nextRecalculationInSeconds": max(
                0, math.ceil(NEXT_RECALCULATION_AT - time.time())
            ),
        }


def get_area_network(query: dict[str, list[str]]) -> dict:
    slug = (query.get("slug") or [""])[0]
    area_name = (query.get("name") or [""])[0]
    primary_signal = (query.get("signal") or [""])[0]
    road = (query.get("road") or [""])[0]

    if not re.fullmatch(r"[a-z0-9-]{1,40}", slug) or not area_name:
        raise ValueError("Area name and a valid area slug are required")

    try:
        latitude = float((query.get("lat") or [""])[0])
        longitude = float((query.get("lng") or [""])[0])
    except ValueError as error:
        raise ValueError("Valid area map coordinates are required") from error

    if (
        not math.isfinite(latitude)
        or not -90 <= latitude <= 90
        or not math.isfinite(longitude)
        or not -180 <= longitude <= 180
    ):
        raise ValueError("Valid area map coordinates are required")

    with LOCK:
        if slug not in AREA_NETWORKS:
            timestamp = now_iso()
            area_signals = []

            for index, (lat_offset, lng_offset) in enumerate(
                SIGNAL_OFFSETS, start=1
            ):
                density = random.randint(22, 86)
                area_signals.append(
                    {
                        "id": f"{slug.upper().replace('-', '')[:5]}-{index:02d}",
                        "name": (
                            primary_signal
                            if index == 1 and primary_signal
                            else f"{road or area_name} · Junction {index}"
                        ),
                        "road": road or area_name,
                        "latitude": latitude + lat_offset,
                        "longitude": longitude + lng_offset,
                        "densityPercent": density,
                        "averageWaitSeconds": round(18 + density * 0.55),
                        "congestion": congestion_label(density),
                        "phase": phase_for(density),
                        "updatedAt": timestamp,
                    }
                )

            AREA_NETWORKS[slug] = {
                "slug": slug,
                "areaName": area_name,
                "signals": area_signals,
                "updatedAt": timestamp,
            }

        network = AREA_NETWORKS[slug]
        network["areaName"] = area_name

        return {
            "area": network["areaName"],
            "count": len(network["signals"]),
            "signals": [signal.copy() for signal in network["signals"]],
            "updatedAt": network["updatedAt"],
            "recalculationIntervalSeconds": RECALCULATION_SECONDS,
            "nextRecalculationInSeconds": max(
                0, math.ceil(NEXT_RECALCULATION_AT - time.time())
            ),
        }


def get_osm_roads(latitude: float, longitude: float) -> list[dict]:
    """Fetch real OSM road geometry, cached for ten minutes."""
    if not (12.5 <= latitude <= 13.5 and 77.0 <= longitude <= 78.0):
        raise ValueError("Road geometry lookup is limited to the Bengaluru pilot area")

    key = (round(latitude, 5), round(longitude, 5))
    cached = OSM_ROAD_CACHE.get(key)

    if cached and cached[0] > time.time():
        return cached[1]

    query = (
        f'[out:json][timeout:8];'
        f'way(around:450,{latitude},{longitude})["highway"];out geom;'
    )

    def fetch_overpass(endpoint: str) -> list[dict]:
        payload = urlencode({"data": query}).encode("ascii")
        request = Request(
            endpoint,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "NammaFlow-Pilot/1.0 (Bengaluru traffic demo)",
            },
        )

        with urlopen(request, timeout=9) as response:
            body = response.read(4_000_001)

        if len(body) > 4_000_000:
            raise ValueError("OpenStreetMap response exceeded the demo size limit")

        elements = json.loads(body).get("elements", [])
        roads = [
            item
            for item in elements
            if item.get("type") == "way" and len(item.get("geometry", [])) > 1
        ]

        if not roads:
            raise ValueError("No road geometry returned by this endpoint")

        return roads

    with ThreadPoolExecutor(max_workers=len(OSM_ENDPOINTS)) as executor:
        futures = [executor.submit(fetch_overpass, endpoint) for endpoint in OSM_ENDPOINTS]

        try:
            for future in as_completed(futures, timeout=10):
                try:
                    roads = future.result()
                    OSM_ROAD_CACHE[key] = (time.time() + 600, roads)
                    return roads
                except Exception:
                    continue
        except TimeoutError:
            pass
        finally:
            for future in futures:
                future.cancel()

    # Use the OpenStreetMap map read API if all Overpass instances fail.
    try:
        lat_radius = 0.0041
        lon_radius = lat_radius / math.cos(math.radians(latitude))
        bbox = (
            longitude - lon_radius,
            latitude - lat_radius,
            longitude + lon_radius,
            latitude + lat_radius,
        )
        url = (
            "https://api.openstreetmap.org/api/0.6/map?bbox="
            + ",".join(str(value) for value in bbox)
        )
        request = Request(
            url,
            headers={
                "Accept": "application/xml",
                "User-Agent": "NammaFlow-Pilot/1.0 (Bengaluru traffic demo)",
            },
        )

        with urlopen(request, timeout=9) as response:
            body = response.read(4_000_001)

        if len(body) > 4_000_000:
            raise ValueError("OpenStreetMap response exceeded the demo size limit")

        root = ET.fromstring(body)
        nodes = {
            node.attrib["id"]: {
                "lat": float(node.attrib["lat"]),
                "lon": float(node.attrib["lon"]),
            }
            for node in root.findall("node")
        }
        excluded = {"footway", "path", "cycleway", "steps", "pedestrian"}
        roads = []

        for way in root.findall("way"):
            tags = {
                tag.attrib["k"]: tag.attrib["v"]
                for tag in way.findall("tag")
            }
            geometry = [
                nodes[node.attrib["ref"]]
                for node in way.findall("nd")
                if node.attrib.get("ref") in nodes
            ]

            if (
                tags.get("highway")
                and tags["highway"] not in excluded
                and len(geometry) > 1
            ):
                roads.append(
                    {
                        "type": "way",
                        "id": way.attrib.get("id"),
                        "tags": tags,
                        "geometry": geometry,
                    }
                )

        if roads:
            OSM_ROAD_CACHE[key] = (time.time() + 600, roads)
            return roads
    except Exception:
        pass

    raise OSError("OpenStreetMap road data is temporarily unavailable")


class NammaFlowHandler(BaseHTTPRequestHandler):
    server_version = "NammaFlowDemo/1.0"

    def log_message(self, _format: str, *_args) -> None:
        return

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid content length") from error

        if length > 8192:
            raise OverflowError("Request body is too large")

        if length == 0:
            return {}

        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("Request body must be valid JSON") from error

        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object")

        return value

    def require_fleet_auth(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {FLEET_TOKEN}":
            self.send_json(401, {"error": "Fleet authentication failed"})
            return False
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/health":
            return self.send_json(
                200, {"ok": True, "service": "NammaFlow demo API"}
            )

        if parsed.path == "/api/config":
            return self.send_json(
                200, {"demoToken": FLEET_TOKEN, "demoMode": True}
            )

        if parsed.path == "/api/state":
            return self.send_json(200, corridor_snapshot())

        if parsed.path == "/api/area-signals":
            try:
                return self.send_json(
                    200, get_area_network(parse_qs(parsed.query))
                )
            except ValueError as error:
                return self.send_json(400, {"error": str(error)})

        if parsed.path == "/api/osm-roads":
            try:
                query = parse_qs(parsed.query)
                latitude = float((query.get("lat") or [""])[0])
                longitude = float((query.get("lng") or [""])[0])

                if not math.isfinite(latitude) or not math.isfinite(longitude):
                    raise ValueError("Valid junction coordinates are required")

                return self.send_json(
                    200,
                    {
                        "elements": get_osm_roads(latitude, longitude),
                        "source": "OpenStreetMap",
                    },
                )
            except (ValueError, IndexError) as error:
                return self.send_json(
                    400,
                    {"error": str(error) or "Valid junction coordinates are required"},
                )
            except Exception:
                return self.send_json(
                    503, {"error": "OpenStreetMap road data is temporarily unavailable"}
                )

        if parsed.path == "/nexgenx-logo.jpeg":
            try:
                body = (ROOT / "nexgenx-logo.jpeg").read_bytes()
            except OSError:
                return self.send_json(404, {"error": "NexGenX logo is missing"})

            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path in ("/", "/index.html"):
            try:
                body = (ROOT / "index.html").read_bytes()
            except OSError:
                return self.send_json(500, {"error": "index.html is missing"})

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/traffic/cycle":
            recalculate_traffic()
            return self.send_json(200, corridor_snapshot())

        if parsed.path != "/api/emergency/priority":
            return self.send_json(404, {"error": "Not found"})

        if not self.require_fleet_auth():
            return

        try:
            payload = self.read_json()
        except OverflowError as error:
            return self.send_json(413, {"error": str(error)})
        except ValueError as error:
            return self.send_json(400, {"error": str(error)})

        vehicle_id = payload.get("vehicleId")
        values = [
            payload.get(key)
            for key in ("latitude", "longitude", "heading", "speedKph")
        ]
        valid_numbers = all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            for value in values
        )

        valid = (
            isinstance(vehicle_id, str)
            and re.fullmatch(r"[A-Z0-9-]{4,24}", vehicle_id)
            and valid_numbers
            and -90 <= values[0] <= 90
            and -180 <= values[1] <= 180
            and 0 <= values[2] < 360
            and 0 <= values[3] <= 180
        )

        if not valid:
            return self.send_json(
                400,
                {
                    "error": (
                        "Telemetry needs a vehicle ID, valid GPS coordinates, "
                        "heading and speed"
                    )
                },
            )

        timestamp = now_iso()

        with LOCK:
            STATE["emergency"] = {
                "active": True,
                "vehicleId": vehicle_id,
                "latitude": values[0],
                "longitude": values[1],
                "heading": values[2],
                "speedKph": values[3],
                "etaMinutes": max(1, round(5 - values[3] / 25)),
                "receivedAt": timestamp,
                "estimatedAt": timestamp,
            }

            for index, signal in enumerate(SIGNALS):
                set_phase(
                    signal,
                    "AMBER" if index == 0 else "RED",
                    4 if index == 0 else 20,
                )

            STATE["updatedAt"] = timestamp

        self.send_json(200, corridor_snapshot())

    def do_DELETE(self) -> None:
        if urlparse(self.path).path != "/api/emergency/priority":
            return self.send_json(404, {"error": "Not found"})

        if not self.require_fleet_auth():
            return

        with LOCK:
            STATE["emergency"] = {"active": False}
            for index, signal in enumerate(SIGNALS):
                set_phase(
                    signal,
                    "RED" if index == 1 else "GREEN",
                    (18, 32, 41)[index],
                )
            STATE["updatedAt"] = now_iso()

        self.send_json(200, corridor_snapshot())


def main() -> None:
    threading.Thread(
        target=automatic_recalculation_loop,
        daemon=True,
        name="traffic-recalculator",
    ).start()

    server = ThreadingHTTPServer(("127.0.0.1", PORT), NammaFlowHandler)
    print(f"NammaFlow Python demo running at http://localhost:{PORT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nNammaFlow server stopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()