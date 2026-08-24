from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .subjects.dsa import seed_dsa
from .config import Settings
from .database import Database
from .learning import LearnerEngine
from .memory import MemoryService
from .retrieval import RetrievalService
from .schemas import EvaluationCase
from .subjects.biology import seed_biology


def load_cases(path: Path) -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                cases.append(EvaluationCase.model_validate_json(line))
            except Exception as exc:
                raise ValueError(f"Invalid evaluation case on line {line_no}: {exc}") from exc
    return cases


def run_cases(cases: list[EvaluationCase]) -> dict[str, Any]:
    top1 = 0
    top3 = 0
    action_hits = 0
    evaluated_gap = 0
    evaluated_action = 0
    entropies: list[float] = []

    with TemporaryDirectory(prefix="adaptive_eval_") as tmp:
        db = Database(Path(tmp) / "eval.db")
        db.initialize()
        retrieval = RetrievalService(db)
        seed_dsa(db)
        seed_biology(db, retrieval)
        memory = MemoryService(db)
        learner = LearnerEngine(db, memory)

        for case in cases:
            session = memory.start_session(
                user_id=case.events[0].user_id if case.events else "eval",
                subject_id=case.subject_id,
                goal=f"evaluation:{case.case_id}",
                source="system",
            )
            for event in case.events:
                event.session_id = session.session_id
                states = learner.update_states_for_event(event)
                memory.commit_learning_update(event=event, states=states)
            result = learner.diagnose(session.user_id, case.target_concept_id)
            entropies.append(result.entropy)

            if case.expected_top_gap:
                evaluated_gap += 1
                ranked = [h.concept_id for h in result.hypotheses]
                top1 += int(bool(ranked and ranked[0] == case.expected_top_gap))
                top3 += int(case.expected_top_gap in ranked[:3])
            if case.expected_action:
                evaluated_action += 1
                action_hits += int(result.recommended_action == case.expected_action)

    return {
        "cases": len(cases),
        "gap_top1_accuracy": top1 / evaluated_gap if evaluated_gap else None,
        "gap_top3_recall": top3 / evaluated_gap if evaluated_gap else None,
        "action_accuracy": action_hits / evaluated_action if evaluated_action else None,
        "mean_diagnosis_entropy": sum(entropies) / len(entropies) if entropies else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run reproducible adaptive-learning diagnosis cases")
    parser.add_argument("cases", type=Path)
    args = parser.parse_args()
    result = run_cases(load_cases(args.cases))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
