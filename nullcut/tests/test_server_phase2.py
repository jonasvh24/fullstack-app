"""Phase 2 endpoints, driven the way the UI drives them."""

from .conftest import needs_ffmpeg
from .test_server import client, wait  # noqa: F401  (fixture and helper)

pytestmark = needs_ffmpeg


def test_styles_compare_and_delete_rules(client):  # noqa: F811
    listed = client.get("/api/styles").json()
    assert {s["id"] for s in listed if s["kind"] == "shipped"} == {
        "explainer", "retention-max", "cinematic-vlog", "podcast-clip", "documentary", "tutorial"}
    rows = client.get("/api/styles/compare", params={"ids": "explainer,tutorial"}).json()
    assert rows and set(rows[0]["values"]) == {"explainer", "tutorial"}
    assert client.get("/api/styles/compare", params={"ids": "explainer"}).status_code == 400
    assert client.delete("/api/styles/explainer").status_code == 400
    assert client.post("/api/styles/extract", json={"name": "x", "folder": "/nope"}
                       ).status_code == 400
    profiles = {p["id"] for p in client.get("/api/thumbnail-profiles").json()}
    assert profiles == {"reaction-face", "object-hero", "before-after"}


def test_both_formats_try_and_master_with_thumbnails(client, footage_broll):  # noqa: F811
    r = client.post("/api/projects", json={"source_dir": str(footage_broll), "name": "P2"})
    pid = r.json()["project"]["id"]
    wait(client, r.json()["job"])
    brief = {**client.get(f"/api/projects/{pid}").json()["brief"], "format": "both",
             "style": "retention-max", "captions": {"mode": "word"},
             "thumbnail_profile": "reaction-face", "title_refs": ""}
    assert client.put(f"/api/projects/{pid}/brief", json=brief).status_code == 200
    plan = wait(client, client.post(f"/api/projects/{pid}/plan", json={}).json())["result"]
    assert plan["beats"]

    tried = wait(client, client.post(f"/api/projects/{pid}/try",
                                     json={"styles": ["explainer", "retention-max"],
                                           "seconds": 6}).json())["result"]["renders"]
    assert [t["style"] for t in tried] == ["explainer", "retention-max"]
    assert all(t["duration_s"] <= 6.2 and 0 <= t["overall"] <= 1 for t in tried)
    assert client.get(f"/api/projects/{pid}/try").json()[0]["url"] == tried[0]["url"]
    assert client.post(f"/api/projects/{pid}/try", json={"styles": ["explainer"]}
                       ).status_code == 400

    wait(client, client.post(f"/api/projects/{pid}/preview").json())
    renders = client.get(f"/api/projects/{pid}/renders").json()
    assert {r["format"] for r in renders} == {"16:9", "9:16"}
    for r in renders:
        assert r["qc"]["style_detail"] and 0 <= r["qc"]["style_overall"] <= 1

    master = wait(client, client.post(f"/api/projects/{pid}/master").json())["result"]
    pkg = master["package"]
    assert pkg["files"]["mp4_vertical"]
    assert len(pkg["thumbnails"]) == 3
    assert all(t["checks"]["passed"] for t in pkg["thumbnails"])
    png = client.get(pkg["thumbnails"][0]["url"])
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
    assert pkg["thumbnails"][0]["checks"]["contrast_ratio"] >= 4.5
