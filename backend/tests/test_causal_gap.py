from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from adaptive_backend.app import create_app


def _register(client: TestClient):
    response = client.post(
        "/v1/auth/register",
        json={"username": "irislearner", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body["user_id"], {"Authorization": f"Bearer {body['access_token']}"}


def _seed_gap(client: TestClient, user_id: str) -> None:
    db = client.app.state.services.db
    now = datetime.now(timezone.utc)
    session_id = str(uuid4())
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO sessions(session_id,user_id,subject_id,started_at,status,source) VALUES(?,?,?,?,?,?)",
            (session_id, user_id, "BIO", (now - timedelta(days=2)).isoformat(), "completed", "system"),
        )
        for index, correct in enumerate((1, 0, 0, 1)):
            conn.execute(
                """
                INSERT INTO learning_events(
                    event_id,session_id,user_id,subject_id,primary_concept_id,concept_ids_json,
                    timestamp,activity_type,input_mode,correct,score,evidence_strength,mistake_type,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(uuid4()), session_id, user_id, "BIO", "bio_peroxisome", db.dumps(["bio_peroxisome"]),
                    (now - timedelta(hours=8 - index)).isoformat(), "practice", "text", correct,
                    float(correct), 0.9, None if correct else "lysosome_peroxisome_confusion",
                    db.dumps({"hint_count": 0, "attempt_count": 1, "transfer": index > 1}),
                ),
            )
        conn.execute(
            """
            INSERT INTO concept_state(user_id,concept_id,mastery_belief,confidence,evidence_count,last_observed_at,status,recurrence_count,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (user_id, "bio_peroxisome", 0.43, 0.81, 4, now.isoformat(), "active_blocker", 2, now.isoformat()),
        )
        conn.execute(
            """
            INSERT INTO gap_hypotheses(user_id,target_concept_id,gap_concept_id,probability,source,evidence_json,updated_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (user_id, "bio_peroxisome", "bio_lysosome", 0.76, "misconception", "[]", now.isoformat()),
        )


def test_iris_forensics_is_isolated_persisted_and_exportable(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        user_id, headers = _register(client)
        _seed_gap(client, user_id)

        analyzed = client.post("/v1/gaps/causal/analyze", headers=headers, json={"subject_id": "BIO", "max_cases": 6})
        assert analyzed.status_code == 200, analyzed.text
        report = analyzed.json()
        assert report["agent_trace"]["orchestrator"] == "X"
        assert report["agent_trace"]["lead"] == "IRIS"
        assert len(report["agent_trace"]["specialists"]) <= 3
        assert report["cases"]
        case = report["cases"][0]
        assert case["cause_type"] == "misconception"
        assert case["calibration_state"] == "heuristic_v1"
        assert case["evidence_quality"]["raw_response_returned"] is False
        assert case["micrograph"]["nodes"]
        assert "response_text" not in str(case)

        stored = client.get("/v1/gaps/causal?subject_id=BIO", headers=headers)
        assert stored.status_code == 200
        assert stored.json()["cases"][0]["case_id"] == case["case_id"]

        plot = client.get(f"/v1/gaps/causal/{case['case_id']}/plot.png", headers=headers)
        assert plot.status_code == 200, plot.text
        assert plot.headers["content-type"] == "image/png"
        assert plot.content.startswith(b"\x89PNG")

        challenged = client.post(
            f"/v1/gaps/causal/{case['case_id']}/challenge",
            headers=headers,
            json={"reason": "I can distinguish these in a different representation."},
        )
        assert challenged.status_code == 200, challenged.text
        assert challenged.json()["abstained"] is True
        assert challenged.json()["status"] == "challenged"
        assert challenged.json()["confidence"] <= 0.45

        agents = client.get("/v1/system/agents", headers=headers).json()
        assert any(lead["name"] == "IRIS" for lead in agents["leads"])


def test_iris_respects_personalization_pause(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        _user_id, headers = _register(client)
        assert client.post("/v1/memory/personalization", headers=headers, json={"enabled": False}).status_code == 200
        response = client.post("/v1/gaps/causal/analyze", headers=headers, json={})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "PERSONALIZATION_PAUSED"


def test_subject_scopes_have_independent_counts_and_stale_cases_are_reconciled(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        user_id, headers = _register(client)
        _seed_gap(client, user_id)
        db = client.app.state.services.db
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            """INSERT INTO concept_state(
                 user_id,concept_id,mastery_belief,confidence,evidence_count,last_observed_at,status,recurrence_count,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (user_id, "recursion", 0.5, 0.0, 0, None, "unknown", 0, now),
        )

        biology = client.post("/v1/gaps/causal/analyze", headers=headers, json={"subject_id": "BIO"}).json()
        dsa = client.post("/v1/gaps/causal/analyze", headers=headers, json={"subject_id": "DSA"}).json()
        assert biology["summary"]["case_count"] == 1
        assert dsa["summary"]["case_count"] == 0
        assert all(case["subject_id"] == "BIO" for case in biology["cases"])

        db.execute(
            "UPDATE concept_state SET mastery_belief=.95,status='verified',recurrence_count=0,updated_at=? WHERE user_id=? AND concept_id='bio_peroxisome'",
            (now, user_id),
        )
        refreshed = client.post("/v1/gaps/causal/analyze", headers=headers, json={"subject_id": "BIO"}).json()
        stored = client.get("/v1/gaps/causal?subject_id=BIO", headers=headers).json()
        assert refreshed["summary"]["case_count"] == 0
        assert stored["summary"]["case_count"] == 0


def test_learning_turn_publishes_iris_case_to_account_in_local_first_mode(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        _user_id, headers = _register(client)
        started = client.post(
            "/v1/learning/sessions",
            headers=headers,
            json={"subject_id": "BIO", "concept_id": "bio_cell", "mode": "study", "source_client": "web", "context_capsule": {}},
        )
        assert started.status_code == 200, started.text
        session_id = started.json()["teaching_session_id"]
        turn = client.post(
            f"/v1/learning/sessions/{session_id}/turns",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"response": "A cell is the smallest structural unit of life.", "learner_intent": "answer", "input_mode": "text"},
        )
        assert turn.status_code == 200, turn.text
        update = turn.json()["gap_update"]
        assert update["subject_id"] == "BIO"
        assert update["agent_trace"]["lead"] == "IRIS"
        stored = client.get("/v1/gaps/causal?subject_id=BIO", headers=headers).json()
        assert stored["summary"]["case_count"] == update["summary"]["case_count"]
