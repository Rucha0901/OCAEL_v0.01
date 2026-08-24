from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from adaptive_backend.agents import SpecialistBudget
from adaptive_backend.app import create_app


def _auth_client(settings, tmp_path: Path):
    cfg = replace(
        settings,
        storage_dir=tmp_path / "uploads",
        auth_secret="s" * 48,
        adaptive_max_difficulty_step=0.08,
    )
    app = create_app(cfg)
    client = TestClient(app)
    return app, client


def _register(client: TestClient, username: str = "learner01"):
    r = client.post(
        "/v1/auth/register",
        json={"username": username, "password": "correct horse battery staple"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return data, {"Authorization": f"Bearer {data['access_token']}"}


def test_ovael_health(settings, tmp_path):
    _, client = _auth_client(settings, tmp_path)
    with client:
        r = client.get("/v1/health")
        assert r.status_code == 200
        assert r.json()["product"] == "OVAEL"
        assert r.json()["dynamic_teaching"] == "enabled"
        assert set(r.json()["agents"]) == {"X", "Sam", "Carl", "Trav", "IRIS"}


def test_auth_register_login_and_me(settings, tmp_path):
    _, client = _auth_client(settings, tmp_path)
    with client:
        data, headers = _register(client)
        assert data["recovery_key"].startswith("OVAEL-")
        assert client.get("/v1/auth/me", headers=headers).json()["username"] == "learner01"
        login = client.post(
            "/v1/auth/login",
            json={"username": "learner01", "password": "correct horse battery staple"},
        )
        assert login.status_code == 200
        assert "access_token" in login.json()


def test_auth_recovery_rotates_key_and_revokes_sessions(settings, tmp_path):
    _, client = _auth_client(settings, tmp_path)
    with client:
        data, headers = _register(client)
        recovered = client.post(
            "/v1/auth/recover",
            json={
                "username": "learner01",
                "recovery_key": data["recovery_key"],
                "new_password": "a completely different password",
            },
        )
        assert recovered.status_code == 200
        assert recovered.json()["recovery_key"] != data["recovery_key"]
        assert client.get("/v1/auth/me", headers=headers).status_code == 401
        assert client.post(
            "/v1/auth/login",
            json={"username": "learner01", "password": "a completely different password"},
        ).status_code == 200


def test_course_isolation(settings, tmp_path):
    _, client = _auth_client(settings, tmp_path)
    with client:
        _, h1 = _register(client, "learnerA")
        _, h2 = _register(client, "learnerB")
        course = client.post(
            "/v1/courses", headers=h1, json={"name": "Private Bio", "subject_id": "BIO"}
        )
        assert course.status_code == 200
        cid = course.json()["course_id"]
        assert client.get(f"/v1/courses/{cid}", headers=h2).status_code == 404


def test_streamed_document_upload_and_duplicate(settings, tmp_path):
    _, client = _auth_client(settings, tmp_path)
    with client:
        _, headers = _register(client)
        content = b"Golgi packages and sorts proteins."
        first = client.post(
            "/v1/documents/upload",
            headers=headers,
            files={"file": ("notes.txt", content, "text/plain")},
        )
        assert first.status_code == 200, first.text
        assert first.json()["status"] in {"ready", "ready_partial"}
        doc_id = first.json()["document_id"]
        page = client.get(f"/v1/documents/{doc_id}/pages/1", headers=headers)
        assert page.status_code == 200
        assert "Golgi" in page.json()["text"]
        second = client.post(
            "/v1/documents/upload",
            headers=headers,
            files={"file": ("notes.txt", content, "text/plain")},
        )
        assert second.status_code == 200
        assert second.json()["document_id"] == doc_id
        assert second.json()["duplicate"] is True


def test_dynamic_teaching_session_not_question_bank(settings, tmp_path):
    _, client = _auth_client(settings, tmp_path)
    with client:
        _, headers = _register(client)
        started = client.post(
            "/v1/learning/sessions",
            headers=headers,
            json={"subject_id": "BIO", "concept_id": "bio_golgi", "mode": "study"},
        )
        assert started.status_code == 200, started.text
        move = started.json()["move"]
        # Study mode is teaching-first: sessions begin with instruction rather
        # than immediately interrogating the learner.
        assert move["action"] in {"explain", "worked_example", "contrast", "prerequisite_rewind", "hint"}
        assert move["generated_item_id"] is None
        assert move["content"]
        assert "question_id" not in move
        assert 0 <= move["target_difficulty"] <= 1

        # The next turn asks for application/verification through the same
        # dynamic TeachingMove engine.
        turn = client.post(
            f"/v1/learning/sessions/{started.json()['teaching_session_id']}/turns",
            headers={**headers, "Idempotency-Key": "teaching-first-check"},
            json={"response": "I understand; let me apply it."},
        )
        assert turn.status_code == 200, turn.text
        assert turn.json()["move"]["action"] in {"practice", "ask", "diagnose", "reassess"}


def test_learning_turn_idempotency(settings, tmp_path):
    _, client = _auth_client(settings, tmp_path)
    with client:
        _, headers = _register(client)
        started = client.post(
            "/v1/learning/sessions",
            headers=headers,
            json={"subject_id": "BIO", "concept_id": "bio_golgi"},
        ).json()
        sid = started["teaching_session_id"]
        h = {**headers, "Idempotency-Key": "turn-1"}
        one = client.post(
            f"/v1/learning/sessions/{sid}/turns",
            headers=h,
            json={"response": "It modifies, sorts and packages proteins."},
        )
        two = client.post(
            f"/v1/learning/sessions/{sid}/turns",
            headers=h,
            json={"response": "This retry must not double count."},
        )
        assert one.status_code == two.status_code == 200
        assert two.json()["idempotent_replay"] is True
        assert two.json()["move"] == one.json()["move"]


def test_agent_trace_and_spawn_cap(settings, tmp_path):
    _, client = _auth_client(settings, tmp_path)
    with client:
        _, headers = _register(client)
        started = client.post(
            "/v1/learning/sessions",
            headers=headers,
            json={"subject_id": "BIO", "concept_id": "bio_golgi"},
        ).json()
        trace = started["move"]["agent_trace"]
        assert set(trace) == {"X", "Sam", "Carl", "Trav"}
        assert len(trace["Sam"]["specialists"]) <= 3
        assert len(trace["Carl"]["specialists"]) <= 3
        assert len(trace["Trav"]["specialists"]) <= 3
        b = SpecialistBudget(max_per_lead=3, max_total=4)
        assert b.grant(99) == 3
        assert b.grant(99) == 1
        assert b.grant(1) == 0


def test_mcp_context_tools_and_scopes(settings, tmp_path):
    _, client = _auth_client(settings, tmp_path)
    with client:
        _, headers = _register(client)
        relay = client.post(
            "/v1/mcp/context-relay",
            headers=headers,
            json={"capsule": {"active_course": "BIO", "active_concept": "bio_golgi"}},
        )
        assert relay.status_code == 200
        con = client.post(
            "/v1/connections/mcp",
            headers=headers,
            json={"client_name": "ChatGPT", "scopes": ["learning.context.read"]},
        )
        assert con.status_code == 200
        mcp_token = con.json()["access_token"]
        rpc = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {mcp_token}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_learning_context", "arguments": {}},
            },
        )
        assert rpc.status_code == 200
        assert rpc.json()["result"]["structuredContent"]["capsule"]["active_course"] == "BIO"
        forbidden = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {mcp_token}"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "begin_teaching_session",
                    "arguments": {"subject_id": "BIO", "concept_id": "bio_golgi"},
                },
            },
        )
        assert "error" in forbidden.json()
        assert "scope" in forbidden.json()["error"]["message"].lower()


def test_mcp_external_teaching_handoff_and_inbox(settings, tmp_path):
    _, client = _auth_client(settings, tmp_path)
    with client:
        _, headers = _register(client)
        client.post(
            "/v1/mcp/context-relay",
            headers=headers,
            json={"capsule": {"active_course": "BIO", "active_concept": "bio_golgi"}},
        )
        con = client.post(
            "/v1/connections/mcp",
            headers=headers,
            json={"client_name": "Claude", "scopes": sorted([
                "learning.context.read",
                "learning.session.create",
                "learning.session.interact",
                "learning.external_events.submit",
            ])},
        ).json()
        mh = {"Authorization": f"Bearer {con['access_token']}"}
        begin = client.post(
            "/mcp",
            headers=mh,
            json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "begin_teaching_session", "arguments": {"subject_id": "BIO", "concept_id": "bio_golgi"}},
            },
        ).json()["result"]["structuredContent"]
        sid = begin["teaching_session_id"]
        handoff = client.post(
            "/mcp",
            headers=mh,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "handoff_to_ovael", "arguments": {"session_id": sid}}},
        ).json()["result"]["structuredContent"]
        assert "/app/continue/" in handoff["url"]
        done = client.post(
            "/mcp",
            headers=mh,
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "complete_external_session", "arguments": {"session_id": sid, "idempotency_key": "complete-1", "summary": "External lesson completed."}}},
        ).json()["result"]["structuredContent"]
        assert done["status"] == "pending"
        inbox = client.get("/v1/mcp/external-inbox", headers=headers)
        assert any(x["inbox_id"] == done["inbox_id"] for x in inbox.json())


def test_model_gateway_uses_mimo_runtime_configuration(settings, tmp_path, monkeypatch):
    import adaptive_backend.models as models_mod

    captured = {}

    class FakeResponse:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return {"choices": [{"message": {"content": "hello"}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, json=json)
            return FakeResponse()

    monkeypatch.setattr(models_mod.httpx, "Client", FakeClient)
    cfg = replace(
        settings,
        mimo_base_url="https://token-plan-sgp.xiaomimimo.com/v1",
        mimo_model="mimo-v2.5",
        mimo_api_key="secret-test-key",
    )
    gateway = models_mod.ModelGateway(cfg)
    reply = gateway.route_chat("teaching", system="s", user="u")
    assert reply.provider == "xiaomi_mimo"
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["headers"]["api-key"] == "secret-test-key"
    assert "secret-test-key" not in str(captured["json"])


def test_uploaded_rag_is_owner_scoped(settings, tmp_path):
    _, client = _auth_client(settings, tmp_path)
    with client:
        _, h1 = _register(client, "ragOwner")
        _, h2 = _register(client, "ragOther")
        course = client.post(
            "/v1/courses", headers=h1, json={"name": "My Biology", "subject_id": "BIO"}
        ).json()
        phrase = b"UniquePersonalMitoPhrase ATP source from my private notes"
        up = client.post(
            "/v1/documents/upload",
            headers=h1,
            params={"course_id": course["course_id"]},
            files={"file": ("private.txt", phrase, "text/plain")},
        )
        assert up.status_code == 200
        owner_hits = client.post(
            "/v1/retrieval/search",
            headers=h1,
            json={"query": "UniquePersonalMitoPhrase", "subject_id": "BIO", "course_id": course["course_id"]},
        )
        assert owner_hits.status_code == 200
        assert any("UniquePersonalMitoPhrase" in h["content"] for h in owner_hits.json())
        # Other user cannot even address the private course.
        other_hits = client.post(
            "/v1/retrieval/search",
            headers=h2,
            json={"query": "UniquePersonalMitoPhrase", "subject_id": "BIO", "course_id": course["course_id"]},
        )
        assert other_hits.status_code == 404
