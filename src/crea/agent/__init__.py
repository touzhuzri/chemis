"""The research agent: orchestration only.

The agent decides which registered tool to call next and when to stop. It
performs no chemistry, retrieves no literature, parses no PDF, and computes
nothing. Everything it knows arrives as a `ToolResult`, and everything it
concludes has to be a `Claim` pointing at an `Evidence`.
"""

from crea.agent.chemistry_agent import (
    MAX_ITERATIONS,
    ChemistryAgent,
    ToolRegistryLike,
)
from crea.agent.intelligence import (
    IntelligenceRunResult,
    ResearchBudget,
    ResearchIntelligence,
    ResearchMode,
)
from crea.agent.intelligence_runtime import AiResearchRuntime
from crea.agent.live import build_live_stack
from crea.agent.loop import MAX_STEPS, MAX_TOOL_CALLS, ResearchAgent
from crea.agent.observer import Observation, ObservationKind, observe
from crea.agent.planner import PlanDecision, PlanSession, Planner
from crea.agent.prompt import SYSTEM
from crea.agent.runtime import CoreAgentRuntime, RuntimeResult, UserPrompt
from crea.agent.tools import ToolRegistry, ToolSpec
from crea.agent.tool_surface import build_agent_tools
from crea.agent.toolsets import build_registry
from crea.agent.workspace import DocumentLibrary

__all__ = [
    "AiResearchRuntime", "ChemistryAgent", "CoreAgentRuntime",
    "DocumentLibrary", "IntelligenceRunResult", "MAX_ITERATIONS",
    "MAX_STEPS", "MAX_TOOL_CALLS", "Observation", "ObservationKind",
    "PlanDecision", "PlanSession", "Planner", "ResearchAgent", "ResearchBudget",
    "ResearchIntelligence", "ResearchMode", "RuntimeResult",
    "SYSTEM", "ToolRegistry", "ToolRegistryLike", "ToolSpec", "UserPrompt",
    "build_agent_tools", "build_live_stack", "build_registry", "observe",
]
