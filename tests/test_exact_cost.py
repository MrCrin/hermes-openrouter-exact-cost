"""Integration test for the hermes-openrouter-exact-cost provider plugin.

Runs against a throwaway Hermes home so it never touches your real install.
Point ``HERMES_HOME`` at an empty directory and run it with the interpreter that
serves Hermes:

    HERMES_HOME=$(mktemp -d) python tests/test_exact_cost.py

Hermes' code must be importable; if it is not already on ``sys.path`` the test
locates it via ``HERMES_AGENT_REPO`` or ``~/.hermes/hermes-agent``.

Exit status is 0 only when every check passes.
"""

from __future__ import annotations

import os
import shutil
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _ensure_hermes_importable() -> None:
    try:
        import providers  # noqa: F401

        return
    except ImportError:
        pass
    candidates = [
        os.environ.get("HERMES_AGENT_REPO"),
        str(Path.home() / ".hermes" / "hermes-agent"),
    ]
    for candidate in candidates:
        if candidate and (Path(candidate) / "providers" / "__init__.py").is_file():
            sys.path.insert(0, candidate)
            return
    raise SystemExit(
        "Could not import Hermes' `providers` package. Run this with Hermes'\n"
        "interpreter, or set HERMES_AGENT_REPO to the hermes-agent checkout."
    )


def _stage_into_sandbox() -> Path:
    home = os.environ.get("HERMES_HOME")
    if not home:
        raise SystemExit(
            "HERMES_HOME must point at a throwaway directory, e.g.\n"
            "  HERMES_HOME=$(mktemp -d) python tests/test_exact_cost.py"
        )
    dest = Path(home) / "plugins" / "model-providers" / "hermes-openrouter-exact-cost"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("__init__.py", "plugin.yaml"):
        shutil.copy2(REPO_ROOT / name, dest / name)
    return dest


_stage_into_sandbox()
_ensure_hermes_importable()

from agent.usage_pricing import CanonicalUsage, estimate_usage_cost  # noqa: E402
from providers import get_provider_profile  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        FAILURES.append(label)


print("\n== 1. registration and inheritance ==")
profile = get_provider_profile("openrouter")
check("profile resolves", profile is not None)
check("registered as 'openrouter'", getattr(profile, "name", None) == "openrouter")
mro = [c.__name__ for c in type(profile).__mro__]
print(f"       MRO: {' -> '.join(mro)}")
check("subclasses the bundled OpenRouterProfile", "OpenRouterProfile" in mro)
check("aliases inherited", "or" in (getattr(profile, "aliases", ()) or ()))
check("endpoint inherited", getattr(profile, "base_url", "").startswith("https://openrouter.ai/"))
check("get_usage_cost overridden by this plugin",
      type(profile).get_usage_cost.__qualname__.startswith("_ExactCostMixin"))

print("\n== 2. bundled provider behaviour still inherited ==")
body = profile.build_extra_body(session_id="sess-123", model="openai/gpt-6-luna-pro")
check("sticky session_id still forwarded", body.get("session_id") == "sess-123", f"body={body}")

print("\n== 3. exact cost from the response usage ==")
usage = CanonicalUsage(
    input_tokens=194, output_tokens=2, cache_write_tokens=100,
    raw_usage={
        "cost": 0.0015,
        "cost_details": {"upstream_inference_cost": 0.0012},
        "cache_discount": 0.0002,
    },
)
res = estimate_usage_cost("openai/gpt-6-luna-pro", usage, provider="openrouter",
                          base_url="https://openrouter.ai/api/v1")
print(f"       amount={res.amount_usd} status={res.status} source={res.source} label={res.label!r}")
check("amount is OpenRouter's reported cost", res.amount_usd == Decimal("0.0015"))
check("status is 'actual'", res.status == "actual")
check("source is 'provider_cost_api'", res.source == "provider_cost_api")
check("label is not marked as an estimate", not res.label.startswith("~"), f"label={res.label!r}")
check("provenance recorded", any("OpenRouter" in n for n in res.notes))

print("\n== 4. a free model's zero cost is still an actual charge ==")
res_free = estimate_usage_cost("meta-llama/llama-3.3-70b:free",
                               CanonicalUsage(input_tokens=10, output_tokens=5, raw_usage={"cost": 0}),
                               provider="openrouter")
check("zero cost reported as actual",
      res_free.status == "actual" and res_free.amount_usd == Decimal("0"))

print("\n== 5. missing cost falls back to Hermes' normal estimate ==")
res_est = estimate_usage_cost(
    "openai/gpt-6-luna-pro",
    CanonicalUsage(input_tokens=1000, output_tokens=500, raw_usage={"prompt_tokens": 1000}),
    provider="openrouter", base_url="https://openrouter.ai/api/v1")
check("no false provider cost claimed", res_est.source != "provider_cost_api", f"source={res_est.source}")

print("\n== 6. malformed cost values are ignored ==")
for bad in ("oops", True, -1, None):
    r = estimate_usage_cost("openai/gpt-6-luna-pro",
                            CanonicalUsage(input_tokens=1, output_tokens=1, raw_usage={"cost": bad}),
                            provider="openrouter")
    check(f"cost={bad!r} ignored", r.source != "provider_cost_api", f"source={r.source}")

print("\n== 7. other providers are untouched ==")
r = estimate_usage_cost("claude-sonnet-4.6",
                        CanonicalUsage(input_tokens=1000, output_tokens=100, raw_usage={"cost": 9.99}),
                        provider="anthropic")
check("anthropic does not use the OpenRouter path", r.source != "provider_cost_api", f"source={r.source}")

print("\n== 8. per-call pricing survives a mid-session model switch ==")
a = estimate_usage_cost("google/gemini-3.8-flash",
                        CanonicalUsage(input_tokens=10, output_tokens=1, raw_usage={"cost": 2.99}),
                        provider="openrouter")
b = estimate_usage_cost("openai/gpt-6-luna-pro",
                        CanonicalUsage(input_tokens=10, output_tokens=1, raw_usage={"cost": 0.0769}),
                        provider="openrouter")
check("first call uses its own payload", a.amount_usd == Decimal("2.99"))
check("second call uses its own payload", b.amount_usd == Decimal("0.0769"))
check("totals add up across the switch", a.amount_usd + b.amount_usd == Decimal("3.0669"))

print()
if FAILURES:
    print(f"RESULT: {len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("RESULT: all checks passed")
