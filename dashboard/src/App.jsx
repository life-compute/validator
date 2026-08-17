import { useState, useEffect, useRef } from 'react'

// ── Theme ─────────────────────────────────────────────────────────────────────
const T = {
  bg:      '#020805',
  surface: '#050f08',
  green:   '#00ff41',
  pink:    '#ff69b4',
  cyan:    '#00ffff',
  amber:   '#ffb700',
  red:     '#ff3366',
  purple:  '#b060ff',
  text:    '#c8ffd4',
  textDim: '#3a5c44',
  border:  '#0a2a12',
}

const glow = (c, s = 3) => `0 0 ${s}px ${c}, 0 0 ${s * 3}px ${c}44`

// ── Scanline overlay ──────────────────────────────────────────────────────────
const SCANLINE_CSS = `
  @import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&display=swap');
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: ${T.bg}; overflow-x: hidden; }
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: ${T.bg}; }
  ::-webkit-scrollbar-thumb { background: ${T.green}44; border-radius: 3px; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }
  @keyframes pulse { 0%,100%{opacity:0.7} 50%{opacity:1} }
  @keyframes scanline {
    0%   { background-position: 0 0; }
    100% { background-position: 0 100vh; }
  }
`

// ── S (style helpers) ─────────────────────────────────────────────────────────
const S = {
  panel: (accent = T.green) => ({
    background:   T.surface,
    border:       `1px solid ${accent}44`,
    borderTop:    `2px solid ${accent}`,
    boxShadow:    `0 0 12px ${accent}18, inset 0 0 20px ${accent}08`,
    borderRadius: '2px',
    padding:      '14px 16px',
    position:     'relative',
    fontFamily:   "'Space Mono','Courier New',monospace",
  }),
  panelBar: (accent = T.green) => ({
    position:   'absolute',
    top: 0, left: 0, right: 0,
    height:     '2px',
    background: `linear-gradient(90deg, transparent, ${accent}, transparent)`,
    boxShadow:  glow(accent, 4),
  }),
  panelTitle: {
    display:        'flex',
    alignItems:     'center',
    gap:            '8px',
    marginBottom:   '12px',
    fontSize:       '11px',
    fontWeight:     700,
    letterSpacing:  '0.12em',
    color:          T.text,
    fontFamily:     "'Space Mono','Courier New',monospace",
  },
  titleAccent: (c) => ({
    display:    'inline-block',
    width:      '8px',
    height:     '8px',
    borderRadius: '50%',
    background: c,
    boxShadow:  glow(c, 4),
    flexShrink: 0,
  }),
  pill: (c) => ({
    display:       'inline-flex',
    alignItems:    'center',
    padding:       '1px 7px',
    border:        `1px solid ${c}88`,
    borderRadius:  '2px',
    fontSize:      '9px',
    fontWeight:    700,
    letterSpacing: '0.12em',
    color:         c,
    textShadow:    glow(c, 1),
    fontFamily:    "'Space Mono','Courier New',monospace",
  }),
  kv: {
    display:        'flex',
    justifyContent: 'space-between',
    alignItems:     'center',
    padding:        '3px 0',
    borderBottom:   `1px solid ${T.border}`,
    fontFamily:     "'Space Mono','Courier New',monospace",
  },
  mono: { fontFamily: "'Space Mono','Courier New',monospace" },
}

// ── Panel wrapper ─────────────────────────────────────────────────────────────
function Panel({ accent = T.green, style, children }) {
  return (
    <div style={{ ...S.panel(accent), ...style }}>
      <div style={S.panelBar(accent)} />
      {children}
    </div>
  )
}

// ── Blinking cursor ───────────────────────────────────────────────────────────
function Cursor() {
  return <span style={{ animation: 'blink 1s step-end infinite', color: T.green }}>█</span>
}

// ── Status panel ──────────────────────────────────────────────────────────────
function StatusPanel({ stats }) {
  const online    = stats?.status === 'ONLINE'
  const accent    = online ? T.green : T.red
  const startedAt = stats?.started_at ? new Date(stats.started_at).toLocaleString() : '—'
  const updatedAt = stats?.last_updated ? new Date(stats.last_updated).toLocaleTimeString() : '—'
  return (
    <Panel accent={accent}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(accent)} />
        <span>VALIDATOR STATUS</span>
        <span style={{ marginLeft: 'auto', ...S.pill(accent) }}>
          {online ? '● ONLINE' : '○ OFFLINE'}
        </span>
      </div>
      {[
        { k: 'NODE_STATUS',   v: online ? 'ACTIVE' : 'DOWN',  c: accent },
        { k: 'STARTED',       v: startedAt,                    c: T.textDim },
        { k: 'LAST_HEARTBEAT',v: updatedAt,                    c: T.cyan },
      ].map(({ k, v, c }) => (
        <div key={k} style={S.kv}>
          <span style={{ color: T.textDim, fontSize: '10px' }}>{k}</span>
          <span style={{ color: c, fontWeight: 700, fontSize: '11px', textShadow: glow(c, 1) }}>{v}</span>
        </div>
      ))}
    </Panel>
  )
}

// ── Stats panels ──────────────────────────────────────────────────────────────
function MetricPanel({ label, value, sub, accent = T.green }) {
  return (
    <Panel accent={accent}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(accent)} />
        <span>{label}</span>
      </div>
      <div style={{ fontSize: '32px', fontWeight: 700, color: accent,
                    textShadow: glow(accent, 4), lineHeight: 1, ...S.mono }}>
        {value}
      </div>
      {sub && (
        <div style={{ fontSize: '10px', color: T.textDim, marginTop: '6px', ...S.mono }}>
          {sub}
        </div>
      )}
    </Panel>
  )
}

// ── Current validation panel ──────────────────────────────────────────────────
function CurrentPanel({ stats, log }) {
  const current = log[log.length - 1] ?? null
  const idle    = !current
  return (
    <Panel accent={T.cyan} style={{ gridColumn: '1 / -1' }}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.cyan)} />
        <span>CURRENT VALIDATION</span>
        <span style={{ marginLeft: 'auto', ...S.pill(idle ? T.textDim : T.cyan) }}>
          {idle ? 'IDLE' : 'PROCESSING'}
        </span>
      </div>
      {idle ? (
        <div style={{ color: T.textDim, fontSize: '11px', ...S.mono }}>
          Polling for pending submissions… <Cursor />
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: '100px 1fr 80px 80px 60px', gap: '12px', fontSize: '11px' }}>
          {[
            { k: 'TARGET',   v: current.target_id ?? '—',                c: T.green },
            { k: 'SMILES',   v: (current.smiles ?? '—').slice(0, 60) + ((current.smiles?.length > 60) ? '…' : ''), c: T.cyan },
            { k: 'CLAIMED',  v: current.claimed?.toFixed(4) ?? '—',       c: T.amber },
            { k: 'RESCORED', v: current.rescored?.toFixed(4) ?? 'running…', c: T.amber },
            { k: 'TIME',     v: current.elapsed_s ? `${current.elapsed_s}s` : '—', c: T.textDim },
          ].map(({ k, v, c }) => (
            <div key={k}>
              <div style={{ color: T.textDim, fontSize: '9px', letterSpacing: '0.1em', marginBottom: '2px' }}>{k}</div>
              <div style={{ color: c, fontWeight: 700, textShadow: glow(c, 1), overflow: 'hidden',
                            textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{v}</div>
            </div>
          ))}
        </div>
      )}
    </Panel>
  )
}

// ── Live scoring feed ─────────────────────────────────────────────────────────
function ScoringFeedPanel({ log }) {
  const recent = [...log].reverse().slice(0, 15)
  return (
    <Panel accent={T.green} style={{ gridColumn: '1 / -1' }}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.green)} />
        <span>BOLTZ2 SCORING FEED</span>
        <span style={{ marginLeft: 'auto', color: T.textDim, fontSize: '10px' }}>
          LAST {Math.min(log.length, 15)} / {log.length} TOTAL
        </span>
      </div>

      {/* Header */}
      <div style={{ display: 'grid', gridTemplateColumns: '70px 70px 1fr 80px 80px 64px 64px',
                    gap: '8px', padding: '3px 0', borderBottom: `1px solid ${T.green}33`,
                    fontSize: '9px', color: T.textDim, letterSpacing: '0.1em', fontWeight: 700 }}>
        {['VERDICT','TARGET','SMILES','CLAIMED','RESCORED','DELTA','TIME'].map(h => (
          <span key={h}>{h}</span>
        ))}
      </div>

      {recent.length === 0 ? (
        <div style={{ color: T.textDim, fontSize: '11px', padding: '8px 0', ...S.mono }}>
          No validations yet — waiting for submissions… <Cursor />
        </div>
      ) : recent.map((r, i) => {
        const ok     = r.within_tolerance || r.verdict === 'CONFIRMED'
        const vc     = ok ? T.green : T.red
        const delta  = r.claimed != null && r.rescored != null
                       ? ((r.rescored - r.claimed) / Math.abs(r.claimed || 1) * 100).toFixed(1) + '%'
                       : '—'
        return (
          <div key={i} style={{ display: 'grid',
                                gridTemplateColumns: '70px 70px 1fr 80px 80px 64px 64px',
                                gap: '8px', padding: '3px 0',
                                borderBottom: `1px solid ${T.border}`,
                                fontSize: '10px', ...S.mono }}>
            <span style={{ color: vc, fontWeight: 700, textShadow: glow(vc, 1) }}>
              {r.verdict ?? (ok ? '✔ CONF' : '✘ REJ')}
            </span>
            <span style={{ color: T.green }}>{r.target_id ?? '—'}</span>
            <span style={{ color: T.textDim, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {(r.smiles ?? '').slice(0, 40)}
            </span>
            <span style={{ color: T.cyan }}>{r.claimed?.toFixed(4) ?? '—'}</span>
            <span style={{ color: T.cyan }}>{r.rescored?.toFixed(4) ?? '—'}</span>
            <span style={{ color: Math.abs(parseFloat(delta)) > 10 ? T.red : T.amber }}>{delta}</span>
            <span style={{ color: T.textDim }}>{r.elapsed_s ? `${r.elapsed_s}s` : '—'}</span>
          </div>
        )
      })}
    </Panel>
  )
}

// ── Audit log ─────────────────────────────────────────────────────────────────
function AuditPanel({ log }) {
  const last10 = [...log].reverse().slice(0, 10)
  return (
    <Panel accent={T.amber} style={{ gridColumn: '1 / -1' }}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.amber)} />
        <span>AUDIT LOG</span>
        <span style={{ marginLeft: 'auto', color: T.textDim, fontSize: '10px' }}>LAST 10 ENTRIES</span>
      </div>
      {last10.length === 0 ? (
        <div style={{ color: T.textDim, fontSize: '11px', ...S.mono }}>No audit entries yet.</div>
      ) : last10.map((r, i) => {
        const ok = r.within_tolerance || r.verdict === 'CONFIRMED'
        const vc = ok ? T.green : T.red
        const ts = r.ts ? new Date(r.ts * 1000).toLocaleTimeString() : '—'
        return (
          <div key={i} style={{ display: 'flex', gap: '12px', alignItems: 'center',
                                padding: '4px 0', borderBottom: `1px solid ${T.border}`,
                                fontSize: '10px', ...S.mono }}>
            <span style={{ color: T.textDim, flexShrink: 0 }}>{ts}</span>
            <span style={{ color: vc, fontWeight: 700, textShadow: glow(vc, 1), width: 80, flexShrink: 0 }}>
              {r.verdict ?? (ok ? 'CONFIRMED' : 'REJECTED')}
            </span>
            <span style={{ color: T.green, width: 60, flexShrink: 0 }}>{r.target_id ?? '—'}</span>
            <span style={{ color: T.textDim, flex: 1, overflow: 'hidden',
                           textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {r.smiles ?? '—'}
            </span>
            <span style={{ color: T.cyan, flexShrink: 0 }}>
              {r.claimed != null ? r.claimed.toFixed(4) : '—'} → {r.rescored != null ? r.rescored.toFixed(4) : '—'}
            </span>
          </div>
        )
      })}
    </Panel>
  )
}

// ── Main App ──────────────────────────────────────────────────────────────────
export default function App() {
  const [stats, setStats] = useState(null)
  const [log,   setLog]   = useState([])
  const [tick,  setTick]  = useState(null)

  useEffect(() => {
    const poll = () => {
      fetch('/stats.json?' + Date.now()).then(r => r.json()).then(d => { setStats(d); setTick(new Date()) }).catch(() => {})
      fetch('/log.json?' + Date.now()).then(r => r.json()).then(setLog).catch(() => {})
    }
    poll()
    const id = setInterval(poll, 5000)
    return () => clearInterval(id)
  }, [])

  const online     = stats?.status === 'ONLINE'
  const validated  = stats?.validated_today ?? 0
  const accepted   = stats?.accepted ?? 0
  const rejected   = stats?.rejected ?? 0
  const acceptRate = stats?.accept_rate ?? 0
  const life       = stats?.life_earned ?? 0
  const arColor    = acceptRate >= 90 ? T.green : acceptRate >= 70 ? T.amber : T.red

  return (
    <>
      <style>{SCANLINE_CSS}</style>
      <div style={{ minHeight: '100vh', background: T.bg, color: T.text,
                    fontFamily: "'Space Mono','Courier New',monospace",
                    position: 'relative', paddingBottom: '40px' }}>

        {/* Scanline overlay */}
        <div style={{ pointerEvents: 'none', position: 'fixed', inset: 0, zIndex: 999,
                      backgroundImage: 'repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,255,65,0.015) 2px,rgba(0,255,65,0.015) 4px)' }} />

        {/* Header */}
        <div style={{ borderBottom: `1px solid ${T.green}33`, padding: '20px 24px',
                      background: `linear-gradient(180deg,#040f07 0%,${T.bg} 100%)`,
                      display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div>
            <div style={{ fontSize: '22px', fontWeight: 700, letterSpacing: '0.1em',
                          textShadow: glow(T.green, 4) }}>
              <span style={{ color: T.green }}>LIFE COM</span>
              <span style={{ color: T.pink, textShadow: glow(T.pink, 4) }}>PUTE</span>
              <span style={{ color: T.green }}> — VALIDATOR NODE</span>
            </div>
            <div style={{ fontSize: '10px', color: T.textDim, letterSpacing: '0.18em',
                          textTransform: 'uppercase', marginTop: '4px' }}>
              SECURING CANCER DRUG DISCOVERY · SOLANA BLOCKCHAIN
            </div>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '4px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <div style={{ width: 8, height: 8, borderRadius: '50%',
                            background: online ? T.green : T.red,
                            boxShadow: online ? glow(T.green, 6) : 'none',
                            animation: online ? 'pulse 2s ease-in-out infinite' : 'none' }} />
              <span style={{ color: online ? T.green : T.red, fontSize: '12px', fontWeight: 700,
                             textShadow: glow(online ? T.green : T.red, 2) }}>
                {online ? 'ONLINE' : 'OFFLINE'}
              </span>
            </div>
            {tick && (
              <span style={{ color: T.textDim, fontSize: '9px' }}>
                SYNC: {tick.toLocaleTimeString()}
              </span>
            )}
          </div>
        </div>

        {/* Grid */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)',
                      gap: '12px', padding: '20px 24px' }}>

          {/* Row 1 — 4 stat panels */}
          <StatusPanel stats={stats} />
          <MetricPanel
            label="VALIDATED TODAY"
            value={validated.toLocaleString()}
            sub={`${accepted} confirmed · ${rejected} rejected`}
            accent={T.cyan}
          />
          <MetricPanel
            label="ACCEPT RATE"
            value={`${acceptRate.toFixed(1)}%`}
            sub="within tolerance window"
            accent={arColor}
          />
          <MetricPanel
            label="$LIFE COMMISSION"
            value={life.toFixed(2)}
            sub="validator rewards earned"
            accent={T.pink}
          />

          {/* Row 2 — current validation, full width */}
          <CurrentPanel stats={stats} log={log} />

          {/* Row 3 — scoring feed, full width */}
          <ScoringFeedPanel log={log} />

          {/* Row 4 — audit log, full width */}
          <AuditPanel log={log} />

        </div>

        {/* Footer */}
        <div style={{ borderTop: `1px solid ${T.green}22`, padding: '10px 24px',
                      display: 'flex', justifyContent: 'space-between', fontSize: '9px',
                      color: T.textDim, letterSpacing: '0.1em' }}>
          <span>LIFE-COMPUTE VALIDATOR v1.0.0 // BIOPUNK EDITION</span>
          <span style={{ color: T.green, textShadow: glow(T.green, 1) }}>
            {tick ? `LAST_SYNC: ${tick.toLocaleTimeString()}` : 'CONNECTING…'}
          </span>
        </div>
      </div>
    </>
  )
}
