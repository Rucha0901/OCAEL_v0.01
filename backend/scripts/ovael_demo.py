from __future__ import annotations

import argparse
import json
import os
import sys
import time
import shutil
import tempfile
import socket
from urllib.parse import urlparse
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adaptive_backend.app import create_app
from adaptive_backend.config import Settings
from adaptive_backend.sdg import SyntheticDataGenerator, seed_bharat_demo


SENSITIVE_KEYS = {"access_token", "refresh_token", "token", "api_key", "authorization", "recovery_key"}


def _redact(value):
    if isinstance(value, dict):
        return {k: ("<redacted>" if k.casefold() in SENSITIVE_KEYS else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact(v) for v in value)
    return value


def _safe_line(value: object, limit: int = 500) -> str:
    return str(_redact(value)).replace("\n", " ")[:limit]


def configure(db_path: Path, *, use_model: bool) -> Settings:
    base = Settings.from_env()
    key = os.getenv("OVAEL_MIMO_API_KEY") if use_model else None
    return replace(
        base,
        db_path=db_path,
        storage_dir=db_path.parent / "uploads",
        auth_required=False,
        memory_mode="server",  # explicit demo-only persistence for the prebuilt test account
        seed_demo_domains=True,
        mimo_base_url=os.getenv("OVAEL_MIMO_BASE_URL", "https://token-plan-sgp.xiaomimimo.com/v1"),
        mimo_model=os.getenv("OVAEL_MIMO_MODEL", "mimo-v2.5"),
        mimo_api_key=key,
        request_timeout_seconds=min(base.request_timeout_seconds, 12 if use_model else 45),
    )


def seed(db_path: Path, training_path: Path) -> dict:
    if db_path.exists():
        db_path.unlink()
    settings = configure(db_path, use_model=False)
    app = create_app(settings)
    with TestClient(app) as client:
        result = seed_bharat_demo(client.app.state.services, password="12345678")
        exported = SyntheticDataGenerator(client.app.state.services.db).export_jsonl(result["sdg_run_id"], training_path)
        result["sdg_exported"] = exported
        result["memory_map"] = client.app.state.services.memory.learning_map(result["user_id"]).model_dump(mode="json")
        state_rows = client.app.state.services.db.fetchall(
            """SELECT c.name,c.subject_id,cs.concept_id,cs.mastery_belief,cs.status,cs.recurrence_count
               FROM concept_state cs LEFT JOIN concepts c ON c.concept_id=cs.concept_id
               WHERE cs.user_id=? ORDER BY c.subject_id,c.name""",
            (result["user_id"],),
        )
        result["personalized_states"] = [dict(r) for r in state_rows]
        summaries = client.app.state.services.db.fetchall(
            "SELECT subject_id,summary_json,created_at FROM session_summaries WHERE user_id=? ORDER BY created_at DESC",
            (result["user_id"],),
        )
        result["session_summaries"] = [
            {"subject_id": r["subject_id"], "created_at": r["created_at"], **client.app.state.services.db.loads(r["summary_json"], {})}
            for r in summaries
        ]
        return result


def write_demo_log(result: dict, path: Path) -> None:
    graph = result["memory_map"]
    node_lines = []
    for state in result.get("personalized_states", []):
        node_lines.append(
            f"| {state.get('name') or state.get('concept_id')} | {state.get('subject_id')} | {state.get('status')} | "
            f"{float(state.get('mastery_belief', 0)):.2f} | {state.get('recurrence_count', 0)} |"
        )
    sessions = "\n".join(
        f"- **{s.get('title','Session')}** ({s.get('subject_id')}): {s.get('learner_visible_summary')}"
        for s in result.get("session_summaries", [])
    )
    text = f"""# Bharat Synthetic Demo Account

This is deliberately synthetic hackathon data. It is not a psychological assessment or a claim about the real learner.

## Login

```text
Username: Bharat
Password: 12345678
```

The weak password is accepted only because this account is pre-seeded in the isolated demo database. Normal OVAEL registration keeps the stronger password policy.

## Demo profile

- Display name: Bharat
- Institution: IIITM Gwalior
- Role: learner
- Voice teaching enabled in synthetic preferences
- Preferred demo playback rate: 1.5×
- Synthetic demo persona: curious, fast-paced, direct-feedback friendly, interactive/problem-solving oriented
- Teaching preference examples: concept-first, why-before-how, worked example before independent practice when blocked, source grounding where available

## Personalized learning graph

| Concept | Subject | State | Demo mastery signal | Recurrences |
|---|---|---|---:|---:|
{chr(10).join(node_lines)}

## Active synthetic gap hypotheses

- Recursion → recursive base cases
- Dynamic programming → state definition
- Biology peroxisome → lysosome/peroxisome function confusion

## Session reports

{sessions}

## SDG training corpus

- SDG run ID: `{result['sdg_run_id']}`
- Structured synthetic teaching episodes: **{result['sdg_episode_count']}**
- Exported records: **{result['sdg_exported']}**
- Synthetic episodes are kept separate from empirical learner events and calibration data.
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def benchmark(db_path: Path, report_path: Path, *, use_model: bool, voice_roundtrip: bool) -> dict:
    # Benchmarks run against an isolated clone so the canonical Bharat demo graph
    # remains deterministic for the hackathon demonstration.
    if not db_path.exists():
        raise FileNotFoundError(f"Seed the demo database first: {db_path}")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    bench_dir = Path(tempfile.mkdtemp(prefix="ovael_benchmark_"))
    bench_db = bench_dir / "benchmark.db"
    shutil.copy2(db_path, bench_db)

    requested_live_model = bool(use_model)
    provider_reachable = True
    provider_reason = "not requested"
    if requested_live_model:
        key_present = bool(os.getenv("OVAEL_MIMO_API_KEY"))
        host = urlparse(os.getenv("OVAEL_MIMO_BASE_URL", "https://token-plan-sgp.xiaomimimo.com/v1")).hostname
        if not key_present:
            provider_reachable = False
            provider_reason = "OVAEL_MIMO_API_KEY is not set"
        elif not host:
            provider_reachable = False
            provider_reason = "MiMo base URL has no hostname"
        else:
            try:
                socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
                provider_reason = "DNS preflight succeeded"
            except OSError as exc:
                provider_reachable = False
                provider_reason = f"runner network/DNS unavailable: {type(exc).__name__}"

    effective_model = requested_live_model and provider_reachable
    settings = configure(bench_db, use_model=effective_model)
    app = create_app(settings)
    checks: list[dict] = []

    def run(name, fn):
        start = time.perf_counter()
        try:
            detail = fn()
            checks.append({"name": name, "status": "PASS", "latency_ms": round((time.perf_counter()-start)*1000, 1), "detail": _safe_line(detail)})
            return detail
        except Exception as exc:
            checks.append({"name": name, "status": "FAIL", "latency_ms": round((time.perf_counter()-start)*1000, 1), "detail": f"{type(exc).__name__}: {_safe_line(exc)}"})
            return None

    if requested_live_model and not provider_reachable:
        checks.append({
            "name": "MiMo live-provider preflight",
            "status": "BLOCKED",
            "latency_ms": 0.0,
            "detail": provider_reason,
        })

    with TestClient(app) as client:
        run("health", lambda: client.get("/v1/health").json())
        login = run("Bharat authentication", lambda: client.post("/v1/auth/login", json={"username":"Bharat","password":"12345678"}).json())
        if not login or "access_token" not in login:
            raise RuntimeError("Demo authentication did not return an access token")
        headers = {"Authorization": f"Bearer {login['access_token']}"}
        run("profile projection", lambda: client.get("/v1/profile", headers=headers).json())
        run("personalized graph", lambda: {"nodes": len(client.get("/v1/memory/map", headers=headers).json().get("nodes", []))})
        run("agent registry", lambda: client.get("/v1/system/agents", headers=headers).json())

        def agent_orchestration_check():
            plan = app.state.services.x.plan(
                user_id=login["user_id"],
                subject_id="DSA",
                concept_id="recursion",
                requested_mode="study",
                query="Teach recursion and repair my base-case gap.",
            )
            specialists = {
                "Sam": [x["specialist"] for x in plan.sam.specialists],
                "Carl": [x["specialist"] for x in plan.carl.specialists],
                "Trav": [x["specialist"] for x in plan.trav.specialists],
                "X": [x["specialist"] for x in plan.x_specialists],
            }
            total = sum(len(v) for v in specialists.values())
            if total > 12 or any(len(v) > 3 for v in specialists.values()):
                raise RuntimeError("specialist budget invariant violated")
            return {
                "target_concept": plan.target_concept_id,
                "action": plan.action,
                "specialists": specialists,
                "specialist_total": total,
            }

        run("X/Sam/Carl/Trav orchestration", agent_orchestration_check)
        run(
            "subject-agent factory",
            lambda: {
                "subject": app.state.services.subject_agents.build(subject_id="BIO", course_id=None).subject_name,
                "dynamic": app.state.services.subject_agents.build(subject_id="BIO", course_id=None).dynamic,
                "tools": list(app.state.services.subject_agents.build(subject_id="BIO", course_id=None).tools),
            },
        )
        run(
            "SDG isolation",
            lambda: {
                "synthetic_episodes": app.state.services.db.fetchone("SELECT COUNT(*) AS n FROM synthetic_teaching_episodes")["n"],
                "empirical_events_tagged_sdg": app.state.services.db.fetchone(
                    "SELECT COUNT(*) AS n FROM learning_events WHERE metadata_json LIKE '%synthetic_teaching_episodes%'"
                )["n"],
            },
        )

        # Teacher/classroom flow: role separation, enrollment and privacy-minimized gap sharing.
        teacher_reg = run(
            "teacher account and class creation",
            lambda: client.post(
                "/v1/auth/register",
                json={"username": "HackathonTeacher", "password": "teacher-demo-password-2026", "role": "teacher"},
            ).json(),
        )
        teacher_headers = None
        class_course = None
        if teacher_reg and teacher_reg.get("access_token"):
            teacher_headers = {"Authorization": f"Bearer {teacher_reg['access_token']}"}
            class_course = run(
                "teacher class course",
                lambda: client.post(
                    "/v1/courses", headers=teacher_headers,
                    json={"name": "Biology Hackathon Class", "subject_id": "BIO", "course_kind": "class"},
                ).json(),
            )
        if teacher_headers and class_course and class_course.get("course_id"):
            class_id = class_course["course_id"]
            run(
                "teacher enrollment",
                lambda: client.post(
                    f"/v1/courses/{class_id}/enroll", headers=teacher_headers, json={"learner_username": "Bharat"}
                ).json(),
            )
            run(
                "learner privacy-minimized progress share",
                lambda: client.post(
                    f"/v1/courses/{class_id}/progress-snapshot", headers=headers,
                    json={
                        "graph_version": "bharat-demo-graph",
                        "concept_states": [
                            {"concept_id": "bio_golgi", "status": "recovered", "raw_answer": "must be removed"},
                            {"concept_id": "bio_peroxisome", "status": "active_blocker"},
                        ],
                        "gap_summary": [{"concept_id": "bio_peroxisome", "state": "active_blocker", "transcript": "must be removed"}],
                        "shared_with_teacher": True,
                    },
                ).json(),
            )
            run(
                "teacher aggregate gap view",
                lambda: client.get(f"/v1/teacher/courses/{class_id}/gaps", headers=teacher_headers).json(),
            )

        # Unlimited subject + textbook/material -> graph -> agent factory flow.
        dynamic_course = run(
            "runtime subject creation",
            lambda: client.post(
                "/v1/courses", headers=headers,
                json={
                    "name": "Bharat Systems Thinking Demo",
                    "subject_name": "Systems Thinking",
                    "description": "Synthetic runtime subject used only by the final benchmark.",
                    "course_kind": "personal",
                },
            ).json(),
        )
        if dynamic_course and dynamic_course.get("course_id"):
            dyn_id = dynamic_course["course_id"]
            material = (
                "Feedback loops connect causes and effects over time. Positive feedback reinforces change, while "
                "negative feedback counteracts change. A stock accumulates flows, and delays can create oscillation. "
                "Causal-loop diagrams should distinguish correlation from a supported causal relationship."
            )
            uploaded = run(
                "material upload and extraction",
                lambda: client.post(
                    f"/v1/documents/upload?course_id={dyn_id}", headers=headers,
                    files={"file": ("systems-thinking.txt", material.encode("utf-8"), "text/plain")},
                ).json(),
            )
            if uploaded and uploaded.get("document_id"):
                proposal = run(
                    "material-to-course-graph proposal",
                    lambda: client.post(
                        f"/v1/courses/{dyn_id}/graph/proposals", headers=headers,
                        json={"document_ids": [uploaded["document_id"]], "objective": "Teach feedback loops, stocks, flows and delays"},
                    ).json(),
                )
                if proposal and proposal.get("revision_id"):
                    approved = run(
                        "reviewed course-graph approval",
                        lambda: client.post(
                            f"/v1/courses/{dyn_id}/graph/proposals/{proposal['revision_id']}/approve", headers=headers
                        ).json(),
                    )
                    if approved and approved.get("subject_id"):
                        run(
                            "dynamic subject-agent factory",
                            lambda: client.get(f"/v1/courses/{dyn_id}/agent-spec", headers=headers).json(),
                        )
                        run(
                            "course-scoped uploaded-material retrieval",
                            lambda: client.post(
                                "/v1/retrieval/search", headers=headers,
                                json={
                                    "query": "feedback loops and delays",
                                    "subject_id": approved["subject_id"],
                                    "concept_ids": [],
                                    "course_id": dyn_id,
                                    "limit": 5,
                                },
                            ).json(),
                        )

        # MCP remains bounded/scoped and must never be raw database access.
        mcp_connection = run(
            "scoped MCP connection",
            lambda: client.post(
                "/v1/connections/mcp", headers=headers,
                json={
                    "client_name": "Hackathon External Tutor",
                    "scopes": ["learning.context.read", "learning.map.read", "learning.materials.search", "learning.session.create", "learning.session.interact"],
                    "days": 1,
                },
            ).json(),
        )
        if mcp_connection:
            run("MCP connection listing", lambda: client.get("/v1/connections/mcp", headers=headers).json())
        if effective_model:
            run(
                "MiMo text generation live",
                lambda: {
                    "provider": app.state.services.models.route_chat(
                        "teaching",
                        system="You are a connectivity benchmark. Answer only with OVAEL_OK.",
                        user="Return OVAEL_OK.",
                        temperature=0.0,
                        # MiMo-V2.5 counts internal reasoning and the visible
                        # answer in the same completion budget. Keep this probe
                        # small, but leave enough room for the requested text.
                        max_tokens=256,
                    ).provider,
                    "response": "received",
                },
            )
        run(
            "RAG retrieval",
            lambda: client.post(
                "/v1/retrieval/search", headers=headers,
                json={"query":"Golgi modifies and sorts proteins","subject_id":"BIO","concept_ids":["bio_golgi"],"limit":5},
            ).json(),
        )
        # Relationship queries can escalate from fast RAG to bounded RLM-style
        # source investigation. Execute exactly once so the benchmark does not
        # duplicate model calls or distort latency/cost measurements.
        def knowledge_check():
            bundle = app.state.services.knowledge.research(
                user_id=login["user_id"],
                subject_id="BIO",
                concept_id="bio_golgi",
                query="Why might a learner confuse rough ER and Golgi, and what relationship separates their roles?",
                force_deep=effective_model,
            )
            return {
                "mode": bundle.mode,
                "hits": len(bundle.hits),
                "subqueries": list(bundle.subqueries),
                "calls_used": bundle.calls_used,
                "depth_used": bundle.depth_used,
                "degraded": bundle.degraded,
                "synthesis_preview": (bundle.synthesis or "")[:200],
            }

        run("RAG/RLM knowledge investigation", knowledge_check)

        session = run(
            "teaching companion start",
            lambda: client.post(
                "/v1/learning/sessions", headers=headers,
                json={"subject_id":"BIO","concept_id":"bio_golgi","mode":"study","goal":"Teach me ER versus Golgi and repair any confusion"},
            ).json(),
        )
        if session and session.get("teaching_session_id"):
            sid = session["teaching_session_id"]

            def voice_lifecycle_check():
                created = client.post(
                    "/v1/voice/calls", headers=headers,
                    json={"teaching_session_id": sid, "playback_rate": 1.5, "voice": "Mia"},
                )
                created.raise_for_status()
                call_id = created.json()["call_id"]
                speed = client.patch(f"/v1/voice/calls/{call_id}/speed?playback_rate=1.75", headers=headers)
                speed.raise_for_status()
                ended = client.post(f"/v1/voice/calls/{call_id}/end", headers=headers)
                ended.raise_for_status()
                return {"started": True, "speed": speed.json()["playback_rate"], "ended": ended.json()["status"]}

            run("voice-call lifecycle", voice_lifecycle_check)
            run(
                "teaching turn / gap-repair loop",
                lambda: client.post(
                    f"/v1/learning/sessions/{sid}/turns",
                    headers={**headers, "Idempotency-Key":"bharat-benchmark-turn-1"},
                    json={"response":"I think the rough ER packages proteins after they are made.","confidence":0.55,"learner_intent":"answer"},
                ).json(),
            )
            run(
                "idempotent replay",
                lambda: client.post(
                    f"/v1/learning/sessions/{sid}/turns",
                    headers={**headers, "Idempotency-Key":"bharat-benchmark-turn-1"},
                    json={"response":"duplicate should not count","learner_intent":"answer"},
                ).json().get("idempotent_replay"),
            )

            def training_export_check():
                from adaptive_backend.training import export_privacy_safe_teaching_episodes
                export_path = bench_dir / "privacy-safe-training.jsonl"
                count = export_privacy_safe_teaching_episodes(bench_db, export_path)
                payload = export_path.read_text(encoding="utf-8") if export_path.exists() else ""
                forbidden = ["Bharat", "I think the rough ER packages proteins after they are made."]
                if any(value in payload for value in forbidden):
                    raise RuntimeError("privacy-safe training export leaked raw learner identity/response")
                return {"records": count, "raw_identity_or_response_leaked": False}

            run("privacy-safe teaching export", training_export_check)

        if voice_roundtrip and effective_model:
            def voice_check():
                phrase = "The Golgi apparatus modifies, sorts and packages proteins."
                wav = app.state.services.speech.synthesize(phrase, voice="Mia", speed=1.0)
                transcript = app.state.services.speech.transcribe(wav, filename="mimo-roundtrip.wav", language="en")
                return {"audio_bytes": len(wav), "transcript": transcript.text}
            run("MiMo TTS→ASR round trip", voice_check)

    total = len(checks)
    passed = sum(1 for c in checks if c["status"] == "PASS")
    blocked = sum(1 for c in checks if c["status"] == "BLOCKED")
    result = {
        "checks": checks, "passed": passed, "blocked": blocked, "total": total,
        "model_requested": requested_live_model, "model_enabled": effective_model,
        "voice_roundtrip": bool(voice_roundtrip and effective_model),
    }
    lines = [
        "# OVAEL End-to-End Benchmark", "",
        f"- Checks passed: **{passed}/{total}**",
        f"- Checks blocked by environment: **{blocked}**",
        f"- Live model requested: **{requested_live_model}**",
        f"- Live model actually enabled: **{effective_model}**",
        f"- Text model: `{settings.mimo_model}`",
        f"- MiMo base URL: `{settings.mimo_base_url}`",
        f"- Voice round-trip executed: **{bool(voice_roundtrip and effective_model)}**", "",
        "| Check | Result | Latency | Detail |", "|---|---|---:|---|",
    ]
    for c in checks:
        lines.append(f"| {c['name']} | {c['status']} | {c['latency_ms']} ms | {c['detail'].replace('|','/')} |")
    lines += ["", "No API key is written to this report."]
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines)+"\n", encoding="utf-8")
    shutil.rmtree(bench_dir, ignore_errors=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed and benchmark the OVAEL Bharat demo")
    parser.add_argument("--db", type=Path, default=ROOT / "demo" / "bharat_demo.db")
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--live-mimo", action="store_true", help="Use OVAEL_MIMO_API_KEY for real MiMo calls")
    parser.add_argument("--voice-roundtrip", action="store_true")
    args = parser.parse_args()
    do_seed = args.seed or not args.benchmark
    if do_seed:
        result = seed(args.db, ROOT / "demo" / "sdg_training.jsonl")
        write_demo_log(result, ROOT / "logs" / "bharat_demo.md")
        print(json.dumps({k:v for k,v in result.items() if k not in {"memory_map","profile","session_summaries","personalized_states"}}, indent=2))
    if args.benchmark:
        report = ROOT / "logs" / ("mimo_live_benchmark.md" if args.live_mimo else "e2e_benchmark.md")
        result = benchmark(args.db, report, use_model=args.live_mimo, voice_roundtrip=args.voice_roundtrip)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
