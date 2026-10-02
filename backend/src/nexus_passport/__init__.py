"""Nexus Agent Passport Package.

Provides agent portability, verification, and multi-runtime execution
across Model Context Protocol (MCP), Lyzr, and REST environments.
"""

from .passport import AgentPassport, load_passport
from .lyzr_adapter import LyzrNexusAgent, NexusLyzrToolBridge
from .portable_runtime import PortableAgentRuntime

__all__ = [
    "AgentPassport",
    "load_passport",
    "LyzrNexusAgent",
    "NexusLyzrToolBridge",
    "PortableAgentRuntime",
]

__version__ = "1.0.0"
