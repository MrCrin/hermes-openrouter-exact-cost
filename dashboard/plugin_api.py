"""Backend API routes for the OpenRouter Exact Cost plugin.

Mounted by ``hermes serve`` (the Desktop backend) at
``/api/plugins/hermes-openrouter-exact-cost/`` -- ``_mount_plugin_api_routes``
discovers ``plugins/<name>/dashboard/manifest.json`` and imports this file's
``router`` when the plugin is in ``plugins.enabled``. The desktop half reaches it
through ``ctx.rest('/session-cost')``.

WHAT THIS SERVES
----------------
The number this plugin exists to get right: **everything OpenRouter charged for
one session**, for the Desktop status-bar chip. The canonical definition, shared
with the WebUI companion extension, is:

    the sum of OpenRouter's own per-request charge for every request the
    session made.

Source of truth
    The provider-plugin half of this package replaces Hermes' ``tokens x rate``
    estimate with the amount OpenRouter *actually billed* per request, and the
    core write path stores it in ``state.db`` (``session_model_usage``) -- one
    row per ``(session, model, provider, task)``. This router rolls those rows
    up. Nothing external is called: no OpenRouter generation API, no
    ``agent.log`` scrape. Consequences:

    * every billed call is counted, including the auxiliary calls (titles,
      compression, approvals) that leave no OpenRouter generation id in the log
      and are therefore invisible to a log-based reconstruction;
    * there is no eventual-consistency window (the plugin writes the cost as it
      is reported) -- the only lag is the core token-writer's coalescing;
    * the shape matches ``/api/plugins/<name>/`` exactly, so the desktop half is
      an ordinary ``ctx.rest`` caller.

WHAT IS *NOT* EXACT, AND HOW IT IS DISCLOSED
--------------------------------------------
    other_provider_calls  served by a provider other than OpenRouter. No
                          OpenRouter invoice exists for them, so they are NOT in
                          the total and are counted here.
    estimated_calls       an OpenRouter call the plugin could not price from the
                          response (it returned no cost), so the row holds a
                          local estimate instead. These ARE in the total -- real
                          spend should never be dropped -- but they are priced
                          approximately, and are counted here so the caller can
                          say so.
    complete              true only when BOTH counters are 0, i.e. every call in
                          the total is a provider-sourced figure.
    has_data              false when the session never used OpenRouter; the chip
                          renders NOTHING rather than a confident ``$0.00``.

Provenance of a row
    * main-loop row (``task = ''``): ``cost_status`` decides -- ``actual`` or
      ``included`` is provider-priced; anything else is an estimate.
    * auxiliary row (``task != ''``): the aux accounting path calls the *same*
      ``get_usage_cost`` seam, so the row holds OpenRouter's figure too -- but
      the ledger does not stamp ``cost_status`` on aux writes, so a task-tagged
      OpenRouter row is counted as provider-priced on that verified basis. This
      is exact only while the plugin is installed; rows written before it was
      installed are estimates. A session whose rows straddle the install can
      therefore read ``complete`` while holding a little pre-install estimate --
      the residuum is small and disclosed by ``estimated_calls`` for main-loop
      rows.

Config: ``HERMES_HOME`` (default ``~/.hermes``). ``?profile=`` selects a named
profile's ``state.db``; omitted / ``default`` uses the process home.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter

log = logging.getLogger(__name__)

router = APIRouter()

_PLUGIN_ID = "hermes-openrouter-exact-cost"
_VERSION = "0.2.0"

# Same id alphabet the session tables use; refuse anything else outright so a
# malformed id can never reach SQL.
_SESSION_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

_OPENROUTER = "openrouter"
#: cost_status values that PROVE a provider-sourced figure on a main-loop row.
_PRICED_STATUS = frozenset({"actual", "included"})
#: profile names that mean "this process's home", not a named profile dir.
_CURRENT_PROFILE = frozenset({"", "current", "default"})


def _hermes_root() -> Path:
    root = os.environ.get("HERMES_HOME")
    return Path(root).expanduser() if root else Path.home() / ".hermes"


def _state_db_candidates(profile: str) -> list[Path]:
    """Ordered candidate ``state.db`` paths for a request, first existing wins.

    The '' / 'default' / 'current' profile is the process home. A named profile
    is tried both as the (possibly request-scoped) ``get_hermes_home()`` and as
    ``<root>/profiles/<name>/state.db`` so a multi-profile host resolves the
    right ledger either way.
    """
    out: list[Path] = []
    try:
        from hermes_constants import get_hermes_home

        out.append(Path(get_hermes_home()) / "state.db")
    except Exception:  # pragma: no cover - hermes_constants always importable in-process
        pass
    root = _hermes_root()
    if profile and profile not in _CURRENT_PROFILE:
        out.append(root / "profiles" / profile / "state.db")
    out.append(root / "state.db")
    return out


def _open_state_db(profile: str) -> Optional[sqlite3.Connection]:
    """Read-only handle on the first existing candidate, or ``None``."""
    seen: set[str] = set()
    for path in _state_db_candidates(profile):
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if not path.is_file():
            continue
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            return con
        except sqlite3.Error:
            continue
    return None


def _session_cost(session_id: str, profile: str) -> dict[str, Any]:
    """Roll up one session's OpenRouter spend (the bill) with its disclosures."""
    con = _open_state_db(profile)
    if con is None:
        return {"session": session_id, "has_data": False, "error": "state.db not found"}

    try:
        rows = list(
            con.execute(
                "SELECT model, task, billing_provider, api_call_count,"
                "       estimated_cost_usd, cost_status"
                "  FROM session_model_usage WHERE session_id = ?",
                (session_id,),
            )
        )
    except sqlite3.Error as exc:
        log.warning("%s: state.db read failed for %s: %s", _PLUGIN_ID, session_id, exc)
        return {"session": session_id, "has_data": False, "error": f"state.db read failed: {exc}"}
    finally:
        con.close()

    by_model: dict[str, dict[str, Any]] = {}
    total = 0.0        # every OpenRouter-charged call for this session (the bill)
    exact_usd = 0.0    # the part priced from OpenRouter's own per-request figure
    estimated_usd = 0.0  # the part priced only by Hermes' local estimator
    priced_calls = 0
    openrouter_calls = 0
    other_provider_calls = 0
    estimated_calls = 0

    for r in rows:
        provider = (r["billing_provider"] or "").strip().lower()
        task = (r["task"] or "").strip()
        status = (r["cost_status"] or "").strip().lower()
        calls = int(r["api_call_count"] or 0)
        cost = float(r["estimated_cost_usd"] or 0.0)

        if provider == _OPENROUTER:
            openrouter_calls += calls
            total += cost
            # Key by the FULL model name (matches the WebUI companion's
            # by_model contract); the client shortens for display.
            name = (r["model"] or "?").strip() or "?"
            entry = by_model.setdefault(name, {"cost_usd": 0.0, "calls": 0})
            entry["cost_usd"] = round(entry["cost_usd"] + cost, 6)
            entry["calls"] += calls
            # A main-loop row proves provenance via cost_status; an auxiliary row
            # is priced through the same provider seam but carries no status, so
            # it counts as provider-priced on that verified basis.
            if task or status in _PRICED_STATUS:
                exact_usd += cost
                priced_calls += calls
            else:
                estimated_usd += cost
                estimated_calls += calls
        elif provider:
            # A different provider billed these; OpenRouter has no record of them.
            other_provider_calls += calls

    has_data = openrouter_calls > 0
    return {
        "session": session_id,
        "has_data": has_data,
        "total_usd": round(total, 6),
        "exact_usd": round(exact_usd, 6),
        "estimated_usd": round(estimated_usd, 6),
        "by_model": by_model,
        "priced_calls": priced_calls,
        "estimated_calls": estimated_calls,
        "openrouter_calls": openrouter_calls,
        "other_provider_calls": other_provider_calls,
        "complete": has_data and (other_provider_calls + estimated_calls) == 0,
        "source": "hermes_state_db",
        "plugin": _PLUGIN_ID,
        "version": _VERSION,
    }


@router.get("/session-cost")
async def session_cost(session: str = "", profile: str = "") -> dict[str, Any]:
    """The OpenRouter bill for one session, with its disclosures (see docstring)."""
    sid = (session or "").strip()
    if not sid or not _SESSION_RE.fullmatch(sid):
        return {"session": sid, "has_data": False, "error": "invalid or missing session id"}
    return _session_cost(sid, (profile or "").strip())


@router.get("/health")
async def health() -> dict[str, Any]:
    """Token-free sanity route; not used by the chip."""
    return {"ok": True, "plugin": _PLUGIN_ID, "version": _VERSION}
