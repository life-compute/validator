/**
 * LIFE Compute Validator Dashboard — static server on :3002
 * Serves pre-built dist/ and proxies /stats.json + /log.json
 */
const http = require('http');
const fs   = require('fs');
const path = require('path');

const PORT   = parseInt(process.env.DASHBOARD_PORT || '3002');
const DIST   = path.join(__dirname, 'dist');
const ROOT   = path.join(__dirname, '..');
const STATS  = path.join(ROOT, 'stats.json');
const LOG    = path.join(ROOT, 'output', 'validator_log.jsonl');

const MIME = {
  '.html': 'text/html', '.js': 'application/javascript',
  '.css':  'text/css',  '.json': 'application/json',
  '.ico':  'image/x-icon', '.svg': 'image/svg+xml',
};

function readJson(p)  { try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return null; } }
function tailJsonl(p, n = 50) {
  try {
    return fs.readFileSync(p, 'utf8').split('\n').filter(Boolean)
      .slice(-n).map(l => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
  } catch { return []; }
}

http.createServer((req, res) => {
  if (req.url === '/stats.json') {
    const d = readJson(STATS) || {};
    res.writeHead(200, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
    return res.end(JSON.stringify(d));
  }
  if (req.url === '/log.json') {
    res.writeHead(200, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
    return res.end(JSON.stringify(tailJsonl(LOG)));
  }

  let urlPath = req.url.split('?')[0];
  if (urlPath === '/') urlPath = '/index.html';
  const file = path.join(DIST, urlPath);
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
