import { useState, useEffect, useRef } from 'react'

/* ─── Biopunk theme tokens ──────────────────────────────────── */
const T = {
  bg:           '#020805',
  surface:      '#050f08',
  surfaceAlt:   '#080f08',
  border:       '#00ff4133',
  borderBright: '#00ff4166',
  green:        '#00ff41',
  greenDim:     '#00cc33',
  greenGlow:    'rgba(0,255,65,0.15)',
  cyan:         '#00ffff',
  cyanDim:      '#00cccc',
  pink:         '#ff69b4',
  pinkGlow:     'rgba(255,105,180,0.15)',
  amber:        '#ffb700',
  amberDim:     '#cc8800',
  red:          '#ff3366',
  redDim:       '#cc0033',
  purple:       '#b060ff',
  text:         '#c8ffd4',
  textBright:   '#e8ffe8',
  textDim:      '#3a5c44',
  muted:        '#0a2a12',
  mono:         "'Space Mono','Courier New',monospace",
}

const glow        = (c, s = 3)  => `0 0 ${s}px ${c}, 0 0 ${s * 3}px ${c}44`
const panelShadow = (c = T.green) => `0 0 1px ${c}44, 0 0 20px ${c}18, inset 0 0 40px ${c}06`

/* ─── CSS keyframes ─────────────────────────────────────────── */
const CSS = `
  @import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&display=swap');
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: ${T.bg}; overflow-x: hidden; font-family: 'Space Mono','Courier New',monospace; }
  ::selection { background: #00ff4133; color: #00ff41; }
  ::-webkit-scrollbar { width: 5px; }
  ::-webkit-scrollbar-track { background: ${T.bg}; }
  ::-webkit-scrollbar-thumb { background: #00ff4133; border-radius: 0; }
  ::-webkit-scrollbar-thumb:hover { background: #00ff4166; }
  @keyframes blink      { 0%,100%{opacity:1} 50%{opacity:0} }
  @keyframes pulse      { 0%,100%{opacity:0.7} 50%{opacity:1} }
  @keyframes textPulse  { 0%,100%{opacity:1} 50%{opacity:0.82} }
  @keyframes helix1     { from{stroke-dashoffset:0} to{stroke-dashoffset:-100} }
  @keyframes spinDot    { from{transform:rotate(0deg)} to{transform:rotate(360deg)} }
`

/* ─── Style helpers ─────────────────────────────────────────── */
const S = {
  wrap: {
    minHeight:  '100vh',
    background: T.bg,
    color:      T.text,
    fontFamily: T.mono,
    position:   'relative',
    overflow:   'hidden',
  },
  scanlines: {
    position:   'fixed',
    top: 0, left: 0, right: 0, bottom: 0,
    pointerEvents: 'none',
    zIndex:     1000,
    background: 'repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,0.07) 2px,rgba(0,0,0,0.07) 4px)',
  },
  matrixCanvas: {
    position:   'fixed',
    top: 0, left: 0,
    width:      '100%',
    height:     '100%',
    opacity:    0.055,
    pointerEvents: 'none',
    zIndex:     0,
  },
  content: {
    position:   'relative',
    zIndex:     1,
  },
  header: {
    borderBottom: `1px solid ${T.border}`,
    background:   `linear-gradient(180deg,#010603 0%,${T.bg} 100%)`,
    position:     'relative',
    overflow:     'hidden',
  },
  headerInner: {
    padding:       '28px 32px 22px',
    display:       'flex',
    flexDirection: 'column',
    alignItems:    'center',
    gap:           '10px',
    position:      'relative',
    zIndex:        2,
  },
  tagline: {
    fontSize:      '26px',
    fontWeight:    700,
    letterSpacing: '0.06em',
    textTransform: 'uppercase',
    textAlign:     'center',
    animation:     'textPulse 3s ease-in-out infinite',
  },
  subtitle: {
    fontSize:      '11px',
    color:         T.cyan,
    letterSpacing: '0.2em',
    textTransform: 'uppercase',
    textShadow:    glow(T.cyan, 4),
  },
  statusBadge: (alive) => ({
    position:      'absolute',
    top:           '24px',
    right:         '32px',
    display:       'flex',
    alignItems:    'center',
    gap:           '8px',
    fontSize:      '11px',
    color:         alive ? T.green : T.red,
    letterSpacing: '0.1em',
    textShadow:    glow(alive ? T.green : T.red, 4),
    border:        `1px solid ${alive ? T.green : T.red}55`,
    padding:       '4px 12px',
    background:    alive ? '#00ff4108' : '#ff336608',
    fontFamily:    T.mono,
  }),
  statusDot: (alive) => ({
    width:         '6px',
    height:        '6px',
    borderRadius:  '50%',
    background:    alive ? T.green : T.red,
    boxShadow:     glow(alive ? T.green : T.red, 4),
    animation:     alive ? 'blink 1.4s step-end infinite' : 'none',
  }),
  grid: {
    display:             'grid',
    gridTemplateColumns: 'repeat(4, 1fr)',
    gap:                 '14px',
    padding:             '22px 28px',
    maxWidth:            '1440px',
    margin:              '0 auto',
  },
  sectionLabel: {
    gridColumn:    '1 / -1',
    fontSize:      '10px',
    letterSpacing: '0.22em',
    textTransform: 'uppercase',
    color:         T.textDim,
    paddingBottom: '6px',
    borderBottom:  `1px solid ${T.border}`,
    marginTop:     '8px',
    display:       'flex',
    alignItems:    'center',
    gap:           '10px',
    fontFamily:    T.mono,
  },
  sectionTick: {
    width:      '6px',
    height:     '6px',
    background: T.green,
    boxShadow:  glow(T.green, 3),
    flexShrink: 0,
  },
  panel: (accent = T.green) => ({
    background:   T.surface,
    border:       `1px solid ${accent}33`,
    borderRadius: '2px',
    padding:      '18px 20px',
    position:     'relative',
    overflow:     'hidden',
    boxShadow:    panelShadow(accent),
    fontFamily:   T.mono,
  }),
  panelBar: (accent = T.green) => ({
    position:   'absolute',
    top: 0, left: 0, right: 0,
    height:     '1px',
    background: `linear-gradient(90deg,transparent,${accent},transparent)`,
    boxShadow:  `0 0 8px ${accent}`,
  }),
  panelCorner: (pos, accent) => ({
    position:                            'absolute',
    [pos.includes('t') ? 'top' : 'bottom']: 0,
    [pos.includes('l') ? 'left' : 'right']: 0,
    width:                               '10px',
    height:                              '10px',
    borderTop:    pos.includes('t') ? `1px solid ${accent}` : 'none',
    borderBottom: pos.includes('b') ? `1px solid ${accent}` : 'none',
    borderLeft:   pos.includes('l') ? `1px solid ${accent}` : 'none',
    borderRight:  pos.includes('r') ? `1px solid ${accent}` : 'none',
  }),
  panelTitle: {
    fontSize:      '10px',
    color:         T.textDim,
    letterSpacing: '0.2em',
    textTransform: 'uppercase',
    marginBottom:  '16px',
    display:       'flex',
    alignItems:    'center',
    gap:           '8px',
    fontFamily:    T.mono,
  },
  titleAccent: (c) => ({
    color:      c,
    textShadow: glow(c, 3),
  }),
  bigNum: (c = T.green) => ({
    fontSize:           '52px',
    fontWeight:         700,
    color:              c,
    lineHeight:         1,
    textShadow:         `0 0 20px ${c}, 0 0 40px ${c}66`,
    marginBottom:       '6px',
    fontVariantNumeric: 'tabular-nums',
    letterSpacing:      '-0.02em',
    animation:          'textPulse 4s ease-in-out infinite',
    fontFamily:         T.mono,
  }),
  kv: {
    display:        'flex',
    justifyContent: 'space-between',
    alignItems:     'center',
    padding:        '7px 0',
    borderBottom:   `1px solid ${T.muted}`,
    fontSize:       '12px',
    fontFamily:     T.mono,
  },
  kvLast: {
    display:        'flex',
    justifyContent: 'space-between',
    alignItems:     'center',
    padding:        '7px 0',
    fontSize:       '12px',
    fontFamily:     T.mono,
  },
  label: {
    fontSize:      '10px',
    color:         T.textDim,
    letterSpacing: '0.06em',
    fontFamily:    T.mono,
  },
  pill: (c) => ({
    background:    c + '18',
    border:        `1px solid ${c}55`,
    color:         c,
    textShadow:    glow(c, 2),
    borderRadius:  '2px',
    padding:       '2px 7px',
    fontSize:      '9px',
    fontWeight:    700,
    letterSpacing: '0.12em',
    fontFamily:    T.mono,
  }),
  terminalBlock: {
    marginTop:    '14px',
    padding:      '10px 12px',
    background:   '#010603',
    border:       `1px solid ${T.border}`,
    fontSize:     '10px',
    color:        T.textDim,
    letterSpacing: '0.06em',
    lineHeight:   1.9,
    fontFamily:   T.mono,
  },
  footer: {
    borderTop:      `1px solid ${T.border}`,
    padding:        '10px 28px',
    display:        'flex',
    justifyContent: 'space-between',
    alignItems:     'center',
    fontSize:       '10px',
    color:          T.textDim,
    letterSpacing:  '0.1em',
    fontFamily:     T.mono,
  },
}

/* ─── Matrix rain canvas ────────────────────────────────────── */
function MatrixRain() {
  const ref = useRef()
  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    const chars = 'ATCGAUCGTAGCTAGCATCG01アイウエオカキクケコ'
    let w, h, cols, drops
    function resize() {
      w = canvas.width  = window.innerWidth
      h = canvas.height = window.innerHeight
      cols  = Math.floor(w / 16)
      drops = Array(cols).fill(0).map(() => Math.random() * -50)
    }
    resize()
    window.addEventListener('resize', resize)
    const tick = () => {
      ctx.fillStyle = 'rgba(2,8,5,0.13)'
      ctx.fillRect(0, 0, w, h)
      ctx.font = '13px "Courier New",monospace'
      drops.forEach((y, i) => {
        const ch = chars[Math.floor(Math.random() * chars.length)]
        ctx.fillStyle = y * 16 < 80 ? '#00ffff' : '#00ff41'
        ctx.fillText(ch, i * 16, y * 16)
        if (y * 16 > h && Math.random() > 0.975) drops[i] = 0
        else drops[i] += 0.4
      })
    }
    const id = setInterval(tick, 50)
    return () => { clearInterval(id); window.removeEventListener('resize', resize) }
  }, [])
  return <canvas ref={ref} style={S.matrixCanvas} />
}

/* ─── DNA Helix SVG ─────────────────────────────────────────── */
function DNAHelix() {
  const pts = 14
  return (
    <svg width="280" height="56" viewBox="0 0 280 56" style={{ opacity: 0.65 }}>
      <defs>
        <filter id="gf">
          <feGaussianBlur stdDeviation="1.2" result="blur"/>
          <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
      </defs>
      <path
        d={`M 0 28 ${Array.from({length:pts},(_,i)=>{
          const x=(i/pts)*280, y=28+Math.sin((i/pts)*Math.PI*2)*17
          return `${i===0?'':'L'} ${x} ${y}`
        }).join(' ')}`}
        fill="none" stroke={T.green} strokeWidth="1.5" filter="url(#gf)"
        style={{animation:'helix1 3s linear infinite'}}
      />
      <path
        d={`M 0 28 ${Array.from({length:pts},(_,i)=>{
          const x=(i/pts)*280, y=28-Math.sin((i/pts)*Math.PI*2)*17
          return `${i===0?'':'L'} ${x} ${y}`
        }).join(' ')}`}
        fill="none" stroke={T.cyan} strokeWidth="1.5" filter="url(#gf)"
        style={{animation:'helix1 3s linear infinite'}}
      />
      {Array.from({length:pts},(_,i)=>{
        const x=(i/pts)*280+10
        const y1=28+Math.sin((i/pts)*Math.PI*2)*17
        const y2=28-Math.sin((i/pts)*Math.PI*2)*17
        const t=Math.abs(Math.sin((i/pts)*Math.PI*2))
        return <line key={i} x1={x} y1={y1} x2={x} y2={y2}
          stroke={t>0.5?T.pink:T.green} strokeWidth="1" opacity={0.5+t*0.5} filter="url(#gf)"/>
      })}
    </svg>
  )
}

// Target ID → gene name (indices 0-29 match life-compute/targets targets.json)
const TARGET_NAMES = [
  'TP53','BRCA1','EGFR','HER2','KRAS','BCL2','CDK4','VEGFR2','PDL1','MDM2',
  'BRAF','PTEN','MYC','STAT3','PIK3CA','MTOR','FGFR1','RET','AR','NTRK1',
  'IDH1','FLT3','SMAD4','APC','PARP1','JAK2','ESR1','HDAC1','HDAC2','ABL1',
]
const targetName = (id) => TARGET_NAMES[id] ?? (id != null ? String(id) : '—')

// Parse ts field — daemon writes ISO strings, not unix epoch
const parseTs = (ts) => {
  if (!ts) return '—'
  const d = new Date(ts)
  return isNaN(d) ? String(ts) : d.toLocaleTimeString()
}

/* ─── Panel wrapper ─────────────────────────────────────────── */
function Panel({ accent = T.green, style, children }) {
  return (
    <div style={{ ...S.panel(accent), ...style }}>
      <div style={S.panelBar(accent)} />
      <div style={S.panelCorner('tl', accent)} />
      <div style={S.panelCorner('br', accent)} />
      {children}
    </div>
  )
}

/* ─── Blinking cursor ───────────────────────────────────────── */
function Cursor() {
  return <span style={{ animation:'blink 1s step-end infinite', color:T.green }}>█</span>
}

/* ─── VALIDATOR STATUS panel ────────────────────────────────── */
function StatusPanel({ stats }) {
  const online    = stats?.status === 'ONLINE'
  const accent    = online ? T.green : T.red
  const startedAt = stats?.started_at      ? new Date(stats.started_at).toLocaleString()      : '—'
  const updatedAt = stats?.last_heartbeat  ? new Date(stats.last_heartbeat).toLocaleTimeString() : '—'

  return (
    <Panel accent={accent}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(accent)}>◈</span>
        <span>VALIDATOR STATUS</span>
        <span style={{ marginLeft:'auto', ...S.pill(accent) }}>
          {online ? '● ONLINE' : '○ OFFLINE'}
        </span>
      </div>
      {[
        { k:'NODE_STATUS',    v: online ? 'ACTIVE' : 'DOWN', c: accent },
        { k:'STARTED',        v: startedAt,                  c: T.textDim },
        { k:'LAST_HEARTBEAT', v: updatedAt,                  c: T.cyan },
      ].map(({ k, v, c }) => (
        <div key={k} style={S.kv}>
          <span style={{ color:T.textDim, fontSize:'10px' }}>{k}</span>
          <span style={{ color:c, fontWeight:700, fontSize:'11px', textShadow:glow(c,1), fontFamily:T.mono }}>{v}</span>
        </div>
      ))}
      <div style={S.terminalBlock}>
        <span style={{ color:T.green }}>{'>'}</span> LIFE-COMPUTE VALIDATOR ONLINE<br/>
        <span style={{ color:T.green }}>{'>'}</span> BOLTZ2 RESCORING <span style={{ color:T.cyan }}>ACTIVE</span><br/>
        <span style={{ color:T.green }}>{'>'}</span> SOLANA DEVNET CONNECTED
      </div>
    </Panel>
  )
}

/* ─── Metric stat panel ─────────────────────────────────────── */
function MetricPanel({ label, value, sub, accent = T.green }) {
  return (
    <Panel accent={accent}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(accent)}>◈</span>
        <span>{label}</span>
      </div>
      <div style={S.bigNum(accent)}>{value}</div>
      {sub && <div style={{ ...S.label, marginTop:'6px' }}>{sub}</div>}
    </Panel>
  )
}

/* ─── CURRENT VALIDATION panel ──────────────────────────────── */
function CurrentPanel({ stats }) {
  // Also show in-progress work from stats (written before Boltz2 completes)
  const activeTarget = stats?.current_target
  const activeSmiles = stats?.current_smiles
  // idle = no active Boltz2 run in progress (current_target cleared after each validation)
  const idle = !activeTarget
  return (
    <Panel accent={T.cyan} style={{ gridColumn:'1 / -1' }}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.cyan)}>⬡</span>
        <span>CURRENT VALIDATION</span>
        <span style={{ marginLeft:'auto', ...S.pill(idle ? T.textDim : T.cyan) }}>
          {idle ? 'IDLE' : 'PROCESSING'}
        </span>
      </div>
      {idle ? (
        <div style={{ color:T.textDim, fontSize:'11px', fontFamily:T.mono }}>
          Polling for pending submissions… <Cursor />
        </div>
      ) : (
        // Boltz2 in progress
        <div style={{ display:'grid', gridTemplateColumns:'100px 1fr', gap:'14px', fontSize:'11px', fontFamily:T.mono }}>
          <div>
            <div style={{ color:T.textDim, fontSize:'9px', letterSpacing:'0.1em', marginBottom:'3px' }}>TARGET</div>
            <div style={{ color:T.green, fontWeight:700, textShadow:glow(T.green,1) }}>{activeTarget}</div>
          </div>
          <div>
            <div style={{ color:T.textDim, fontSize:'9px', letterSpacing:'0.1em', marginBottom:'3px' }}>SMILES</div>
            <div style={{ color:T.cyan, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
              {(activeSmiles ?? '').slice(0,80)}{(activeSmiles?.length ?? 0) > 80 ? '…' : ''}
            </div>
          </div>
        </div>
      )}
    </Panel>
  )
}

/* ─── BOLTZ2 SCORING FEED panel ─────────────────────────────── */
function ScoringFeedPanel({ log }) {
  const recent = [...log].reverse().slice(0, 15)
  const cols   = '70px 70px 1fr 80px 80px 64px 64px'
  return (
    <Panel accent={T.green} style={{ gridColumn:'1 / -1' }}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.green)}>⬡</span>
        <span>BOLTZ2 SCORING FEED</span>
        <span style={{ marginLeft:'auto', color:T.textDim, fontSize:'10px', letterSpacing:'0.12em' }}>
          LAST {Math.min(log.length,15)} / {log.length} TOTAL
        </span>
      </div>

      {/* header row */}
      <div style={{ display:'grid', gridTemplateColumns:cols, gap:'8px',
                    padding:'4px 0 6px', borderBottom:`1px solid ${T.green}33`,
                    fontSize:'9px', color:T.textDim, letterSpacing:'0.12em', fontWeight:700, fontFamily:T.mono }}>
        {['VERDICT','TARGET','SMILES','CLAIMED','RESCORED','DELTA','TIME'].map(h=>(
          <span key={h}>{h}</span>
        ))}
      </div>

      {recent.length === 0 ? (
        <div style={{ color:T.textDim, fontSize:'11px', padding:'12px 0', fontFamily:T.mono }}>
          No validations yet — waiting for submissions… <Cursor />
        </div>
      ) : recent.map((r, i) => {
        const ok    = r.within_tolerance || r.verdict === 'CONFIRMED'
        const vc    = ok ? T.green : T.red
        const delta = r.claimed != null && r.rescored != null
          ? ((r.rescored - r.claimed) / Math.abs(r.claimed || 1) * 100).toFixed(1) + '%'
          : '—'
        return (
          <div key={i} style={{ display:'grid', gridTemplateColumns:cols, gap:'8px',
                                padding:'5px 0', borderBottom:`1px solid ${T.muted}`,
                                fontSize:'10px', fontFamily:T.mono,
                                background: i === 0 ? '#00ff4106' : 'transparent' }}>
            <span style={{ color:vc, fontWeight:700, textShadow:glow(vc,1) }}>
              {r.verdict ?? (ok ? '✔ CONF' : '✘ REJ')}
            </span>
            <span style={{ color:T.cyan }}>{targetName(r.target_id)}</span>
            <span style={{ color:T.textDim, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
              {(r.smiles ?? '').slice(0,40)}
            </span>
            <span style={{ color:T.cyan }}>{r.claimed?.toFixed(4)  ?? '—'}</span>
            <span style={{ color:T.cyan }}>{r.rescored?.toFixed(4) ?? '—'}</span>
            <span style={{ color:Math.abs(parseFloat(delta))>10 ? T.red : T.amber }}>{delta}</span>
            <span style={{ color:T.textDim }}>{r.elapsed_s ? `${r.elapsed_s}s` : '—'}</span>
          </div>
        )
      })}
    </Panel>
  )
}

/* ─── AUDIT LOG panel ───────────────────────────────────────── */
function AuditPanel({ log }) {
  const last10 = [...log].reverse().slice(0, 10)
  return (
    <Panel accent={T.amber} style={{ gridColumn:'1 / -1' }}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.amber)}>◈</span>
        <span>AUDIT LOG</span>
        <span style={{ marginLeft:'auto', color:T.textDim, fontSize:'10px', letterSpacing:'0.12em' }}>
          LAST 10 ENTRIES
        </span>
      </div>
      {last10.length === 0 ? (
        <div style={{ color:T.textDim, fontSize:'11px', fontFamily:T.mono }}>
          No audit entries yet — awaiting first validation… <Cursor />
        </div>
      ) : last10.map((r, i) => {
        const ok = r.within_tolerance || r.verdict === 'CONFIRMED'
        const vc = ok ? T.green : T.red
        const ts = parseTs(r.ts)
        return (
          <div key={i} style={{ display:'flex', gap:'12px', alignItems:'center',
                                padding:'5px 0', borderBottom:`1px solid ${T.muted}`,
                                fontSize:'10px', fontFamily:T.mono }}>
            <span style={{ color:T.textDim, flexShrink:0, minWidth:'70px' }}>{ts}</span>
            <span style={{ color:vc, fontWeight:700, textShadow:glow(vc,1), width:80, flexShrink:0 }}>
              {r.verdict ?? (ok ? 'CONFIRMED' : 'REJECTED')}
            </span>
            <span style={{ color:T.green, width:60, flexShrink:0 }}>{targetName(r.target_id)}</span>
            <span style={{ color:T.textDim, flex:1, overflow:'hidden',
                           textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
              {r.smiles ?? '—'}
            </span>
            <span style={{ color:T.cyan, flexShrink:0, fontVariantNumeric:'tabular-nums' }}>
              {r.claimed != null ? r.claimed.toFixed(4) : '—'}
              {' → '}
              {r.rescored != null ? r.rescored.toFixed(4) : '—'}
            </span>
          </div>
        )
      })}
    </Panel>
  )
}

/* ─── App ───────────────────────────────────────────────────── */
export default function App() {
  const [stats, setStats] = useState(null)
  const [log,   setLog]   = useState([])
  const [tick,  setTick]  = useState(null)

  useEffect(() => {
    const poll = () => {
      fetch('/stats.json?' + Date.now()).then(r=>r.json()).then(d=>{setStats(d);setTick(new Date())}).catch(()=>{})
      fetch('/log.json?'   + Date.now()).then(r=>r.json()).then(setLog).catch(()=>{})
    }
    poll()
    const id = setInterval(poll, 5000)
    return () => clearInterval(id)
  }, [])

  const online     = stats?.status      === 'ONLINE'
  const validated  = stats?.validated_today ?? 0
  const accepted   = stats?.confirmed       ?? 0
  const rejected   = stats?.rejected        ?? 0
  const acceptRate = stats?.accept_rate     ?? 0
  const life       = stats?.life_commission ?? 0
  const arColor    = acceptRate >= 90 ? T.green : acceptRate >= 70 ? T.amber : T.red

  return (
    <>
      <style>{CSS}</style>
      <div style={S.wrap}>
        <MatrixRain />
        <div style={S.scanlines} />
        <div style={S.content}>

          {/* ── Header ── */}
          <header style={S.header}>
            <div style={S.headerInner}>
              {/* Logo */}
              <div style={{
                width:        '120px',
                height:       '120px',
                borderRadius: '4px',
                overflow:     'hidden',
                border:       `1px solid ${T.green}55`,
                boxShadow:    `0 0 18px ${T.green}44, 0 0 40px ${T.green}22, inset 0 0 20px rgba(0,0,0,0.6)`,
                flexShrink:   0,
                position:     'relative',
              }}>
                <img
                  src="/logo.jpg"
                  alt="LIFE Compute"
                  style={{
                    width:      '100%',
                    height:     '100%',
                    objectFit:  'cover',
                    objectPosition: 'center top',
                    filter:     'brightness(1.1) saturate(1.3) contrast(1.05)',
                    display:    'block',
                  }}
                />
                {/* green scan-line overlay */}
                <div style={{
                  position:   'absolute',
                  inset:      0,
                  background: `repeating-linear-gradient(0deg,transparent,transparent 3px,${T.green}08 3px,${T.green}08 4px)`,
                  pointerEvents: 'none',
                }} />
              </div>

              {/* Title */}
              <div style={S.tagline}>
                <span style={{ color:T.green, textShadow:`0 0 20px ${T.green},0 0 40px ${T.green}88` }}>
                  LIFE COM
                </span>
                <span style={{ color:T.pink, textShadow:`0 0 20px ${T.pink},0 0 40px ${T.pink}88` }}>
                  PUTE
                </span>
                <span style={{ color:T.green, textShadow:`0 0 20px ${T.green},0 0 40px ${T.green}88` }}>
                  {' — VALIDATOR NODE'}
                </span>
              </div>

              {/* DNA helix */}
              <DNAHelix />

              {/* Subtitle */}
              <div style={S.subtitle}>
                SECURING CANCER DRUG DISCOVERY · BOLTZ2 RESCORING · SOLANA BLOCKCHAIN
              </div>
            </div>

            {/* Status badge top-right */}
            <div style={S.statusBadge(online)}>
              <div style={S.statusDot(online)} />
              {online ? 'SYS:ONLINE' : 'SYS:OFFLINE'}
            </div>
          </header>

          {/* ── Grid ── */}
          <div style={S.grid}>

            {/* Section: validator telemetry */}
            <div style={S.sectionLabel}>
              <div style={S.sectionTick} />
              VALIDATOR // LIVE TELEMETRY
            </div>

            {/* Row 1 — 4 stat panels */}
            <StatusPanel stats={stats} />

            <MetricPanel
              label="VALIDATED TODAY"
              value={validated.toLocaleString()}
              sub={`${accepted} confirmed  ·  ${rejected} rejected`}
              accent={T.cyan}
            />

            <MetricPanel
              label="ACCEPT RATE"
              value={`${acceptRate.toFixed(1)}%`}
              sub="submissions within tolerance"
              accent={arColor}
            />

            <MetricPanel
              label="$LIFE COMMISSION"
              value={life.toFixed(2)}
              sub="validator rewards earned"
              accent={T.pink}
            />

            {/* Section: validation pipeline */}
            <div style={S.sectionLabel}>
              <div style={S.sectionTick} />
              PIPELINE // REAL-TIME VALIDATION STREAM
            </div>

            {/* Current validation — full width */}
            <CurrentPanel stats={stats} />

            {/* Scoring feed — full width */}
            <ScoringFeedPanel log={log} />

            {/* Section: audit */}
            <div style={S.sectionLabel}>
              <div style={S.sectionTick} />
              SECURITY // AUDIT LOG
            </div>

            {/* Audit log — full width */}
            <AuditPanel log={log} />

          </div>

          {/* ── Footer ── */}
          <footer style={S.footer}>
            <span>LIFE-COMPUTE VALIDATOR v1.0.0 // BIOPUNK EDITION</span>
            <span style={{ color:T.pink, textShadow:glow(T.pink,2) }}>
              SECURING CANCER DRUG DISCOVERY ON-CHAIN
            </span>
            <span style={{ color:tick ? T.green : T.textDim, textShadow: tick ? glow(T.green,2) : 'none' }}>
              {tick ? `LAST_SYNC: ${tick.toLocaleTimeString()}` : 'CONNECTING…'}
            </span>
          </footer>
        </div>
      </div>
    </>
  )
}
