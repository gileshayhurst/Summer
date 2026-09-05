"""End-to-end check that an aligned interview is not aligned twice.

test_forven_reuse.py mocks storage and the store, so it proves the routing and
nothing else. This drives the real pull route through real storage and a real
database, stubbing only Forven itself. It is what actually demonstrates the
saving: pull an interview, align it, pull it again, and confirm the second pull
neither goes back to Forven nor leaves work for the encoder.

Needs a database. Skipped without DATABASE_URL, like test_sz_store.py.
"""

import os
from pathlib import Path

import pytest

import forven_api
import forven_ingest
import storage
import sz_store
from app import create_app

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

REF = "ZZREUSE1"

# Turn-level timings only: no end times, so the encoder still has work to do.
PLAIN = "[00:00] Interviewer: what did you think?\n[00:05] Participant: it was good\n"
# What the encoder leaves behind - every respondent line has a real end.
RICH = ("[00:00-00:04] Interviewer: what did you think?\n"
        "[00:05-00:11] Participant: it was good\n")


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("FORVEN_STAGING_API_KEY", "fvk_stg")
    monkeypatch.setenv("FORVEN_STAGING_TENANT_ID", "stg-tenant")
    monkeypatch.setenv("FORVEN_SOURCE_ENV", "staging")

    class _FakeClient:
        def __init__(self, base_url, api_key):
            pass

    monkeypatch.setattr(forven_api, "ForvenClient", _FakeClient)

    calls = []

    def fake_ingest(client_, *, tenant_public_id, refs, session_key, log=print):
        """Stands in for Forven: writes a plain transcript and a video."""
        calls.append(list(refs))
        for ref in refs:
            storage.upload_bytes(f"{session_key}/{ref}.txt",
                                 PLAIN.encode("utf-8"), "text/plain")
            storage.upload_bytes(f"{session_key}/{ref}.mp4", b"NOT-REAL-MP4",
                                 "video/mp4")
        return session_key

    monkeypatch.setattr(forven_ingest, "ingest", fake_ingest)

    sz_store.init_schema()
    c = create_app(testing=True).test_client()
    c.fetched = calls
    return c


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with sz_store.cursor() as cur:
        cur.execute("DELETE FROM sz_ingested_interviews WHERE interview_ref = %s",
                    (REF,))


def _align(session_key):
    """Stand in for the encoder: overwrite the transcript in place, as it does."""
    storage.upload_bytes(f"{session_key}/{REF}.txt", RICH.encode("utf-8"),
                         "text/plain")
    sz_store.mark_aligned([REF])


def test_an_aligned_interview_is_reused_rather_than_pulled_again(client):
    first = client.post("/forven/pull", json={"refs": [REF]}).get_json()
    assert client.fetched == [[REF]], "the first pull must go to Forven"
    _align(first["session_key"])

    second = client.post("/forven/pull", json={"refs": [REF]}).get_json()

    # Nothing went back to Forven.
    assert client.fetched == [[REF]], "the second pull re-fetched from Forven"
    assert second["reused"] == 1
    assert second["session_key"] != first["session_key"]

    # The new session holds the ALIGNED transcript, not a fresh plain one.
    landed = set(storage.list_keys(second["session_key"]))
    assert f"{second['session_key']}/{REF}.txt" in landed
    assert f"{second['session_key']}/{REF}.mp4" in landed
    text = storage.read_file_bytes(f"{second['session_key']}/{REF}.txt").decode("utf-8")
    assert text == RICH, "the copy is plain - the alignment was thrown away"

    # And the index agrees, so the encoder is not dispatched again.
    held = sz_store.ingested_sessions([REF])[REF]
    assert held["session_key"] == second["session_key"]
    assert held["aligned_at"] is not None, "a reused alignment was cleared"


def test_the_encoder_has_nothing_left_to_do_after_a_reuse(client):
    """The real test of the saving: dispatch would find no pending work."""
    import app as app_module

    first = client.post("/forven/pull", json={"refs": [REF]}).get_json()
    assert app_module._sessions_needing_encode(
        storage.list_keys(first["session_key"])), "a fresh pull should need encoding"
    _align(first["session_key"])

    second = client.post("/forven/pull", json={"refs": [REF]}).get_json()

    assert app_module._sessions_needing_encode(
        storage.list_keys(second["session_key"])) == []


def test_refresh_really_does_go_back_to_forven(client):
    first = client.post("/forven/pull", json={"refs": [REF]}).get_json()
    _align(first["session_key"])

    second = client.post("/forven/pull",
                         json={"refs": [REF], "refresh": True}).get_json()

    assert client.fetched == [[REF], [REF]], "refresh did not re-fetch"
    assert second["reused"] == 0
    # A fresh pull brings a plain transcript, so the old alignment must not
    # still be claimed.
    text = storage.read_file_bytes(f"{second['session_key']}/{REF}.txt").decode("utf-8")
    assert text == PLAIN
    assert sz_store.ingested_sessions([REF])[REF]["aligned_at"] is None
