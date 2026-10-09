/**
 * OpenRouter Exact Cost — Hermes Desktop status-bar chip.
 *
 * The desktop half of the unified `hermes-openrouter-exact-cost` package. It
 * shows the focused session's exact OpenRouter spend, read from the agent half's
 * backend (`dashboard/plugin_api.py`, mounted by `hermes serve` at
 * `/api/plugins/hermes-openrouter-exact-cost/session-cost`).
 *
 * WHY THIS EXISTS
 * The Hermes Desktop app cannot render WebUI extensions and paints no cost line
 * of its own. The agent half already records the amount OpenRouter billed per
 * request into `state.db` (`cost_status='actual'`, `cost_source='provider_cost_api'`);
 * this chip surfaces that single session's figure in the status bar, with an
 * honest disclosure of what is NOT included.
 *
 * LABEL CONTRACT (mirrors the WebUI companion extension)
 *   "OpenRouter: $X"                                  normal case
 *   "OpenRouter: $X (+N calls from other providers)"  session also used another provider
 *   "OpenRouter: $X (+N estimated)"                   N OpenRouter calls are priced locally,
 *                                                     not from OpenRouter's own figure
 *   (nothing)                                         session never used OpenRouter
 * The figure is the whole bill: every call OpenRouter charged for this session,
 * including the auxiliary calls (titles, compression, approvals) that a
 * log-based reconstruction cannot see. Only the two suffix classes sit outside
 * the "exact" guarantee, and both are disclosed. A trailing "*" marks an
 * incomplete figure at a glance; the popover and the native title spell out why.
 *
 * SCOPING
 * Keyed on `focusedStoredSessionId` (the durable id `state.db` rows carry) of
 * the focused chat, on its own backend. No focused id (a draft) or no data ->
 * the chip renders nothing at all, never a confident "$0.00".
 *
 * ENTRY
 * Ships as the `desktop/` half of the package; the app copies it into
 * `$HERMES_HOME/desktop-plugins/hermes-openrouter-exact-cost/`. Against a REMOTE
 * backend (the app on one machine, the agent on another) that copy is local to
 * the agent host and does not reach the desktop machine, so the desktop half is
 * installed there directly. Load errors appear as a toast; ⌘K -> "Reload
 * desktop plugins" reloads it.
 *
 * NAMING
 * This is the Hermes DESKTOP surface of the package. The exported `id` must
 * equal the agent package folder name (`hermes-openrouter-exact-cost`) because
 * Hermes pairs a unified package's desktop half to its agent half by that name —
 * so "desktop" cannot live in the id. The surface is carried instead by the
 * `desktop/` path and the `(Desktop)` display name. Do not "tidy" the id.
 *
 * Style note: disk plugins are NOT scanned by the app's Tailwind build, so the
 * layout below is plain CSS using theme variables (never hardcoded colours).
 */

import {
  Button,
  Popover,
  PopoverContent,
  PopoverTrigger,
  STATUSBAR_AREAS,
  host,
  icons,
  queryClient,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'hermes-openrouter-exact-cost'
const LABEL = 'OpenRouter'
/** How often to re-ask the backend while a session is open. A finished turn also
 *  invalidates immediately (see register), so this only covers in-turn progress;
 *  never poll faster than a few seconds. */
const REFRESH_MS = 5000

const CSS = `
.orx-chip{display:inline-flex;align-items:center;gap:4px;min-width:0;max-width:100%;font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--ui-text-tertiary)}
.orx-chip:hover{color:var(--ui-text-secondary)}
.orx-chip[data-incomplete=true]{color:var(--ui-text-secondary)}
.orx-chip-text{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.orx-chip-star{color:var(--ui-accent);flex-shrink:0}
.orx-chip-icon{display:inline-flex;align-items:center;flex-shrink:0;opacity:.85}
/* The popover box is the app's standard w-72 surface (288px wide, p-2 = 8px each
   side, so a 272px content box). NEVER set a width here: a 288px panel inside a
   272px box is 16px wider than its container and its text spills past the border.
   Fill the box and let the text wrap. */
.orx-panel{width:100%;min-width:0}
.orx-head{display:flex;align-items:baseline;justify-content:space-between;gap:8px;min-width:0}
.orx-head-label{font-size:11px;color:var(--ui-text-secondary);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.orx-head-total{flex-shrink:0;font-size:13px;font-variant-numeric:tabular-nums;color:var(--ui-text-primary)}
.orx-sub{margin-top:2px;font-size:11px;line-height:15px;color:var(--ui-text-quaternary);overflow-wrap:anywhere}
.orx-list{margin-top:8px;display:flex;flex-direction:column;gap:1px;min-width:0}
.orx-row{display:flex;align-items:center;justify-content:space-between;gap:10px;min-width:0;height:20px;font-size:11px}
.orx-row-name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ui-text-secondary)}
.orx-row-cost{flex-shrink:0;font-variant-numeric:tabular-nums;color:var(--ui-text-tertiary)}
.orx-note{margin-top:8px;font-size:11px;line-height:15px;color:var(--ui-text-quaternary);overflow-wrap:anywhere}
`

/** Sub-cent values render at 4 dp so the display is never a misleading $0.00. */
function fmt(usd) {
  if (typeof usd !== 'number' || !isFinite(usd)) return null
  if (usd === 0) return '$0.00'
  if (usd < 0.01) {
    const s = usd.toFixed(4)
    return s === '0.0000' ? '<$0.0001' : '$' + s
  }
  return '$' + usd.toFixed(2)
}

function shortModel(name) {
  if (!name) return '?'
  const base = String(name).split('/').pop()
  return base.replace(/-\d{8}$/, '')
}

function buildLabel(d) {
  let label = LABEL + ': ' + (fmt(d.total_usd) || '—')
  if (d.other_provider_calls > 0) {
    label += ' (+' + d.other_provider_calls + ' call' +
      (d.other_provider_calls === 1 ? '' : 's') + ' from other providers)'
  }
  if (d.estimated_calls > 0) {
    label += ' (+' + d.estimated_calls + ' estimated)'
  }
  return label
}

function buildNote(d) {
  let note = 'The amount OpenRouter charged for this session, recorded per request by the ' +
    'exact-cost provider plugin (' + (d.priced_calls + (d.estimated_calls || 0)) +
    ' call' + (d.priced_calls + (d.estimated_calls || 0) === 1 ? '' : 's') + ').'
  if (d.other_provider_calls > 0) {
    note += ' ' + d.other_provider_calls +
      ' call(s) were served by another provider and are not included.'
  }
  if (d.estimated_calls > 0) {
    note += ' ' + d.estimated_calls +
      ' OpenRouter call(s) had no figure from OpenRouter and use a local estimate.'
  }
  return note
}

function rowsOf(d) {
  const by = d.by_model || {}
  return Object.keys(by)
    .map(name => ({ name: name, cost: by[name].cost_usd, calls: by[name].calls }))
    .filter(row => typeof row.cost === 'number')
    .sort((a, b) => b.cost - a.cost)
}

function Breakdown({ data }) {
  const rows = rowsOf(data)
  return jsxs('div', { className: 'orx-panel', children: [
    jsxs('div', { className: 'orx-head', children: [
      jsx('span', { className: 'orx-head-label', children: LABEL }),
      jsx('span', { className: 'orx-head-total', children: fmt(data.total_usd) || '—' })
    ] }),
    jsxs('div', { className: 'orx-sub', children: [
      data.priced_calls + ' priced' +
        (data.estimated_calls > 0 ? ' · ' + data.estimated_calls + ' estimated' : ''),
      data.complete ? ' · complete' : ' · incomplete'
    ] }),
    rows.length ? jsx('div', { className: 'orx-list', children: rows.map(row =>
      jsxs('div', { className: 'orx-row', children: [
        jsx('span', { className: 'orx-row-name', title: row.name, children: shortModel(row.name) }),
        jsx('span', { className: 'orx-row-cost', children: (fmt(row.cost) || '—') + ' · ' + row.calls })
      ] }, row.name)
    ) }) : null,
    jsx('div', { className: 'orx-note', children: buildNote(data) })
  ] })
}

function ExactCostChip({ rest }) {
  const storedId = useValue(host.state.focusedStoredSessionId)
  const profile = useValue(host.state.focusedSessionProfile)

  const { data } = useQuery({
    queryKey: [ID, 'session-cost', profile, storedId],
    enabled: Boolean(storedId),
    staleTime: 2000,
    refetchInterval: REFRESH_MS,
    retry: false,
    queryFn: () => rest(
      '/session-cost?session=' + encodeURIComponent(String(storedId)) +
      '&profile=' + encodeURIComponent(profile || 'default')
    )
  })

  // No focused session, no backend answer, or a session that never used
  // OpenRouter -> contribute nothing to the bar.
  if (!storedId || !data || !data.has_data) return null

  const label = buildLabel(data)
  const note = buildNote(data)
  const incomplete = !data.complete

  return jsx(Popover, { children: [
    jsx(PopoverTrigger, { asChild: true, children: jsx(Button, {
      variant: 'ghost',
      size: 'micro',
      className: 'orx-chip',
      'data-incomplete': incomplete ? 'true' : 'false',
      title: note,
      'aria-label': label + '. ' + note,
      children: [
        jsx('span', { className: 'orx-chip-icon', 'aria-hidden': true, children: jsx(icons.CreditCard, { size: 12 }) }),
        jsx('span', { className: 'orx-chip-text', children: label }),
        incomplete ? jsx('span', { className: 'orx-chip-star', 'aria-hidden': true, children: '*' }) : null
      ].filter(Boolean)
    }) }),
    jsx(PopoverContent, { side: 'top', align: 'end', 'aria-label': LABEL, children: jsx(Breakdown, { data }) })
  ] })
}

export default {
  id: ID,
  // Surface marker lives in the display name, not the id (see NAMING above).
  name: 'OpenRouter Exact Cost (Desktop)',
  description: 'Hermes Desktop status-bar chip: the exact amount OpenRouter billed for the focused session, with a per-model breakdown.',
  register(ctx) {
    const style = document.createElement('style')
    style.textContent = CSS
    document.head.append(style)
    ctx.onDispose(() => style.remove())

    // A finished turn is the moment the ledger settles, so refresh at once;
    // the interval only covers in-turn progress. Tracked -> retired with us.
    ctx.onEvent('message.complete', () => {
      queryClient.invalidateQueries({ queryKey: [ID, 'session-cost'] })
    })

    ctx.register({
      id: 'chip',
      area: STATUSBAR_AREAS.right,
      order: 130,
      render: () => jsx(ExactCostChip, { rest: path => ctx.rest(path) })
    })
  }
}
