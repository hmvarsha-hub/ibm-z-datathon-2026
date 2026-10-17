const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

const PORT = Number(process.env.PORT || 3000);
const DEMO_FLEET_TOKEN = process.env.DEMO_FLEET_TOKEN || 'nammaflow-demo-only';
const RECALCULATION_INTERVAL_MS = 30_000;

let nextRecalculationAt = Date.now() + RECALCULATION_INTERVAL_MS;
let recalculationTimer;

const areaSignalNetworks = new Map();
const osmRoadCache = new Map();

const OSM_ENDPOINTS = [
  'https://overpass-api.de/api/interpreter',
  'https://overpass.private.coffee/api/interpreter',
  'https://overpass.kumi.systems/api/interpreter',
];

const SIGNAL_OFFSETS = [
  [0, 0],
  [0.0015, 0.0017],
  [-0.0016, 0.0013],
  [0.0013, -0.0016],
  [-0.0015, -0.0014],
  [0.0027, 0.0002],
];

const signals = [
  {
    id: 'N-04',
    name: 'Brigade × St. Marks',
    road: 'Brigade Road',
    densityPercent: 28,
    phase: 'GREEN',
    remainingSeconds: 18,
  },
  {
    id: 'N-07',
    name: 'Richmond × Hospital Link',
    road: 'Richmond Road',
    densityPercent: 81,
    phase: 'RED',
    remainingSeconds: 32,
  },
  {
    id: 'N-09',
    name: 'Cubbon × MG Road',
    road: 'Cubbon Road',
    densityPercent: 24,
    phase: 'GREEN',
    remainingSeconds: 41,
  },
];

const state = {
  averageWaitSeconds: 42,
  vehiclesPerMinute: 186,
  activeIntersections: 12,
  densities: [
    { label: 'Low', percent: 24 },
    { label: 'Moderate', percent: 57 },
    { label: 'High', percent: 81 },
  ],
  emergency: { active: false },
  updatedAt: new Date().toISOString(),
};

function congestionLabel(percent) {
  if (percent >= 70) return 'High';
  if (percent >= 45) return 'Moderate';
  return 'Low';
}

function setPhase(signal, phase, duration) {
  signal.phase = phase;
  signal.remainingSeconds = duration;
}

function snapshot() {
  const now = Date.now();
  const highestDensity = state.densities.reduce(
    (max, item) => item.percent > max.percent ? item : max
  );

  return {
    metrics: {
      averageWaitSeconds: state.averageWaitSeconds,
      vehiclesPerMinute: state.vehiclesPerMinute,
      activeIntersections: state.activeIntersections,
      densityLabel: highestDensity.label,
    },
    signals: signals.map((signal) => ({ ...signal })),
    densities: state.densities.map((density) => ({ ...density })),
    emergency: { ...state.emergency },
    updatedAt: state.updatedAt,
    recalculationIntervalSeconds: RECALCULATION_INTERVAL_MS / 1000,
    nextRecalculationInSeconds: Math.max(
      0,
      Math.ceil((nextRecalculationAt - now) / 1000)
    ),
  };
}

function recalculateTraffic() {
  const levels = state.densities.map(({ percent }) =>
    Math.max(10, Math.min(95, percent + Math.floor(Math.random() * 25) - 12))
  );

  state.densities = levels.map((percent) => ({
    label: congestionLabel(percent),
    percent,
  }));

  signals.forEach((signal, index) => {
    signal.densityPercent = levels[index];
    signal.remainingSeconds = Math.max(
      12,
      Math.min(60, 55 - Math.round(levels[index] * 0.34))
    );
    setPhase(
      signal,
      index === 1 ? 'RED' : 'GREEN',
      signal.remainingSeconds
    );
  });

  const averageDensity =
    levels.reduce((sum, value) => sum + value, 0) / levels.length;

  state.averageWaitSeconds = 30 + Math.floor(averageDensity * 0.18);
  state.vehiclesPerMinute =
    150 + Math.floor(levels.reduce((sum, value) => sum + value, 0) * 0.55);

  if (state.emergency.active) {
    state.emergency.etaMinutes = Math.max(
      1,
      Math.round(5 - state.emergency.speedKph / 25)
    );
    state.emergency.estimatedAt = new Date().toISOString();

    [0, 1, 2].forEach((index) => {
      setPhase(
        signals[index],
        index === 0 ? 'AMBER' : 'RED',
        index === 0 ? 4 : 20
      );
    });
  }

  state.updatedAt = new Date().toISOString();

  for (const network of areaSignalNetworks.values()) {
    network.signals.forEach((signal) => {
      signal.densityPercent = Math.max(
        8,
        Math.min(
          96,
          signal.densityPercent + Math.floor(Math.random() * 25) - 12
        )
      );
      signal.averageWaitSeconds = Math.round(
        18 + signal.densityPercent * 0.55
      );
      signal.congestion = congestionLabel(signal.densityPercent);
      signal.phase =
        signal.densityPercent >= 70
          ? 'RED'
          : signal.densityPercent >= 45
            ? 'AMBER'
            : 'GREEN';
      signal.updatedAt = state.updatedAt;
    });
    network.updatedAt = state.updatedAt;
  }
}

function getAreaSignalNetwork(url, response) {
  const slug = url.searchParams.get('slug') || '';
  const latitude = Number(url.searchParams.get('lat'));
  const longitude = Number(url.searchParams.get('lng'));
  const areaName = url.searchParams.get('name') || '';
  const primarySignal = url.searchParams.get('signal') || '';
  const road = url.searchParams.get('road') || '';

  if (
    !/^[a-z0-9-]{1,40}$/.test(slug)
    || !areaName
    || !Number.isFinite(latitude)
    || latitude < -90
    || latitude > 90
    || !Number.isFinite(longitude)
    || longitude < -180
    || longitude > 180
  ) {
    sendJson(response, 400, {
      error: 'Area name and valid map coordinates are required',
    });
    return null;
  }

  if (!areaSignalNetworks.has(slug)) {
    const now = new Date().toISOString();

    const areaSignals = SIGNAL_OFFSETS.map(
      ([latOffset, lngOffset], index) => {
        const densityPercent = 22 + Math.floor(Math.random() * 65);

        return {
          id: `${slug.toUpperCase().replace(/-/g, '').slice(0, 5)}-${String(index + 1).padStart(2, '0')}`,
          name:
            index === 0 && primarySignal
              ? primarySignal
              : `${road || areaName} · Junction ${index + 1}`,
          road: road || areaName,
          latitude: latitude + latOffset,
          longitude: longitude + lngOffset,
          densityPercent,
          averageWaitSeconds: Math.round(18 + densityPercent * 0.55),
          congestion: congestionLabel(densityPercent),
          phase:
            densityPercent >= 70
              ? 'RED'
              : densityPercent >= 45
                ? 'AMBER'
                : 'GREEN',
          updatedAt: now,
        };
      }
    );

    areaSignalNetworks.set(slug, {
      slug,
      areaName,
      signals: areaSignals,
      updatedAt: now,
    });
  }

  return areaSignalNetworks.get(slug);
}

function areaSignalSnapshot(network) {
  return {
    area: network.areaName,
    count: network.signals.length,
    signals: network.signals.map((signal) => ({ ...signal })),
    updatedAt: network.updatedAt,
    recalculationIntervalSeconds: RECALCULATION_INTERVAL_MS / 1000,
    nextRecalculationInSeconds: Math.max(
      0,
      Math.ceil((nextRecalculationAt - Date.now()) / 1000)
    ),
  };
}

async function getOsmRoads(latitude, longitude) {
  if (
    latitude < 12.5
    || latitude > 13.5
    || longitude < 77
    || longitude > 78
  ) {
    throw Object.assign(
      new Error('Road geometry lookup is limited to the Bengaluru pilot area'),
      { status: 400 }
    );
  }

  const key = `${latitude.toFixed(5)},${longitude.toFixed(5)}`;
  const cached = osmRoadCache.get(key);

  if (cached && cached.expiresAt > Date.now()) {
    return cached.elements;
  }

  const query =
    `[out:json][timeout:8];way(around:450,${latitude},${longitude})["highway"];out geom;`;

  const fetchRoads = async (endpoint) => {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: {
        'content-type': 'application/x-www-form-urlencoded',
        accept: 'application/json',
        'user-agent': 'NammaFlow-Pilot/1.0 (Bengaluru traffic demo)',
      },
      body: new URLSearchParams({ data: query }),
      signal: AbortSignal.timeout(9000),
    });

    if (!response.ok) throw new Error(`Overpass ${response.status}`);

    const data = await response.json();
    const elements = (data.elements || []).filter(
      (item) => item.type === 'way' && item.geometry?.length > 1
    );

    if (!elements.length) throw new Error('No road geometry returned');
    return elements;
  };

  try {
    const elements = await Promise.any(OSM_ENDPOINTS.map(fetchRoads));
    osmRoadCache.set(key, {
      elements,
      expiresAt: Date.now() + 600_000,
    });
    return elements;
  } catch {
    // Fallback to OSM's map read API if all Overpass mirrors are unavailable.
    try {
      const latRadius = 0.0041;
      const lonRadius = latRadius / Math.cos(latitude * Math.PI / 180);
      const bbox = [
        longitude - lonRadius,
        latitude - latRadius,
        longitude + lonRadius,
        latitude + latRadius,
      ].join(',');

      const response = await fetch(
        `https://api.openstreetmap.org/api/0.6/map?bbox=${bbox}`,
        {
          headers: {
            accept: 'application/xml',
            'user-agent': 'NammaFlow-Pilot/1.0 (Bengaluru traffic demo)',
          },
          signal: AbortSignal.timeout(9000),
        }
      );

      if (!response.ok) throw new Error(`OSM API ${response.status}`);

      const xml = await response.text();
      const decodeXml = (value) =>
        value
          .replace(/&amp;/g, '&')
          .replace(/&quot;/g, '"')
          .replace(/&apos;/g, "'")
          .replace(/&lt;/g, '<')
          .replace(/&gt;/g, '>');

      const attr = (tag, name) =>
        decodeXml(tag.match(new RegExp(`\\b${name}="([^"]*)"`))?.[1] || '');

      const nodes = new Map(
        [...xml.matchAll(/<node\b([^>]*)\/?\s*>/g)].map((match) => {
          const tag = `<node ${match[1]}>`;
          return [
            attr(tag, 'id'),
            [Number(attr(tag, 'lat')), Number(attr(tag, 'lon'))],
          ];
        })
      );

      const elements = [
        ...xml.matchAll(/<way\b([^>]*)>([\s\S]*?)<\/way>/g),
      ].flatMap((match) => {
        const block = match[2];
        const tags = Object.fromEntries(
          [...block.matchAll(/<tag\b([^>]*)\/?\s*>/g)].map((tag) => [
            attr(`<tag ${tag[1]}>`, 'k'),
            attr(`<tag ${tag[1]}>`, 'v'),
          ])
        );

        if (
          !tags.highway
          || ['footway', 'path', 'cycleway', 'steps', 'pedestrian'].includes(
            tags.highway
          )
        ) {
          return [];
        }

        const geometry = [
          ...block.matchAll(/<nd\b([^>]*)\/?\s*>/g),
        ]
          .map((nd) => nodes.get(attr(`<nd ${nd[1]}>`, 'ref')))
          .filter(Boolean)
          .map(([lat, lon]) => ({ lat, lon }));

        return geometry.length > 1
          ? [{
              type: 'way',
              id: attr(`<way ${match[1]}>`, 'id'),
              tags,
              geometry,
            }]
          : [];
      });

      if (!elements.length) throw new Error('No road geometry in OSM response');

      osmRoadCache.set(key, {
        elements,
        expiresAt: Date.now() + 600_000,
      });

      return elements;
    } catch {
      throw Object.assign(
        new Error('OpenStreetMap road data is temporarily unavailable'),
        { status: 503 }
      );
    }
  }
}

function scheduleRecalculation() {
  clearTimeout(recalculationTimer);
  nextRecalculationAt = Date.now() + RECALCULATION_INTERVAL_MS;
  recalculationTimer = setTimeout(() => {
    recalculateTraffic();
    scheduleRecalculation();
  }, RECALCULATION_INTERVAL_MS);
  recalculationTimer.unref();
}

function sendJson(response, status, data) {
  response.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
  });
  response.end(JSON.stringify(data));
}

async function readJson(request) {
  let raw = '';

  for await (const chunk of request) {
    raw += chunk;
    if (raw.length > 8192) {
      throw Object.assign(new Error('Request body is too large'), { status: 413 });
    }
  }

  try {
    return raw ? JSON.parse(raw) : {};
  } catch {
    throw Object.assign(
      new Error('Request body must be valid JSON'),
      { status: 400 }
    );
  }
}

function requireFleetAuth(request, response) {
  if (request.headers.authorization !== `Bearer ${DEMO_FLEET_TOKEN}`) {
    sendJson(response, 401, { error: 'Fleet authentication failed' });
    return false;
  }
  return true;
}

const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, `http://${request.headers.host || 'localhost'}`);

  try {
    if (request.method === 'GET' && url.pathname === '/api/health') {
      return sendJson(response, 200, {
        ok: true,
        service: 'NammaFlow demo API',
      });
    }

    if (request.method === 'GET' && url.pathname === '/api/config') {
      return sendJson(response, 200, {
        demoToken: DEMO_FLEET_TOKEN,
        demoMode: true,
      });
    }

    if (request.method === 'GET' && url.pathname === '/api/state') {
      return sendJson(response, 200, snapshot());
    }

    if (request.method === 'GET' && url.pathname === '/api/area-signals') {
      const network = getAreaSignalNetwork(url, response);
      if (!network) return;
      network.areaName = url.searchParams.get('name') || network.areaName;
      return sendJson(response, 200, areaSignalSnapshot(network));
    }

    if (request.method === 'GET' && url.pathname === '/api/osm-roads') {
      const latitude = Number(url.searchParams.get('lat'));
      const longitude = Number(url.searchParams.get('lng'));

      if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) {
        return sendJson(response, 400, {
          error: 'Valid junction coordinates are required',
        });
      }

      const elements = await getOsmRoads(latitude, longitude);

      return sendJson(response, 200, {
        elements,
        source: 'OpenStreetMap',
      });
    }

    if (request.method === 'POST' && url.pathname === '/api/traffic/cycle') {
      recalculateTraffic();
      scheduleRecalculation();
      return sendJson(response, 200, snapshot());
    }

    if (request.method === 'POST' && url.pathname === '/api/emergency/priority') {
      if (!requireFleetAuth(request, response)) return;

      const payload = await readJson(request);
      const valid =
        typeof payload.vehicleId === 'string'
        && /^[A-Z0-9-]{4,24}$/.test(payload.vehicleId)
        && Number.isFinite(payload.latitude)
        && payload.latitude >= -90
        && payload.latitude <= 90
        && Number.isFinite(payload.longitude)
        && payload.longitude >= -180
        && payload.longitude <= 180
        && Number.isFinite(payload.heading)
        && payload.heading >= 0
        && payload.heading < 360
        && Number.isFinite(payload.speedKph)
        && payload.speedKph >= 0
        && payload.speedKph <= 180;

      if (!valid) {
        return sendJson(response, 400, {
          error: 'Telemetry needs a vehicle ID, valid GPS coordinates, heading and speed',
        });
      }

      const timestamp = new Date().toISOString();

      state.emergency = {
        active: true,
        vehicleId: payload.vehicleId,
        latitude: payload.latitude,
        longitude: payload.longitude,
        heading: payload.heading,
        speedKph: payload.speedKph,
        etaMinutes: Math.max(1, Math.round(5 - payload.speedKph / 25)),
        receivedAt: timestamp,
        estimatedAt: timestamp,
      };

      [0, 1, 2].forEach((index) => {
        setPhase(
          signals[index],
          index === 0 ? 'AMBER' : 'RED',
          index === 0 ? 4 : 20
        );
      });

      state.updatedAt = timestamp;

      return sendJson(response, 200, snapshot());
    }

    if (request.method === 'DELETE' && url.pathname === '/api/emergency/priority') {
      if (!requireFleetAuth(request, response)) return;

      state.emergency = { active: false };

      [0, 1, 2].forEach((index) => {
        setPhase(
          signals[index],
          index === 1 ? 'RED' : 'GREEN',
          [18, 32, 41][index]
        );
      });

      state.updatedAt = new Date().toISOString();
      return sendJson(response, 200, snapshot());
    }

    if (request.method === 'GET' && url.pathname === '/nexgenx-logo.jpeg') {
      response.writeHead(200, {
        'Content-Type': 'image/jpeg',
        'Cache-Control': 'public, max-age=3600',
        'X-Content-Type-Options': 'nosniff',
      });
      return fs
        .createReadStream(path.join(__dirname, 'nexgenx-logo.jpeg'))
        .pipe(response);
    }

    if (
      request.method === 'GET'
      && (url.pathname === '/' || url.pathname === '/index.html')
    ) {
      response.writeHead(200, {
        'Content-Type': 'text/html; charset=utf-8',
        'Cache-Control': 'no-cache',
        'X-Content-Type-Options': 'nosniff',
      });
      return fs.createReadStream(path.join(__dirname, 'index.html')).pipe(response);
    }

    return sendJson(response, 404, { error: 'Not found' });
  } catch (error) {
    return sendJson(response, error.status || 500, {
      error: error.status ? error.message : 'Internal server error',
    });
  }
});

server.listen(PORT, () => {
  console.log(`NammaFlow demo running at http://localhost:${PORT}`);
});

scheduleRecalculation();