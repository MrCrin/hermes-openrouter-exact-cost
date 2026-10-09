"""OpenRouter exact-cost accounting for Hermes Agent.

Hermes prices every API call from a local ``tokens x rate`` table. For
first-party APIs (Anthropic, OpenAI, Google, DeepSeek, ...) that is the only
option available, because those APIs return token counts and never a dollar
figure. OpenRouter is different: each response carries the amount it actually
billed for that request, in the response's ``usage`` object (``usage.cost``,
with a ``cost_details`` breakdown alongside it). OpenRouter is also the one
gateway that prices dynamically -- per upstream provider, with separate
cache-read and cache-write rates and router margin -- so a static table drifts
away from the real bill in exactly the situations agent work creates most:
long contexts, tool-call turns, and cache-heavy sessions.

Hermes already exposes the seam for this. ``ProviderProfile.get_usage_cost()``
is consulted *before* the local estimator, and its own docstring describes its
purpose as distinguishing "estimates from invoices". The bundled OpenRouter
profile simply never implemented it, so OpenRouter sessions get estimated like
every other provider.

This plugin re-registers the ``openrouter`` provider with that one method
implemented. Session costs then reflect OpenRouter's real billed amounts, per
request -- so the figure stays correct across mid-session model switches,
reasoning turns, cache-heavy contexts, and auxiliary calls (vision, compression,
title generation).

Design
------
Inherit, don't fork
    The profile class subclasses the bundled ``OpenRouterProfile``, so every
    provider quirk (reasoning clamping, sticky ``session_id`` routing, endpoint
    pins, catalog fetching) is inherited rather than reimplemented. The instance
    copies the bundled instance's dataclass fields, so upstream changes to
    ``base_url``, ``fallback_models`` and friends flow through automatically
    instead of being frozen here.

Defer to upstream
    If the bundled profile ever implements ``get_usage_cost`` itself, this
    plugin returns that value untouched, so an upstream fix always wins.

Never regress
    When OpenRouter reports no cost -- BYOK requests, routes that omit it, or a
    non-OpenRouter provider after a mid-session switch -- the method returns
    ``None`` and Hermes falls back to its normal estimate unchanged.

Install
-------
``hermes plugins install MrCrin/hermes-openrouter-exact-cost``, or drop this
directory into
``$HERMES_HOME/plugins/model-providers/hermes-openrouter-exact-cost/``.
No configuration and no API key of its own: it reads the same
``OPENROUTER_API_KEY`` the provider already uses.

It is a drop-in replacement for the bundled profile, so nothing else changes --
the provider still appears as "OpenRouter" in the model picker, and uninstall
restores the estimate behaviour exactly.
"""

from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import logging
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

__version__ = "0.2.0"

PROVIDER_NAME = "openrouter"
#: Bundled plugin module for this provider. Only importable once bundled
#: discovery has run -- see :func:`_load_bundled` for why that is not a given.
BUNDLED_MODULE = "plugins.model_providers.openrouter"
#: Private module name used when the bundled plugin has to be loaded from its
#: file instead of imported (it is not on ``sys.modules`` yet at that point).
_BUNDLED_LOADED_AS = "_hermes_bundled_openrouter_for_exact_cost"

# ``CostStatus`` / ``CostSource`` are Literals in ``agent.usage_pricing`` and
# these strings must match exactly. The amount is reported by the provider
# itself for the request, so it is an actual cost sourced from the provider's
# own cost reporting -- not a local estimate.
COST_STATUS = "actual"
COST_SOURCE = "provider_cost_api"
PRICING_VERSION = "openrouter-response-cost"

#: Last-resort profile fields, used only if the bundled instance is missing
#: (e.g. this plugin installed beside a fork that dropped it). Mirrors the
#: bundled OpenRouter profile so behaviour is unchanged.
_FALLBACK_FIELDS: dict[str, Any] = {
    "name": PROVIDER_NAME,
    "aliases": ("or",),
    "env_vars": ("OPENROUTER_API_KEY",),
    "display_name": "OpenRouter",
    "description": "OpenRouter - unified API for 200+ models",
    "signup_url": "https://openrouter.ai/keys",
    "base_url": "https://openrouter.ai/api/v1",
    "models_url": "https://openrouter.ai/api/v1/models",
}


def _bundled_plugin_file() -> Optional[Path]:
    """Path to the bundled OpenRouter plugin's ``__init__.py``, if it exists."""
    import providers as _providers

    bundled_dir = getattr(_providers, "_BUNDLED_PLUGINS_DIR", None)
    if bundled_dir is None:
        bundled_dir = (
            Path(_providers.__file__).resolve().parent.parent
            / "plugins" / "model-providers"
        )
    init_file = Path(bundled_dir) / "openrouter" / "__init__.py"
    return init_file if init_file.is_file() else None


def _load_bundled_from_file() -> tuple[Optional[type], Any]:
    """Load the bundled OpenRouter plugin directly from its source file.

    Needed because user plugins are imported *during* provider discovery, and
    discovery can reach a user plugin before it has imported the bundled ones
    (the bundled plugins are iterated in name order and one of them triggers the
    ``$HERMES_HOME`` scan). At that moment ``plugins.model_providers.openrouter``
    is not on ``sys.modules`` and cannot be imported by name, so we execute the
    plugin file under a private module name instead.

    The bundled file's own ``register_provider()`` call runs as a side effect;
    this plugin's registration follows immediately and overwrites it in the same
    (home) layer, and the bundled profile that full discovery imports later lands
    in the process-wide registry, which the home layer shadows. Net effect is
    unchanged: exactly one OpenRouter profile is selectable, and it is this one.
    """
    init_file = _bundled_plugin_file()
    if init_file is None:
        return None, None

    cached = sys.modules.get(_BUNDLED_LOADED_AS)
    if cached is not None:
        return getattr(cached, "OpenRouterProfile", None), getattr(cached, "openrouter", None)

    try:
        spec = importlib.util.spec_from_file_location(
            _BUNDLED_LOADED_AS, init_file,
            submodule_search_locations=[str(init_file.parent)],
        )
        if spec is None or spec.loader is None:
            return None, None
        module = importlib.util.module_from_spec(spec)
        sys.modules[_BUNDLED_LOADED_AS] = module
        spec.loader.exec_module(module)
    except Exception:  # pragma: no cover - depends on the host install
        sys.modules.pop(_BUNDLED_LOADED_AS, None)
        logger.debug("%s: could not load bundled OpenRouter plugin from file", __name__,
                     exc_info=True)
        return None, None
    return getattr(module, "OpenRouterProfile", None), getattr(module, "openrouter", None)


def _load_bundled() -> tuple[Optional[type], Any]:
    """Return the bundled OpenRouter profile class and configured instance.

    Two paths, because the bundled plugin may or may not be loaded yet when this
    runs:

    1. A plain import -- the normal case, when bundled discovery finished first.
    2. Loading the plugin file directly -- when this plugin was imported mid
       discovery, ahead of the bundled plugins.

    Returns ``(None, None)`` if neither works, and the caller then falls back to
    a bare ``ProviderProfile`` so the plugin still registers and still reports
    exact costs.
    """
    try:
        module = importlib.import_module(BUNDLED_MODULE)
        bundled_class = getattr(module, "OpenRouterProfile", None)
        if bundled_class is not None:
            return bundled_class, getattr(module, "openrouter", None)
    except Exception:
        logger.debug("%s: bundled OpenRouter module not importable by name yet", __name__,
                     exc_info=True)
    return _load_bundled_from_file()


def _reported_cost(usage: Any) -> Optional[Decimal]:
    """OpenRouter's billed amount for this request, from the response ``usage``.

    ``usage`` is a ``CanonicalUsage``. Its ``raw_usage`` is the provider's own
    usage object as a dict, which is where OpenRouter puts ``cost`` (the total
    charged to the account). Returns ``None`` for anything unusable -- no cost
    field, a non-dict payload, a non-numeric or negative value -- so the caller
    falls back to Hermes' normal estimate rather than inventing a number.
    """
    raw = getattr(usage, "raw_usage", None)
    if not isinstance(raw, dict):
        return None
    value = raw.get("cost")
    # ``bool`` is an int subclass; a boolean is never a real cost.
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount


def _cost_notes(usage: Any) -> tuple[str, ...]:
    """Short provenance notes recorded beside the ledger row.

    These explain *where* the number came from and flag the cases where the
    OpenRouter-charged amount is not the whole story (BYOK, cache discounts,
    router margin versus upstream cost).
    """
    raw = getattr(usage, "raw_usage", None)
    if not isinstance(raw, dict):
        return ()
    notes = ["cost reported by OpenRouter for this request"]

    details = raw.get("cost_details")
    if isinstance(details, dict):
        upstream = details.get("upstream_inference_cost")
        if upstream is not None and not isinstance(upstream, bool):
            try:
                notes.append(f"upstream inference cost {Decimal(str(upstream)):.6f}")
            except (InvalidOperation, ValueError, TypeError):
                pass

    if raw.get("cache_discount"):
        notes.append("prompt-cache discount applied")
    if raw.get("is_byok"):
        notes.append("BYOK request: not charged to OpenRouter credits")
    return tuple(notes)


def _cost_label(amount: Decimal) -> str:
    """Display label, without the ``~`` marker Hermes uses for estimates.

    ``format_cost_label`` prefixes estimates with ``~`` and applies the sub-cent
    formatting rules; the number here is an actual charge, so the marker is
    stripped. If the upstream helper stops emitting it, ``lstrip`` is a no-op.
    """
    from agent.usage_pricing import format_cost_label

    return format_cost_label(amount).lstrip("~")


class _ExactCostMixin:
    """Adds exact provider-reported cost to an OpenRouter provider profile."""

    def get_usage_cost(self, model: str, usage: Any) -> Any | None:
        """Price one request from OpenRouter's own reported cost.

        Called by ``agent.usage_pricing.estimate_usage_cost`` once per API
        response with the model and provider active for *that* call, so a
        mid-session model switch is priced per call rather than retroactively.
        """
        # 1. Defer to the bundled profile: if upstream ever implements this,
        #    its answer is authoritative and we must not shadow it.
        inherited = super().get_usage_cost(model, usage)
        if inherited is not None:
            return inherited

        # 2. OpenRouter's own number for this request, when the response has one.
        amount = _reported_cost(usage)
        if amount is None:
            return None

        # 3. Report it as an actual, provider-sourced cost.
        from agent.usage_pricing import CostResult

        return CostResult(
            amount_usd=amount,
            status=COST_STATUS,
            source=COST_SOURCE,
            label=_cost_label(amount),
            pricing_version=PRICING_VERSION,
            notes=_cost_notes(usage),
        )


def _profile_kwargs(bundled_instance: Any) -> dict[str, Any]:
    """Constructor kwargs for our profile.

    Copied from the bundled instance when available, so upstream changes to the
    bundled profile are inherited automatically instead of being frozen into
    this plugin. Only ``name`` is forced, so the registration targets the
    provider we intend to override.
    """
    if dataclasses.is_dataclass(bundled_instance):
        kwargs = {
            field.name: getattr(bundled_instance, field.name)
            for field in dataclasses.fields(bundled_instance)
            if field.init
        }
        if kwargs:
            kwargs["name"] = PROVIDER_NAME
            return kwargs
    return dict(_FALLBACK_FIELDS)


_BundledClass, _BundledInstance = _load_bundled()
_BaseClass = _BundledClass or ProviderProfile


class OpenRouterExactCostProfile(_ExactCostMixin, _BaseClass):  # type: ignore[misc,valid-type]
    """Bundled OpenRouter profile plus exact per-request cost reporting."""


register_provider(OpenRouterExactCostProfile(**_profile_kwargs(_BundledInstance)))
