# hermes-openrouter-exact-cost

One package, three surfaces. It makes Hermes report the amount OpenRouter
**actually billed** for each API call instead of a local estimate — and shows
the focused session's figure in the **Hermes Desktop** status bar. The Hermes
**WebUI** gets the same figure from a companion repo.

Hermes prices calls from a `tokens × rate` table. OpenRouter states what it
charged for each request in the response itself (`usage.cost`), and this plugin
uses that figure.

Drop-in replacement for the bundled OpenRouter provider — no config, no separate
API key.

## Surfaces and naming

The repo ships an agent-side plugin and a **Hermes Desktop** half; the Hermes
**WebUI** half lives in a separate repo. Each surface is named for where it runs:

| Surface | Where | Path / repo |
|---|---|---|
| **Agent** plugin (OpenRouter provider) | any Hermes surface | repo root — `plugin.yaml`, `__init__.py` |
| **Hermes Desktop** plugin | the native desktop app's status bar | `desktop/plugin.js` (id `hermes-openrouter-exact-cost`, display **OpenRouter Exact Cost (Desktop)**) |
| Desktop backend route | `hermes serve`, behind the chip | `dashboard/plugin_api.py` → `/api/plugins/hermes-openrouter-exact-cost/` |
| **Hermes WebUI** extension | the WebUI composer tooltip | repo [`hermes-openrouter-exact-cost-webui`](https://github.com/MrCrin/hermes-openrouter-exact-cost-webui) |

> **Why the Desktop plugin's id has no `-desktop` in it.** Hermes pairs a unified
> package's desktop half to its agent half **by folder name**, and a desktop
> plugin's exported `id` must equal that folder (here,
> `hermes-openrouter-exact-cost`). The surface is therefore carried by the
> `desktop/` path and the `(Desktop)` display name, not the id. The WebUI half is
> a separate install path, so it carries `-webui` in its repo name.

They are independent — install any subset. The Desktop half reads the agent
half's own records through `dashboard/plugin_api.py` (below), so it needs the
agent half enabled on the machine its backend runs on.

<p>
<a href="hermes://plugin/install?repo=MrCrin/hermes-openrouter-exact-cost&enable=1">Install in Hermes Desktop</a>
&nbsp;·&nbsp;
<a href="hermes://plugin/install?repo=MrCrin/hermes-openrouter-exact-cost&enable=1&force=1">Update the desktop chip</a>
</p>

## What changes

| | Before | After |
|---|---|---|
| Session cost | local estimate | OpenRouter's billed amount |
| Analytics cost totals | local estimate | OpenRouter's billed amount |
| Ledger rows | `status="estimated"` | `status="actual"`, `source="provider_cost_api"` |
| Mid-session model switch | priced per call (estimated) | priced per call, exactly |
| Auxiliary calls (vision, compression, titles) | estimated | exact, when on OpenRouter |
| Non-OpenRouter routes | estimated | estimated (unchanged) |

Everything else — the model list, reasoning handling, sticky `session_id`
routing — is inherited from the bundled profile and unchanged.

## The Hermes Desktop chip (`desktop/plugin.js`)

A chip on the right of the **Hermes Desktop** status bar, for the focused
session on its own backend. It renders nothing when the session never used
OpenRouter, so a non-OpenRouter chat is never shown a misleading `$0.00`.

Click the chip for the per-model breakdown (sorted by spend) and the disclosure
of what is *not* included.

### What the label means

```
OpenRouter: $1.23                                  normal
OpenRouter: $1.23 (+4 calls from other providers)  the session also used another provider
OpenRouter: $1.23 (+95 estimated)                  95 OpenRouter calls are locally priced, not from OpenRouter's own figure
OpenRouter: $1.23 (+4 …) (+95 …)*                  a trailing * marks an incomplete figure at a glance
(nothing)                                          the session never used OpenRouter
```

**The figure is the whole bill** — every call OpenRouter charged for the session,
including the auxiliary calls (chat titles, compression, approvals, vision) that
leave no generation id in the log and are therefore invisible to a log-based
reconstruction. Two classes sit outside the exact guarantee, both disclosed and
never merged:

* `other providers` — served somewhere OpenRouter has no invoice for, so **not**
  in the figure.
* `estimated` — an OpenRouter call the plugin could not price from OpenRouter's
  own figure, so it **is** in the figure but priced locally.

### Data path

The desktop app cannot render WebUI extensions and paints no cost line of its
own. The chip is the `desktop/` half of this package; it reads the provider
half's records through `dashboard/plugin_api.py`:

```
GET /api/plugins/hermes-openrouter-exact-cost/session-cost?session=<id>[&profile=<p>]
  → { has_data, total_usd, by_model, priced_calls, other_provider_calls,
      unpriced_calls, complete, source }
```

The route is served by the `hermes serve` backend (the Desktop app's backend),
reads `state.db` (`session_model_usage`) read-only, and calls nothing external —
no OpenRouter generation API, no `agent.log` scrape, no eventual-consistency
window. `ctx.rest('/session-cost')` scopes it to the plugin by construction.

## Install

### The provider plugin (required)

```bash
hermes plugins install MrCrin/hermes-openrouter-exact-cost
```

Or manually, by dropping the directory into your Hermes home:

```bash
mkdir -p ~/.hermes/plugins/model-providers
cp -r hermes-openrouter-exact-cost ~/.hermes/plugins/model-providers/
```

Either way it lives under `~/.hermes/`, so `hermes update` does not touch it.

### The Hermes Desktop chip

The chip is the `desktop/` half of this package (display name **OpenRouter Exact
Cost (Desktop)** in the app's Plugins list). The Desktop app looks for desktop
plugins in `$HERMES_HOME/desktop-plugins/<id>/plugin.js` (and copies a unified
package's `desktop/` half there **when the backend is local**).

Easiest install, from inside the app: **Capabilities → Plugins → Install from
Git**, repo `MrCrin/hermes-openrouter-exact-cost`, and tick the **Desktop**
target. (The [install link](#surfaces-and-naming) above opens the same dialog.)

**Updating — do not hand-edit the file.** Re-run that same install with **Force
reinstall (replace if already installed)** ticked: it re-clones the repo and
replaces the desktop half in place. The [update link](#surfaces-and-naming) above
does the same in one click. A git-installed desktop half does not self-update,
because the app can only re-materialise a package's `desktop/` half automatically
when the backend is local — against a remote backend the clone in
`desktop-plugins/` is the only copy, and `force` is what refreshes it.

- **Local backend** (app and agent on one machine): installing the package is
  enough. Where the copy is missing or stale, **Capabilities → Plugins →
  Rescan** refreshes it.
- **Remote backend** (the app on one machine, the agent on another — e.g.
  Michael's Desktop against `hermes serve` on a server): the remote
  `plugins/` directory is not a filesystem the app can read, so the automatic
  copy does **not** reach the desktop machine. Install the desktop half there
  directly — drop `desktop/plugin.js` at
  `$HERMES_HOME/desktop-plugins/hermes-openrouter-exact-cost/plugin.js` on the
  machine running the app (the folder name must equal the plugin id). Saving
  hot-reloads it; ⌘K → **Reload desktop plugins** forces a reload.

The backend route mounts on the first `ctx.rest` call (no backend restart
needed); if `ctx.rest` returns 404, confirm the provider plugin is in
`plugins.enabled` in `config.yaml` and restart the backend once.

## Check it is working

Run a session against OpenRouter and compare against OpenRouter's own records:

- **Sessions:** <https://openrouter.ai/logs?tab=sessions> — Hermes passes its
  session id to OpenRouter as `session_id`, so one Hermes session groups there.
- **Per request:** `GET https://openrouter.ai/api/v1/generation?id=<gen-id>`
  returns `total_cost` for that request.

Ledger rows for the session should carry `cost_status="actual"` and
`cost_source="provider_cost_api"`.

**The desktop chip and the WebUI extension now share one definition** — every
request OpenRouter charged for the session. The WebUI extension prices the
generation ids it harvests from `agent.log` through OpenRouter's `/generation`
API and adds the auxiliary calls no generation id covers from the ledger; the
chip rolls the whole ledger up. For any session run with this plugin active the
two agree exactly. For a session that predates the plugin the chip's ledger rows
are local estimates, so the chip is slightly low and says so with
`(+N estimated)`, while the WebUI's generation-priced figure is exact.

## Limits

- **The figure is exact; the column name is not.** Hermes' core write path stores
  it in `estimated_cost_usd`, with `cost_status` / `cost_source` alongside.
  Populating the separate `actual_cost_usd` column requires a core change, not a
  plugin.
- **BYOK requests** report a cost of ~0 to OpenRouter, because the upstream
  provider bills you directly. OpenRouter's figure is recorded as reported.
- **`:free` models** report `$0.00`.
- **Router margin** means the OpenRouter charge can exceed the upstream inference
  cost.
- **No cost in the response** → Hermes estimates as it did before; the chip
  counts that call as *unpriced* rather than showing an estimate as exact.
- **Auxiliary calls** are priced through the same provider seam, so their cost
  is OpenRouter's too — but the ledger does not stamp `cost_status` on auxiliary
  rows, so the chip treats a task-tagged OpenRouter row as priced on that basis.

## Uninstall

```bash
hermes plugins remove hermes-openrouter-exact-cost
```

On the desktop machine, disable the chip in **Capabilities → Plugins**, or use
that row's **Reveal folder** to delete the `hermes-openrouter-exact-cost` folder
under `desktop-plugins/`.

Hermes returns to estimating.

## Licence

MIT — see [LICENSE](LICENSE).
