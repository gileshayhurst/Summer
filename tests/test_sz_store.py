"""Tests for the Postgres-backed Sizzle Reel store.

These need a real database - psycopg2 against Postgres, not a stand-in, because
the things worth testing here (ON CONFLICT upsert, ON DELETE CASCADE, DISTINCT
across clips) are database behaviour rather than Python behaviour. Skipped
entirely when DATABASE_URL is unset, so the suite still runs without one.

Every test cleans up the rows it makes; nothing here truncates a table.
"""

import os

import pytest

import sz_store

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

REF_A = "ZZTESTAA"
REF_B = "ZZTESTBB"


@pytest.fixture(autouse=True)
def _schema():
    sz_store.init_schema()
    yield


@pytest.fixture()
def cleanup():
    """Remove anything a test created, including on failure."""
    made = {"reels": [], "refs": []}
    yield made
    with sz_store.cursor() as cur:
        for reel_id in made["reels"]:
            cur.execute("DELETE FROM sz_reels WHERE id = %s", (reel_id,))
        if made["refs"]:
            cur.execute(
                "DELETE FROM sz_ingested_interviews WHERE interview_ref = ANY(%s)",
                (made["refs"],),
            )


def _reel(cleanup, clips=None):
    reel = sz_store.create_reel(
        title="test reel",
        prompt="test prompt",
        session_key="sessions/test",
        clips=clips or [
            {"interview_ref": REF_A, "start_seconds": 1.0, "end_seconds": 4.0},
        ],
    )
    cleanup["reels"].append(reel["id"])
    return reel


def test_a_reel_records_its_clips_in_order(cleanup):
    reel = _reel(cleanup, clips=[
        {"interview_ref": REF_A, "start_seconds": 10.0, "end_seconds": 14.0},
        {"interview_ref": REF_B, "start_seconds": 2.0, "end_seconds": 6.0},
    ])

    clips = sz_store.reel_clips(reel["id"])

    assert [c["position"] for c in clips] == [0, 1]
    assert [c["interview_ref"] for c in clips] == [REF_A, REF_B]
    assert reel["clip_count"] == 2


def test_a_reel_needs_at_least_one_clip(cleanup):
    with pytest.raises(ValueError):
        sz_store.create_reel(title="t", prompt="p", session_key="s", clips=[])


def test_source_refs_are_deduplicated(cleanup):
    """register wants exactly the interviews in the cut, each named once."""
    reel = _reel(cleanup, clips=[
        {"interview_ref": REF_A, "start_seconds": 1.0, "end_seconds": 3.0},
        {"interview_ref": REF_B, "start_seconds": 1.0, "end_seconds": 3.0},
        {"interview_ref": REF_A, "start_seconds": 8.0, "end_seconds": 9.0},
    ])

    assert sz_store.source_refs(reel["id"]) == sorted([REF_A, REF_B])


def test_clips_go_when_the_reel_goes(cleanup):
    reel = _reel(cleanup)

    with sz_store.cursor() as cur:
        cur.execute("DELETE FROM sz_reels WHERE id = %s", (reel["id"],))

    assert sz_store.reel_clips(reel["id"]) == []


def test_delivery_records_what_forven_called_it(cleanup):
    reel = _reel(cleanup)

    sz_store.set_media(reel["id"], media_key="reels/x.mp4", duration_seconds=41)
    sz_store.mark_delivered(reel["id"], reel_ref="FR-ZZZZZZ", reel_public_id="pub-1")

    after = sz_store.get_reel(reel["id"])
    assert after["status"] == sz_store.STATUS_DELIVERED
    assert after["forven_reel_ref"] == "FR-ZZZZZZ"
    assert after["media_key"] == "reels/x.mp4"
    assert after["duration_seconds"] == 41


def test_reels_quoting_finds_the_reel(cleanup):
    reel = _reel(cleanup)

    assert reel["id"] in [r["id"] for r in sz_store.reels_quoting(REF_A)]
    assert sz_store.reels_quoting("ZZNOSUCH") == []


def test_the_ingest_index_round_trips(cleanup):
    cleanup["refs"] = [REF_A, REF_B]

    sz_store.record_ingested([REF_A, REF_B], tenant_public_id="t1",
                             session_key="sessions/one")

    assert {REF_A, REF_B} <= sz_store.already_ingested()


def test_re_ingesting_supersedes_rather_than_failing(cleanup):
    """A later pull genuinely replaces where an interview lives."""
    cleanup["refs"] = [REF_A]
    sz_store.record_ingested([REF_A], tenant_public_id="t1", session_key="sessions/one")

    sz_store.record_ingested([REF_A], tenant_public_id="t1", session_key="sessions/two")

    with sz_store.cursor() as cur:
        cur.execute(
            "SELECT session_key, aligned_at FROM sz_ingested_interviews "
            "WHERE interview_ref = %s",
            (REF_A,),
        )
        row = cur.fetchone()
    assert row["session_key"] == "sessions/two"
    assert row["aligned_at"] is None


def test_marking_aligned_sets_the_timestamp(cleanup):
    cleanup["refs"] = [REF_A]
    sz_store.record_ingested([REF_A], tenant_public_id="t1", session_key="sessions/one")

    assert sz_store.mark_aligned([REF_A]) == 1

    with sz_store.cursor() as cur:
        cur.execute(
            "SELECT aligned_at FROM sz_ingested_interviews WHERE interview_ref = %s",
            (REF_A,),
        )
        assert cur.fetchone()["aligned_at"] is not None


def test_ingested_sessions_says_where_an_interview_lives(cleanup):
    cleanup["refs"] = [REF_A, REF_B]
    sz_store.record_ingested([REF_A], tenant_public_id="t1",
                             session_key="sessions/one")

    held = sz_store.ingested_sessions([REF_A, REF_B])

    assert held[REF_A]["session_key"] == "sessions/one"
    assert held[REF_A]["aligned_at"] is None
    # Never pulled, so nothing to reuse - not an empty row.
    assert REF_B not in held
    assert sz_store.ingested_sessions([]) == {}


def test_a_reused_copy_keeps_its_alignment(cleanup):
    """The whole point of reuse: an aligned interview must not go back through
    the encoder just because it was pulled into another session."""
    cleanup["refs"] = [REF_A]
    sz_store.record_ingested([REF_A], tenant_public_id="t1",
                             session_key="sessions/one")
    sz_store.mark_aligned([REF_A])

    sz_store.record_ingested([REF_A], tenant_public_id="t1",
                             session_key="sessions/two", preserve_aligned=True)

    held = sz_store.ingested_sessions([REF_A])
    assert held[REF_A]["session_key"] == "sessions/two"
    assert held[REF_A]["aligned_at"] is not None


def test_a_fresh_pull_still_clears_alignment(cleanup):
    """A fetched transcript is plain, so an old timestamp would describe a file
    that is no longer there."""
    cleanup["refs"] = [REF_A]
    sz_store.record_ingested([REF_A], tenant_public_id="t1",
                             session_key="sessions/one")
    sz_store.mark_aligned([REF_A])

    sz_store.record_ingested([REF_A], tenant_public_id="t1",
                             session_key="sessions/two")

    assert sz_store.ingested_sessions([REF_A])[REF_A]["aligned_at"] is None

