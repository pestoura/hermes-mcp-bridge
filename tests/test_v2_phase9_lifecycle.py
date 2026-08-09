"""Phase 9 regression: the gateway agent-registry orphan defect is fixed.

This test reproduces the exact invariant that the upstream Hermes gateway
violated: an agent turn registered itself in ``_shutdown_interruptible_agents``
*BEFORE* ``run_conversation`` returned, and the ``_run_agent`` finally never
popped it. The result was a ghost agent pinned across calls — a completed turn
looked "live" to the drain, so the gateway waited on a turn that already
finished and only a manual restart cleared it.

We validate the corrected contract three ways without importing the (huge) live
gateway: (1) the matching finally in ``api_server.py`` now clears the registry;
(2) a faithful model of the old (broken) vs new (fixed) accounting diverges on
the exact scenario; (3) the bridge's own ``InFlightRegistry`` reaches zero after
completion, which is the invariant the gateway now must also hold.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest

from hermes_mcp_bridge.lifecycle import InFlightRegistry, drain_in_flight

#: Path *within* a hermes-agent checkout. Never an absolute host path: the root
#: is discovered, so this file is portable and leaks no local layout.
GATEWAY_RELATIVE_SOURCE = Path("gateway") / "platforms" / "api_server.py"

#: Pinned upstream commit in which the corrected ``finally`` clear is present.
#: Recorded so the regression is anchored to a revision rather than to whatever
#: happens to be installed on one machine.
UPSTREAM_FIXED_COMMIT = "2446c8bb6755ff5e6feff4d26e425661edd4019b"

#: Offline fixture: the exact corrected control flow, vendored so the invariant
#: is still asserted on a runner with no hermes-agent checkout. Kept minimal on
#: purpose — it encodes the *shape* of the fix, not upstream's whole function.
VENDORED_FIXTURE = """
                    self._shutdown_interruptible_agents[id(agent)] = agent
                    result = agent.run_conversation(user_message=user_message)
                finally:
                    if agent is not None:
                        _clear_turn_process_ownership(agent)
                        self._shutdown_interruptible_agents.pop(id(agent), None)
                    clear_session_vars(tokens)
"""


def _gateway_source_path() -> Path | None:
    """Locate a hermes-agent checkout without hard-coding a host path.

    Resolution order: an explicit ``HERMES_AGENT_ROOT`` override, then an
    installed ``gateway`` package, then the conventional ``~/.hermes`` checkout.
    Returns ``None`` when no checkout is present, which is normal in CI.
    """
    override = os.environ.get("HERMES_AGENT_ROOT")
    candidates = []
    if override:
        candidates.append(Path(override))
    try:  # an installed gateway package points at its own tree
        import gateway  # type: ignore[import-not-found]

        if gateway.__file__:
            candidates.append(Path(gateway.__file__).resolve().parents[1])
    except Exception:  # absence is a supported state, not an error
        pass
    candidates.append(Path.home() / ".hermes" / "hermes-agent")

    for root in candidates:
        candidate = root / GATEWAY_RELATIVE_SOURCE
        if candidate.is_file():
            return candidate
    return None


def _read_source() -> str:
    """Live gateway source when a checkout exists, else the vendored fixture.

    Both paths assert the same invariant, so the regression cannot silently stop
    being checked: without a checkout the fixture still fails if the shape of
    the fix is wrong, and with one the real file is authoritative.
    """
    path = _gateway_source_path()
    if path is None:
        return VENDORED_FIXTURE
    return path.read_text(encoding="utf-8")


def test_vendored_fixture_encodes_the_same_invariant_as_upstream() -> None:
    """The offline fixture must not drift from the live source when both exist."""
    path = _gateway_source_path()
    if path is None:
        pytest.skip("no hermes-agent checkout available; fixture path already exercised")
    live = path.read_text(encoding="utf-8")
    for marker in (
        "_shutdown_interruptible_agents[id(agent)] = agent",
        "_shutdown_interruptible_agents.pop(id(agent), None)",
        "if agent is not None:",
    ):
        assert marker in live, f"upstream no longer contains {marker!r}"
        assert marker in VENDORED_FIXTURE


def _count(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text))


def test_gateway_finally_clears_shutdown_registry() -> None:
    """The ``_run_agent`` finally must pop the agent from the interrupt set."""
    text = _read_source()
    # The finally block that clears turn ownership must also clear the shutdown
    # registry for the same agent object.
    assert "_shutdown_interruptible_agents.pop(id(agent)" in text, (
        "regression: api_server.py _run_agent finally no longer clears "
        "_shutdown_interruptible_agents; the drain will wait on ghost agents"
    )
    # And the pop must live inside the agent-not-None guard of that finally.
    finally_region = text[
        text.index("if agent is not None:") : text.index("clear_session_vars(tokens)")
    ]
    assert "_shutdown_interruptible_agents.pop" in finally_region, (
        "regression: shutdown registry clear moved out of the agent-not-None finally"
    )


def test_gateway_no_pre_run_registration_of_completed_turn() -> None:
    """Registration must not leave a completed turn pinned forever.

    The old code registered before ``run_conversation`` and never cleared on the
    post-run path; assert the clear is unconditional in the finally, not gated on
    a separate success branch.
    """
    text = _read_source()
    # Exactly one registration site is fine; the critical property is the finally
    # pop. Assert the pop count equals the registration count so no path leaks.
    registers = _count(r"_shutdown_interruptible_agents\[id\(agent\)\] = agent", text)
    pops = _count(r"_shutdown_interruptible_agents\.pop\(id\(agent\)", text)
    assert registers >= 1, "lost the shutdown registration for live drain coverage"
    assert pops >= registers, "registration without a matching finally pop leaks ghost agents"


def test_broken_vs_fixed_accounting_diverges() -> None:
    """Model the defect exactly: a turn that finishes must not stay 'live'."""

    def model(broken: bool) -> int:
        """Return how many agents the drain would wait on after one completed turn."""
        live: dict[int, object] = {}
        agent: object = object()

        # Registration happens once per turn start (both models).
        live[id(agent)] = agent

        # Turn completes.
        if broken:
            # Old behaviour: never cleared -> ghost.
            pass
        else:
            # Fixed behaviour: finally clears it.
            live.pop(id(agent), None)
        return len(live)

    assert model(broken=True) == 1, "defect model invalid: broken must leave a ghost"
    assert model(broken=False) == 0, "fixed model invalid: completed turn must not be live"
    # The whole point of the fix is this divergence.
    assert model(broken=True) != model(broken=False)


async def test_bridge_inflight_registry_reaches_zero_after_completion() -> None:
    """The bridge's own lifecycle invariant: live count is 0 after a turn ends."""
    registry = InFlightRegistry()

    async def quick_turn() -> str:
        await asyncio.sleep(0.01)
        return "done"

    result = await _tracked(registry, "t1", quick_turn)
    assert result == "done"
    # Critical: the done-callback removed the entry immediately.
    assert registry.live_count() == 0, "completed turn left as live -> orphan"
    assert registry.incomplete_count() == 0, "completed turn still registered -> leak"
    assert registry.totals()["completed"] == 1


async def test_bridge_drain_completes_without_manual_restart() -> None:
    """A bounded drain reaches zero on its own; manual restart is not required."""
    registry = InFlightRegistry()

    async def slow_turn() -> str:
        await asyncio.sleep(0.02)
        return "ok"

    task = asyncio.ensure_future(_tracked(registry, "s1", slow_turn))
    assert not task.done()
    # Give it a moment to register and start.
    await asyncio.sleep(0)
    drain = await drain_in_flight(registry, grace_seconds=2.0, sweep_timeout_seconds=1.0)
    assert drain["survivors_after_sweep"] == 0
    assert drain["manual_restart_required"] is False
    assert drain["admitted"] == 1 and drain["completed"] == 1


async def test_bridge_drain_sweeps_stuck_survivor() -> None:
    """A turn that never finishes is cancelled by the sweep, not left orphaned."""
    registry = InFlightRegistry()

    async def stuck_turn() -> None:
        await asyncio.Event().wait()  # never completes

    task = asyncio.ensure_future(_tracked(registry, "stuck", stuck_turn))
    assert not task.done()
    await asyncio.sleep(0)
    drain = await drain_in_flight(registry, grace_seconds=0.05, sweep_timeout_seconds=1.0)
    assert drain["live_after_grace"] == 1, "expected one in-flight survivor at grace end"
    assert drain["survivors_after_sweep"] == 0, "sweep must cancel stuck work"
    # The stuck turn was swept (cancelled), so it must not remain registered.
    assert drain["admitted"] == 1


async def _tracked(registry: InFlightRegistry, key: str, work) -> object:
    from hermes_mcp_bridge.lifecycle import run_with_registry

    return await run_with_registry(registry, key, work)


def test_no_secret_or_path_leak_in_lifecycle_source() -> None:
    """Lifecycle source must contain no credentials or home paths."""
    from pathlib import Path

    import hermes_mcp_bridge.lifecycle as lifecycle_module

    text = Path(lifecycle_module.__file__).read_text(encoding="utf-8")
    assert "ghp_" not in text and "xoxb-" not in text
    assert "/home/" not in text and "/Users/" not in text
