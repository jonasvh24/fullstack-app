"""Phase 3 endpoints, driven the way the UI drives them."""

import time

from .conftest import needs_ffmpeg
from .test_server import client, wait  # noqa: F401  (fixture and helper)

pytestmark = needs_ffmpeg


def _project(client, footage):  # noqa: F811
    r = client.post("/api/projects", json={"source_dir": str(footage), "name": "P3"})
    pid = r.json()["project"]["id"]
    wait(client, r.json()["job"])
    brief = {**client.get(f"/api/projects/{pid}").json()["brief"], "style": "retention-max",
             "captions": {"mode": "word"}}
    assert client.put(f"/api/projects/{pid}/brief", json=brief).status_code == 200
    wait(client, client.post(f"/api/projects/{pid}/plan", json={}).json())
    wait(client, client.post(f"/api/projects/{pid}/preview").json())
    return pid


def _refine(client, pid, text):  # noqa: F811
    r = client.post(f"/api/projects/{pid}/refine", json={"text": text})
    assert r.status_code == 200, r.json()
    body = r.json()
    if body["job"] is None:
        return body, None
    job, partial = body["job"], None
    end = time.time() + 300
    while job["status"] in ("queued", "running"):
        assert time.time() < end
        if job["result"] and partial is None:
            partial = job["result"]          # the preview, before QC
        time.sleep(0.2)
        job = client.get(f"/api/jobs/{job['id']}").json()
    assert job["status"] == "done", job["error"]
    return body, {"final": job["result"], "partial": partial}


def test_refine_chat_round_trip(client, footage_broll):  # noqa: F811
    pid = _project(client, footage_broll)
    body, out = _refine(client, pid, "less zoom")
    assert body["step"]["kind"] == "tweak" and body["step"]["changes"][0]["label"]
    final = out["final"]
    assert final["step"]["timing"]["to_preview_s"] > 0
    assert final["step"]["render"]["items"] >= final["step"]["render"]["encoded"]
    assert final["render"]["qc_pending"] is False and final["render"]["qc"]["style_detail"]
    if out["partial"]:                       # caught mid-job: the preview came first
        assert out["partial"]["render"]["url"] == final["render"]["url"]

    body, out = _refine(client, pid, "drop the part about the plan")
    assert body["step"]["kind"] == "structural"
    assert out["final"]["step"]["render"]["encoded"] < out["final"]["step"]["render"]["items"]

    r = client.post(f"/api/projects/{pid}/undo")
    assert r.status_code == 200 and r.json()["step"]["kind"] == "undo"
    wait(client, r.json()["job"])
    steps = client.get(f"/api/projects/{pid}/refinements").json()
    assert [s["kind"] for s in steps] == ["tweak", "structural", "undo"]
    assert steps[1]["undone"] is True and steps[2]["undoes"] == 2

    unclear = client.post(f"/api/projects/{pid}/refine", json={"text": "hmm, not sure"})
    assert unclear.status_code == 200 and unclear.json()["step"] is None
    assert "less zoom" in unclear.json()["message"]
    missing = client.post(f"/api/projects/{pid}/refine",
                          json={"text": "drop the part about zebras"})
    assert missing.status_code == 400 and "zebras" in missing.json()["detail"]

    brief = client.get(f"/api/projects/{pid}").json()["brief"]
    assert brief["overrides"]["zoom"]["per_minute"] == 5.0

    master = wait(client, client.post(f"/api/projects/{pid}/master").json())["result"]
    pkg = master["package"]
    assert pkg["files"]["fcpxml"] and pkg["files"]["cube"]
    assert pkg["export_checks"]["fcpxml"]["ok"], pkg["export_checks"]
    health = client.get("/api/health").json()
    assert health["musicgen"]["backend"] == "builtin" and "CC0" in health["musicgen"]["licence"]


def test_learning_endpoints(client):  # noqa: F811
    s = client.get("/api/learning").json()
    assert s["active"] == "hand" and s["examples"] == 0
    fit = client.post("/api/learning/refit").json()
    assert fit["adopted"] is False and "needs" in fit["reason"]
    assert client.delete("/api/learning").json()["last_fit"] is None
