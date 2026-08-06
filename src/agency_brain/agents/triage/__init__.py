"""WS-G1 Triage Agent. PRD §6.3, spec §5.1 + §7."""

from .models import TriageInput, TriageOutput
from .triage_agent import TriageAgent

__all__ = ["TriageAgent", "TriageInput", "TriageOutput"]
