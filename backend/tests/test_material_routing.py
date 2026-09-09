from __future__ import annotations

from fastapi.testclient import TestClient

from adaptive_backend.app import create_app


def _register(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/v1/auth/register",
        json={"username": "materiallearner", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


SPORTS_TEXT = b"""Sports performance and football training
An athlete improves match performance through repeated passing drills, recovery,
team tactics, fitness, and tournament preparation. Coaches compare training load
with match outcomes and adapt the next practice session.
"""


def test_auto_routed_material_creates_a_source_locked_sports_lesson(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        headers = _register(client)
        upload = client.post(
            "/v1/documents/upload?auto_route=true",
            headers=headers,
            files={"file": ("football-training.txt", SPORTS_TEXT, "text/plain")},
        )
        assert upload.status_code == 200, upload.text
        document = upload.json()
        assert document["status"] == "ready"
        assert document["subject_name"] == "Sports"
        assert document["subject_id"].startswith("USR:")
        assert document["course_id"]
        assert document["focus_concept_id"]

        retrieval = client.post(
            "/v1/retrieval/search",
            headers=headers,
            json={
                "query": "training load and match outcomes",
                "subject_id": document["subject_id"],
                "course_id": document["course_id"],
                "concept_ids": [document["focus_concept_id"]],
                "limit": 6,
            },
        )
        assert retrieval.status_code == 200, retrieval.text
        assert any(hit["source_id"] == document["document_id"] for hit in retrieval.json())

        lesson = client.post(
            "/v1/learning/sessions",
            headers=headers,
            json={
                "subject_id": document["subject_id"],
                "concept_id": document["focus_concept_id"],
                "course_id": document["course_id"],
                "goal": "Teach this sports material",
                "mode": "study",
                "source_client": "web",
                "context_capsule": {"launch_document_id": document["document_id"]},
            },
        )
        assert lesson.status_code == 200, lesson.text
        assert lesson.json()["move"]["citations"]


def test_existing_mis_scoped_material_can_be_detected_and_re_routed(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        headers = _register(client)
        course = client.post(
            "/v1/courses",
            headers=headers,
            json={"name": "Personal Biology", "subject_id": "BIO", "course_kind": "personal"},
        ).json()
        upload = client.post(
            f"/v1/documents/upload?course_id={course['course_id']}",
            headers=headers,
            files={"file": ("sports-notes.txt", SPORTS_TEXT, "text/plain")},
        )
        assert upload.status_code == 200, upload.text
        assert upload.json()["subject_id"] == "BIO"

        routed = client.post(f"/v1/documents/{upload.json()['document_id']}/route", headers=headers)
        assert routed.status_code == 200, routed.text
        assert routed.json()["subject_name"] == "Sports"
        assert routed.json()["subject_id"] != "BIO"
        assert routed.json()["focus_concept_id"]
