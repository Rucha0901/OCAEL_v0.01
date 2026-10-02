from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from adaptive_backend.app import create_app
from adaptive_backend.training import export_privacy_safe_teaching_episodes


def register(client: TestClient, username: str, role: str = "learner") -> dict[str, str]:
    r = client.post("/v1/auth/register", json={"username": username, "password": "correct-horse-battery", "role": role})
    assert r.status_code == 200, r.text
    body = r.json()
    return {"Authorization": f"Bearer {body['access_token']}"}


def test_roles_dynamic_subject_enrollment_graph_and_teacher_progress(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        teacher = register(client, "teacher01", "teacher")
        learner = register(client, "learner01", "learner")

        created = client.post(
            "/v1/courses", headers=teacher,
            json={"name": "Cell Biology", "subject_name": "Biology from my textbook", "course_kind": "class"},
        )
        assert created.status_code == 200, created.text
        course = created.json(); course_id = course["course_id"]; subject_id = course["subject_id"]
        assert subject_id.startswith("USR:")

        enrolled = client.post(f"/v1/courses/{course_id}/enroll", headers=teacher, json={"learner_username": "learner01"})
        assert enrolled.status_code == 200, enrolled.text

        upload = client.post(
            f"/v1/documents/upload?course_id={course_id}", headers=teacher,
            files={"file": ("cell.txt", b"Endoplasmic reticulum synthesizes and begins processing proteins. The Golgi apparatus modifies, sorts and packages proteins received from the ER.", "text/plain")},
        )
        assert upload.status_code == 200, upload.text
        doc_id = upload.json()["document_id"]
        db = client.app.state.services.db
        chunk = db.fetchone("SELECT chunk_id FROM knowledge_chunks WHERE document_id=?", (doc_id,))
        assert chunk

        proposed = client.post(
            f"/v1/courses/{course_id}/graph/proposals", headers=teacher,
            json={"document_ids": [doc_id], "objective": "Teach ER and Golgi without confusing their roles"},
        )
        assert proposed.status_code == 200, proposed.text
        rev = proposed.json()["revision_id"]
        graph = {
            "nodes": [
                {"key": "rough_er", "name": "Rough Endoplasmic Reticulum", "description": "Protein synthesis and early processing", "evaluator_family": "theory_rubric", "source_chunk_ids": [chunk["chunk_id"]]},
                {"key": "golgi", "name": "Golgi Apparatus", "description": "Modification, sorting and packaging", "evaluator_family": "theory_rubric", "source_chunk_ids": [chunk["chunk_id"]]},
            ],
            "edges": [{"source": "rough_er", "target": "golgi", "relation": "PREREQUISITE_OF"}],
        }
        edited = client.put(f"/v1/courses/{course_id}/graph/proposals/{rev}", headers=teacher, json={"graph": graph})
        assert edited.status_code == 200, edited.text
        approved = client.post(f"/v1/courses/{course_id}/graph/proposals/{rev}/approve", headers=teacher)
        assert approved.status_code == 200, approved.text
        concept_ids = approved.json()["concept_ids"]
        assert len(concept_ids) == 2

        # Enrolled learner can use teacher-owned material but not unrelated private material.
        search = client.post(
            "/v1/retrieval/search", headers=learner,
            json={"query": "Golgi packages proteins", "subject_id": subject_id, "course_id": course_id, "concept_ids": [concept_ids[1]], "limit": 5},
        )
        assert search.status_code == 200, search.text
        assert any("Golgi" in hit["content"] for hit in search.json())

        started = client.post(
            "/v1/learning/sessions", headers=learner,
            json={"subject_id": subject_id, "concept_id": concept_ids[1], "course_id": course_id, "mode": "study"},
        )
        assert started.status_code == 200, started.text
        assert started.json()["move"]["action"] in {"explain", "worked_example", "contrast", "prerequisite_rewind", "hint"}

        shared = client.post(
            f"/v1/courses/{course_id}/progress-snapshot", headers=learner,
            json={
                "graph_version": "g1",
                "concept_states": [{"concept_id": concept_ids[0], "status": "stable"}, {"concept_id": concept_ids[1], "status": "needs_review"}],
                "gap_summary": [{"concept_id": concept_ids[1], "state": "active_gap"}],
                "shared_with_teacher": True,
            },
        )
        assert shared.status_code == 200, shared.text
        gaps = client.get(f"/v1/teacher/courses/{course_id}/gaps", headers=teacher)
        assert gaps.status_code == 200, gaps.text
        assert gaps.json()["gap_counts"][concept_ids[1]] == 1


def test_voice_call_control_uses_same_teaching_session(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        h = register(client, "voicelearner")
        started = client.post("/v1/learning/sessions", headers=h, json={"subject_id": "BIO", "concept_id": "bio_golgi", "mode": "study"})
        assert started.status_code == 200
        call = client.post("/v1/voice/calls", headers=h, json={"teaching_session_id": started.json()["teaching_session_id"], "playback_rate": 1.5})
        assert call.status_code == 200, call.text
        cid = call.json()["call_id"]
        speed = client.patch(f"/v1/voice/calls/{cid}/speed?playback_rate=1.75", headers=h)
        assert speed.status_code == 200, speed.text
        assert speed.json()["playback_rate"] == 1.75
        ended = client.post(f"/v1/voice/calls/{cid}/end", headers=h)
        assert ended.status_code == 200


def test_privacy_safe_training_export_contains_no_raw_response_or_user(settings, tmp_path: Path):
    app = create_app(settings)
    with TestClient(app) as client:
        h = register(client, "traininglearner")
        started = client.post("/v1/learning/sessions", headers=h, json={"subject_id": "BIO", "concept_id": "bio_golgi", "mode": "study"}).json()
        raw_secret_response = "MY_PRIVATE_STUDENT_RESPONSE_12345"
        turn = client.post(
            f"/v1/learning/sessions/{started['teaching_session_id']}/turns",
            headers={**h, "Idempotency-Key": "train-export-turn"},
            json={"response": raw_secret_response},
        )
        assert turn.status_code == 200
    out = tmp_path / "episodes.jsonl"
    count = export_privacy_safe_teaching_episodes(settings.db_path, out)
    assert count >= 1
    text = out.read_text(encoding="utf-8")
    assert raw_secret_response not in text
    assert "traininglearner" not in text
    for line in text.splitlines():
        row = json.loads(line)
        assert row["constraints"]["privacy_safe"] is True


def test_dynamic_difficulty_metadata_is_consumed(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        h = register(client, "difficultylearner")
        s = client.app.state.services
        # Directly exercise the canonical event metadata path regression.
        from adaptive_backend.schemas import EvidenceEvent
        session = s.memory.start_session(user_id="difficulty-user", subject_id="BIO", goal=None, source="system")
        event = EvidenceEvent(
            session_id=session.session_id, user_id="difficulty-user", subject_id="BIO",
            concept_ids=["bio_golgi"], primary_concept_id="bio_golgi", activity_type="practice",
            correct=True, score=1.0, evidence_strength=1.0,
            metadata={"difficulty": 0.88, "irt": {"a": 1.4, "c": 0.2}},
        )
        assert s.adaptive._event_difficulty(event) == 0.88
        assert s.adaptive._event_discrimination(event) == 1.4
        assert s.adaptive._event_guessing(event) == 0.2


def test_study_intent_can_request_reteach_instead_of_forced_quiz(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        h = register(client, "intentlearner")
        started = client.post("/v1/learning/sessions", headers=h, json={"subject_id": "BIO", "concept_id": "bio_golgi", "mode": "study"}).json()
        sid = started["teaching_session_id"]
        turn = client.post(
            f"/v1/learning/sessions/{sid}/turns",
            headers={**h, "Idempotency-Key": "intent-reteach"},
            json={"response": "I do not understand that", "learner_intent": "need_explanation"},
        )
        assert turn.status_code == 200, turn.text
        assert turn.json()["move"]["action"] == "explain"


def test_private_dynamic_subject_not_visible_to_unrelated_user(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = register(client, "subjectowner")
        outsider = register(client, "subjectoutsider")
        course = client.post("/v1/courses", headers=owner, json={"name": "Private Linguistics", "subject_name": "My Private Language Notes"}).json()
        owner_subjects = client.get("/v1/subjects", headers=owner).json()
        outsider_subjects = client.get("/v1/subjects", headers=outsider).json()
        assert any(x["subject_id"] == course["subject_id"] for x in owner_subjects)
        assert all(x["subject_id"] != course["subject_id"] for x in outsider_subjects)
        blocked = client.post(
            "/v1/learning/sessions", headers=outsider,
            json={"subject_id": course["subject_id"], "concept_id": "nonexistent", "mode": "study"},
        )
        assert blocked.status_code == 400


def test_approved_graph_isolates_builtin_subject_and_replaces_stale_edges(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        teacher = register(client, "graphteacher", "teacher")
        course = client.post(
            "/v1/courses", headers=teacher,
            json={"name": "My Biology Book", "subject_id": "BIO", "course_kind": "class"},
        ).json()
        course_id = course["course_id"]
        upload = client.post(
            f"/v1/documents/upload?course_id={course_id}", headers=teacher,
            files={"file": ("bio.txt", b"Rough ER handles early protein processing. Golgi modifies and sorts proteins.", "text/plain")},
        )
        assert upload.status_code == 200, upload.text
        doc_id = upload.json()["document_id"]
        db = client.app.state.services.db
        chunk_id = db.fetchone("SELECT chunk_id FROM knowledge_chunks WHERE document_id=?", (doc_id,))["chunk_id"]

        def create_revision(with_edge: bool):
            proposed = client.post(
                f"/v1/courses/{course_id}/graph/proposals", headers=teacher,
                json={"document_ids": [doc_id], "objective": "Map ER and Golgi"},
            )
            assert proposed.status_code == 200, proposed.text
            rev = proposed.json()["revision_id"]
            graph = {
                "nodes": [
                    {"key": "rough_er", "name": "Rough ER", "source_chunk_ids": [chunk_id]},
                    {"key": "golgi", "name": "Golgi", "source_chunk_ids": [chunk_id]},
                ],
                "edges": ([{"source": "rough_er", "target": "golgi", "relation": "PREREQUISITE_OF"}] if with_edge else []),
            }
            assert client.put(
                f"/v1/courses/{course_id}/graph/proposals/{rev}", headers=teacher, json={"graph": graph}
            ).status_code == 200
            approved = client.post(f"/v1/courses/{course_id}/graph/proposals/{rev}/approve", headers=teacher)
            assert approved.status_code == 200, approved.text
            return approved.json()

        first = create_revision(True)
        assert first["subject_id"].startswith("USR:")
        assert first["subject_id"] != "BIO"
        current_course = db.fetchone("SELECT subject_id FROM courses WHERE course_id=?", (course_id,))
        assert current_course["subject_id"] == first["subject_id"]
        current_chunk = db.fetchone("SELECT subject_id FROM knowledge_chunks WHERE chunk_id=?", (chunk_id,))
        assert current_chunk["subject_id"] == first["subject_id"]
        assert db.fetchone("SELECT 1 FROM concepts WHERE subject_id='BIO' AND concept_id LIKE 'cg:%'") is None
        prefix = f"cg:{course_id.replace('-', '')}:"
        assert db.fetchone(
            "SELECT 1 FROM concept_edges WHERE source_concept_id LIKE ? AND relation='PREREQUISITE_OF'", (prefix + "%",)
        ) is not None

        second = create_revision(False)
        assert second["subject_id"] == first["subject_id"]
        assert db.fetchone(
            "SELECT 1 FROM concept_edges WHERE (source_concept_id LIKE ? OR target_concept_id LIKE ?) AND relation='PREREQUISITE_OF'",
            (prefix + "%", prefix + "%"),
        ) is None


def test_course_graph_rejects_prerequisite_cycle(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = register(client, "cycleteacher", "teacher")
        course = client.post(
            "/v1/courses", headers=owner,
            json={"name": "Cycle Test", "subject_name": "Cycle Subject", "course_kind": "class"},
        ).json()
        upload = client.post(
            f"/v1/documents/upload?course_id={course['course_id']}", headers=owner,
            files={"file": ("c.txt", b"Concept A. Concept B.", "text/plain")},
        ).json()
        db = client.app.state.services.db
        chunk = db.fetchone("SELECT chunk_id FROM knowledge_chunks WHERE document_id=?", (upload["document_id"],))["chunk_id"]
        proposed = client.post(
            f"/v1/courses/{course['course_id']}/graph/proposals", headers=owner,
            json={"document_ids": [upload["document_id"]], "objective": "two concepts"},
        ).json()
        cyclic = {
            "nodes": [
                {"key": "a", "name": "A", "source_chunk_ids": [chunk]},
                {"key": "b", "name": "B", "source_chunk_ids": [chunk]},
            ],
            "edges": [
                {"source": "a", "target": "b", "relation": "PREREQUISITE_OF"},
                {"source": "b", "target": "a", "relation": "PREREQUISITE_OF"},
            ],
        }
        response = client.put(
            f"/v1/courses/{course['course_id']}/graph/proposals/{proposed['revision_id']}",
            headers=owner, json={"graph": cyclic},
        )
        assert response.status_code == 400, response.text


def test_outsider_cannot_retrieve_guessed_private_course(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = register(client, "retrievalowner")
        outsider = register(client, "retrievaloutsider")
        course = client.post(
            "/v1/courses", headers=owner,
            json={"name": "Secret Notes", "subject_name": "Private Subject"},
        ).json()
        client.post(
            f"/v1/documents/upload?course_id={course['course_id']}", headers=owner,
            files={"file": ("secret.txt", b"PRIVATE_SOURCE_MARKER_92", "text/plain")},
        )
        response = client.post(
            "/v1/retrieval/search", headers=outsider,
            json={"query": "PRIVATE_SOURCE_MARKER_92", "subject_id": course["subject_id"], "course_id": course["course_id"]},
        )
        assert response.status_code == 404


def test_account_delete_cascades_new_v002_user_tables(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        teacher = register(client, "delete_teacher", "teacher")
        learner = register(client, "delete_learner")
        db = client.app.state.services.db
        learner_id = db.fetchone("SELECT user_id FROM users WHERE username='delete_learner'")["user_id"]
        course = client.post(
            "/v1/courses", headers=teacher,
            json={"name": "Delete Class", "subject_id": "BIO", "course_kind": "class"},
        ).json()
        course_id = course["course_id"]
        assert client.post(
            f"/v1/courses/{course_id}/enroll", headers=teacher, json={"learner_username": "delete_learner"}
        ).status_code == 200
        started = client.post(
            "/v1/learning/sessions", headers=learner,
            json={"subject_id": "BIO", "concept_id": "bio_golgi", "course_id": course_id, "mode": "study"},
        ).json()
        assert client.post(
            "/v1/voice/calls", headers=learner,
            json={"teaching_session_id": started["teaching_session_id"], "playback_rate": 1.0},
        ).status_code == 200
        assert client.post(
            f"/v1/courses/{course_id}/progress-snapshot", headers=learner,
            json={"graph_version": "builtin", "concept_states": [{"concept_id": "bio_golgi", "status": "developing"}], "gap_summary": [], "shared_with_teacher": True},
        ).status_code == 200
        deleted = client.request(
            "DELETE", "/v1/auth/account", headers=learner, json={"password": "correct-horse-battery"}
        )
        assert deleted.status_code == 200, deleted.text
        assert db.fetchone("SELECT 1 FROM users WHERE user_id=?", (learner_id,)) is None
        assert db.fetchone("SELECT 1 FROM course_enrollments WHERE learner_user_id=?", (learner_id,)) is None
        assert db.fetchone("SELECT 1 FROM progress_snapshots WHERE learner_user_id=?", (learner_id,)) is None
        assert db.fetchone("SELECT 1 FROM voice_calls WHERE owner_user_id=?", (learner_id,)) is None


def test_internal_chaos_telemetry_and_dpo_routes_are_absent(settings):
    app = create_app(settings)
    paths = set()
    for route in app.routes:
        if hasattr(route, "path"):
            paths.add(route.path)
        elif hasattr(route, "routes"):
            paths.update(r.path for r in route.routes if hasattr(r, "path"))
    forbidden = {
        "/v1/chaos/inject",
        "/v1/chaos/status",
        "/v1/chaos/reset",
        "/v1/telemetry/events",
        "/v1/telemetry/dpo-dataset",
    }
    assert forbidden.isdisjoint(paths)


def test_specialist_trace_has_purpose_contract_but_not_system_prompt(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        h = register(client, "specialistlearner")
        s = client.app.state.services
        from adaptive_backend.agents import SpecialistBudget
        finding = s.sam.analyze(user_id="ephemeral-user", subject_id="BIO", concept_id="bio_golgi", budget=SpecialistBudget())
        for specialist in finding.specialists:
            assert specialist["lead"] == "Sam"
            assert specialist["purpose"]
            assert specialist["prompt_contract"] == "ephemeral-v1"
            assert len(specialist["prompt_fingerprint"]) == 16
            assert "system_prompt" not in specialist


def test_sdg_bharat_demo_is_isolated_and_authenticates(settings):
    from adaptive_backend.sdg import seed_bharat_demo
    app = create_app(settings)
    with TestClient(app) as client:
        result = seed_bharat_demo(client.app.state.services, password="12345678")
        assert result["username"] == "Bharat"
        assert result["synthetic"] is True
        assert result["concept_state_count"] >= 10
        assert result["sdg_episode_count"] > 0
        login = client.post("/v1/auth/login", json={"username": "Bharat", "password": "12345678"})
        assert login.status_code == 200, login.text
        h = {"Authorization": f"Bearer {login.json()['access_token']}"}
        graph = client.get("/v1/memory/map", headers=h)
        assert graph.status_code == 200, graph.text
        node_ids = {n["id"] for n in graph.json()["nodes"]}
        assert "recursion_base_case" in node_ids
        assert "bio_golgi" in node_ids
        profile = client.app.state.services.db.fetchone(
            "SELECT institution,preferences_json,synthetic FROM learner_profiles WHERE user_id=?",
            (result["user_id"],),
        )
        assert profile["institution"] == "IIITM Gwalior"
        assert bool(profile["synthetic"]) is True
        profile_api = client.get("/v1/profile", headers=h)
        assert profile_api.status_code == 200, profile_api.text
        assert profile_api.json()["display_name"] == "Bharat"
        assert profile_api.json()["institution"] == "IIITM Gwalior"
        assert profile_api.json()["synthetic"] is True
        # SDG training rows are isolated from empirical learner telemetry/events.
        assert client.app.state.services.db.fetchone(
            "SELECT 1 FROM synthetic_teaching_episodes WHERE run_id=?", (result["sdg_run_id"],)
        ) is not None
        profiles = {r["profile_key"] for r in client.app.state.services.db.fetchall(
            "SELECT DISTINCT profile_key FROM synthetic_teaching_episodes WHERE run_id=?", (result["sdg_run_id"],)
        )}
        assert profiles == {"bharat-synthetic-demo"}
        assert result["sdg_episode_count"] == result["concept_state_count"] * 12
        assert client.app.state.services.db.fetchone(
            "SELECT 1 FROM learning_events WHERE metadata_json LIKE '%synthetic_teaching_episodes%'"
        ) is None


def test_sdg_refuses_to_overwrite_non_synthetic_bharat(settings):
    from adaptive_backend.sdg import seed_bharat_demo
    app = create_app(settings)
    with TestClient(app) as client:
        # Normal production registration creates a non-synthetic account.
        r = client.post("/v1/auth/register", json={"username": "Bharat", "password": "a-strong-demo-password"})
        assert r.status_code == 200
        import pytest
        with pytest.raises(ValueError):
            seed_bharat_demo(client.app.state.services, password="12345678", reset=True)


def test_mimo_asr_openai_compat_payload(settings, monkeypatch):
    import base64
    from dataclasses import replace
    from adaptive_backend.speech import SpeechService
    import adaptive_backend.speech as speech_module

    seen = {}

    class FakeResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return {"choices": [{"message": {"content": "Golgi apparatus"}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            seen["timeout"] = kwargs.get("timeout")
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def post(self, url, *, headers=None, json=None, **kwargs):
            seen.update(url=url, headers=headers, payload=json)
            return FakeResponse()

    monkeypatch.setattr(speech_module.httpx, "Client", FakeClient)
    cfg = replace(
        settings,
        stt_url=None,
        whisper_cpp_bin=None,
        whisper_cpp_model=None,
        mimo_base_url="https://token-plan-sgp.xiaomimimo.com/v1",
        mimo_api_key="test-only-not-real",
        stt_model=None,
    )
    transcript = SpeechService(cfg).transcribe(b"RIFFfakewav", filename="answer.wav", language="en")
    assert transcript.text == "Golgi apparatus"
    assert transcript.engine == "mimo-v2.5-asr"
    assert seen["url"] == "https://token-plan-sgp.xiaomimimo.com/v1/chat/completions"
    assert seen["headers"]["api-key"] == "test-only-not-real"
    assert seen["payload"]["model"] == "mimo-v2.5-asr"
    part = seen["payload"]["messages"][0]["content"][0]
    assert part["type"] == "input_audio"
    assert part["input_audio"]["data"].startswith("data:audio/wav;base64,")
    assert base64.b64decode(part["input_audio"]["data"].split(",", 1)[1]) == b"RIFFfakewav"
    assert seen["payload"]["asr_options"]["language"] == "en"


def test_mimo_vision_ocr_openai_compat_payload(settings, monkeypatch):
    from dataclasses import replace
    from PIL import Image
    from adaptive_backend.ocr import OCRService
    import adaptive_backend.ocr as ocr_module

    seen = {}

    class FakeResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return {"choices": [{"message": {"content": "Recursion stops at the base case."}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            seen["timeout"] = kwargs.get("timeout")
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def post(self, url, *, headers=None, json=None, **kwargs):
            seen.update(url=url, headers=headers, payload=json)
            return FakeResponse()

    monkeypatch.setattr(ocr_module.httpx, "Client", FakeClient)
    cfg = replace(
        settings,
        mimo_base_url="https://token-plan-sgp.xiaomimimo.com/v1",
        mimo_model="mimo-v2.5",
        mimo_api_key="test-only-not-real",
    )
    result = OCRService(cfg)._mimo_extract(Image.new("RGB", (80, 40), "white"))
    assert result.engine == "mimo-v2.5-vision-ocr"
    assert result.text == "Recursion stops at the base case."
    assert seen["url"] == "https://token-plan-sgp.xiaomimimo.com/v1/chat/completions"
    assert seen["headers"]["api-key"] == "test-only-not-real"
    content = seen["payload"]["messages"][1]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_image_upload_runs_ocr_and_indexes_course_text(settings):
    import io
    from PIL import Image
    from fastapi.testclient import TestClient
    from adaptive_backend.app import create_app
    from adaptive_backend.schemas import OCRResult

    app = create_app(settings)
    with TestClient(app) as client:
        auth = client.post(
            "/v1/auth/register",
            json={"username": "ocrlearner", "password": "StrongPass123!", "role": "learner"},
        ).json()
        headers = {"Authorization": f"Bearer {auth['access_token']}"}
        course = client.post(
            "/v1/courses",
            headers=headers,
            json={"name": "OCR Notes", "subject_id": "DSA", "course_kind": "personal"},
        ).json()
        app.state.services.ocr.extract = lambda data, language="eng": OCRResult(
            text="A recursive base case returns without another recursive call.",
            engine="test-vision-ocr",
            confidence=None,
        )
        image = Image.new("RGB", (160, 90), "white")
        payload = io.BytesIO()
        image.save(payload, format="PNG")
        upload = client.post(
            f"/v1/documents/upload?course_id={course['course_id']}",
            headers=headers,
            files={"file": ("recursion-note.png", payload.getvalue(), "image/png")},
        )
        assert upload.status_code == 200, upload.text
        document = upload.json()
        assert document["status"] == "ready"
        page = client.get(f"/v1/documents/{document['document_id']}/pages/1", headers=headers)
        assert page.status_code == 200, page.text
        assert "recursive base case" in page.json()["text"]
        assert page.json()["provenance"]["method"] == "test-vision-ocr"
        hits = client.post(
            "/v1/retrieval/search",
            headers=headers,
            json={
                "query": "base case recursive call",
                "subject_id": "DSA",
                "course_id": course["course_id"],
                "limit": 5,
            },
        )
        assert hits.status_code == 200, hits.text
        assert any(
            hit.get("document_id") == document["document_id"]
            or hit.get("source_id") == document["document_id"]
            for hit in hits.json()
        )
def test_mimo_tts_openai_compat_payload(settings, monkeypatch):
    import base64
    from dataclasses import replace
    from adaptive_backend.speech import SpeechService
    import adaptive_backend.speech as speech_module

    expected = b"fake-wave-bytes"
    seen = {}

    class FakeResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return {"choices": [{"message": {"audio": {"data": base64.b64encode(expected).decode("ascii")}}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def post(self, url, *, headers=None, json=None, **kwargs):
            seen.update(url=url, headers=headers, payload=json)
            return FakeResponse()

    monkeypatch.setattr(speech_module.httpx, "Client", FakeClient)
    cfg = replace(
        settings,
        tts_url=None,
        mimo_base_url="https://token-plan-sgp.xiaomimimo.com/v1",
        mimo_api_key="test-only-not-real",
        tts_model=None,
    )
    audio = SpeechService(cfg).synthesize("Explain the Golgi apparatus.", voice="Mia", speed=1.75)
    assert audio == expected
    assert seen["url"] == "https://token-plan-sgp.xiaomimimo.com/v1/chat/completions"
    assert seen["headers"]["api-key"] == "test-only-not-real"
    assert seen["payload"]["model"] == "mimo-v2.5-tts"
    assert seen["payload"]["messages"][-1] == {"role": "assistant", "content": "Explain the Golgi apparatus."}
    assert "1.75x" in seen["payload"]["messages"][0]["content"]
    assert seen["payload"]["audio"] == {"format": "wav", "voice": "Mia"}


def test_hybrid_rag_rlm_escalates_once_and_stays_bounded(settings, monkeypatch):
    from types import SimpleNamespace
    from adaptive_backend.app import create_app
    from adaptive_backend.rlm import HybridKnowledgeEngine

    app = create_app(settings)
    with TestClient(app) as client:
        s = client.app.state.services
        calls = {"plan": 0, "synthesis": 0}

        monkeypatch.setattr(type(s.models), "tutor_available", property(lambda self: True))

        def fake_json(task, *, system, payload, **kwargs):
            calls["plan"] += 1
            assert task == "planning"
            assert "untrusted" in system.lower()
            return {"subqueries": ["rough ER protein processing", "Golgi protein sorting"]}

        def fake_chat(task, *, system, user, **kwargs):
            calls["synthesis"] += 1
            assert task == "summarization"
            assert "untrusted" in system.lower()
            return SimpleNamespace(text="Rough ER performs synthesis/early processing; Golgi modifies, sorts and packages cargo.")

        monkeypatch.setattr(s.models, "route_json", fake_json)
        monkeypatch.setattr(s.models, "route_chat", fake_chat)
        engine = HybridKnowledgeEngine(s.retrieval, s.models, enabled=True, max_depth=1, max_calls=4)
        bundle = engine.research(
            user_id="rlm-test-user",
            subject_id="BIO",
            concept_id="bio_golgi",
            query="Why can rough ER and Golgi be confused, and how do their roles differ?",
            force_deep=True,
        )
        assert bundle.mode == "rlm_rag"
        assert bundle.depth_used == 1
        assert bundle.calls_used == 2
        assert calls == {"plan": 1, "synthesis": 1}
        assert len(bundle.subqueries) == 2
        assert "Golgi" in (bundle.synthesis or "")


def test_material_scoped_chat_passes_the_learner_message_and_source(settings, monkeypatch):
    import json
    from types import SimpleNamespace
    from adaptive_backend.app import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        headers = register(client, "material-chat-learner")
        course = client.post(
            "/v1/courses", headers=headers,
            json={"name": "Scanned notes", "subject_id": "BIO", "course_kind": "personal"},
        ).json()
        uploaded = client.post(
            f"/v1/documents/upload?course_id={course['course_id']}", headers=headers,
            files={"file": ("scan.txt", b"The scanned page says chlorophyll captures light energy.", "text/plain")},
        )
        assert uploaded.status_code == 200

        services = client.app.state.services
        teaching_payloads: list[dict] = []
        monkeypatch.setattr(type(services.models), "tutor_available", property(lambda self: True))

        def fake_chat(task, *, user, **kwargs):
            if task == "teaching":
                teaching_payloads.append(json.loads(user))
            return SimpleNamespace(text="The reply is grounded in the scanned page.")

        monkeypatch.setattr(services.models, "route_chat", fake_chat)
        started = client.post(
            "/v1/learning/sessions", headers=headers,
            json={
                "subject_id": "BIO", "concept_id": "bio_cell", "course_id": course["course_id"],
                "mode": "study", "goal": "Help me understand this scanned page.",
            },
        )
        assert started.status_code == 200
        assert teaching_payloads
        payload = teaching_payloads[-1]
        assert payload["learner_message"] == "Help me understand this scanned page."
        assert "chlorophyll captures light energy" in payload["trusted_source_excerpt"]


def test_voice_turn_uses_mimo_tts_configuration_and_same_teaching_session(settings, monkeypatch):
    import base64
    from dataclasses import replace
    from adaptive_backend.schemas import SpeechTranscript

    app = create_app(settings)
    with TestClient(app) as client:
        h = register(client, "voice-turn-learner")
        started = client.post(
            "/v1/learning/sessions", headers=h,
            json={"subject_id": "BIO", "concept_id": "bio_golgi", "mode": "study"},
        ).json()
        call = client.post(
            "/v1/voice/calls", headers=h,
            json={"teaching_session_id": started["teaching_session_id"], "playback_rate": 1.5, "voice": "Mia"},
        ).json()
        s = client.app.state.services
        s.speech.settings = replace(
            s.speech.settings,
            tts_url=None,
            mimo_base_url="https://token-plan-sgp.xiaomimimo.com/v1",
            mimo_api_key="test-only-not-real",
        )
        monkeypatch.setattr(
            s.speech, "transcribe",
            lambda data, filename="audio.wav", language=None: SpeechTranscript(
                text="I think the rough ER packages the protein.", engine="test-stt", language="en"
            ),
        )
        rendered = b"WAV-DEMO-RESPONSE"
        synth_calls = []
        monkeypatch.setattr(
            s.speech, "synthesize",
            lambda text, voice=None, speed=1.0: (synth_calls.append((text, voice, speed)) or rendered),
        )
        response = client.post(
            f"/v1/voice/calls/{call['call_id']}/turn",
            headers={**h, "Idempotency-Key": "voice-turn-1"},
            files={"audio": ("answer.wav", b"RIFF-demo", "audio/wav")},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["teaching"]["teaching_session_id"] == started["teaching_session_id"]
        assert body["transcript"]["text"].startswith("I think")
        assert base64.b64decode(body["audio_base64"]) == rendered
        assert synth_calls and synth_calls[0][1:] == ("Mia", 1.5)
