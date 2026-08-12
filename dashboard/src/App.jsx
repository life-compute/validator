import { useState, useEffect } from 'react'

const T = {
  bg: '#0a0a0a', surface: '#111111', border: '#1a1a2a',
  accent: '#60a5fa', accentGlow: 'rgba(96,165,250,0.12)',
  green: '#00ff88', red: '#ff6b35', gold: '#ffe066',
  text: '#e2e8f0', textDim: '#718096', muted: '#4a5568',
}

const card = {
  background: T.surface, border: `1px solid ${T.border}`,
  borderRadius: '12px', padding: '28px 32px',
}

function Panel({ label, value, sub, color = T.accent }) {
  return (
    <div style={card}>
      <div style={{ fontSize: '11px', color: T.textDim, letterSpacing: '0.12em',
                    textTransform: 'uppercase', marginBottom: '12px' }}>{label}</div>
      <div style={{ fontSize: '36px', fontWeight: 700, color, lineHeight: 1 }}>{value}</div>
      {sub && <div style={{ fontSize: '12px', color: T.textDim, marginTop: '8px' }}>{sub}</div>}
    </div>
  )
}

export default function App() {
  const [stats, setStats] = useState(null)
  const [log,   setLog]   = useState([])

  useEffect(() => {
    const poll = () => {
      fetch('/stats.json').then(r => r.json()).then(setStats).catch(() => {})
      fetch('/log.json').then(r => r.json()).then(setLog).catch(() => {})
    }
    poll()
    const id = setInterval(poll, 5000)
    return () => clearInterval(id)
  }, [])

  const online     = stats?.status === 'ONLINE'
  const validated  = stats?.validated_today ?? 0
  const acceptRate = stats?.accept_rate ?? 0
  const life       = stats?.life_earned ?? 0

  return (
    <div style={{ minHeight: '100vh', background: T.bg, color: T.text,
                  fontFamily: "'JetBrains Mono','Fira Mono',monospace", padding: 0 }}>

      {/* Header */}
      <div style={{ borderBottom: `1px solid ${T.border}`, padding: '24px 32px',
                    background: `linear-gradient(180deg,#0a0f1a 0%,${T.bg} 100%)`,
                    display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <div style={{ fontSize: '20px', fontWeight: 700, color: T.accent,
                        letterSpacing: '0.06em', textShadow: `0 0 24px ${T.accent}` }}>
            L I F E  C O M P U T E
          </div>
          <div style={{ fontSize: '11px', color: T.textDim, letterSpacing: '0.14em',
                        textTransform: 'uppercase', marginTop: '4px' }}>
            Validator Node
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <div style={{ width: 8, height: 8, borderRadius: '50%',
                        background: online ? T.green : T.red,
                        boxShadow: online ? `0 0 8px ${T.green}` : 'none' }} />
          <span style={{ fontSize: '12px', color: online ? T.green : T.red }}>
            {online ? 'ONLINE' : 'OFFLINE'}
          </span>
        </div>
      </div>

      {/* 4-panel grid */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr',
                    gap: '16px', padding: '32px' }}>
        <Panel label="Status"
               value={online ? 'ONLINE' : 'OFFLINE'}
               color={online ? T.green : T.red}
               sub={stats?.last_updated ? `Updated ${new Date(stats.last_updated).toLocaleTimeString()}` : '—'} />
        <Panel label="Validated Today"
               value={validated.toLocaleString()}
               sub={`${stats?.accepted ?? 0} confirmed · ${stats?.rejected ?? 0} rejected`} />
        <Panel label="Accept Rate"
               value={`${acceptRate}%`}
               color={acceptRate >= 90 ? T.green : acceptRate >= 70 ? T.gold : T.red}
               sub="within 5% tolerance" />
        <Panel label="$LIFE Earned"
               value={life.toFixed(1)}
               color={T.gold}
               sub="validator rewards" />
      </div>

      {/* Recent log */}
      <div style={{ padding: '0 32px 32px' }}>
        <div style={{ ...card }}>
          <div style={{ fontSize: '11px', color: T.textDim, letterSpacing: '0.12em',
                        textTransform: 'uppercase', marginBottom: '16px' }}>
            Recent Validations
          </div>
          {log.length === 0
            ? <div style={{ color: T.muted, fontSize: '13px' }}>No validations yet.</div>
            : log.slice(-10).reverse().map((r, i) => (
              <div key={i} style={{ display: 'flex', gap: '12px', alignItems: 'center',
                                    padding: '8px 0', borderBottom: `1px solid ${T.border}`,
                                    fontSize: '12px' }}>
                <span style={{ color: r.within_tolerance ? T.green : T.red, width: 60 }}>
                  {r.verdict}
                </span>
                <span style={{ color: T.textDim, flex: 1, overflow: 'hidden',
                               textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {r.smiles}
                </span>
                <span style={{ color: T.textDim }}>claimed {r.claimed?.toFixed(2)}</span>
                <span style={{ color: T.textDim }}>rescored {r.rescored?.toFixed(2)}</span>
                <span style={{ color: T.muted }}>{r.elapsed_s}s</span>
              </div>
            ))
          }
        </div>
      </div>
    </div>
  )
}
