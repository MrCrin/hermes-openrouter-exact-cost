# hermes-openrouter-exact-cost

Makes Hermes report the amount OpenRouter **actually billed** for each API call,
instead of a local estimate.

Hermes prices calls from a `tokens × rate` table. OpenRouter states what it
charged for each request in the response itself (`usage.cost`), and this plugin
uses that figure.

Drop-in replacement for the bundled OpenRouter provider — no config, no separate
API key.

Companion project: [hermes-openrouter-exact-cost-webui](https://github.com/MrCrin/hermes-openrouter-exact-cost-webui)
displays the same figure in the Hermes WebUI. Neither project requires the other.

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

## Install

```bash
hermes plugins install MrCrin/hermes-openrouter-exact-cost
```

Or manually, by dropping the directory into your Hermes home:

```bash
mkdir -p ~/.hermes/plugins/model-providers
cp -r hermes-openrouter-exact-cost ~/.hermes/plugins/model-providers/
```

Either way it lives under `~/.hermes/`, so `hermes update` does not touch it.

## Check it is working

Run a session against OpenRouter and compare against OpenRouter's own records:

- **Sessions:** <https://openrouter.ai/logs?tab=sessions> — Hermes passes its
  session id to OpenRouter as `session_id`, so one Hermes session groups there.
- **Per request:** `GET https://openrouter.ai/api/v1/generation?id=<gen-id>`
  returns `total_cost` for that request.

Ledger rows for the session should carry `cost_status="actual"` and
`cost_source="provider_cost_api"`.

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
- **No cost in the response** → Hermes estimates as it did before.

## Uninstall

```bash
hermes plugins remove hermes-openrouter-exact-cost
```

Hermes returns to estimating.

## Licence

MIT — see [LICENSE](LICENSE).
