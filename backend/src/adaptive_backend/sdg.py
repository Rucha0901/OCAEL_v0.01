from __future__ import annotations

"""Synthetic Data Generation for OVAEL.

The SDG subsystem is intentionally separated from live learner evidence. Synthetic
training episodes are marked by run/profile provenance and are never inserted into
real learner-state tables unless an explicit demo seed is requested.
"""

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from argon2 import PasswordHasher

from .database import Database


def _iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


@dataclass(slots=True, frozen=True)
class SDGResult:
    run_id: str
    episode_count: int
    seed: int
    scenario: str


class SyntheticDataGenerator:
    """Deterministic, provenance-safe pedagogical SDG.

    It creates structured *teaching episodes*, not fake calibrated questions.
    Synthetic rows never masquerade as empirical learner telemetry or item
    calibration evidence.
    """

    ACTIONS = (
        "explain", "contrast", "worked_example", "prerequisite_rewind",
        "practice", "reassess",
    )
    STATES = ("unknown", "developing", "needs_review", "active_blocker", "recovered")

    def __init__(self, db: Database):
        self.db = db

    def generate(
        self,
        *,
        scenario: str = "general-pedagogy",
        seed: int = 260823,
        per_concept: int = 4,
        profile_key: str = "generic-synthetic-learner",
        concept_ids: list[str] | tuple[str, ...] | None = None,
        profile_context: dict[str, Any] | None = None,
    ) -> SDGResult:
        """Generate isolated synthetic teaching episodes with explicit provenance.

        ``profile_key`` is a synthetic dataset label, never a real user identifier.
        ``concept_ids`` can scope a run to the concepts represented by a demo
        learner. ``profile_context`` is stored only as synthetic generation metadata
        and copied into the episode learner-state projection so downstream training
        can distinguish pedagogical preferences from empirical traits.
        """
        rng = random.Random(seed)
        run_id = str(uuid4())
        now = _iso()
        safe_profile = str(profile_key or "generic-synthetic-learner")[:128]
        context = dict(profile_context or {})
        self.db.execute(
            "INSERT INTO sdg_runs(run_id,scenario,seed,generator,metadata_json,created_at) VALUES(?,?,?,?,?,?)",
            (
                run_id, scenario, seed, "ovael-structured-sdg",
                self.db.dumps({
                    "synthetic": True, "empirical": False,
                    "profile_key": safe_profile,
                    "profile_context": context,
                    "concept_scope": list(concept_ids or []),
                }),
                now,
            ),
        )
        if concept_ids:
            ids = [str(v) for v in dict.fromkeys(concept_ids) if str(v)]
            placeholders = ",".join("?" for _ in ids)
            concepts = self.db.fetchall(
                f"SELECT c.concept_id,c.subject_id,c.name,c.metadata_json FROM concepts c WHERE c.concept_id IN ({placeholders}) ORDER BY c.subject_id,c.concept_id",
                tuple(ids),
            )
        else:
            concepts = self.db.fetchall(
                "SELECT c.concept_id,c.subject_id,c.name,c.metadata_json FROM concepts c ORDER BY c.subject_id,c.concept_id"
            )
        count = 0
        for concept in concepts:
            cid = str(concept["concept_id"])
            subject = str(concept["subject_id"])
            name = str(concept["name"])
            prereq = self.db.fetchone(
                "SELECT source_concept_id FROM concept_edges WHERE target_concept_id=? AND relation='PREREQUISITE_OF' ORDER BY strength DESC LIMIT 1",
                (cid,),
            )
            component = self.db.fetchone(
                "SELECT source_concept_id FROM concept_edges WHERE target_concept_id=? AND relation='PART_OF' ORDER BY strength DESC LIMIT 1",
                (cid,),
            )
            confused = self.db.fetchone(
                """SELECT CASE WHEN source_concept_id=? THEN target_concept_id ELSE source_concept_id END AS other
                   FROM concept_edges WHERE relation='COMMONLY_CONFUSED_WITH'
                   AND (source_concept_id=? OR target_concept_id=?) LIMIT 1""",
                (cid, cid, cid),
            )
            possible_gaps: list[tuple[str, str]] = [(cid, "direct")]
            if prereq:
                possible_gaps.append((str(prereq["source_concept_id"]), "prerequisite"))
            if component:
                possible_gaps.append((str(component["source_concept_id"]), "component"))
            if confused:
                possible_gaps.append((str(confused["other"]), "misconception"))

            source_rows = self.db.fetchall(
                "SELECT source_id,citation_location FROM knowledge_chunks WHERE subject_id=? AND verified=1 AND concept_ids_json LIKE ? LIMIT 3",
                (subject, f'%"{cid}"%'),
            )
            refs = [
                {"source_id": r["source_id"], "citation": r["citation_location"]}
                for r in source_rows
            ]
            for i in range(max(1, min(per_concept, 20))):
                state = rng.choice(self.STATES)
                gap_concept, gap_type = rng.choice(possible_gaps)
                if state in {"active_blocker", "needs_review"}:
                    action = rng.choice(("explain", "contrast", "prerequisite_rewind", "worked_example"))
                elif state == "recovered":
                    action = "reassess"
                else:
                    action = rng.choice(self.ACTIONS)
                target_difficulty = round(rng.uniform(0.28, 0.78), 2)
                if state == "active_blocker":
                    target_difficulty = round(max(0.20, target_difficulty - 0.12), 2)
                improved = rng.random() < (0.78 if action != "practice" else 0.64)
                recovered = improved and state in {"active_blocker", "needs_review"} and rng.random() < 0.55
                response = {
                    "synthetic": True,
                    "response_pattern": (
                        "applies the taught rule to a new case" if improved
                        else "repeats the earlier misconception or misses one constraint"
                    ),
                    "independent": action in {"practice", "reassess"},
                }
                episode = (
                    str(uuid4()), run_id, safe_profile, subject, cid,
                    self.db.dumps({
                        "status": state,
                        "target_difficulty": target_difficulty,
                        "concept_name": name,
                        "synthetic_profile_context": context,
                    }),
                    self.db.dumps({
                        "concept_id": gap_concept,
                        "type": gap_type,
                        "synthetic_hypothesis": True,
                    }),
                    action,
                    self.db.dumps(response),
                    self.db.dumps({
                        "improved": improved,
                        "recovered": recovered,
                        "quality_gate": "synthetic-not-empirical",
                    }),
                    self.db.dumps(refs), now,
                )
                self.db.execute(
                    """INSERT INTO synthetic_teaching_episodes(
                       episode_id,run_id,profile_key,subject_id,concept_id,learner_state_json,gap_json,
                       teaching_action,synthetic_response_json,evaluation_json,source_refs_json,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    episode,
                )
                count += 1
        return SDGResult(run_id=run_id, episode_count=count, seed=seed, scenario=scenario)

    def export_jsonl(self, run_id: str, path: Path) -> int:
        rows = self.db.fetchall(
            "SELECT * FROM synthetic_teaching_episodes WHERE run_id=? ORDER BY subject_id,concept_id,episode_id",
            (run_id,),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for r in rows:
                record = {
                    "synthetic": True,
                    "provenance": {"run_id": run_id, "profile_key": r["profile_key"]},
                    "subject_id": r["subject_id"],
                    "concept_id": r["concept_id"],
                    "learner_state": self.db.loads(r["learner_state_json"], {}),
                    "gap": self.db.loads(r["gap_json"], {}),
                    "teaching_action": r["teaching_action"],
                    "synthetic_response": self.db.loads(r["synthetic_response_json"], {}),
                    "evaluation": self.db.loads(r["evaluation_json"], {}),
                    "source_refs": self.db.loads(r["source_refs_json"], []),
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return len(rows)


def seed_bharat_demo(services: Any, *, password: str = "12345678", reset: bool = True) -> dict[str, Any]:
    """Create the explicitly synthetic Bharat hackathon demo account and graph.

    The requested eight-character password is allowed only in this seed helper;
    normal registration keeps the stronger production password policy.
    """
    db: Database = services.db
    username = "Bharat"
    existing = db.fetchone("SELECT user_id FROM users WHERE username=? COLLATE NOCASE", (username,))
    if existing:
        profile = db.fetchone("SELECT synthetic FROM learner_profiles WHERE user_id=?", (existing["user_id"],))
        if not reset or not profile or not bool(profile["synthetic"]):
            raise ValueError("Bharat already exists and is not an SDG-owned demo account")
        # This database is explicitly a demo seed; remove only the prior SDG-owned account.
        db.execute("DELETE FROM users WHERE user_id=?", (existing["user_id"],))

    user_id = str(uuid4())
    ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
    recovery = "OVAEL-DEMO-BHARAT-RECOVERY-ONLY"
    now = datetime.now(timezone.utc)
    db.execute(
        """INSERT INTO users(user_id,username,password_hash,recovery_hash,role,created_at,updated_at,disabled)
           VALUES(?,?,?,?, 'learner',?,?,0)""",
        (user_id, username, ph.hash(password), ph.hash(recovery), _iso(now), _iso(now)),
    )
    preferences = {
        "synthetic_demo": True,
        "institution": "IIITM Gwalior",
        "interaction": {
            "voice_enabled": True,
            "preferred_voice_speed": 1.5,
            "teaching_preferences": [
                "concept-first explanation",
                "why-before-how",
                "worked example before independent practice when blocked",
                "source-grounded explanations when material is available",
            ],
            "feedback_style": "direct and specific",
        },
        "demo_persona": {
            "label": "Synthetic hackathon learner persona",
            "traits": [
                "curious and willing to explore a topic beyond the first explanation",
                "prefers a fast learning pace but benefits from prerequisite rewinds when blocked",
                "responds well to direct feedback, comparisons and worked examples",
                "likes voice teaching and interactive problem solving rather than passive reading",
            ],
            "confidence": "synthetic-only",
        },
        "note": "All personality/learning preferences in this profile are synthetic demo data, not measured traits.",
    }
    db.execute(
        "INSERT INTO learner_profiles(user_id,display_name,institution,preferences_json,synthetic,updated_at) VALUES(?,?,?,?,1,?)",
        (user_id, "Bharat", "IIITM Gwalior", db.dumps(preferences), _iso(now)),
    )
    services.memory.ensure_privacy_row(user_id)

    # Synthetic personalized concept overlay. Values are intentionally human-demo
    # states, not claims of psychometric calibration.
    states = [
        # subject, concept, mastery, confidence, count, status, recurrence
        ("DSA", "dsa_arrays", 0.94, 0.84, 10, "verified", 0),
        ("DSA", "binary_search", 0.88, 0.76, 8, "verified", 0),
        ("DSA", "binary_search_boundary", 0.79, 0.79, 9, "recovered", 3),
        ("DSA", "binary_search_loop_invariant", 0.64, 0.58, 5, "developing", 1),
        ("DSA", "recursion", 0.57, 0.65, 6, "developing", 2),
        ("DSA", "recursion_base_case", 0.29, 0.70, 7, "active_blocker", 4),
        ("DSA", "dynamic_programming", 0.44, 0.50, 4, "needs_review", 2),
        ("DSA", "dp_state_definition", 0.31, 0.50, 4, "needs_review", 3),
        ("BIO", "bio_cell", 0.91, 0.76, 8, "verified", 0),
        ("BIO", "bio_rer", 0.80, 0.70, 7, "recovered", 2),
        ("BIO", "bio_golgi", 0.77, 0.70, 7, "recovered", 2),
        ("BIO", "bio_lysosome", 0.62, 0.58, 5, "developing", 1),
        ("BIO", "bio_peroxisome", 0.33, 0.65, 6, "active_blocker", 3),
    ]
    for idx, (subject, cid, mastery, confidence, count, status, recurrence) in enumerate(states):
        observed = now - timedelta(hours=12 + idx * 5)
        db.execute(
            """INSERT INTO concept_state(user_id,concept_id,mastery_belief,confidence,evidence_count,last_observed_at,status,recurrence_count,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (user_id, cid, mastery, confidence, count, _iso(observed), status, recurrence, _iso(now)),
        )
        theta = (mastery - 0.5) * 3.0
        target = min(0.86, max(0.22, 0.35 + mastery * 0.5))
        db.execute(
            """INSERT INTO adaptive_state(user_id,subject_id,concept_id,theta,variance,target_difficulty,difficulty_band,observations,micro_stage_position,behavior_profile,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, subject, cid, theta, max(0.35, 2.0 / max(count, 1)), target,
             "medium", count, count % 3, "synthetic_demo_profile", _iso(now)),
        )

    gaps = [
        ("recursion", "recursion_base_case", 0.78, "component", ["Repeated missing/incorrect base-case reasoning"]),
        ("dynamic_programming", "dp_state_definition", 0.71, "component", ["State definition changes across similar problems"]),
        ("dynamic_programming", "recursion", 0.19, "prerequisite", ["Recursive decomposition is not yet fully stable"]),
        ("bio_peroxisome", "bio_lysosome", 0.66, "misconception", ["Functions of lysosome and peroxisome were confused in synthetic history"]),
    ]
    for target, gap, prob, source, evidence in gaps:
        db.execute(
            "INSERT INTO gap_hypotheses(user_id,target_concept_id,gap_concept_id,probability,source,evidence_json,updated_at) VALUES(?,?,?,?,?,?,?)",
            (user_id, target, gap, prob, source, db.dumps(evidence), _iso(now)),
        )

    # Historical sessions + summaries make the demo account immediately useful.
    reports = [
        ("DSA", "Binary Search boundary recovery", 6, ["binary_search", "binary_search_boundary"], "contrast", "recovered"),
        ("BIO", "ER versus Golgi distinction", 4, ["bio_rer", "bio_golgi"], "contrast", "recovered"),
        ("DSA", "Recursion base-case diagnosis", 2, ["recursion", "recursion_base_case"], "prerequisite_rewind", "active_blocker"),
        ("DSA", "Dynamic programming state definition", 1, ["dynamic_programming", "dp_state_definition"], "worked_example", "needs_review"),
    ]
    session_ids: list[str] = []
    for n, (subject, title, days_ago, concepts, intervention, result_state) in enumerate(reports):
        sid = str(uuid4())
        start = now - timedelta(days=days_ago, minutes=35 + n * 5)
        end = start + timedelta(minutes=28 + n * 3)
        db.execute(
            "INSERT INTO sessions(session_id,user_id,subject_id,started_at,ended_at,status,goal,source) VALUES(?,?,?,?,?,'completed',?,'system')",
            (sid, user_id, subject, _iso(start), _iso(end), title),
        )
        summary = {
            "synthetic_demo": True,
            "title": title,
            "concepts": concepts,
            "intervention": intervention,
            "result_state": result_state,
            "duration_minutes": round((end-start).total_seconds()/60),
            "learner_visible_summary": (
                f"OVAEL used {intervention.replace('_',' ')} and recorded {result_state.replace('_',' ')} as the demo outcome."
            ),
        }
        db.execute(
            "INSERT INTO session_summaries(session_id,user_id,subject_id,summary_json,created_at) VALUES(?,?,?,?,?)",
            (sid, user_id, subject, db.dumps(summary), _iso(end)),
        )
        session_ids.append(sid)

    focus_concepts = [cid for _, cid, *_ in states]
    run = SyntheticDataGenerator(db).generate(
        scenario="bharat-hackathon-demo",
        seed=260823,
        per_concept=12,
        profile_key="bharat-synthetic-demo",
        concept_ids=focus_concepts,
        profile_context={
            "synthetic_persona": "curious-fast-paced-direct-feedback-interactive",
            "institution_demo_label": "IIITM Gwalior",
            "voice_preference": True,
            "preferred_voice_speed": 1.5,
            "teaching_preferences": [
                "concept-first",
                "why-before-how",
                "worked-example-before-independent-practice-when-blocked",
                "source-grounded-when-available",
            ],
        },
    )
    return {
        "username": username,
        "password": password,
        "user_id": user_id,
        "institution": "IIITM Gwalior",
        "synthetic": True,
        "profile": preferences,
        "concept_state_count": len(states),
        "gap_count": len(gaps),
        "session_reports": len(reports),
        "session_ids": session_ids,
        "sdg_run_id": run.run_id,
        "sdg_episode_count": run.episode_count,
    }
