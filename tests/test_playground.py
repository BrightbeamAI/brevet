"""The playground drives a real workspace through the whole loop."""

from brevet.playground import STEPS, PlaygroundSession


def test_playground_full_loop(tmp_path):
    s = PlaygroundSession(tmp_path / "pg")

    # order is enforced
    out = s.run_step("release")
    assert not out["ok"] and "next step is 'work'" in out["error"]

    results = {}
    for name in STEPS:
        out = s.run_step(name)
        assert out["ok"], (name, out)
        results[name] = out

    # the self-promotion attempt was rejected and changed nothing
    assert results["block"]["artifact"]["held"] is True
    assert "dream:nightcycle" in results["block"]["artifact"]["attempted_approver"]

    # the gate passed on measured deltas and the release shipped signed
    assert results["evals"]["artifact"]["gate"]["passed_gate"] is True
    assert results["release"]["artifact"]["signature"]["algorithm"] == "ed25519"
    lock = results["release"]["artifact"]["capabilities_lock"]
    assert lock["resolved"] and all(
        c["approved_by"] == "mission_group:right_first_time"
        for c in lock["resolved"])

    # recall flagged the release; the chain replays clean
    assert results["recall"]["artifact"]["affected_releases"]
    assert results["verify"]["artifact"]["chain_ok"] is True
    assert results["verify"]["artifact"]["envelopes"] > 30

    # every step reported fresh envelopes except the blocked attempt
    assert results["block"]["new_envelopes"] == []
    assert results["work"]["new_envelopes"]


def test_playground_state_shape(tmp_path):
    s = PlaygroundSession(tmp_path / "pg2")
    st = s.state()
    assert st["steps"] == STEPS
    assert st["done"] == []
    assert st["status"]["version"] == "0.1.0"


def test_playground_refuses_cross_site_and_malformed_posts(tmp_path):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from brevet import playground

    playground._Handler.base = None
    playground._Handler.session = PlaygroundSession(tmp_path / "pg")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), playground._Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(path, headers, body=b"{}"):
        req = urllib.request.Request(url + path, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        json_type = {"Content-Type": "application/json"}
        assert post("/api/reset", {"Content-Type": "text/plain"}) == 415
        assert post("/api/reset", {**json_type, "Origin": "https://evil.example"}) == 403
        assert post("/api/step", json_type, b"{not json") == 400
        assert post("/api/step", json_type, b'{"name": "work"}') == 200
    finally:
        httpd.shutdown()
