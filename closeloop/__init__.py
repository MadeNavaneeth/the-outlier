"""CloseLoop - autonomous bank reconciliation with a human exception desk.

Stdlib only. No pip installs required to run the demo.
"""

from .agents.orchestrator import Orchestrator
from .config import Policy
from .eval import Evaluator, improvement_table
from .llm import MockProvider, OpenAIProvider, get_provider
from .matcher import Matcher, MatcherConfig
from .reporter import reconciliation_report, write_run_artifacts
from .reviewer import SimulatedReviewer
from .store import Store
from .synthetic import GeneratorConfig, write_dataset

__version__ = "1.0.0"

__all__ = [
    "Policy",
    "Evaluator",
    "improvement_table",
    "MockProvider",
    "OpenAIProvider",
    "get_provider",
    "Matcher",
    "MatcherConfig",
    "Orchestrator",
    "reconciliation_report",
    "write_run_artifacts",
    "SimulatedReviewer",
    "Store",
    "GeneratorConfig",
    "write_dataset",
    "__version__",
]
