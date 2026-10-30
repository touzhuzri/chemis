"""Evaluation infrastructure: running blind cases and recording what
they did.

This package sits above the agent and the services -- it orchestrates a
run of one golden case, it does not extend the agent. The layering is
the same as everywhere else: the runner reaches the runtime only through
`CoreAgentRuntime`'s public `run`, and reaches storage only through
`GoldenCaseStore`'s named methods.
"""

from crea.evaluation.blind_runner import (
    BlindResearchRunner,
    BlindRunOutcome,
    RunEnvironment,
    run_blind_case,
)

__all__ = [
    "BlindResearchRunner",
    "BlindRunOutcome",
    "RunEnvironment",
    "run_blind_case",
]
