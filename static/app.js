/* ProjectorCal — Single-page app logic */

'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  ws: null,
  wsReconnectTimer: null,
  warmupTimer: null,
  warmupReadings: [],
  warmupMonitoring: false,
  patchData: {},        // patch_name → {deltaE, iterations, converged, active}
  runReport: null,      // last completed report
  totalPatches: 9,
  donePatches: 0,
};

// Guided-setup progress. Drives the workflow strip, the per-step checkmarks,
// and the Run tab readiness banner.
const setupState = {
  connections: false,          // Save & Test passed for projector + PGenerator
  probe: false,                // command set verified (ESC/VP21 probe or ExLink spike)
  placement: false,            // placement checklist confirmed
  validated: false,            // pre-flight validation returned ready
  calibrated: false,           // at least one run completed this session
  colorimeterAvailable: null,  // null = unknown, false = POSIX-only host problem
  deviceType: 'epson_5040ub',  // from /api/setup/status
};

const PATCH_COLORS = {
  white:   '#ffffff', red:    '#ff2020', green:  '#20dd20',
  blue:    '#2060ff', cyan:   '#00e8e8', magenta:'#ff00ff',
  yellow:  '#ffee00', grey75: '#bfbfbf', grey50: '#808080',
};

const PATCH_ORDER = ['white','red','green','blue','cyan','magenta','yellow','grey75','grey50'];

// ---------------------------------------------------------------------------
// Tab routing
// ---------------------------------------------------------------------------

document.querySelectorAll('nav button').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('view-' + btn.dataset.view).classList.add('active');
    if (btn.dataset.view === 'profiles') loadProfiles();
    if (btn.dataset.view === 'run') refreshSetupStatus().then(renderRunReadiness);
  });
});

// ---------------------------------------------------------------------------
// Guided setup — progress tracking and readiness
// ---------------------------------------------------------------------------

function updateSetupProgress() {
  const steps = [
    ['ov-connect',  'step-status-connect',  setupState.connections],
    ['ov-probe',    'step-status-probe',    setupState.probe],
    ['ov-place',    'step-status-place',    setupState.placement],
    ['ov-validate', 'step-status-validate', setupState.validated],
    ['ov-run',      null,                   setupState.calibrated],
  ];
  steps.forEach(([ovId, statusId, done]) => {
    const ov = document.getElementById(ovId);
    if (ov) ov.classList.toggle('done', !!done);
    if (statusId) {
      const el = document.getElementById(statusId);
      if (el) el.textContent = done ? '✓ done' : '';
    }
  });
}

async function refreshSetupStatus() {
  try {
    const res = await fetch('/api/setup/status');
    const data = await res.json();
    setupState.deviceType = data.device_type || 'epson_5040ub';
    setupState.probe = setupState.deviceType === 'samsung_ks8000_exlink'
      ? !!data.exlink_wb_verified
      : !!data.command_table_complete;
    setupState.colorimeterAvailable = !!data.colorimeter_available;
  } catch { /* leave last-known state */ }
  updateSetupProgress();
}

function renderRunReadiness() {
  const el = document.getElementById('run-readiness');
  if (!el) return;
  const samsung = setupState.deviceType === 'samsung_ks8000_exlink';
  const warnings = [];
  if (samsung) {
    warnings.push('Samsung KS8000 (ExLink): supports <strong>SDR</strong> + the ' +
                  '<strong>White Balance</strong> phase only — set Mode to SDR and Phase to White Balance.');
    if (!setupState.probe) {
      warnings.push('ExLink WB commands are not yet verified on your TV — run ' +
                    '<code>scripts/exlink_spike.py</code> first; until then only <strong>Dry Run</strong> is allowed.');
    }
  } else if (!setupState.probe) {
    warnings.push('The command table is incomplete — run the <strong>Probe</strong> ' +
                  '(Setup, step 2) first. Corrections can’t be sent without it.');
  }
  if (setupState.colorimeterAvailable === false) {
    warnings.push('No colorimeter driver on this machine (Linux/macOS only) — ' +
                  'only <strong>Dry Run</strong> will work here.');
  }
  el.classList.remove('hidden');
  if (warnings.length) {
    el.className = 'banner banner-warn';
    el.innerHTML = warnings.map(w => '⚠ ' + w).join('<br>');
  } else {
    el.className = 'banner banner-ok';
    el.innerHTML = '✓ Setup looks good. Pick a mode and press Start — or tick Dry Run to rehearse safely.';
  }
}

function onDeviceTypeChange() {
  const samsung = document.getElementById('device-type').value === 'samsung_ks8000_exlink';
  document.getElementById('exlink-fields').classList.toggle('hidden', !samsung);
  document.getElementById('projector-conn-fields').classList.toggle('hidden', samsung);
}

function onPlacementConfirm(checked) {
  document.getElementById('placement-check').textContent = checked ? '✅' : '⚪';
  setupState.placement = checked;
  updateSetupProgress();
}

// ---------------------------------------------------------------------------
// Config load/save — the Setup fields ARE the config the run uses
// ---------------------------------------------------------------------------

async function loadConfigIntoFields() {
  try {
    const res = await fetch('/api/config');
    const cfg = await res.json();
    document.getElementById('proj-ip').value      = cfg.projector.host || '';
    document.getElementById('proj-port').value    = cfg.projector.port;
    document.getElementById('pgen-ip').value      = cfg.pgen.host || '';
    document.getElementById('pgen-port').value    = cfg.pgen.port;
    document.getElementById('spotread-path').value = cfg.colorimeter.spotread_path;
    document.getElementById('serial-baud').value  = cfg.projector.serial_baud;
    if (cfg.projector.transport === 'serial') {
      document.querySelector('input[name="proj-transport"][value="serial"]').checked = true;
      setTransport('serial');
    }
    if (cfg.device) {
      document.getElementById('device-type').value = cfg.device.type;
      document.getElementById('exlink-port').value = cfg.device.exlink_port || '';
      onDeviceTypeChange();
    }
  } catch { /* keep placeholders */ }
}

async function saveConfigFromFields() {
  const statusEl = document.getElementById('config-save-status');
  const body = {
    config: {
      projector: {
        host: v('proj-ip'),
        port: parseInt(v('proj-port')) || 3629,
        transport: getTransport(),
        serial_port: v('serial-port-select'),
        serial_baud: parseInt(v('serial-baud')) || 9600,
      },
      pgen: { host: v('pgen-ip'), port: parseInt(v('pgen-port')) || 85 },
      colorimeter: { spotread_path: v('spotread-path') || 'spotread' },
      device: {
        type: v('device-type'),
        exlink_port: v('exlink-port') || '/dev/ttyUSB1',
      },
    },
  };
  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (res.ok) {
      if (statusEl) statusEl.textContent = 'Saved ✓';
      return true;
    }
    const err = await res.json().catch(() => ({}));
    if (statusEl) statusEl.textContent = 'Not saved: ' + (err.detail || res.statusText);
    return false;
  } catch (e) {
    if (statusEl) statusEl.textContent = 'Not saved: ' + e.message;
    return false;
  }
}

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;

  ws.onopen = () => {
    document.getElementById('ws-status').textContent = 'Connected';
    document.getElementById('ws-status').className = 'connected';
    if (state.wsReconnectTimer) { clearTimeout(state.wsReconnectTimer); state.wsReconnectTimer = null; }
  };

  ws.onclose = () => {
    document.getElementById('ws-status').textContent = 'Disconnected';
    document.getElementById('ws-status').className = '';
    state.wsReconnectTimer = setTimeout(connectWS, 3000);
  };

  ws.onerror = () => ws.close();

  ws.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }
    handleEvent(ev);
  };
}

function handleEvent(ev) {
  switch (ev.event) {
    case 'patch_start':
      setPatchActive(ev.patch);
      updatePhaseLabel(
        ev.mode === 'wb' ? 'White Balance' : ev.mode === 'verify' ? 'Verification' : 'CMS',
        ev.patch);
      break;

    case 'measurement':
      updatePatchDE(ev.patch, ev.delta_e);
      logLine(`${ev.patch}: ΔE=${ev.delta_e.toFixed(4)}`);
      break;

    case 'correction':
      logLine(`  correction ${ev.axis}: ${ev.before} → ${ev.after}`, 'log-info');
      break;

    case 'patch_done':
      setPatchDone(ev.patch, ev.delta_e, ev.converged);
      state.donePatches++;
      updateProgress();
      break;

    case 'phase_start':
      logLine(`▶ Phase: ${ev.phase.toUpperCase()}`, 'log-info');
      if (ev.phase === 'wb') state.totalPatches = 1;
      else if (ev.phase === 'cms') state.totalPatches += 6;
      else if (ev.phase === 'verify') state.totalPatches += 9;
      break;

    case 'phase_done':
      logLine(`✓ Phase ${ev.phase} complete`, 'log-info');
      break;

    case 'run_complete':
      state.runReport = ev.report;
      finishRun();
      renderResults(ev.report);
      if (!ev.report.aborted) {
        setupState.calibrated = true;
        updateSetupProgress();
      }
      break;

    case 'error':
      logLine(`ERROR: ${ev.message}`, 'log-error');
      finishRun();
      break;

    case 'probe_progress':
      appendProbeLog(ev.message);
      break;

    case 'probe_done':
      appendProbeLog('✓ Probe complete. Missing: ' + (ev.missing.length ? ev.missing.join(', ') : 'none'));
      setupState.probe = ev.missing.length === 0;
      updateSetupProgress();
      break;

    case 'discovery_start':
      document.getElementById('discovery-msg').textContent = `Scanning ${ev.subnet}…`;
      document.getElementById('discovery-status').classList.remove('hidden');
      break;

    case 'discovery_found':
      appendDeviceCard(ev.device);
      break;

    case 'discovery_done':
      document.getElementById('discovery-msg').textContent =
        `Found ${ev.total} device(s). Click [Select] to use one.`;
      break;

    case 'agent_result':
      if (ev.agent === 'results_analyst') renderAIAnalysis(ev.result);
      break;

    case 'setup_check':
      renderValidation(ev);
      break;

    case 'warmup_reading':
      addWarmupReading(ev.Y, ev.stable);
      break;

    case 'profile_applied':
      logLine(`Profile '${ev.profile}' applied to projector`, 'log-info');
      break;

    case 'ai_settings_changed':
      handleAISettingsChanged(ev);
      break;
  }
}

// ---------------------------------------------------------------------------
// Setup — Transport toggle
// ---------------------------------------------------------------------------

function setTransport(mode) {
  document.getElementById('tcp-fields').classList.toggle('hidden', mode !== 'tcp');
  document.getElementById('serial-fields').classList.toggle('hidden', mode !== 'serial');
  if (mode === 'serial') loadSerialPorts();
}

async function loadSerialPorts() {
  try {
    const res = await fetch('/api/serial-ports');
    const ports = await res.json();
    const sel = document.getElementById('serial-port-select');
    if (ports.length) {
      sel.innerHTML = ports.map(p =>
        `<option value="${p.port}">${p.port} — ${p.description}</option>`
      ).join('');
    }
  } catch (e) {
    // leave defaults
  }
}

function getTransport() {
  return document.querySelector('input[name="proj-transport"]:checked').value;
}

// ---------------------------------------------------------------------------
// Setup — Connections
// ---------------------------------------------------------------------------

async function testConnections() {
  // Persist the fields first — the calibration run reads the saved config,
  // not the input fields, so testing without saving would be misleading.
  const saved = await saveConfigFromFields();
  if (!saved) return;

  const transport = getTransport();
  const samsung = v('device-type') === 'samsung_ks8000_exlink';
  const checks = [
    // The ExLink serial link isn't testable over the network — the spike
    // script verifies it. Only test the projector for the Epson device.
    ...(samsung ? [] : [{
      label: transport === 'serial'
        ? `Projector (serial: ${v('serial-port-select')})`
        : `Projector (TCP: ${v('proj-ip')}:${v('proj-port')})`,
      type: 'projector',
      transport,
      ip: v('proj-ip'), port: v('proj-port'),
      serial_port: v('serial-port-select'), serial_baud: v('serial-baud'),
    }]),
    { label: 'PGenerator', type: 'pgen', transport: 'tcp', ip: v('pgen-ip'), port: v('pgen-port') },
  ];

  const ul = document.getElementById('conn-checklist');
  ul.innerHTML = '';
  document.getElementById('conn-results').classList.remove('hidden');

  let allOk = true;
  for (const c of checks) {
    const li = document.createElement('li');
    li.innerHTML = `<span class="check-icon"><span class="spinner"></span></span><div>${c.label} (${c.ip}:${c.port})</div>`;
    ul.appendChild(li);

    try {
      const res = await fetch('/api/test-connection', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          type: c.type,
          transport: c.transport || 'tcp',
          ip: c.ip,
          port: parseInt(c.port),
          serial_port: c.serial_port || '/dev/ttyUSB0',
          serial_baud: parseInt(c.serial_baud || 9600),
        }),
      });
      const data = await res.json();
      li.querySelector('.check-icon').textContent = data.connected ? '✅' : '❌';
      if (!data.connected) {
        allOk = false;
        li.querySelector('div').innerHTML += `<div class="check-note">${data.error || 'Connection refused'}</div>`;
      }
    } catch (e) {
      allOk = false;
      li.querySelector('.check-icon').textContent = '❌';
    }
  }
  if (samsung) {
    const li = document.createElement('li');
    li.innerHTML = `<span class="check-icon">ℹ️</span><div>Samsung ExLink (${v('exlink-port')})` +
      `<div class="check-note">Serial link — verify once with <code>scripts/exlink_spike.py</code>; not testable from here.</div></div>`;
    ul.appendChild(li);
  }
  setupState.connections = allOk;
  updateSetupProgress();
}

async function startDiscovery() {
  document.getElementById('discovery-status').classList.remove('hidden');
  document.getElementById('discovery-msg').textContent = 'Starting scan…';
  document.getElementById('device-list').innerHTML = '';
  await fetch('/api/discover', { method: 'POST' });
}

function appendDeviceCard(dev) {
  const list = document.getElementById('device-list');
  const card = document.createElement('div');
  card.className = 'device-card';
  card.innerHTML = `
    <div>
      <div class="device-type">${dev.device_type === 'projector' ? '📽 Projector' : '🖥 PGenerator'}</div>
      <div class="device-ip">${dev.ip}:${dev.port} ${dev.hostname ? '· ' + dev.hostname : ''}</div>
    </div>
    <span class="badge ${dev.confirmed ? 'badge-ok' : 'badge-warn'} device-method">${dev.method}${dev.confirmed ? ' ✓' : ''}</span>
    <button class="btn btn-sm" onclick="selectDevice('${dev.device_type}','${dev.ip}',${dev.port})">Select</button>
  `;
  list.appendChild(card);
}

function selectDevice(type, ip, port) {
  if (type === 'projector') {
    document.getElementById('proj-ip').value = ip;
    document.getElementById('proj-port').value = port;
  } else {
    document.getElementById('pgen-ip').value = ip;
    document.getElementById('pgen-port').value = port;
  }
}

// ---------------------------------------------------------------------------
// Setup — Probe
// ---------------------------------------------------------------------------

async function runProbe() {
  const out = document.getElementById('probe-output');
  out.innerHTML = '';
  out.classList.remove('hidden');
  appendProbeLog('Starting probe…');
  await fetch('/api/probe', { method: 'POST' });
}

function appendProbeLog(msg) {
  const out = document.getElementById('probe-output');
  const line = document.createElement('div');
  line.textContent = msg;
  out.appendChild(line);
  out.scrollTop = out.scrollHeight;
}

// ---------------------------------------------------------------------------
// Setup — Warm-up monitor
// ---------------------------------------------------------------------------

function toggleWarmup() {
  if (state.warmupMonitoring) {
    clearInterval(state.warmupTimer);
    state.warmupMonitoring = false;
    document.getElementById('warmup-btn').textContent = 'Start Monitoring';
  } else {
    state.warmupMonitoring = true;
    document.getElementById('warmup-btn').textContent = 'Stop Monitoring';
    takeWarmupReading();
    state.warmupTimer = setInterval(takeWarmupReading, 30000);
  }
}

async function takeWarmupReading() {
  try {
    const res = await fetch('/api/warmup');
    const data = await res.json();
    addWarmupReading(data.Y);
  } catch (e) {
    document.getElementById('warmup-info').textContent = 'Error: ' + e.message;
  }
}

function addWarmupReading(Y, stable) {
  state.warmupReadings.push(Y);
  if (state.warmupReadings.length > 20) state.warmupReadings.shift();
  renderWarmupChart();

  // Check variance over last 3 readings
  const recent = state.warmupReadings.slice(-3);
  if (recent.length >= 3) {
    const mean = recent.reduce((a,b) => a+b, 0) / recent.length;
    const variance = Math.sqrt(recent.map(y => (y-mean)**2).reduce((a,b)=>a+b,0)/recent.length) / mean;
    const isStable = stable !== undefined ? stable : (variance < 0.005);
    const badge = document.getElementById('warmup-badge');
    if (isStable) {
      badge.textContent = 'Stable ✓';
      badge.className = 'badge badge-ok';
    } else {
      badge.textContent = `Warming Up (${(variance*100).toFixed(2)}% var)`;
      badge.className = 'badge badge-warn';
    }
    document.getElementById('warmup-info').textContent =
      `Last: Y=${Y.toFixed(2)} cd/m²  ·  variance=${(variance*100).toFixed(2)}%  ·  ${recent.length} readings`;
  } else {
    document.getElementById('warmup-info').textContent = `Reading ${state.warmupReadings.length}: Y=${Y.toFixed(2)} cd/m²`;
  }
}

function renderWarmupChart() {
  const chart = document.getElementById('warmup-chart');
  chart.innerHTML = '';
  const readings = state.warmupReadings;
  const max = Math.max(...readings) * 1.05;
  readings.forEach(Y => {
    const bar = document.createElement('div');
    bar.className = 'warmup-bar';
    bar.style.height = Math.max(2, (Y / max) * 68) + 'px';
    bar.title = `Y=${Y.toFixed(2)}`;
    chart.appendChild(bar);
  });
}

// ---------------------------------------------------------------------------
// Setup — Validation
// ---------------------------------------------------------------------------

async function validateSetup() {
  const body = {
    projector_connected: false,
    pgen_connected: false,
    warm_up_stable: document.getElementById('warmup-badge').className.includes('badge-ok'),
    colorimeter_detected: false,
    screen_info: {
      diagonal_inches: v('screen-size'),
      material: v('screen-material'),
      throw_distance: v('throw-dist'),
    },
  };

  // Quick connection checks
  try {
    const transport = getTransport();
    const r1 = await fetch('/api/test-connection', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        type: 'projector', transport,
        ip: v('proj-ip'), port: parseInt(v('proj-port')),
        serial_port: v('serial-port-select'), serial_baud: parseInt(v('serial-baud')),
      }),
    });
    body.projector_connected = (await r1.json()).connected;
  } catch {}

  try {
    const r2 = await fetch('/api/test-connection', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ type: 'pgen', ip: v('pgen-ip'), port: parseInt(v('pgen-port')) }),
    });
    body.pgen_connected = (await r2.json()).connected;
  } catch {}

  try {
    const r3 = await fetch('/api/warmup');
    body.colorimeter_detected = r3.ok;
  } catch {}

  const res = await fetch('/api/setup/validate', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body),
  });
  const result = await res.json();
  renderValidation(result);
  setupState.validated = !!result.ready;
  updateSetupProgress();
}

function renderValidation(result) {
  const wrap = document.getElementById('validation-result');
  const ul   = document.getElementById('validation-checklist');
  const blk  = document.getElementById('validation-blocking');
  ul.innerHTML = '';
  wrap.classList.remove('hidden');

  (result.checklist || []).forEach(item => {
    const li = document.createElement('li');
    li.innerHTML = `
      <span class="check-icon">${item.pass ? '✅' : '❌'}</span>
      <div>
        <div>${item.item}</div>
        <div class="check-note">${item.note || ''}</div>
      </div>`;
    ul.appendChild(li);
  });

  if (result.blocking_issues && result.blocking_issues.length) {
    blk.innerHTML = result.blocking_issues.map(i => `<div class="badge badge-fail mb-8">${i}</div>`).join('');
  } else {
    blk.innerHTML = '<div class="badge badge-ok">Ready to calibrate</div>';
  }
}

// ---------------------------------------------------------------------------
// Run Dashboard
// ---------------------------------------------------------------------------

function initPatchGrid() {
  const grid = document.getElementById('patch-grid');
  grid.innerHTML = '';
  state.patchData = {};
  state.donePatches = 0;

  PATCH_ORDER.forEach(name => {
    state.patchData[name] = { deltaE: null, converged: false, active: false };
    const cell = document.createElement('div');
    cell.className = 'patch-cell';
    cell.id = 'patch-' + name;
    cell.innerHTML = `
      <div class="swatch" style="background:${PATCH_COLORS[name]}"></div>
      <div class="patch-name">${name}</div>
      <div class="patch-de text-dim">—</div>
    `;
    grid.appendChild(cell);
  });
}

function setPatchActive(name) {
  document.querySelectorAll('.patch-cell').forEach(c => c.classList.remove('active'));
  const cell = document.getElementById('patch-' + name);
  if (cell) cell.classList.add('active');
}

function updatePatchDE(name, de) {
  const cell = document.getElementById('patch-' + name);
  if (!cell) return;
  const deEl = cell.querySelector('.patch-de');
  // delta_e is null when a patch was never measured (aborted run)
  if (de === null || de === undefined || !isFinite(de)) {
    deEl.textContent = '—';
    deEl.className = 'patch-de text-dim';
    return;
  }
  deEl.textContent = de.toFixed(3);
  deEl.className = 'patch-de ' + deClass(de);
}

function setPatchDone(name, de, converged) {
  const cell = document.getElementById('patch-' + name);
  if (!cell) return;
  cell.classList.remove('active');
  if (converged) cell.classList.add('converged');
  updatePatchDE(name, de);
}

function deClass(de) {
  if (de < 0.5) return 'de-excellent';
  if (de < 1.0) return 'de-good';
  if (de < 2.0) return 'de-ok';
  return 'de-fail';
}

function updateProgress() {
  const fill  = document.getElementById('run-progress-fill');
  const label = document.getElementById('run-progress-label');
  const pct   = Math.min(100, (state.donePatches / state.totalPatches) * 100);
  fill.style.width = pct + '%';
  label.textContent = `${state.donePatches} / ${state.totalPatches} patches`;
}

function updatePhaseLabel(phase, patch) {
  document.getElementById('run-phase-label').textContent = `Phase: ${phase} — ${patch}`;
}

function logLine(msg, cls = '') {
  const log = document.getElementById('run-log');
  const line = document.createElement('div');
  if (cls) line.className = cls;
  line.textContent = msg;
  log.appendChild(line);
  if (log.children.length > 200) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

async function startRun() {
  // The run reads the saved server config — persist the Setup fields first so
  // Start always targets the addresses the user can see on screen.
  const saved = await saveConfigFromFields();
  if (!saved) {
    const el = document.getElementById('run-readiness');
    el.className = 'banner banner-warn';
    el.classList.remove('hidden');
    el.textContent = '⚠ Could not save the Setup values — fix the fields in the Setup tab, then Start again.';
    return;
  }

  initPatchGrid();
  document.getElementById('run-progress-wrap').classList.remove('hidden');
  document.getElementById('start-btn').classList.add('hidden');
  document.getElementById('stop-btn').classList.remove('hidden');

  state.totalPatches = 0;
  state.donePatches = 0;
  updateProgress();

  await fetch('/api/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode:    document.getElementById('run-mode').value,
      phase:   document.getElementById('run-phase').value,
      dry_run: document.getElementById('dry-run').checked,
    }),
  });
}

async function stopRun() {
  await fetch('/api/run/stop', { method: 'POST' });
}

function finishRun() {
  document.getElementById('start-btn').classList.remove('hidden');
  document.getElementById('stop-btn').classList.add('hidden');
  document.querySelectorAll('.patch-cell').forEach(c => c.classList.remove('active'));
}

// ---------------------------------------------------------------------------
// Before / After results
// ---------------------------------------------------------------------------

function renderResults(report) {
  const tbody = document.getElementById('results-table-body');
  tbody.innerHTML = '';

  const allPatches = [
    ...(report.wb_result ? [report.wb_result] : []),
    ...report.cms_results,
    ...report.verify_results,
  ];

  const byName = {};
  allPatches.forEach(p => {
    if (!byName[p.patch]) byName[p.patch] = {};
    // Initial de comes from first pass, final from verify
    if (p.initial_delta_e !== null && p.initial_delta_e !== undefined) {
      byName[p.patch].before = p.initial_delta_e;
    }
    byName[p.patch].after = p.delta_e;
    byName[p.patch].converged = p.converged;
  });

  Object.entries(byName).forEach(([patch, data]) => {
    const before = data.before;
    // delta_e is null for patches never measured (aborted run)
    const after  = (data.after === null || data.after === undefined) ? null : data.after;
    const improvement = (before !== undefined && after !== null) ? (before - after) : null;
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${patch}</td>
      <td class="${before !== undefined ? deClass(before) : 'text-dim'}">${before !== undefined ? before.toFixed(4) : '—'}</td>
      <td class="${after !== null ? deClass(after) : 'text-dim'}">${after !== null ? after.toFixed(4) : '—'}</td>
      <td class="${improvement !== null && improvement > 0 ? 'de-excellent' : 'de-fail'}">${improvement !== null ? (improvement > 0 ? '▼' : '▲') + ' ' + Math.abs(improvement).toFixed(4) : '—'}</td>
      <td><span class="badge ${data.converged ? 'badge-ok' : 'badge-fail'}">${data.converged ? 'Pass' : 'Fail'}</span></td>
    `;
    tbody.appendChild(tr);
  });

  // Switch to results tab
  document.querySelector('[data-view="results"]').click();

  // Trigger AI analysis
  document.getElementById('ai-analysis-content').innerHTML =
    '<div class="flex align-center gap-8"><span class="spinner"></span><span class="text-dim">Analyzing results…</span></div>';
}

function renderAIAnalysis(result) {
  if (result.error) {
    document.getElementById('ai-analysis-content').innerHTML =
      `<div class="text-dim">AI analysis unavailable: ${result.error}</div>`;
    return;
  }
  document.getElementById('ai-analysis-content').innerHTML = `
    <span class="ai-grade grade-${result.overall_grade}">${result.overall_grade}</span>
    <p style="font-size:13px; line-height:1.7">${result.summary}</p>
    ${result.issues.length ? `
      <div class="ai-section-title">Issues</div>
      <ul class="ai-list">${result.issues.map(i => `<li>${i}</li>`).join('')}</ul>
    ` : ''}
    ${result.recommendations.length ? `
      <div class="ai-section-title">Recommendations</div>
      <ul class="ai-list">${result.recommendations.map(r => `<li>${r}</li>`).join('')}</ul>
    ` : ''}
  `;
}

function downloadResults() {
  if (!state.runReport) return;
  const blob = new Blob([JSON.stringify(state.runReport, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `calibration_results_${state.runReport.mode}_${new Date().toISOString().slice(0,19).replace(/:/g,'-')}.json`;
  a.click();
}

// ---------------------------------------------------------------------------
// Profiles
// ---------------------------------------------------------------------------

async function loadProfiles() {
  const res  = await fetch('/api/profiles');
  const list = await res.json();
  const container = document.getElementById('profiles-list');

  if (!list.length) {
    container.innerHTML = '<div class="text-dim" style="font-size:13px; padding:16px 0">No profiles saved yet.</div>';
    return;
  }

  // Group by mode
  const sdr   = list.filter(p => p.mode === 'sdr');
  const hdr   = list.filter(p => p.mode === 'hdr10');

  container.innerHTML = '';
  if (sdr.length)  renderProfileGroup('SDR (Rec.709)', sdr, container);
  if (hdr.length)  renderProfileGroup('HDR10 (P3-D65)', hdr, container);
}

function renderProfileGroup(title, profiles, container) {
  const header = document.createElement('div');
  header.className = 'card-title mt-16';
  header.textContent = title;
  container.appendChild(header);

  profiles.forEach(p => {
    const avgDE = Object.values(p.final_delta_e);
    const meanDE = avgDE.length
      ? (avgDE.reduce((a,b)=>a+b,0)/avgDE.length).toFixed(3)
      : '—';

    const card = document.createElement('div');
    card.className = 'device-card mb-8';
    card.style.marginBottom = '8px';

    // Extract filename from name/mode/date to use as ID
    const created = new Date(p.created_at).toLocaleString();
    // We need the filename for API calls — derive it same way as Python
    const safeName = p.name.replace(/[^\w\-]/g, '_').replace(/_+/g,'_').replace(/^_|_$/g,'').slice(0,64) || 'profile';
    const ts = p.created_at.replace(/[-:]/g,'').replace('T','_').slice(0,15);
    const filename = `${safeName}_${p.mode}_${ts}.json`;

    card.innerHTML = `
      <div style="flex:1">
        <div class="device-type">${p.name}</div>
        <div class="device-ip">${created} · avg ΔE ${meanDE}</div>
      </div>
      <span class="badge ${meanDE !== '—' && parseFloat(meanDE) < 1.0 ? 'badge-ok' : 'badge-warn'}">ΔE ${meanDE}</span>
      <button class="btn btn-sm btn-success" onclick="applyProfile('${filename}')">Apply</button>
      <button class="btn btn-sm btn-danger" onclick="deleteProfile('${filename}', this)">Delete</button>
    `;
    container.appendChild(card);
  });
}

async function applyProfile(filename) {
  if (!confirm(`Apply profile '${filename}' to the projector now?`)) return;
  const res = await fetch(`/api/profiles/${encodeURIComponent(filename)}/apply`, { method: 'POST' });
  const data = await res.json();
  if (!data.ok) alert('Failed to apply profile: ' + (data.detail || 'unknown error'));
  else alert('Profile applied successfully.');
}

async function deleteProfile(filename, btn) {
  if (!confirm(`Delete profile '${filename}'?`)) return;
  await fetch(`/api/profiles/${encodeURIComponent(filename)}`, { method: 'DELETE' });
  loadProfiles();
}

async function saveCurrentProfile() {
  const name = prompt('Profile name:');
  if (!name) return;
  const mode = prompt('Mode (sdr or hdr10):', 'sdr');
  if (!mode) return;
  const res = await fetch('/api/profiles', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, mode, screen_info: {} }),
  });
  const data = await res.json();
  if (data.ok) { loadProfiles(); alert('Profile saved: ' + data.file); }
  else alert('Error: ' + (data.detail || 'unknown'));
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function v(id) {
  return document.getElementById(id).value;
}

// ---------------------------------------------------------------------------
// AI Settings
// ---------------------------------------------------------------------------

async function loadAISettings() {
  try {
    const res  = await fetch('/api/ai-settings');
    const data = await res.json();
    document.getElementById('ai-enabled-toggle').checked = data.enabled;
    document.getElementById('ai-key-preview').textContent =
      data.key_preview ? data.key_preview : '';
    updateAIBadge(data.enabled, data.key_set);
  } catch (e) {
    // server may not be ready yet — silently ignore
  }
}

function updateAIBadge(enabled, keySet) {
  const badge = document.getElementById('ai-status-badge');
  if (enabled && keySet) {
    badge.textContent = 'Active ✓';
    badge.className = 'badge badge-ok';
  } else if (keySet && !enabled) {
    badge.textContent = 'Key saved — disabled';
    badge.className = 'badge badge-warn';
  } else {
    badge.textContent = 'Not configured';
    badge.className = 'badge badge-warn';
  }
}

function onAIToggle(checked) {
  // Optimistic UI update — actual save happens on "Save" click
  const badge = document.getElementById('ai-status-badge');
  if (!checked) {
    badge.textContent = 'Disabled';
    badge.className = 'badge badge-warn';
  }
}

async function saveAISettings() {
  const enabled = document.getElementById('ai-enabled-toggle').checked;
  const apiKey  = document.getElementById('ai-api-key').value.trim();

  const res = await fetch('/api/ai-settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled, api_key: apiKey }),
  });
  const data = await res.json();

  // Clear the input field after saving (key is now in server memory)
  document.getElementById('ai-api-key').value = '';
  updateAIBadge(data.enabled, data.key_set);
  document.getElementById('ai-key-preview').textContent =
    data.key_set ? '(key saved)' : '';
}

function toggleKeyVisibility() {
  const inp = document.getElementById('ai-api-key');
  inp.type = inp.type === 'password' ? 'text' : 'password';
}

// Handle ai_settings_changed event from WebSocket
function handleAISettingsChanged(ev) {
  document.getElementById('ai-enabled-toggle').checked = ev.enabled;
  updateAIBadge(ev.enabled, ev.key_set);
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

initPatchGrid();
connectWS();
loadAISettings();
loadConfigIntoFields();
refreshSetupStatus();

// Load last discovery results on startup
fetch('/api/discover/last').then(r => r.json()).then(data => {
  if (data.devices && data.devices.length) {
    document.getElementById('discovery-status').classList.remove('hidden');
    document.getElementById('discovery-msg').textContent =
      `Last scan: ${data.devices.length} device(s) found (click Scan Network to refresh)`;
    data.devices.forEach(appendDeviceCard);
  }
}).catch(() => {});
