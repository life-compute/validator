/**
 * LIFE Compute Validator Dashboard — static server on :3002
 *
 * Routes:
 *   GET  /stats.json        → reads ../stats.json (written by validator_daemon.py)
 *   GET  /log.json          → last 50 entries from ../output/validator_log.jsonl
 *   GET  /api/agent-context → context bundle for LIFE AGENT (logs + daemon errors, read-only)
 *   POST /api/agent         → proxy to Anthropic API using operator's BYOK key
 *   GET  /*                 → serves dist/ (React build); SPA fallback to index.html
 *
 * No external deps — stdlib http + https only.
 * Port: DASHBOARD_PORT env var (default 3002).
 */
'use strict';
const http  = require('http');
const https = require('https');
const fs    = require('fs');
const path  = require('path');

const PORT   = parseInt(process.env.DASHBOARD_PORT || '3002', 10);
const DIST   = path.join(__dirname, 'dist');
const ROOT   = path.join(__dirname, '..');
const STATS  = path.join(ROOT, 'stats.json');
const LOG    = path.join(ROOT, 'output', 'validator_log.jsonl');
const AUDIT  = path.join(ROOT, 'output', 'validator_audit.jsonl');
const DAEMON = path.join(ROOT, 'validator_daemon.py');

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
  if (url === '/audit.json') {
    res.writeHead(200, JSON_HEADERS);
    return res.end(JSON.stringify(tailJsonl(AUDIT, 200)));
  }
  if (url === '/crispr-log.json') {
    // All CRISPR entries from the full log file (not capped to last 50)
    const all = (() => {
      try {
        return fs.readFileSync(LOG, 'utf8')
          .split('\n').filter(Boolean)
          .map(l => { try { return JSON.parse(l); } catch { return null; } })
          .filter(l => l && l.target_type === 'CRISPR');
      } catch { return []; }
    })();
    res.writeHead(200, JSON_HEADERS);
    return res.end(JSON.stringify(all));
  }

  // ── LIFE AGENT context bundle (read-only) ────────────────────
  if (url === '/api/agent-context') {
    const stats  = readJson(STATS) || {};
    const logs   = tailJsonl(LOG, 80);
    // Extract recent error/warning lines for quick diagnosis
    const errors = logs.filter(l => l && (l.level === 'ERROR' || l.level === 'WARNING' ||
                                          (l.msg && /error|fail|exception|traceback/i.test(l.msg))));
    // Read first 200 lines of daemon for code context (no write capability exposed)
    let daemonHead = '';
    try {
      const lines = fs.readFileSync(DAEMON, 'utf8').split('\n').slice(0, 200);
      daemonHead = lines.join('\n');
    } catch { daemonHead = '(daemon file not readable)'; }

    res.writeHead(200, JSON_HEADERS);
    return res.end(JSON.stringify({
      stats,
      recent_errors: errors.slice(-20),
      recent_log:    logs.slice(-30),
      daemon_head:   daemonHead,
    }));
  }

  // ── LIFE AGENT proxy (BYOK — operator supplies their own Anthropic key) ──
  if (url === '/api/agent' && req.method === 'POST') {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      let payload;
      try { payload = JSON.parse(body); } catch {
        res.writeHead(400, JSON_HEADERS);
        return res.end(JSON.stringify({ error: 'Invalid JSON body' }));
      }

      const { apiKey, message, history = [], context = {} } = payload;
      if (!apiKey || typeof apiKey !== 'string' || !apiKey.startsWith('sk-')) {
        res.writeHead(401, JSON_HEADERS);
        return res.end(JSON.stringify({ error: 'Invalid or missing Anthropic API key. Get yours at console.anthropic.com' }));
      }
      if (!message || typeof message !== 'string') {
        res.writeHead(400, JSON_HEADERS);
        return res.end(JSON.stringify({ error: 'Missing message' }));
      }

      // Build system prompt with read-only context
      const ctxStats  = context.stats  ? JSON.stringify(context.stats, null, 2)  : '(not provided)';
      const ctxErrors = context.recent_errors ? JSON.stringify(context.recent_errors, null, 2) : '(none)';
      const ctxLog    = context.recent_log    ? JSON.stringify(context.recent_log,    null, 2) : '(none)';
      const ctxDaemon = context.daemon_head   || '(not provided)';

      const systemPrompt = `You are LIFE AGENT, a read-only diagnostic assistant for the LIFE Compute validator node.
You help validator operators diagnose issues, understand log output, and craft fixes.

STRICT SAFETY RULES (never violate):
- You CANNOT write files, restart processes, or execute commands — you are display-only.
- When suggesting code changes, ALWAYS output them as a unified diff (--- a/file / +++ b/file format).
- The operator applies diffs manually via terminal. Never suggest automated application.
- Never suggest changes that would cause the validator to skip validation logic or bypass safety checks.

VALIDATOR CONTEXT (live snapshot):
--- stats.json ---
${ctxStats}

--- recent errors (last 20) ---
${ctxErrors}

--- recent log (last 30 entries) ---
${ctxLog}

--- validator_daemon.py (first 200 lines) ---
${ctxDaemon}

Respond concisely in the biopunk terminal style used by the dashboard. Use code blocks for diffs and code. Keep diagnostic explanations tight — operators are technical.`;

      // Build messages array (last 10 history turns to keep context window sane)
      const safeHistory = (Array.isArray(history) ? history : []).slice(-10);
      const messages = [
        ...safeHistory.map(m => ({ role: m.role, content: m.content })),
        { role: 'user', content: message },
      ];

      const requestBody = JSON.stringify({
        model:      'claude-opus-4-5',
        max_tokens: 2048,
        system:     systemPrompt,
        messages,
      });

      const options = {
        hostname: 'api.anthropic.com',
        path:     '/v1/messages',
        method:   'POST',
        headers: {
          'Content-Type':      'application/json',
          'Content-Length':    Buffer.byteLength(requestBody),
          'x-api-key':         apiKey,
          'anthropic-version': '2023-06-01',
        },
      };

      const proxyReq = https.request(options, (proxyRes) => {
        let data = '';
        proxyRes.on('data', chunk => { data += chunk; });
        proxyRes.on('end', () => {
          res.writeHead(proxyRes.statusCode, JSON_HEADERS);
          res.end(data);
        });
      });
      proxyReq.on('error', (e) => {
        res.writeHead(502, JSON_HEADERS);
        res.end(JSON.stringify({ error: `Upstream error: ${e.message}` }));
      });
      proxyReq.write(requestBody);
      proxyReq.end();
    });
    return;
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
