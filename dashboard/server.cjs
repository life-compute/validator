/**
 * LIFE Compute Validator Dashboard — static server on :3002
 *
 * Routes:
 *   GET /stats.json  → reads ../stats.json (written by validator_daemon.py)
 *   GET /log.json    → last 50 entries from ../output/validator_log.jsonl
 *   GET /*           → serves dist/ (React build); SPA fallback to index.html
 *
 * No external deps — stdlib http only.
 * Port: DASHBOARD_PORT env var (default 3002).
 */
'use strict';
const http = require('http');
const fs   = require('fs');
const path = require('path');

const PORT  = parseInt(process.env.DASHBOARD_PORT || '3002', 10);
const DIST  = path.join(__dirname, 'dist');
const ROOT  = path.join(__dirname, '..');
const STATS = path.join(ROOT, 'stats.json');
const LOG   = path.join(ROOT, 'output', 'validator_log.jsonl');

const MIME = {
  '.html': 'text/html',
  '.js':   'application/javascript',
  '.css':  'text/css',
  '.json': 'application/json',
  '.ico':  'image/x-icon',
  '.svg':  'image/svg+xml',
  '.png':  'image/png',
  '.woff2':'font/woff2',
};

const JSON_HEADERS = {
  'Content-Type': 'application/json',
  'Access-Control-Allow-Origin': '*',
  'Cache-Control': 'no-store',
};

function readJson(p) {
  try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return null; }
}

function tailJsonl(p, n = 50) {
  try {
    return fs.readFileSync(p, 'utf8')
      .split('\n').filter(Boolean).slice(-n)
      .map(l => { try { return JSON.parse(l); } catch { return null; } })
      .filter(Boolean);
  } catch { return []; }
}

http.createServer((req, res) => {
  const url = req.url.split('?')[0];

  // ── API routes ───────────────────────────────────────────────
  if (url === '/stats.json') {
    const d = readJson(STATS) || { status: 'OFFLINE' };
    res.writeHead(200, JSON_HEADERS);
    return res.end(JSON.stringify(d));
  }
  if (url === '/log.json') {
    res.writeHead(200, JSON_HEADERS);
    return res.end(JSON.stringify(tailJsonl(LOG)));
  }

  // ── Static files ─────────────────────────────────────────────
  const file = path.join(DIST, url === '/' ? '/index.html' : url);
  const ext  = path.extname(file);

  fs.readFile(file, (err, data) => {
    if (err) {
      // SPA fallback
      fs.readFile(path.join(DIST, 'index.html'), (e2, d2) => {
        if (e2) { res.writeHead(404); return res.end('Not found'); }
        res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end(d2);
      });
      return;
    }
    res.writeHead(200, { 'Content-Type': MIME[ext] || 'application/octet-stream' });
    res.end(data);
  });

}).listen(PORT, () => console.log(`LIFE Validator Dashboard → http://localhost:${PORT}`));
