from __future__ import annotations

from fastapi.testclient import TestClient

from adaptive_backend.app import create_app


def test_backend_only_api_flow(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        session = client.post(
            "/sessions",
            json={"user_id": "api-user", "subject_id": "BIO", "source": "text"},
        )
        assert session.status_code == 200
        sid = session.json()["session_id"]

        answer = client.post(
            "/theory/answer",
            json={
                "session_id": sid,
                "user_id": "api-user",
                "question_id": "bio_q_golgi_packaging",
                "answer": "RER",
                "input_mode": "text",
            },
        )
        assert answer.status_code == 200
        assert answer.json()["evaluation"]["correct"] is False
        assert "adaptive" in answer.json()

        adaptive = client.post(
            "/adaptive/next",
            json={"user_id": "api-user", "subject_id": "BIO", "concept_id": "bio_golgi"},
        )
        assert adaptive.status_code == 200
        assert adaptive.json()["mode"] in {"diagnostic", "practice"}

        graph = client.get("/memory/map", params={"user_id": "api-user", "subject_id": "BIO"})
        assert graph.status_code == 200
        assert graph.json()["nodes"]

        q = client.get("/questions/bio_q_golgi_packaging")
        assert q.status_code == 200
        assert "answer_key" not in q.json()

        done = client.post(f"/sessions/{sid}/complete")
        assert done.status_code == 200


def test_data_driven_subject_pack_registration(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        payload = {
            "subject_id": "MATH",
            "name": "Mathematics",
            "description": "Test pack",
            "concepts": [
                {"concept_id": "math_add", "name": "Addition"},
                {"concept_id": "math_linear", "name": "Linear Equations"},
            ],
            "edges": [
                {
                    "source_concept_id": "math_add",
                    "target_concept_id": "math_linear",
                    "relation": "PREREQUISITE_OF",
                    "strength": 0.5,
                }
            ],
            "questions": [
                {
                    "question_id": "math_add_easy",
                    "primary_concept_id": "math_add",
                    "stem": "What is 2 + 3?",
                    "answer_type": "short",
                    "answer_key": ["5"],
                    "difficulty": 0.2,
                }
            ],
        }
        created = client.post("/subjects/packs", json=payload)
        assert created.status_code == 200
        assert created.json()["subject_id"] == "MATH"
        subjects = client.get("/subjects")
        assert subjects.status_code == 200
        assert any(s["subject_id"] == "MATH" for s in subjects.json())
