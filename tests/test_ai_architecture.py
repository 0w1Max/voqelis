"""Import-contract checks for the split Planner AI implementation."""

from voqelis.planner.ai import (
    AIProviderRouter,
    CloudflarePlannerAI,
    GeminiPlannerAI,
    GroqPlannerAI,
    PlannerAI,
)
from voqelis.planner.ai_contracts import PlannerAI as PlannerAIContract
from voqelis.planner.ai_providers import (
    CloudflarePlannerAI as CloudflareAdapter,
)
from voqelis.planner.ai_providers import (
    GeminiPlannerAI as GeminiAdapter,
)
from voqelis.planner.ai_providers import (
    GroqPlannerAI as GroqAdapter,
)
from voqelis.planner.ai_router import AIProviderRouter as Router


def test_existing_planner_ai_import_contract_remains_stable() -> None:
    assert PlannerAI is PlannerAIContract
    assert AIProviderRouter is Router
    assert GroqPlannerAI is GroqAdapter
    assert GeminiPlannerAI is GeminiAdapter
    assert CloudflarePlannerAI is CloudflareAdapter
