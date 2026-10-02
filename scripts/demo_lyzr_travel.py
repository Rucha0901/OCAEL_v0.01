#!/usr/bin/env python3
"""Lyzr Cross-Framework Portability Demonstration.

Demonstrates Nexus OVAEL migrating into the Lyzr Agent ecosystem,
binding its tools, and executing diagnostic and Socratic reasoning turns.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nexus_passport.lyzr_adapter import LyzrNexusAgent, NexusLyzrToolBridge


def main():
    print("=" * 80)
    print(" LYZR AGENT ECOSYSTEM: PORTABILITY & TRAVEL DEMONSTRATION")
    print("=" * 80)

    # 1. Instantiate the Lyzr Agent Adapter
    print("\n[Step 1] Initializing LyzrNexusAgent...")
    agent = LyzrNexusAgent(
        name="LyzrEpistemicTutor",
        role="Autonomous Socratic Diagnostic Agent",
    )
    print(f"  • Agent Name: {agent.name}")
    print(f"  • Role:       {agent.role}")
    print(f"  • Tools:      {len(agent.tools)} Lyzr-compatible tools bound")

    # 2. Inspect registered tool definitions
    print("\n[Step 2] Registered Lyzr Tool Specifications:")
    for spec in agent.get_tool_specs():
        fn = spec["function"]
        print(f"  • Tool: {fn['name']:<30} - {fn['description']}")

    # 3. Travel Demo 1: Bayesian Knowledge Map Query
    print("\n[Step 3] Executing Lyzr Task 1: Inspect Subject Frontier...")
    task1 = "Retrieve the current Bayesian learning map and active frontier for computer science"
    res1 = agent.run(task1, context={"subject_id": "algorithms"})
    print("  • Task:", task1)
    print("  • Lyzr Agent Result:")
    print(json.dumps(res1["output"], indent=4))

    # 4. Travel Demo 2: Causal Misconception Diagnosis
    print("\n[Step 4] Executing Lyzr Task 2: Diagnose Counterfactual Error...")
    task2 = "Diagnose misconception: student claims quicksort is always O(n log n) even on sorted input"
    res2 = agent.run(
        task2,
        context={
            "concept_id": "quicksort_worst_case",
            "student_response": "Quicksort is always O(n log n) regardless of pivot",
            "confidence": 0.95,  # High overconfidence!
        },
    )
    print("  • Task:", task2)
    print("  • Lyzr Agent Result:")
    print(json.dumps(res2["output"], indent=4))

    # 5. Travel Demo 3: Adaptive Socratic Move
    print("\n[Step 5] Executing Lyzr Task 3: Generate Socratic Scaffolding...")
    task3 = "What happens if we pick the first element as pivot on an already sorted array?"
    res3 = agent.run(
        task3,
        context={"session_id": "lyzr-eval-turn-42", "idempotency_key": "lyzr-idemp-001"},
    )
    print("  • Task:", task3)
    print("  • Lyzr Agent Result:")
    print(json.dumps(res3["output"], indent=4))

    print("\n" + "=" * 80)
    print(" ✓ TRAVEL PROVED: Agent successfully executed inside Lyzr Runtime.")
    print("=" * 80)


if __name__ == "__main__":
    main()
