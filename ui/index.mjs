// SI Local Inference Switch - dashboard page bundle.
//
// Hand-written ESM (no build step): the Kiro Crew host loads this module and
// renders the default-exported component. React and lucide-react are injected
// by the host on window.__kirocrew_modules before the import.
//
// Visuals: the SI AIDLC "neon" theme (deep-space grid background, glass
// panels with neon borders, glow-on-hover, and a few orchestrated motions -
// fade-up entrance, live-dot glow, title flicker, heartbeat on the running
// profile, scanline). Scoped with a `zli-` prefix so it never collides with
// the host's own CSS or AIDLC's `aidlc-` classes.

import {
  getState,
  createProfile,
  updateProfile,
  deleteProfile,
  startProcess,
  stopProcess,
  subscribeLogs,
  startMetricsPoll,
  clearStats,
  fmtDur,
  fmtTime,
  fmtBytes,
  fmtNum,
  fmtPct,
  fmtTps,
} from "./lib.mjs"

// Side-effect import: the persistent "everywhere" bar. bar.mjs mounts itself
// on document.body (outside the React tree) when its localStorage flag is on,
// which is what the "everywhere" toggle in the header below controls.
import "./bar.mjs"

const React = window.__kirocrew_modules.react
const { useState, useEffect, useRef, useCallback } = React
const h = React.createElement
const {
  Cpu,
  Play,
  Square,
  Plus,
  Pencil,
  Trash2,
  Terminal,
  RefreshCw,
  Activity,
  CircleCheck,
  LoaderCircle,
} = window.__kirocrew_modules["lucide-react"]

const MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

const EXAMPLE_COMMAND =
  "./ninfer/build/apps/ninfer-serve models/qwen3_8_27b_nvfp4_v3.ninfer " +
  "--host 127.0.0.1 --port 1234 --max-context 131072 --kv-capacity 212992 " +
  "--prefill-chunk 16384 --max-concurrency 2 --spec mtp --draft-tokens 3 " +
  "--lm-head-draft --kv-dtype nvfp4 --preserve-thinking --device-state-slots 3 " +
  "--host-kv-mib 4096 --temperature 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 " +
  "--pending-timeout-ms 180000 --vision"

const SHELLS = [
  ["login", "login — bash -lc (login shell env)"],
  ["interactive", "interactive — bash -ilc (full terminal env, reads .bashrc)"],
  ["plain", "plain — sh -c"],
  ["exec", "exec — run directly, no shell (command is shlex-split)"],
]

const BAR_STORAGE_KEY = "zli.bar"
const PERSIST_FLAG_KEY = "zli.persistBar"
const PERSIST_FLAG_EVENT = "zli:persist-flag"

function readPersistFlag() {
  try {
    return window.localStorage.getItem(PERSIST_FLAG_KEY) === "1"
  } catch {
    return false
  }
}

// ---------------------------------------------------------------------------
// NEON theme (ported from si-crewapp-aidlc) - a <style> block with keyframes +
// scoped zli-* classes that give the app its "futuristic / fluorescent" look:
// a deep-space grid background, glass panels with neon borders, glow-on-hover,
// and a few orchestrated motions (fade-up entrance, live-dot glow, title
// flicker, heartbeat on the live card, scanline). All motion respects the
// user's prefers-reduced-motion.

const NEON_CSS = `
@keyframes zli-spin { to { transform: rotate(360deg); } }
@keyframes zli-fade-up {
  from { opacity: 0; transform: translateY(12px); }
  to   { opacity: 1; transform: translateY(0); }
}
@keyframes zli-glow {
  0%, 100% { box-shadow: 0 0 10px rgba(34,211,238,.20); }
  50%      { box-shadow: 0 0 28px rgba(34,211,238,.50); }
}
@keyframes zli-dot-pulse {
  0%, 100% { transform: scale(1); }
  50%      { transform: scale(1.45); }
}
@keyframes zli-flicker {
  0%, 91%, 100% { opacity: 1; }
  92% { opacity: .55; }
  93% { opacity: 1; }
  95% { opacity: .8; }
  96% { opacity: 1; }
}
.zli-root {
  position: relative;
  min-height: 100vh;
  background-color: #05060f;
  background-image:
    radial-gradient(1200px 620px at 18% -8%, rgba(34,211,238,.17), transparent 62%),
    radial-gradient(1000px 520px at 92% 2%, rgba(168,85,247,.17), transparent 62%),
    radial-gradient(900px 460px at 50% 118%, rgba(52,211,153,.12), transparent 60%),
    linear-gradient(rgba(34,211,238,.05) 1px, transparent 1px),
    linear-gradient(90deg, rgba(34,211,238,.05) 1px, transparent 1px);
  background-size: auto, auto, auto, 44px 44px, 44px 44px;
}
.zli-neon-title { text-shadow: 0 0 10px rgba(34,211,238,.7), 0 0 22px rgba(34,211,238,.35); }
.zli-card { transition: border-color .25s ease, box-shadow .25s ease, transform .25s ease; }
.zli-card:hover {
  border-color: rgba(34,211,238,.55);
  box-shadow: 0 0 26px rgba(34,211,238,.20), inset 0 0 18px rgba(34,211,238,.06);
  transform: translateY(-2px);
}
.zli-btn { transition: box-shadow .2s ease, transform .15s ease, filter .2s ease; }
.zli-btn:hover:not(:disabled) {
  box-shadow: 0 0 18px rgba(52,211,153,.55);
  transform: translateY(-1px);
  filter: brightness(1.1);
}
.zli-btn:active:not(:disabled) { transform: translateY(0); }
.zli-input { transition: border-color .2s ease, box-shadow .2s ease; }
.zli-input:focus {
  border-color: rgba(34,211,238,.75);
  box-shadow: 0 0 0 3px rgba(34,211,238,.16), 0 0 20px rgba(34,211,238,.28);
}
.zli-fade-up { animation: zli-fade-up .55s cubic-bezier(.22,1,.36,1) both; }
.zli-glow-pulse { animation: zli-glow 3.2s ease-in-out infinite; }
.zli-dot-pulse { animation: zli-dot-pulse 1.6s ease-in-out infinite; }
.zli-flicker { animation: zli-flicker 7s linear infinite; }
@keyframes zli-heartbeat {
  0%        { box-shadow: 0 0 0 0 rgba(52,211,153,.50);  opacity: .9; }
  14%       { box-shadow: 0 0 26px 4px rgba(52,211,153,.80); opacity: 1; }
  30%       { box-shadow: 0 0 5px 1px rgba(52,211,153,.25); opacity: .5; }
  44%       { box-shadow: 0 0 18px 3px rgba(52,211,153,.65); opacity: .95; }
  75%, 100% { box-shadow: 0 0 0 0 rgba(52,211,153,0);    opacity: .3; }
}
.zli-heartbeat { position: relative; }
.zli-heartbeat::after {
  content: "";
  position: absolute;
  inset: -2px;
  border-radius: 16px;
  border: 1px solid rgba(52,211,153,.35);
  pointer-events: none;
  animation: zli-heartbeat 2.2s ease-in-out infinite;
}
@keyframes zli-scan {
  0%   { top: -15%; }
  100% { top: 115%; }
}
.zli-scan-clip {
  position: absolute;
  inset: 0;
  overflow: hidden;
  pointer-events: none;
  z-index: 40;
}
.zli-scanline {
  position: absolute;
  left: 0;
  right: 0;
  top: -15%;
  height: 140px;
  background: linear-gradient(180deg, transparent, rgba(34,211,238,.06) 45%, rgba(168,85,247,.05) 55%, transparent);
  pointer-events: none;
  animation: zli-scan 10s linear infinite;
}
@media (prefers-reduced-motion: reduce) {
  .zli-fade-up, .zli-glow-pulse, .zli-dot-pulse, .zli-flicker { animation: none !important; }
  .zli-heartbeat::after, .zli-scanline { animation: none !important; }
  .zli-scanline { display: none; }
  .zli-card, .zli-btn, .zli-input { transition: none !important; }
}
`

function NeonStyles() {
  return h("style", null, NEON_CSS)
}

// ---------------------------------------------------------------------------
// shared bits (AIDLC look-and-feel)

function Dot({ tone = "#6b7280", pulse = false, size = 8 }) {
  return h("span", {
    className: pulse ? "zli-dot-pulse" : undefined,
    style: {
      width: size,
      height: size,
      borderRadius: "50%",
      background: tone,
      display: "inline-block",
      flexShrink: 0,
      boxShadow: `0 0 ${size * 0.8}px ${tone}cc, 0 0 ${size * 1.8}px ${tone}55`,
    },
  })
}

function Chip({ children, tone = "#94a3b8", bg, title }) {
  return h(
    "span",
    {
      title,
      style: {
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        fontSize: 11,
        fontWeight: 600,
        color: tone,
        background: bg || "rgba(148,163,184,.08)",
        border: `1px solid ${tone}33`,
        borderRadius: 999,
        padding: "2px 9px",
        whiteSpace: "nowrap",
        textShadow: `0 0 8px ${tone}66`,
      },
    },
    children
  )
}

function btnStyle(kind = "ok") {
  if (kind === "ghost")
    return {
      background: "rgba(34,211,238,.04)",
      border: "1px solid rgba(34,211,238,.35)",
      color: "#a5f3fc",
    }
  if (kind === "danger")
    return {
      background: "rgba(248,113,113,.1)",
      border: "1px solid rgba(248,113,113,.45)",
      color: "#fca5a5",
      boxShadow: "0 0 14px rgba(248,113,113,.18)",
    }
  return {
    background: "linear-gradient(135deg, #4ade80, #22d3ee 70%, #818cf8)",
    border: "1px solid rgba(34,211,238,.6)",
    color: "#04120f",
    fontWeight: 700,
    boxShadow: "0 0 16px rgba(34,211,238,.35)",
  }
}

function Button({ kind, onClick, disabled, children, title, small }) {
  return h(
    "button",
    {
      onClick,
      disabled,
      title,
      className: "zli-btn",
      style: {
        ...btnStyle(kind),
        padding: small ? "4px 10px" : "7px 14px",
        borderRadius: 8,
        cursor: disabled ? "not-allowed" : "pointer",
        fontSize: small ? 12 : 13,
        opacity: disabled ? 0.5 : 1,
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
      },
    },
    children
  )
}

const inputStyle = {
  width: "100%",
  fontFamily: MONO,
  fontSize: 13,
  padding: "8px 10px",
  borderRadius: 8,
  border: "1px solid rgba(34,211,238,.28)",
  background: "rgba(5,7,18,.72)",
  color: "#e2e8f0",
  outline: "none",
  boxSizing: "border-box",
}

function Field({ label, children, hint }) {
  return h(
    "div",
    { style: { display: "flex", flexDirection: "column", gap: 5 } },
    h(
      "label",
      { style: { fontSize: 11, color: "#94a3b8", fontWeight: 600, letterSpacing: ".04em", textTransform: "uppercase" } },
      label
    ),
    children,
    hint && h("div", { style: { fontSize: 11, color: "#64748b" } }, hint)
  )
}

// the glass card look shared by profile cards, the form, and the log panel
const GLASS_CARD = {
  border: "1px solid rgba(34,211,238,.16)",
  borderRadius: 14,
  background: "rgba(10,14,26,.55)",
  backdropFilter: "blur(8px)",
  WebkitBackdropFilter: "blur(8px)",
  boxShadow: "0 0 0 1px rgba(34,211,238,.04), 0 0 22px rgba(34,211,238,.09), 0 10px 34px rgba(2,6,23,.55)",
}

function describeExit(le) {
  if (!le) return ""
  if (le.detached) return "stopped (detached — exit status unknown)"
  if (le.signal) return `signal ${le.signal}`
  if (le.code !== null && le.code !== undefined) return `code ${le.code}`
  return "unknown exit"
}

function ProfileForm({ initial, busy, errors, onClose, onSave }) {
  const [name, setName] = useState(initial?.name || "")
  const [command, setCommand] = useState(initial?.command || "")
  const [cwd, setCwd] = useState(initial?.cwd || "~")
  const [shell, setShell] = useState(initial?.shell || "login")
  const [port, setPort] = useState(initial?.port != null ? String(initial.port) : "")
  const [envText, setEnvText] = useState(
    Object.entries(initial?.env || {})
      .map(([k, v]) => `${k}=${v}`)
      .join("\n")
  )

  const submit = () => {
    const env = {}
    for (const raw of envText.split("\n")) {
      const line = raw.trim()
      if (!line) continue
      const i = line.indexOf("=")
      if (i <= 0) {
        setErrors ? null : null
        onSave(null, "bad env line (expected KEY=VALUE): " + line)
        return
      }
      env[line.slice(0, i).trim()] = line.slice(i + 1)
    }
    const portNum = port.trim() === "" ? null : Number(port.trim())
    onSave({
      name: name.trim(),
      command: command.trim(),
      cwd: cwd.trim() || "~",
      shell,
      env,
      port: portNum == null || Number.isNaN(portNum) ? null : Math.trunc(portNum),
    })
  }

  return h(
    "div",
    {
      key: "profile-form",
      className: "zli-card zli-fade-up",
      style: {
        ...GLASS_CARD,
        gridColumn: "1 / -1",
        padding: 16,
        display: "flex",
        flexDirection: "column",
        gap: 12,
      },
    },
    h(
      "div",
      { style: { display: "flex", alignItems: "center", gap: 7, fontWeight: 600, fontSize: 13, color: "#e2e8f0" } },
      h(Pencil, { size: 14, style: { color: "#22d3ee" } }),
      initial ? "Edit profile" : "New profile"
    ),
    h(
      Field,
      { label: "Name" },
      h("input", {
        className: "zli-input",
        style: inputStyle,
        value: name,
        maxLength: 64,
        placeholder: "e.g. ninfer qwen3-27b",
        onChange: (e) => setName(e.target.value),
      })
    ),
    h(
      Field,
      { label: "Command — exactly as you would type it in the terminal" },
      h("textarea", {
        className: "zli-input",
        style: { ...inputStyle, minHeight: 96, resize: "vertical" },
        value: command,
        placeholder: EXAMPLE_COMMAND,
        onChange: (e) => setCommand(e.target.value),
      })
    ),
    h(
      "div",
      { style: { display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10 } },
      h(
        Field,
        { label: "Working directory" },
        h("input", {
          className: "zli-input",
          style: inputStyle,
          value: cwd,
          placeholder: "~",
          onChange: (e) => setCwd(e.target.value),
        })
      ),
      h(
        Field,
        { label: "Shell mode" },
        h(
          "select",
          { className: "zli-input", style: inputStyle, value: shell, onChange: (e) => setShell(e.target.value) },
          SHELLS.map(([v, label]) => h("option", { key: v, value: v }, label))
        )
      ),
      h(
        Field,
        {
          label: "Port (optional)",
          hint: "Used to detect & verify the service; defaults to --port in the command",
        },
        h("input", {
          className: "zli-input",
          style: inputStyle,
          value: port,
          placeholder: "e.g. 1234",
          onChange: (e) => setPort(e.target.value),
        })
      )
    ),
    h(
      Field,
      { label: "Extra environment (one KEY=VALUE per line) — optional" },
      h("textarea", {
        className: "zli-input",
        style: { ...inputStyle, minHeight: 56, resize: "vertical" },
        value: envText,
        placeholder: "CUDA_VISIBLE_DEVICES=0",
        onChange: (e) => setEnvText(e.target.value),
      })
    ),
    h(
      "div",
      { style: { display: "flex", gap: 8, marginTop: 4 } },
      h(
        Button,
        { onClick: submit, disabled: busy },
        busy
          ? h(LoaderCircle, { size: 13, style: { animation: "zli-spin 1.2s linear infinite" } })
          : h(CircleCheck, { size: 13 }),
        "Save profile"
      ),
      h(Button, { kind: "ghost", onClick: onClose }, "Cancel")
    )
  )
}

// ---------------------------------------------------------------------------
// Live dashboard (ninfer /metrics) — throughput + sparkline, token ledgers,
// requests + latency, spec decode + memory. Data comes from the app backend's
// /api/metrics, which polls the ninfer Prometheus endpoint server-side (the
// service sends no CORS headers, so browser-side polling isn't possible).

// Hand-rolled canvas sparkline (no chart lib): decode line (solid, glowing)
// over a faint dashed total line, with an area fill. `data` = [ts, decode, total][].
function Sparkline({ data = [], width = 280, height = 64, color = "#22d3ee", faint = "#a855f7" }) {
  const ref = useRef(null)
  useEffect(() => {
    const c = ref.current
    if (!c) return
    const dpr = window.devicePixelRatio || 1
    c.width = Math.round(width * dpr)
    c.height = Math.round(height * dpr)
    const ctx = c.getContext("2d")
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, width, height)

    const pts = data.slice(-150)
    if (pts.length < 2) {
      ctx.strokeStyle = "rgba(148,163,184,.25)"
      ctx.setLineDash([4, 4])
      ctx.lineWidth = 1
      ctx.beginPath()
      ctx.moveTo(0, height / 2)
      ctx.lineTo(width, height / 2)
      ctx.stroke()
      ctx.setLineDash([])
      return
    }

    const maxV = Math.max(1, ...pts.map((p) => Math.max(p[1] || 0, p[2] || 0)))
    const px = (i) => (i / (pts.length - 1)) * width
    const py = (v) => height - 3 - (v / maxV) * (height - 8)

    // faint horizontal grid
    ctx.strokeStyle = "rgba(148,163,184,.10)"
    ctx.lineWidth = 1
    for (let g = 1; g < 4; g++) {
      ctx.beginPath()
      ctx.moveTo(0, (height / 4) * g)
      ctx.lineTo(width, (height / 4) * g)
      ctx.stroke()
    }

    // area fill under the decode line
    const grad = ctx.createLinearGradient(0, 0, 0, height)
    grad.addColorStop(0, color + "33")
    grad.addColorStop(1, color + "00")
    ctx.beginPath()
    ctx.moveTo(0, height)
    pts.forEach((p, i) => ctx.lineTo(px(i), py(p[1] || 0)))
    ctx.lineTo(width, height)
    ctx.closePath()
    ctx.fillStyle = grad
    ctx.fill()

    // total line (faint, dashed)
    ctx.strokeStyle = faint + "88"
    ctx.setLineDash([3, 3])
    ctx.beginPath()
    pts.forEach((p, i) => (i ? ctx.lineTo(px(i), py(p[2] || 0)) : ctx.moveTo(px(0), py(p[2] || 0))))
    ctx.stroke()
    ctx.setLineDash([])

    // decode line (glowing)
    ctx.strokeStyle = color
    ctx.lineWidth = 1.5
    ctx.shadowColor = color
    ctx.shadowBlur = 6
    ctx.beginPath()
    pts.forEach((p, i) => (i ? ctx.lineTo(px(i), py(p[1] || 0)) : ctx.moveTo(px(0), py(p[1] || 0))))
    ctx.stroke()
    ctx.shadowBlur = 0
  }, [data, width, height, color, faint])
  return h("canvas", { ref, style: { width, height, display: "block" } })
}

// small label/value block used inside the dashboard cards
function Stat({ label, value, sub, tone }) {
  return h(
    "div",
    { style: { display: "flex", flexDirection: "column", gap: 2, minWidth: 0 } },
    h(
      "span",
      {
        style: {
          fontSize: 10,
          color: "#64748b",
          textTransform: "uppercase",
          letterSpacing: ".06em",
          whiteSpace: "nowrap",
        },
      },
      label
    ),
    h(
      "span",
      {
        style: {
          fontSize: 16,
          fontWeight: 700,
          fontFamily: MONO,
          color: tone || "#e2e8f0",
          whiteSpace: "nowrap",
        },
      },
      value
    ),
    sub != null &&
      h(
        "span",
        {
          style: {
            fontSize: 10,
            color: "#64748b",
            fontFamily: MONO,
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          },
        },
        sub
      )
  )
}

// thin utilization bar (value/max)
function Meter({ value = 0, max = 1, color = "#22d3ee", height = 6 }) {
  const pct = max > 0 ? Math.min(1, Math.max(0, value / max)) * 100 : 0
  return h(
    "div",
    {
      style: {
        flex: 1,
        height,
        borderRadius: 3,
        background: "rgba(148,163,184,.12)",
        overflow: "hidden",
      },
    },
    h("div", {
      style: {
        width: pct + "%",
        height: "100%",
        borderRadius: 3,
        background: `linear-gradient(90deg, ${color}, ${color}77)`,
        boxShadow: `0 0 8px ${color}55`,
        transition: "width .4s ease",
      },
    })
  )
}

function CardTitle({ icon, children }) {
  return h(
    "div",
    { style: { display: "flex", alignItems: "center", gap: 7 } },
    icon,
    h(
      "span",
      {
        style: {
          fontSize: 11,
          fontWeight: 700,
          letterSpacing: ".08em",
          textTransform: "uppercase",
          color: "#94a3b8",
        },
      },
      children
    )
  )
}

function MetricsDashboard() {
  const [m, setM] = useState(null)
  const [clearing, setClearing] = useState(false)

  useEffect(() => {
    const sub = startMetricsPoll(setM, 2500)
    return () => sub.close()
  }, [])

  const doClear = async () => {
    const ok = window.confirm(
      "Clear all stats?\n\nResets the per-model token ledger, the lifetime decode-speed average, and the in-memory rate history. Counters start fresh from the next tick."
    )
    if (!ok) return
    setClearing(true)
    try {
      await clearStats()
    } catch (e) {
      window.alert("Clear stats failed: " + (e.message || e))
    } finally {
      setClearing(false)
    }
  }

  const header = h(
    "div",
    {
      key: "dash-h",
      style: {
        display: "flex",
        alignItems: "baseline",
        gap: 8,
        margin: "4px 0 8px",
        flexWrap: "wrap",
      },
    },
    h("span", { style: { fontSize: 13, fontWeight: 700, color: "#e2e8f0" } }, "Live dashboard"),
    m &&
      m.ok &&
      h(
        "span",
        { style: { fontSize: 11, color: "#64748b", fontFamily: MONO } },
        `ninfer · port ${m.port} · updated ${fmtTime(m.updated_at)}`
      ),
    m && !m.ok && h(Chip, { tone: "#f87171", bg: "rgba(248,113,113,.1)" }, "offline"),
    h("span", { style: { flex: 1 } }),
    h(
      Button,
      {
        kind: "danger",
        small: true,
        disabled: clearing,
        title: "Reset the per-model token ledger, the lifetime decode-speed average, and the rate history",
        onClick: doClear,
      },
      clearing
        ? h(LoaderCircle, { size: 12, style: { animation: "zli-spin 1.2s linear infinite" } })
        : h(Trash2, { size: 12 }),
      "Clear stats"
    )
  )

  if (!m)
    return h(
      "div",
      { key: "dash" },
      header,
      h(
        "div",
        {
          style: {
            ...GLASS_CARD,
            padding: "18px 16px",
            fontSize: 12,
            color: "#64748b",
            fontFamily: MONO,
          },
        },
        "connecting to the metrics collector…"
      )
    )

  if (!m.ok)
    return h(
      "div",
      { key: "dash" },
      header,
      h(
        "div",
        {
          className: "zli-card",
          style: {
            ...GLASS_CARD,
            padding: "14px 16px",
            display: "flex",
            alignItems: "center",
            gap: 10,
          },
        },
        h(Dot, { tone: "#f87171", size: 8 }),
        h(
          "div",
          { style: { display: "flex", flexDirection: "column", gap: 2 } },
          h(
            "span",
            { style: { fontSize: 12, fontWeight: 700, color: "#f87171", fontFamily: MONO } },
            "NINFER OFFLINE"
          ),
          h(
            "span",
            { style: { fontSize: 11, color: "#94a3b8" } },
            `${m.reason || "unknown"} — start a profile below; the dashboard reconnects automatically.`
          )
        )
      )
    )

  const t = m.throughput || {}
  const r = m.requests || {}
  const s = m.spec_decode || null
  const mem = m.memory || {}
  const dev = mem.device || {}
  const hostM = mem.host || {}
  const tok = m.tokens || {}
  const sess = tok.session || {}
  const model = m.model || {}

  const cardStyle = {
    ...GLASS_CARD,
    padding: 14,
    display: "flex",
    flexDirection: "column",
    gap: 10,
  }

  return h(
    "div",
    { key: "dash" },
    header,
    h(
      "div",
      {
        style: {
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))",
          gap: 14,
        },
      },
      // ---- throughput ------------------------------------------------------
      h(
        "div",
        { key: "c-thru", className: "zli-card", style: cardStyle },
        h(CardTitle, { icon: h(Activity, { size: 14, style: { color: "#22d3ee" } }) }, "Throughput"),
        h(
          "div",
          { style: { display: "flex", gap: 18, flexWrap: "wrap" } },
          h(
            Stat,
            {
              label: "decode now",
              value: t.decode_tps != null ? fmtTps(t.decode_tps) + " t/s" : "—",
              tone: "#67e8f9",
            }
          ),
          h(
            Stat,
            {
              label: "avg (ema)",
              value: t.decode_tps_avg != null ? fmtTps(t.decode_tps_avg) + " t/s" : "—",
            }
          ),
          h(
            Stat,
            {
              label: "avg (lifetime)",
              value:
                m.lifetime && m.lifetime.avg_decode_tps != null
                  ? fmtTps(m.lifetime.avg_decode_tps) + " t/s"
                  : "—",
              sub:
                m.lifetime && m.lifetime.active_seconds > 0
                  ? `over ${fmtDur(m.lifetime.active_seconds)}`
                  : null,
            }
          ),
          h(
            Stat,
            {
              label: "max (lifetime)",
              value:
                m.lifetime && m.lifetime.max_decode_tps != null
                  ? fmtTps(m.lifetime.max_decode_tps) + " t/s"
                  : "—",
              sub: "fastest ~3s window",
            }
          ),
          h(
            Stat,
            {
              label: "total now",
              value: t.total_tps != null ? fmtTps(t.total_tps) + " t/s" : "—",
              sub: "prompt + decode",
            }
          )
        ),
        h(Sparkline, { data: t.history || [] }),
        h(
          "div",
          { style: { display: "flex", gap: 12, fontSize: 10, color: "#64748b", fontFamily: MONO } },
          h("span", null, "— decode"),
          h("span", null, "--- total"),
          h("span", { style: { marginLeft: "auto" } }, "last ~7 min")
        )
      ),
      // ---- tokens ----------------------------------------------------------
      h(
        "div",
        { key: "c-tok", className: "zli-card", style: cardStyle },
        h(CardTitle, { icon: h(Terminal, { size: 14, style: { color: "#a855f7" } }) }, "Tokens"),
        h(
          "div",
          { style: { display: "flex", gap: 18, flexWrap: "wrap" } },
          h(
            Stat,
            {
              label: "prompt",
              value: fmtNum(tok.prompt),
              sub: tok.cached_prompt
                ? `${fmtPct(tok.cached_prompt / Math.max(1, tok.prompt))} cached`
                : null,
            }
          ),
          h(Stat, { label: "generation", value: fmtNum(tok.generation) }),
          h(Stat, { label: "total", value: fmtNum(tok.total) })
        ),
        h(
          "div",
          {
            style: {
              fontSize: 11,
              color: "#94a3b8",
              fontFamily: MONO,
              background: "rgba(34,211,238,.06)",
              border: "1px solid rgba(34,211,238,.25)",
              borderRadius: 8,
              padding: "7px 10px",
            },
          },
          `lifetime (app ledger) · ${fmtNum(tok.lifetime_total)} tok generated${
            m.lifetime && m.lifetime.first_at ? ` · since ${fmtTime(m.lifetime.first_at)}` : ""
          }`
        ),
        sess.model
          ? h(
              "div",
              {
                style: {
                  fontSize: 11,
                  color: "#94a3b8",
                  fontFamily: MONO,
                  background: "rgba(168,85,247,.07)",
                  border: "1px solid rgba(168,85,247,.25)",
                  borderRadius: 8,
                  padding: "7px 10px",
                },
              },
              `this session · ${sess.model} — ${fmtNum(sess.total)} tok${
                sess.since ? ` · running ${fmtDur(Date.now() / 1000 - sess.since)}` : ""
              }`
            )
          : null,
        (tok.by_model || []).length
          ? h(
              "div",
              { style: { display: "flex", flexDirection: "column", gap: 4 } },
              (tok.by_model || []).map((bm) =>
                h(
                  "div",
                  {
                    key: bm.model,
                    style: {
                      display: "flex",
                      alignItems: "baseline",
                      gap: 8,
                      fontSize: 11,
                      fontFamily: MONO,
                    },
                  },
                  h(
                    "span",
                    { style: { color: "#e2e8f0", fontWeight: 600, whiteSpace: "nowrap" } },
                    bm.model
                  ),
                  bm.spec_backend && h("span", { style: { color: "#67e8f9" } }, bm.spec_backend),
                  h("span", { style: { flex: 1 } }),
                  h(
                    "span",
                    { style: { color: "#64748b", whiteSpace: "nowrap" } },
                    `${fmtNum(bm.prompt)}p / ${fmtNum(bm.generation)}g`
                  ),
                  h("span", { style: { color: "#a5b4fc", whiteSpace: "nowrap" } }, fmtNum(bm.total))
                )
              )
            )
          : h("div", { style: { fontSize: 11, color: "#64748b" } }, "no model usage recorded yet")
      ),
      // ---- requests --------------------------------------------------------
      h(
        "div",
        { key: "c-req", className: "zli-card", style: cardStyle },
        h(CardTitle, { icon: h(LoaderCircle, { size: 14, style: { color: "#34d399" } }) }, "Requests"),
        h(
          "div",
          { style: { display: "flex", gap: 18, flexWrap: "wrap" } },
          h(
            Stat,
            {
              label: "running",
              value: `${Math.round(r.running || 0)} / ${Math.round(r.max_concurrency || 0)}`,
              sub: r.waiting ? `${Math.round(r.waiting)} waiting` : null,
              tone: (r.running || 0) > 0 ? "#34d399" : undefined,
            }
          ),
          h(Stat, { label: "completed", value: fmtNum(r.completed) }),
          h(
            Stat,
            {
              label: "lost",
              value: fmtNum((r.failed || 0) + (r.cancelled || 0) + (r.rejected || 0)),
              sub: `f ${Math.round(r.failed || 0)} · c ${Math.round(r.cancelled || 0)} · r ${Math.round(
                r.rejected || 0
              )}`,
            }
          )
        ),
        h(
          "div",
          {
            style: {
              display: "flex",
              gap: 18,
              flexWrap: "wrap",
              borderTop: "1px solid rgba(148,163,184,.12)",
              paddingTop: 10,
            },
          },
          h(Stat, { label: "ttft avg", value: r.ttft_s != null ? r.ttft_s + " s" : "—" }),
          h(Stat, { label: "duration avg", value: r.duration_s != null ? r.duration_s + " s" : "—" }),
          h(Stat, { label: "queue avg", value: r.queue_s != null ? r.queue_s + " s" : "—" })
        )
      ),
      // ---- spec decode + memory ---------------------------------------------
      h(
        "div",
        { key: "c-spec", className: "zli-card", style: cardStyle },
        h(CardTitle, { icon: h(Cpu, { size: 14, style: { color: "#fbbf24" } }) }, "Spec decode · memory"),
        s && s.draft_window != null
          ? h(
              "div",
              { style: { display: "flex", gap: 14, alignItems: "flex-end", flexWrap: "wrap" } },
              h(
                Stat,
                {
                  label: "acceptance",
                  value: s.acceptance != null ? fmtPct(s.acceptance) : "—",
                  tone: "#fbbf24",
                  sub: `${fmtNum(s.draft_tokens)} draft · ${fmtNum(s.accepted_tokens)} accepted`,
                }
              ),
              (s.positions || []).length
                ? h(
                    "div",
                    {
                      style: { display: "flex", gap: 5, alignItems: "flex-end" },
                      title: "acceptance by draft position",
                    },
                    (s.positions || []).map((p) =>
                      h(
                        "div",
                        {
                          key: p.position,
                          style: {
                            display: "flex",
                            flexDirection: "column",
                            alignItems: "center",
                            gap: 2,
                          },
                        },
                        h(
                          "div",
                          {
                            style: {
                              width: 16,
                              height: 44,
                              background: "rgba(148,163,184,.10)",
                              borderRadius: 3,
                              position: "relative",
                              overflow: "hidden",
                            },
                            title: `position ${p.position}: ${Math.round((p.rate || 0) * 100)}% accepted`,
                          },
                          h("div", {
                            style: {
                              position: "absolute",
                              bottom: 0,
                              left: 0,
                              right: 0,
                              height: `${Math.round((p.rate || 0) * 100)}%`,
                              background: "linear-gradient(180deg, #22d3ee, #a855f7)",
                              borderRadius: 3,
                            },
                          })
                        ),
                        h(
                          "span",
                          { style: { fontSize: 9, color: "#64748b", fontFamily: MONO } },
                          String(p.position)
                        )
                      )
                    )
                  )
                : null
            )
          : h("div", { style: { fontSize: 11, color: "#64748b" } }, "no speculative decoding configured"),
        h(
          "div",
          {
            style: {
              display: "flex",
              flexDirection: "column",
              gap: 8,
              borderTop: "1px solid rgba(148,163,184,.12)",
              paddingTop: 10,
            },
          },
          h(
            "div",
            { style: { display: "flex", alignItems: "center", gap: 8, fontSize: 11, fontFamily: MONO } },
            h("span", { style: { color: "#64748b", width: 44, flexShrink: 0 } }, "device"),
            h(Meter, {
              value: dev.allocated || 0,
              max: dev.budget || 0,
              color: dev.utilization != null && dev.utilization > 0.95 ? "#f87171" : "#22d3ee",
            }),
            h(
              "span",
              { style: { color: "#e2e8f0", whiteSpace: "nowrap" } },
              `${fmtBytes(dev.allocated)} / ${fmtBytes(dev.budget)}${
                dev.utilization != null ? ` · ${fmtPct(dev.utilization)}` : ""
              }`
            )
          ),
          h(
            "div",
            { style: { display: "flex", gap: 14, fontSize: 10.5, color: "#64748b", fontFamily: MONO, flexWrap: "wrap" } },
            h("span", null, `weights ${fmtBytes(dev.weights)}`),
            h("span", null, `kv ${fmtBytes(dev.kv)}`),
            h("span", { style: { marginLeft: "auto" } }, `host kv ${fmtBytes(hostM.kv_pinned)}`)
          ),
          model.max_context
            ? h(
                "div",
                { style: { fontSize: 10.5, color: "#64748b", fontFamily: MONO } },
                `max context ${fmtNum(model.max_context)} tok · ${model.name || "?"}${
                  model.spec_backend ? ` (${model.spec_backend})` : ""
                }`
              )
            : null
        )
      )
    )
  )
}

export default function SILocalInferencingApp() {
  const [state, setState] = useState(null)
  const [lines, setLines] = useState([])
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState(null) // null | "new" | profile object
  const [barVisible, setBarVisible] = useState(() => {
    try {
      return window.localStorage.getItem(BAR_STORAGE_KEY) !== "0"
    } catch {
      return true
    }
  })
  const [persistOn, setPersistOn] = useState(readPersistFlag)
  const [autoScroll, setAutoScroll] = useState(true)

  const logRef = useRef(null)
  // The LIVE OUTPUT scroller is the inner <pre> (overflowY:auto). logRef sits
  // on the outer overflow:hidden wrapper (used by scrollLogIntoView), so the
  // autoscroll must target the <pre> specifically -- a scrollTop write on the
  // wrapper is a no-op and the scroller stays pinned to the top.
  const logScrollRef = useRef(null)
  const lastSeq = useRef(0)

  const refresh = useCallback(async () => {
    try {
      const s = await getState()
      setState(s)
      setError("")
    } catch (e) {
      setError(e.message || String(e))
    }
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 2000)
    return () => clearInterval(t)
  }, [refresh])

  const onLine = useCallback((d) => {
    if (!d || !d.seq || d.seq <= lastSeq.current) return
    lastSeq.current = d.seq
    setLines((prev) => {
      const next = prev.length >= 1500 ? prev.slice(-1200) : prev.slice()
      next.push(d)
      return next
    })
  }, [])

  const onExit = useCallback(() => {
    refresh()
  }, [refresh])

  useEffect(() => {
    const sub = subscribeLogs({ after: 0, onLine, onExit })
    return () => sub.close()
  }, [onLine, onExit, refresh])

  // Keep the "everywhere" toggle in sync with writes from elsewhere: the
  // persistent bar's own Hide button (ui/bar.mjs), another browser tab, etc.
  useEffect(() => {
    const sync = () => setPersistOn(readPersistFlag())
    const onStorage = (e) => {
      if (e.key === PERSIST_FLAG_KEY || e.key === null) sync()
    }
    window.addEventListener(PERSIST_FLAG_EVENT, sync)
    window.addEventListener("storage", onStorage)
    return () => {
      window.removeEventListener(PERSIST_FLAG_EVENT, sync)
      window.removeEventListener("storage", onStorage)
    }
  }, [])

  useEffect(() => {
    // Pin the LIVE OUTPUT scroller (<pre>) to its latest line. logScrollRef
    // targets the inner scroller; the outer logRef wrapper is overflow:hidden
    // and would silently ignore a scrollTop write.
    if (autoScroll && logScrollRef.current)
      logScrollRef.current.scrollTop = logScrollRef.current.scrollHeight
  }, [lines, autoScroll])

  const act = async (fn) => {
    setBusy(true)
    setError("")
    try {
      await fn()
      await refresh()
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  const proc = (state && state.process) || { running: false }
  const tone = proc.running ? (proc.attached === false ? "#fbbf24" : "#34d399") : "#6b7280"
  const profiles = (state && state.profiles) || []
  const lastExit = state && state.last_exit
  const uptime =
    proc.running && proc.started_at
      ? ((state && state.now) || Date.now() / 1000) - proc.started_at
      : 0

  const doStart = (p) => {
    const switching = proc.running && proc.profile_id !== p.id
    const msg = switching
      ? `Start "${p.name}"? The currently running process (${
          proc.profile_name || proc.command || proc.pid
        }) will be stopped first.`
      : `Start "${p.name}"?\n\n${p.command}`
    if (!window.confirm(msg)) return
    act(async () => {
      await startProcess(p.id, switching)
    })
  }

  const doStop = () => {
    if (!window.confirm(`Stop "${proc.profile_name || "the running process"}" (pid ${proc.pid})?`))
      return
    act(async () => {
      const r = await stopProcess(8000)
      if (r && r.stopped === false && r.running)
        throw new Error(`stop failed: service still running (pid ${r.pid})`)
    })
  }

  const doDelete = (p) => {
    if (!window.confirm(`Delete profile "${p.name}"?`)) return
    act(() => deleteProfile(p.id))
  }

  const saveProfile = (data, clientErr) => {
    if (clientErr) {
      setError(clientErr)
      return
    }
    if (!data) return
    act(async () => {
      if (editing === "new") await createProfile(data)
      else await updateProfile(editing.id, data)
      setEditing(null)
    })
  }

  const setBar = (v) => {
    setBarVisible(v)
    try {
      window.localStorage.setItem(BAR_STORAGE_KEY, v ? "1" : "0")
    } catch {
      /* ignore */
    }
  }

  const setPersist = (v) => {
    setPersistOn(v)
    try {
      window.localStorage.setItem(PERSIST_FLAG_KEY, v ? "1" : "0")
    } catch {
      /* ignore */
    }
    try {
      window.dispatchEvent(new CustomEvent(PERSIST_FLAG_EVENT))
    } catch {
      /* ignore */
    }
  }

  const scrollLogIntoView = () => {
    if (logRef.current) logRef.current.scrollIntoView({ behavior: "smooth", block: "nearest" })
  }

  const lastLine = lines.length ? lines[lines.length - 1].text : ""
  // When "everywhere" is on, the persistent bar (ui/bar.mjs, mounted on
  // document.body) already covers this page — suppress the page-local bar and
  // the restore pill so only one bar is ever visible.
  const showPageBar = barVisible && !persistOn
  const showPill = !barVisible && !persistOn

  const bar = showPageBar
    ? h(
        "div",
        {
          key: "bar",
          style: {
            position: "fixed",
            left: 0,
            right: 0,
            bottom: 0,
            zIndex: 50,
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "8px 14px",
            background: "rgba(5,7,18,.96)",
            borderTop: proc.running ? "1px solid rgba(52,211,153,.5)" : "1px solid rgba(34,211,238,.35)",
            boxShadow: "0 -8px 24px rgba(34,211,238,.12)",
            fontFamily: MONO,
            fontSize: 12,
            color: "#94a3b8",
          },
        },
        h(Dot, { tone, pulse: proc.running }),
        h(
          "span",
          { style: { fontWeight: 600, whiteSpace: "nowrap", color: "#e2e8f0" } },
          proc.running ? proc.profile_name || "process" : "idle"
        ),
        proc.running && h("span", { style: { whiteSpace: "nowrap", color: "#64748b" } }, `pid ${proc.pid} · ${fmtDur(uptime)}`),
        h(
          "span",
          {
            style: { flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
            title: lastLine,
          },
          lastLine || (proc.running ? "no output yet" : "nothing running")
        ),
        h(Button, { kind: "ghost", small: true, onClick: scrollLogIntoView }, "Show log"),
        h(
          Button,
          {
            kind: "ghost",
            small: true,
            title: "Hide the bottom bar",
            onClick: () => setBar(false),
          },
          "Hide"
        )
      )
    : showPill
    ? h(
        "div",
        {
          key: "bar-pill",
          style: {
            position: "fixed",
            right: 16,
            bottom: 14,
            zIndex: 50,
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "7px 12px",
            borderRadius: 999,
            background: "rgba(10,14,26,.9)",
            border: "1px solid rgba(34,211,238,.35)",
            boxShadow: "0 0 18px rgba(34,211,238,.25)",
            cursor: "pointer",
            fontSize: 12,
            color: "#cbd5e1",
          },
          title: "Show the Ninfer bottom bar",
          onClick: () => setBar(true),
        },
        h(Terminal, { size: 14, style: { color: "#67e8f9" } }),
        "Ninfer bar",
        h(Dot, { tone, pulse: proc.running })
      )
    : null

  return h(
    "div",
    {
      className: "zli-root px-6 pt-4 pb-8",
      style: {
        paddingBottom: barVisible || persistOn ? 76 : 48, // room for the bar (page-local or persistent)
      },
    },
    h(NeonStyles),
    h("div", { className: "zli-scan-clip", "aria-hidden": true }, h("div", { className: "zli-scanline" })),
    // header
    h(
      "div",
      { key: "hdr", style: { display: "flex", alignItems: "center", gap: 12, marginBottom: 16, flexWrap: "wrap" } },
      h(
        "div",
        {
          style: {
            display: "flex",
            alignItems: "center",
            gap: 9,
            padding: "8px 14px",
            borderRadius: 10,
            background: "rgba(34,211,238,.07)",
            border: "1px solid rgba(34,211,238,.35)",
            boxShadow: "0 0 18px rgba(34,211,238,.15)",
          },
        },
        h(Cpu, { size: 18, style: { color: "#22d3ee" } }),
        h(
          "span",
          {
            className: "zli-neon-title zli-flicker",
            style: { fontWeight: 700, fontSize: 15, letterSpacing: ".02em", color: "#e2e8f0" },
          },
          "SI LOCAL INFERENCE SWITCH"
        ),
        h("span", { style: { fontSize: 11, color: "#64748b" } }, "managed local LLM backend runner")
      ),
      h(
        "div",
        {
          style: {
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "6px 12px",
            borderRadius: 999,
            border: "1px solid rgba(148,163,184,.3)",
            background: "rgba(2,6,23,.5)",
            fontSize: 12,
          },
        },
        h(Dot, { tone, pulse: proc.running, size: 8 }),
        proc.running
          ? h(
              "span",
              { style: { whiteSpace: "nowrap", color: "#e2e8f0" } },
              `${proc.profile_name || "process"} · pid ${proc.pid} · ${fmtDur(uptime)}${
                proc.attached === false ? " · detached" : ""
              }${proc.readopted ? " · adopted" : ""}`
            )
          : h("span", { style: { color: "#94a3b8" } }, "idle"),
        state && state.service_detected && !proc.running
          ? h(
              Chip,
              {
                tone: "#fbbf24",
                bg: "rgba(251,191,36,.1)",
                title: "A service matching a profile is running outside the app. Stop will adopt and stop it.",
              },
              `outside app: pid ${state.service_detected.pid}${
                state.service_detected.profile_name ? ` (${state.service_detected.profile_name})` : ""
              }`
            )
          : null,
        h(
          Button,
          {
            kind: "ghost",
            small: true,
            title: "Refresh",
            onClick: refresh,
          },
          h(RefreshCw, { size: 13 })
        ),
        h("span", { style: { width: 1, height: 16, background: "rgba(148,163,184,.3)" } }),
        h(
          "button",
          {
            style: {
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "4px 11px",
              borderRadius: 999,
              cursor: "pointer",
              fontSize: 12,
              fontWeight: 600,
              fontFamily: MONO,
              border: persistOn ? "1px solid rgba(52,211,153,.6)" : "1px solid rgba(148,163,184,.3)",
              background: persistOn ? "rgba(34,211,238,.14)" : "transparent",
              color: persistOn ? "#67e8f9" : "#94a3b8",
              textShadow: persistOn ? "0 0 10px rgba(34,211,238,.6)" : "none",
            },
            title: persistOn
              ? "The ninfer bar shows on every dashboard page — click to limit it to this page"
              : "Show the ninfer bar on every dashboard page (remembered in this browser)",
            onClick: () => setPersist(!persistOn),
          },
          persistOn ? "everywhere: on" : "everywhere: off"
        )
      )
    ),
    // error banner
    error &&
      h(
        "div",
        {
          key: "err",
          style: {
            fontSize: 12,
            color: "#fca5a5",
            background: "rgba(248,113,113,.08)",
            border: "1px solid rgba(248,113,113,.3)",
            borderRadius: 8,
            padding: "8px 12px",
            marginBottom: 12,
          },
        },
        error
      ),
    // live dashboard (ninfer /metrics)
    h(MetricsDashboard),
    // profiles
    h(
      "div",
      { key: "profiles-h", style: { display: "flex", alignItems: "baseline", gap: 8, margin: "20px 0 8px" } },
      h("span", { style: { fontSize: 13, fontWeight: 700, color: "#e2e8f0" } }, "Command profiles"),
      h(
        "span",
        { style: { fontSize: 11, color: "#64748b" } },
        `${profiles.length} defined${proc.running ? ` · ${proc.profile_name || "a process"} running` : ""}`
      )
    ),
    h(
      "div",
      {
        key: "grid",
        style: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))", gap: 14 },
      },
      profiles.map((p) => {
        const isRunning = proc.running && proc.profile_id === p.id
        const isLast = !proc.running && state && state.last_started_profile_id === p.id
        return h(
          "div",
          {
            key: p.id,
            className: "zli-card zli-fade-up" + (isRunning ? " zli-heartbeat" : ""),
            style: {
              ...GLASS_CARD,
              borderColor: isRunning ? "rgba(52,211,153,.55)" : undefined,
              padding: 16,
              display: "flex",
              flexDirection: "column",
              gap: 10,
            },
          },
          h(
            "div",
            { style: { display: "flex", alignItems: "center", gap: 8 } },
            isRunning && h(Dot, { tone: "#34d399", pulse: true }),
            h("span", { style: { fontWeight: 600, fontSize: 14, flex: 1, color: "#e2e8f0" } }, p.name),
            isRunning
              ? h(Chip, { tone: "#34d399", bg: "rgba(52,211,153,.1)" }, "RUNNING")
              : isLast
              ? h(Chip, null, "LAST LOADED")
              : null
          ),
          h(
            "div",
            {
              style: {
                fontFamily: MONO,
                fontSize: 11,
                color: "#64748b",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              },
              title: p.command,
            },
            p.command
          ),
          h(
            "div",
            { style: { fontSize: 11, color: "#64748b" } },
            `cwd ${p.cwd || "~"} · shell ${p.shell}${p.port ? ` · port ${p.port}` : ""}${p.env && Object.keys(p.env).length ? ` · ${Object.keys(p.env).length} env` : ""}`
          ),
          h(
            "div",
            { style: { display: "flex", gap: 8, marginTop: 4 } },
            h(
              Button,
              {
                onClick: () => doStart(p),
                disabled: busy,
              },
              busy
                ? h(LoaderCircle, { size: 13, style: { animation: "zli-spin 1.2s linear infinite" } })
                : h(Play, { size: 13 }),
              isRunning ? "Restart" : "Start"
            ),
            isRunning
              ? h(Button, { kind: "danger", disabled: busy, onClick: doStop }, h(Square, { size: 13 }), "Stop")
              : null,
            h(
              Button,
              {
                kind: "ghost",
                small: true,
                title: "Edit profile",
                disabled: isRunning,
                onClick: () => setEditing(p),
              },
              h(Pencil, { size: 13 })
            ),
            h(
              Button,
              {
                kind: "ghost",
                small: true,
                title: "Delete profile",
                disabled: isRunning || busy,
                onClick: () => doDelete(p),
              },
              h(Trash2, { size: 13 })
            )
          )
        )
      }),
      editing === null
        ? h(
            "div",
            {
              key: "new-tile",
              className: "zli-card",
              role: "button",
              tabIndex: 0,
              onClick: () => setEditing("new"),
              onKeyDown: (e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault()
                  setEditing("new")
                }
              },
              style: {
                border: "1px dashed rgba(34,211,238,.4)",
                borderRadius: 14,
                padding: 14,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                gap: 8,
                cursor: "pointer",
                fontSize: 13,
                color: "#67e8f9",
                minHeight: 110,
                background: "rgba(34,211,238,.03)",
              },
            },
            h(Plus, { size: 16 }),
            "New profile"
          )
        : h(ProfileForm, {
            key: "profile-form-wrap",
            initial: editing === "new" ? null : editing,
            busy,
            onClose: () => setEditing(null),
            onSave: saveProfile,
          })
    ),
    // process output
    h(
      "div",
      {
        key: "log-panel",
        ref: logRef,
        className: "zli-card zli-fade-up",
        style: {
          ...GLASS_CARD,
          marginTop: 20,
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
        },
      },
      h(
        "div",
        {
          style: {
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "10px 14px",
            borderBottom: "1px solid rgba(34,211,238,.18)",
          },
        },
        h(Activity, { size: 14, style: { color: proc.running ? "#34d399" : "#67e8f9" } }),
        h(
          "span",
          {
            style: {
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: ".08em",
              textTransform: "uppercase",
              color: "#94a3b8",
            },
          },
          "Live output"
        ),
        proc.running &&
          h(Chip, { tone: "#34d399", bg: "rgba(52,211,153,.1)" }, h(Dot, { tone: "#34d399", pulse: true, size: 6 }), "streaming"),
        h("span", { style: { flex: 1 } }),
        h(
          "label",
          { style: { display: "flex", alignItems: "center", gap: 5, cursor: "pointer", fontSize: 11, color: "#64748b" } },
          h("input", {
            type: "checkbox",
            checked: autoScroll,
            onChange: (e) => setAutoScroll(e.target.checked),
            style: { accentColor: "#34d399" },
          }),
          "auto-scroll"
        ),
        h("span", { style: { fontSize: 11, color: "#64748b", fontFamily: MONO } }, `${lines.length} lines`),
        h(Button, { kind: "ghost", small: true, onClick: () => setLines([]) }, "Clear view")
      ),
      h(
        "pre",
        {
          ref: logScrollRef,
          style: {
            margin: 0,
            padding: "10px 14px",
            height: 320,
            overflowY: "auto",
            fontFamily: MONO,
            fontSize: 12,
            lineHeight: 1.5,
            color: "#94a3b8",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            background: "rgba(2,6,23,.7)",
          },
        },
        lines.length ? lines.map((l) => l.text).join("\n") : "— no output captured yet —"
      ),
      lastExit &&
        h(
          "div",
          {
            style: {
              padding: "6px 14px",
              borderTop: `1px solid ${lastExit.code === 0 ? "rgba(52,211,153,.3)" : "rgba(248,113,113,.3)"}`,
              fontSize: 11,
              fontFamily: MONO,
              color: lastExit.code === 0 ? "#34d399" : "#f87171",
            },
          },
          `last exit: ${lastExit.profile_name || lastExit.profile_id || "?"} — ${describeExit(lastExit)} at ${fmtTime(lastExit.at)}`
        )
    ),
    bar
  )
}
