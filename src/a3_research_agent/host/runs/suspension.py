"""Converge an interrupted run into one Host state and one observation record."""

from a3_research_agent.host.runs.state import RunStateStore
from a3_research_agent.host.runs.supervisor import SIGINT_EXIT_CODE, SIGINT_REASON, RunSupervisor


def suspend_interrupted(
    supervisor: RunSupervisor | None,
    store: RunStateStore,
    *,
    reason: str = SIGINT_REASON,
) -> int:
    """Save the Host checkpoint as suspended and close the supervision record.

    A window-boundary SIGINT is an explicit stop, so the caller exits with the
    conventional 130 code after this function returns. Writing both files here
    keeps ``.host/state.json`` and ``supervision.json`` consistent even when a
    session happened to be interrupted mid-observation.
    """
    store.update(status="suspended")
    if supervisor is not None:
        supervisor.record_suspended(reason)
    return SIGINT_EXIT_CODE
