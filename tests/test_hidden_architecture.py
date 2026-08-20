from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from adaptive_backend.app import create_app


def _client(settings, tmp_path: Path):
    cfg = replace(
        settings,
        storage_dir=tmp_path / "uploads-hidden",
        auth_secret="h" * 48,
        access_token_minutes=5,
        session_days=2,
        memory_mode="local_first",
        adaptive_max_difficulty_step=0.08,
    )
    app = create_app(cfg)
    return app, TestClient(app)


def _register(client: TestClient, username: str = "hidden01"):
    r = client.post(
        "/v1/auth/register",
        json={"username": username, "password": "correct horse battery staple"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body, {"Authorization": f"Bearer {body['access_token']}"}


def test_local_first_teaching_uses_ephemeral_shadow_and_scrubs_on_complete(settings, tmp_path):
    app, client = _client(settings, tmp_path)
    with client:
        data, headers = _register(client)
        capsule = {
            "concept_states": [
                {
                    "concept_id": "bio_golgi",
                    "mastery_belief": 0.42,
                    "confidence": 0.55,
                    "evidence_count": 3,
                    "status": "developing",
                }
            ],
            "adaptive_states": [
                {
                    "concept_id": "bio_golgi",
                    "theta": -0.2,
                    "variance": 1.4,
                    "target_difficulty": 0.47,
                    "observations": 3,
                }
            ],
        }
        started = client.post(
            "/v1/learning/sessions",
            headers=headers,
            json={"subject_id": "BIO", "concept_id": "bio_golgi", "context_capsule": capsule},
        )
        assert started.status_code == 200, started.text
        sid = started.json()["teaching_session_id"]
        state_row = app.state.services.db.fetchone(
            "SELECT state_json FROM teaching_sessions WHERE teaching_session_id=?", (sid,)
        )
        state = app.state.services.db.loads(state_row["state_json"], {})
        engine_id = state["engine_user_id"]
        assert engine_id.startswith(f"lf:{data['user_id']}:")
        assert app.state.services.db.fetchone(
            "SELECT 1 FROM concept_state WHERE user_id=? AND concept_id='bio_golgi'", (engine_id,)
        )
        assert app.state.services.db.fetchone(
            "SELECT 1 FROM concept_state WHERE user_id=?", (data["user_id"],)
        ) is None

        turned = client.post(
            f"/v1/learning/sessions/{sid}/turns",
            headers={**headers, "Idempotency-Key": "lf-turn-1"},
            json={"response": "The Golgi modifies and sorts proteins."},
        )
        assert turned.status_code == 200, turned.text
        turn_row = app.state.services.db.fetchone(
            "SELECT learner_response_json FROM teaching_turns WHERE teaching_session_id=?", (sid,)
        )
        response_storage = app.state.services.db.loads(turn_row["learner_response_json"], {})
        assert response_storage.get("redacted") is True
        assert "The Golgi" not in turn_row["learner_response_json"]

        completed = client.post(f"/v1/learning/sessions/{sid}/complete", headers=headers)
        assert completed.status_code == 200, completed.text
        assert completed.json()["state_delta"]["memory_mode"] == "local_first"
        assert app.state.services.db.fetchone("SELECT 1 FROM sessions WHERE user_id=?", (engine_id,)) is None
        assert app.state.services.db.fetchone("SELECT 1 FROM concept_state WHERE user_id=?", (engine_id,)) is None
        scrubbed = app.state.services.db.fetchone(
            "SELECT state_json FROM teaching_sessions WHERE teaching_session_id=?", (sid,)
        )
        scrubbed_state = app.state.services.db.loads(scrubbed["state_json"], {})
        assert "engine_user_id" not in scrubbed_state
        assert "context_capsule" not in scrubbed_state


def test_auth_refresh_cookie_rotates_refresh_token(settings, tmp_path):
    app, client = _client(settings, tmp_path)
    with client:
        body, _headers = _register(client, "refresh01")
        assert "refresh_token" not in body
        old_cookie = client.cookies.get("ovael_refresh")
        csrf = client.cookies.get("ovael_csrf")
        assert old_cookie and csrf
        refreshed = client.post(
            "/v1/auth/refresh",
            headers={"X-CSRF-Token": csrf},
            json={},
        )
        assert refreshed.status_code == 200, refreshed.text
        assert "access_token" in refreshed.json()
        assert "refresh_token" not in refreshed.json()
        new_cookie = client.cookies.get("ovael_refresh")
        assert new_cookie and new_cookie != old_cookie
        # Old refresh credential is invalid immediately after rotation.
        replay = client.post("/v1/auth/refresh", json={"refresh_token": old_cookie})
        assert replay.status_code == 401


def test_mcp_suggestion_is_consumed_as_bounded_advisory(settings, tmp_path):
    app, client = _client(settings, tmp_path)
    with client:
        _body, headers = _register(client, "mcpadvice")
        relay = client.post(
            "/v1/mcp/context-relay",
            headers=headers,
            json={"capsule": {"concept_states": []}},
        )
        assert relay.status_code == 200
        conn = client.post(
            "/v1/connections/mcp",
            headers=headers,
            json={"client_name": "test-client"},
        )
        assert conn.status_code == 200, conn.text
        token = conn.json()["access_token"]
        user_id, scopes, _ = app.state.services.mcp.authenticate(token)
        started = app.state.services.mcp.call_tool(
            user_id=user_id,
            scopes=scopes,
            name="begin_teaching_session",
            arguments={"subject_id": "BIO", "concept_id": "bio_golgi"},
        )
        sid = started["teaching_session_id"]
        suggested = app.state.services.mcp.call_tool(
            user_id=user_id,
            scopes=scopes,
            name="submit_teaching_suggestion",
            arguments={"session_id": sid, "suggestion": "Use a contrast with rough ER."},
        )
        assert suggested["status"] == "accepted_as_advisory"
        result = app.state.services.mcp.call_tool(
            user_id=user_id,
            scopes=scopes,
            name="next_teaching_turn",
            arguments={"session_id": sid, "response": "Golgi sorts proteins", "idempotency_key": "mcp-turn-1"},
        )
        assert "state_delta" in result
        row = app.state.services.db.fetchone(
            "SELECT status FROM teaching_suggestions WHERE suggestion_id=?", (suggested["suggestion_id"],)
        )
        assert row["status"] == "used_as_context"
        relay_after = app.state.services.mcp.get_context(user_id)["capsule"]
        assert "concept_states" in relay_after


def test_local_first_turn_delta_contains_replayable_events_and_idempotent_replay(settings, tmp_path):
    app, client = _client(settings, tmp_path)
    with client:
        _body, headers = _register(client, "delta01")
        started = client.post(
            "/v1/learning/sessions",
            headers=headers,
            json={"subject_id": "BIO", "concept_id": "bio_golgi"},
        )
        assert started.status_code == 200, started.text
        sid = started.json()["teaching_session_id"]
        request_headers = {**headers, "Idempotency-Key": "delta-turn-1"}
        first = client.post(
            f"/v1/learning/sessions/{sid}/turns",
            headers=request_headers,
            json={"response": "The Golgi apparatus modifies, sorts and packages proteins."},
        )
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert first_body["state_delta"]["merge_semantics"] == "deduplicate_by_event_id_then_replay_in_timestamp_order"
        assert first_body["state_delta"]["events"], first_body
        event_ids = {event["event_id"] for event in first_body["state_delta"]["events"]}
        assert len(event_ids) == len(first_body["state_delta"]["events"])

        replay = client.post(
            f"/v1/learning/sessions/{sid}/turns",
            headers=request_headers,
            json={"response": "The Golgi apparatus modifies, sorts and packages proteins."},
        )
        assert replay.status_code == 200, replay.text
        replay_body = replay.json()
        assert replay_body["idempotent_replay"] is True
        assert replay_body["turn_id"] == first_body["turn_id"]
        assert replay_body["state_delta"] == first_body["state_delta"]


def test_personalization_pause_suppresses_persistent_delta_and_mcp_relay(settings, tmp_path):
    app, client = _client(settings, tmp_path)
    with client:
        _body, headers = _register(client, "pause01")
        paused = client.post(
            "/v1/memory/personalization",
            headers=headers,
            json={"enabled": False},
        )
        assert paused.status_code == 200, paused.text

        relay = client.post(
            "/v1/mcp/context-relay",
            headers=headers,
            json={"capsule": {"concept_states": [{"concept_id": "bio_golgi", "mastery_belief": 0.9}]}},
        )
        assert relay.status_code == 200, relay.text
        assert relay.json()["status"] == "personalization_paused"

        started = client.post(
            "/v1/learning/sessions",
            headers=headers,
            json={
                "subject_id": "BIO",
                "concept_id": "bio_golgi",
                "context_capsule": {"concept_states": [{"concept_id": "bio_golgi", "mastery_belief": 0.9}]},
            },
        )
        assert started.status_code == 200, started.text
        delta = started.json()["state_delta"]
        assert delta["personalization_enabled"] is False
        assert delta["persist"] is False
        assert delta["events"] == []
        assert delta["merge_semantics"] == "do_not_persist"
