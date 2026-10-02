from __future__ import annotations

import json
from pathlib import Path

import pytest
from nexus_passport.passport import load_passport
from nexus_passport.lyzr_adapter import LyzrNexusAgent, NexusLyzrToolBridge
from nexus_passport.portable_runtime import PortableAgentRuntime


@pytest.fixture
def passport():
    return load_passport()


def test_passport_schema_integrity(passport):
    """Checkpoint 1: Validate agent_passport.json schema."""
    res = passport.verify_checkpoint_1_schema()
    assert res.status == "passed"
    assert passport.id == "nexus-agent-passport"
    assert passport.version == "1.0.0"
    assert len(passport.sha256) == 64


def test_passport_agent_topology(passport):
    """Checkpoint 2: Validate 8 lead agents and 15 specialist contracts."""
    res = passport.verify_checkpoint_2_topology()
    assert res.status == "passed"
    assert len(passport.lead_agents) == 8
    assert len(passport.specialists) == 15

    names = {a["name"] for a in passport.lead_agents}
    expected = {
        "NavigatorAgent", "TutorAgent", "MemoryCuratorAgent",
        "SamAgent", "CarlAgent", "TravAgent", "IrisAgent", "XAgent"
    }
    assert expected.issubset(names)


def test_passport_mcp_tool_catalog(passport):
    """Checkpoint 3: Validate 8 MCP tools and parameter schemas."""
    res = passport.verify_checkpoint_3_tool_registry()
    assert res.status == "passed"
    assert len(passport.tools) == 8

    tool_names = {t["name"] for t in passport.tools}
    assert "get_learning_map" in tool_names
    assert "next_teaching_turn" in tool_names
    assert "begin_teaching_session" in tool_names
    assert "search_learning_material" in tool_names


def test_passport_runtime_travel_declarations(passport):
    """Checkpoint 4: Validate cross-runtime travel declarations."""
    res = passport.verify_checkpoint_4_runtime_travel()
    assert res.status == "passed"
    runtimes = [r["name"] for r in passport.runtimes]
    assert "Model Context Protocol (MCP)" in runtimes
    assert "Lyzr Agent Ecosystem" in runtimes
    assert "REST / OpenAPI Gateway" in runtimes


def test_passport_quality_gates(passport):
    """Checkpoint 5: Validate security and privacy declarations."""
    res = passport.verify_checkpoint_5_quality_gates()
    assert res.status == "passed"


def test_lyzr_agent_adapter_and_tools():
    """Verify native Lyzr agent adapter interoperability."""
    agent = LyzrNexusAgent(name="TestLyzrAgent")
    assert agent.name == "TestLyzrAgent"
    assert len(agent.tools) == 4

    specs = agent.get_tool_specs()
    assert len(specs) == 4
    tool_names = [s["function"]["name"] for s in specs]
    assert "nexus_diagnose_misconception" in tool_names
    assert "nexus_get_learning_map" in tool_names

    # Test executing a learning map task
    map_res = agent.run("Get learning map for algorithms", context={"subject_id": "algorithms"})
    assert map_res["passport_certified"] is True
    assert "active_frontier" in map_res["output"]

    # Test executing a diagnostic task
    diag_res = agent.run(
        "Diagnose student mistake",
        context={"concept_id": "binary_search", "student_response": "Mid is calculated with overflow", "confidence": 0.8},
    )
    assert diag_res["passport_certified"] is True
    assert "misconception_type" in diag_res["output"]["findings"]


def test_portable_agent_runtime_circuit(passport):
    """Verify agent migration across Lyzr, MCP, and REST runtimes."""
    runtime = PortableAgentRuntime(passport)
    results = runtime.prove_all_travels()

    assert len(results) == 3
    runtimes = [r.runtime for r in results]
    assert "Lyzr-Agent-Framework" in runtimes
    assert "Model-Context-Protocol-STDIO" in runtimes
    assert "FastAPI-REST-Microservice" in runtimes

    for r in results:
        assert r.status == "success"
        assert r.verifiable is True
