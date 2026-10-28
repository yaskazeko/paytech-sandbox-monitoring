// Runs the read-only validation collection with Newman on a schedule and serves the results
// as Prometheus metrics on :9106/metrics. The API key comes from $GW_KEY and stays in memory:
// it is handed to Newman as a variable, never put on a command line or into a file.
'use strict';

const http = require('http');
const path = require('path');
const newman = require('newman');

const KEY = process.env.GW_KEY;
const BASE = (process.env.GW_BASE_URL || '').replace(/\/$/, '');
const INTERVAL = Number(process.env.VALIDATION_INTERVAL_SECONDS || 300);
const PORT = Number(process.env.VALIDATION_PORT || 9106);
const COLLECTION = path.join(__dirname, 'gateway-readonly.postman_collection.json');

if (!KEY || !BASE) {
  console.error('[validator] GW_KEY and GW_BASE_URL must be set');
  process.exit(1);
}

let runs = 0;
let failedRuns = 0;
let body = '# no run yet\n';

const esc = (s) => String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, ' ');

function render(summary, err, seconds) {
  const lines = [];
  const add = (name, help, type, rows) => {
    lines.push(`# HELP ${name} ${help}`, `# TYPE ${name} ${type}`);
    rows.forEach(([labels, value]) => {
      const l = Object.entries(labels).map(([k, v]) => `${k}="${esc(v)}"`).join(',');
      lines.push(l ? `${name}{${l}} ${value}` : `${name} ${value}`);
    });
  };

  add('gateway_validation_up', '1 if the last validation run completed (Newman itself did not fail)', 'gauge',
    [[{}, err ? 0 : 1]]);
  add('gateway_validation_last_run_timestamp_seconds', 'Unix time the last validation run finished', 'gauge',
    [[{}, Math.floor(Date.now() / 1000)]]);
  add('gateway_validation_run_duration_seconds', 'Wall time of the last validation run', 'gauge',
    [[{}, seconds.toFixed(3)]]);
  add('gateway_validation_runs_total', 'Validation runs since the runner started', 'counter', [[{}, runs]]);
  add('gateway_validation_failed_runs_total', 'Runs with at least one failed check or a runner error', 'counter',
    [[{}, failedRuns]]);

  if (!summary) return lines.join('\n') + '\n';

  const stats = summary.run.stats.assertions;
  add('gateway_validation_assertions', 'Assertions in the last run by result', 'gauge',
    [[{ result: 'passed' }, stats.total - stats.failed], [{ result: 'failed' }, stats.failed]]);

  const ok = [];
  const took = [];
  summary.run.executions.forEach((ex) => {
    const check = ex.item.name;
    const failed = Boolean(ex.requestError) || (ex.assertions || []).some((a) => a.error);
    ok.push([{ check }, failed ? 0 : 1]);
    if (ex.response) took.push([{ check }, (ex.response.responseTime / 1000).toFixed(3)]);
  });
  add('gateway_validation_check_ok', '1 if every assertion of the check passed in the last run', 'gauge', ok);
  add('gateway_validation_check_response_seconds', 'Response time of the check request in the last run', 'gauge',
    took);
  return lines.join('\n') + '\n';
}

function runOnce() {
  const started = Date.now();
  newman.run({
    collection: require(COLLECTION),
    envVar: [{ key: 'base_url', value: BASE }, { key: 'api_key', value: KEY }],
    reporters: [],
    timeoutRequest: 20000,
  }, (err, summary) => {
    const seconds = (Date.now() - started) / 1000;
    runs += 1;
    const failures = summary ? summary.run.failures : [];
    if (err || failures.length) failedRuns += 1;
    body = render(err ? null : summary, err, seconds);
    if (err) {
      console.error(`[validator] run failed: ${err.message}`);
    } else {
      // assertion names only: responses can carry payment data, so they are not logged
      failures.forEach((f) => console.log(`[validator] FAIL ${f.source.name}: ${f.error.test || f.error.name}`));
      console.log(`[validator] run ${runs}: ${failures.length} failed of ${summary.run.stats.assertions.total}`);
    }
    setTimeout(runOnce, INTERVAL * 1000);
  });
}

http.createServer((req, res) => {
  if (req.url.split('?')[0] !== '/metrics') {
    res.writeHead(404);
    res.end();
    return;
  }
  res.writeHead(200, { 'Content-Type': 'text/plain; version=0.0.4' });
  res.end(body);
}).listen(PORT, () => console.log(`[validator] serving :${PORT}/metrics, every ${INTERVAL}s`));

runOnce();
