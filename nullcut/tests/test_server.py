"""The HTTP API (docs/api.md), driven the way the UI drives it."""

import time

import pytest

from .conftest import needs_ffmpeg

pytestmark = needs_ffmpeg


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from nullcut.server import create_app

    with TestClient(create_app(tmp_path / "home")) as c:
        yield c


def wait(client, job, timeout=300):
    end = time.time() + timeout
    while job["status"] in ("queued", "running"):
        assert time.time() < end, f"job {job['kind']} timed out at {job['message']}"
        time.sleep(0.3)
        job = client.get(f"/api/jobs/{job['id']}").json()
    assert job["status"] == "done", job["error"]
    return job


def test_full_flow_from_folder_to_package(client, footage_broll):
    assert client.get("/api/health").json()["encoder"] in ("libx264", "h264_nvenc")
    assert {s["id"] for s in client.get("/api/styles").json()} == \
           {"explainer", "retention-max", "cinematic-vlog"}

    r = client.post("/api/projects", json={"source_dir": str(footage_broll), "name": "Flow"})
    assert r.status_code == 200
    pid = r.json()["project"]["id"]
    wait(client, r.json()["job"])

    proj = client.get(f"/api/projects/{pid}").json()
    assert [c["speech"] for c in proj["clips"]] == [True, True, False]
    assert all(c["thumb_url"] for c in proj["clips"])
    thumb = client.get(proj["clips"][0]["thumb_url"])
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"

    brief = {**proj["brief"], "text": "open on the plan", "target_s": 15, "style": "explainer",
             "captions": {"mode": "clean"}}
    assert client.put(f"/api/projects/{pid}/brief", json=brief).status_code == 200
    bad = client.put(f"/api/projects/{pid}/brief", json={"style": "nope"})
    assert bad.status_code == 400 and "unknown style" in bad.json()["detail"]

    # 15 s asked of ~9 s of speech: the planner asks before it plans.
    asked = wait(client, client.post(f"/api/projects/{pid}/plan", json={}).json())["result"]
    assert asked["questions"] and not asked["beats"]
    qid = asked["questions"][0]["id"]
    plan_job = wait(client, client.post(f"/api/projects/{pid}/plan",
                                        json={"answers": {qid: "ship it"}}).json())
    plan = plan_job["result"]
    assert not plan["questions"]
    assert plan["beats"][0]["role"] == "hook" and plan["beats"][0]["segments"][0]["thumb_url"]
    assert client.get(f"/api/projects/{pid}/plan").json()["version"] == plan["version"]

    beats = plan["beats"]
    edit = [{"id": b["id"], "locked": i == 0, "segment_ids": [s["id"] for s in b["segments"]]}
            for i, b in enumerate(beats)]
    edit = edit[:1] + edit[1:][::-1]
    saved = client.put(f"/api/projects/{pid}/plan", json={"beats": edit}).json()
    assert [b["id"] for b in saved["beats"]] == [e["id"] for e in edit]
    assert saved["beats"][0]["locked"]
    assert client.put(f"/api/projects/{pid}/plan",
                      json={"beats": [{"id": "nope"}]}).status_code == 400

    preview = wait(client, client.post(f"/api/projects/{pid}/preview").json())["result"]
    assert preview["kind"] == "preview" and preview["qc"]["cuts_clean"]
    assert preview["qc"]["loudness_lufs"] == pytest.approx(-14.0, abs=1.0)
    tl = client.get(f"/api/projects/{pid}/timeline").json()
    assert tl["beats"] and tl["items"] and tl["duration_s"] == pytest.approx(
        preview["duration_s"], abs=0.05)

    video = client.get(preview["url"], headers={"Range": "bytes=0-99"})
    assert video.status_code == 206 and len(video.content) == 100

    master = wait(client, client.post(f"/api/projects/{pid}/master").json())["result"]
    pkg = master["package"]
    assert pkg["titles"] and all(t["verified"] for t in pkg["titles"])
    assert set(pkg["files"]) >= {"srt", "vtt", "otio", "fcpxml"}
    for url in (pkg["files"]["otio"], pkg["files"]["srt"]):
        assert client.get(url).status_code == 200
    kinds = [r["kind"] for r in client.get(f"/api/projects/{pid}/renders").json()]
    assert "master" in kinds and "preview" in kinds


def test_media_refuses_paths_outside_the_project(client, footage):
    r = client.post("/api/projects", json={"source_dir": str(footage)})
    pid = r.json()["project"]["id"]
    wait(client, r.json()["job"])
    assert client.get(f"/media/{pid}/../../jobs.sqlite").status_code == 404
    assert client.get(f"/media/{pid}/%2e%2e/%2e%2e/jobs.sqlite").status_code == 404
    assert client.get("/media/nope/project.json").status_code == 404
    assert client.get(f"/media/{pid}/project.json").status_code == 200


def test_bad_inputs_answer_with_a_reason(client):
    r = client.post("/api/projects", json={"source_dir": "/definitely/not/here"})
    assert r.status_code == 400 and "not a folder" in r.json()["detail"]
    assert client.get("/api/projects/nope").status_code == 404
    assert client.get("/api/jobs/nope").status_code == 404
