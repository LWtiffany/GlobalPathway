"""Agent2 v2 orchestration boundary.

The existing S1 runner remains available while domain workflows migrate behind
these contracts one at a time.
"""

from .contracts import Domain, OrchestrationInput, OrchestrationOutput
from .workflow import Agent2Workflow, build_default_workflow

__all__ = ["Agent2Workflow", "Domain", "OrchestrationInput", "OrchestrationOutput",
           "build_default_workflow"]
