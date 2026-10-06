"""The Brevet playground: the governed evolution loop, stage by stage, in a browser.

`brevet playground` starts a local web page that drives a real Brevet
workspace one stage at a time, using the same library code as everything
else, on the demo's synthetic data. Each stage shows the envelopes it
appended and the artefact it produced, including the rejected attempt by
the dream cycle to promote its own candidate.

Zero additional dependencies: stdlib HTTP server, one standalone page,
no network calls. Every run uses a fresh workspace directory; the
evidence chain is append-only, so nothing is ever overwritten.
"""

from __future__ import annotations

import json
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

import brevet
from brevet.demo import (
    CASES,
    MISSION_GROUP,
    REVIEWER,
    _evolved_stub,
    _human_final,
    _naive_stub,
)
from brevet.models import AgentManifest
from brevet.runner import EvalRunner

STEPS = ["work", "override", "dream", "block", "dawn",
         "evals", "release", "recall", "verify"]


def _manifest() -> AgentManifest:
    return AgentManifest(
        agent="deviation_triage_assistant",
        version="0.1.0",
        description="Playground: drafts deviation severity classifications.",
        identity_policy={"agent_id": "deviation_triage_assistant",
                         "owner": REVIEWER, "mission_group": MISSION_GROUP,
                         "may": ["draft_triage"], "may_not": ["close_deviation"]},
        prompt_architecture={"system_prompt": "Classify deviation severity; draft only."},
        cognitive_core={"model_policy": {"local_default": "ollama:gemma4:12b",
                                         "allowed_remote": []}},
        bindings={"tools": {"read": ["deviation_log.search"],
                            "suggest": ["draft_triage"],
                            "act": [], "controlled_act": []},
                  "capabilities_lock": "capabilities.lock"},
        runtime_safety={"loop": {"policy": "react", "max_iterations": 4},
                        "evidence": {"ledger": "file:./ledger.jsonl",
                                     "capture_overrides": True},
                        "evals": {"gate": "conservative"},
                        "dream": {"enabled": True}},
    )


class PlaygroundSession:
    """One real Brevet workspace, driven stage by stage."""

    def __init__(self, workdir: Path | None = None):
        self.root = Path(workdir) if workdir else Path(
            tempfile.mkdtemp(prefix="brevet-playground-"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.agent = brevet.wrap(_naive_stub, manifest=_manifest(),
                                 workdir=self.root / ".brevet")
        self.agent.manifest_path = self.root / "agent.yaml"
        self.done: list[str] = []
        self._runs: list = []
        self._runs_bound: tuple | None = None
        self._cand_id: str | None = None
        self._gate: dict = {}
        self._seen_envelopes = 0

    # ------------------------------------------------------------- state
    def _ledger_lines(self) -> list[dict]:
        path = self.root / ".brevet" / "ledger.jsonl"
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]

    def _new_envelopes(self) -> list[dict]:
        lines = self._ledger_lines()
        fresh = [{"seq": i + 1, "kind": e["kind"],
                  "ref": (e.get("refs") or [""])[0]}
                 for i, e in enumerate(lines)][self._seen_envelopes:]
        self._seen_envelopes = len(lines)
        return fresh

    def state(self) -> dict:
        return {"workspace": str(self.root), "steps": STEPS,
                "done": self.done, "status": self.agent.status()}

    # -------------------------------------------------------------- steps
    def run_step(self, name: str) -> dict:
        if name not in STEPS:
            return {"ok": False, "error": f"unknown step '{name}'"}
        expected = STEPS[len(self.done)] if len(self.done) < len(STEPS) else None
        if name != expected:
            return {"ok": False, "error":
                    f"the loop runs in order; next step is '{expected}'"}
        out = getattr(self, f"_step_{name}")()
        self.done.append(name)
        out.update(ok=True, step=name, new_envelopes=self._new_envelopes(),
                   state=self.state())
        return out

    def _step_work(self) -> dict:
        rows = []
        for task, _final in CASES:
            r = self.agent.run(task, task_family="deviation_triage")
            self._runs.append(r)
            sev = next((line.split(": ")[1] for line in r.output.splitlines()
                        if line.startswith("severity")), "?")
            rows.append({"task": task, "draft_severity": sev})
        return {"summary": f"The agent drafted {len(CASES)} severity ratings under signed "
                           f"harness 0.1.0. The harness stays fixed while the agent works.",
                "artifact": rows}

    def _step_override(self) -> dict:
        n = 0
        last = None
        for (task, sev), r in zip(CASES, self._runs):
            ov = self.agent.record_final(
                r.task_id, _human_final(task, sev), participant=REVIEWER,
                rationale=("Recurrent vibration on CIP-adjacent duty is a known "
                           "seal-wear precursor; treat as major." if sev == "major" else ""),
                tags=(["vibration-cip-underrated"] if sev == "major" else []))
            if ov:
                n, last = n + 1, ov
        return {"summary": f"The reviewer corrected the drafts and used her own finals. "
                           f"{n} differed from the agent's, and Brevet recorded each as an "
                           f"override with her reason. Nobody filled in a form.",
                "artifact": json.loads(last.model_dump_json()) if last else None}

    def _step_dream(self) -> dict:
        counts = self.agent.dream()
        pending = self.agent.dawn()
        self._cand_id = next(c.capability_id for c in pending
                             if c.kind.value == "prompt_rule")
        return {"summary": f"Offline, the dream cycle mined "
                           f"{counts['overrides']} overrides into "
                           f"{counts['candidates']} candidate rule and "
                           f"{counts['eval_cases']} eval cases. All sit at the "
                           f"Evidence layer, with no authority.",
                "artifact": [{"capability_id": c.capability_id,
                              "kind": c.kind.value, "title": c.title,
                              "recurrence": c.evidence.recurrence_count,
                              "authority": c.authority_layer.value}
                             for c in pending]}

    def _step_block(self) -> dict:
        try:
            self.agent.dawn(decide=(self._cand_id, "promote"),
                            approver="dream:nightcycle")
            return {"summary": "UNEXPECTED: the promotion succeeded.",
                    "artifact": {"invariant": "I2", "held": False}}
        except PermissionError as e:
            return {"summary": "The dream cycle tried to promote its own "
                               "candidate as dream:nightcycle, and Brevet rejected "
                               "it. Promotion needs a human or mission-group "
                               "identity.",
                    "artifact": {"invariant": "I2 (no self-authorisation)",
                                 "attempted_approver": "dream:nightcycle",
                                 "error": str(e), "held": True}}

    def _step_dawn(self) -> dict:
        pending = self.agent.dawn()
        for cap in pending:
            self.agent.dawn(decide=(cap.capability_id, "promote"),
                            approver=MISSION_GROUP)
        return {"summary": f"{MISSION_GROUP} reviewed the dawn queue and promoted "
                           f"{len(pending)} capabilities to Advisory. Each decision "
                           f"is a promotion envelope on the evidence chain.",
                "artifact": [{"capability_id": c.capability_id,
                              "promoted_to": "advisory",
                              "approver": MISSION_GROUP} for c in pending]}

    def _step_evals(self) -> dict:
        before = self.agent.evaluate(baseline=True)
        self.agent.adapter.target = _evolved_stub  # the promoted rule, applied
        after = self.agent.evaluate()
        self._gate = EvalRunner.compare(before, after)
        self._runs_bound = (before, after)
        g = self._gate
        return {"summary": f"Override-compiled evals: held-in "
                           f"{before['held_in_pass_rate']:.2f} -> {after['held_in_pass_rate']:.2f}, "
                           f"held-out {before['held_out_pass_rate']:.2f} -> "
                           f"{after['held_out_pass_rate']:.2f}. Conservative gate: "
                           f"{'pass' if g['passed_gate'] else 'fail'}.",
                "artifact": {"before": before, "after": after, "gate": g}}

    def _step_release(self) -> dict:
        self.agent.release(to_version="0.2.0", channel="trial",
                           approver=MISSION_GROUP, evals=self._runs_bound,
                           rationale="Playground release: vibration/CIP severity rule.")
        lock = json.loads((self.root / "capabilities.lock").read_text(encoding="utf-8")) \
            if (self.root / "capabilities.lock").exists() else \
            json.loads((self.root / ".brevet" / "capabilities.lock").read_text(encoding="utf-8"))
        manifest = yaml.safe_load((self.root / "agent.yaml").read_text(encoding="utf-8"))
        return {"summary": "Release 0.1.0 -> 0.2.0 on the trial channel: "
                           "promoted capabilities are locked into "
                           "capabilities.lock and the manifest is signed with Ed25519.",
                "artifact": {"capabilities_lock": lock,
                             "signature": manifest.get("signature", {})}}

    def _step_recall(self) -> dict:
        notice = self.agent.recall(
            self._cand_id,
            reason="Engineering confirmed the vibration signature was a sensor "
                   "artefact on the P-301 family; the rule over-generalises.",
            issued_by=MISSION_GROUP)
        return {"summary": "The rule proved wrong, so it was recalled and "
                           "every release that shipped it is flagged. It is "
                           "excluded from future releases.",
                "artifact": json.loads(notice.model_dump_json())}

    def _step_verify(self) -> dict:
        ok, n = self.agent.verify()
        return {"summary": f"Replayed the evidence chain: {'intact' if ok else 'BROKEN'} "
                           f"across {n} envelopes. Anyone holding the ledger can "
                           f"repeat this check.",
                "artifact": {"chain_ok": ok, "envelopes": n}}


# ------------------------------------------------------------------ http
def _fresh_root(base: Path | None) -> Path | None:
    """A new folder for each reset: inside --workdir when one was given,
    otherwise a new temporary folder."""
    if base is None:
        return None
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="run-", dir=base))


class _Handler(BaseHTTPRequestHandler):
    session: PlaygroundSession
    base: Path | None = None

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            self._json(type(self).session.state())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        # Only this page may drive the playground: a JSON content type cannot
        # be sent cross-site without a CORS preflight, which is never
        # answered, and a foreign Origin is refused outright.
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and origin not in (f"http://{host}", f"https://{host}"):
            self._json({"error": "cross-origin request refused"}, 403)
            return
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self._json({"error": "send application/json"}, 415)
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            self._json({"error": "the request body is not JSON"}, 400)
            return
        if self.path == "/api/step":
            self._json(type(self).session.run_step(str(payload.get("name", ""))))
        elif self.path == "/api/reset":
            type(self).session = PlaygroundSession(_fresh_root(type(self).base))
            self._json(type(self).session.state())
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, *args):  # keep the terminal quiet
        pass


def serve(port: int = 8765, workdir: Path | None = None,
          open_browser: bool = True) -> None:
    _Handler.base = Path(workdir) if workdir else None
    _Handler.session = PlaygroundSession(_fresh_root(_Handler.base))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"brevet playground: {url}")
    print(f"workspace: {_Handler.session.root} (fresh per reset, append-only)")
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nplayground stopped")


# ------------------------------------------------------------------ page
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Brevet playground</title>
<style>
  :root{--bg:#1B1C1E;--card:#242528;--line:#3A3C40;--ink:#F5F5F5;--dim:#B3B8BF;
        --faint:#8A8F98;--ember:#EA4700;--amber:#FF9900;--green:#3FBF71;--blue:#7FA6E8;
        --chip:#2C2D31;--mono:Consolas,Menlo,monospace}
  *{box-sizing:border-box;margin:0}
  body{background:var(--bg);color:var(--ink);font:15px/1.5 Arial,Helvetica,sans-serif;padding:28px}
  header{max-width:1180px;margin:0 auto 20px}
  h1{font-size:24px} h1 b{color:var(--amber)}
  .sub{color:var(--dim);margin-top:4px;font-size:14px}
  .ws{font-family:var(--mono);font-size:12px;color:var(--faint);margin-top:6px;word-break:break-all}
  .wrap{max-width:1180px;margin:0 auto;display:grid;grid-template-columns:390px 1fr;gap:18px}
  @media(max-width:900px){.wrap{grid-template-columns:1fr}}
  .steps{display:flex;flex-direction:column;gap:10px}
  .step{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;
        display:flex;align-items:center;gap:12px}
  .step .n{width:30px;height:30px;border-radius:50%;background:var(--chip);color:var(--dim);
           display:flex;align-items:center;justify-content:center;font-weight:bold;font-size:13px;flex:none}
  .step.done .n{background:var(--green);color:#10241a}
  .step.next .n{background:var(--amber);color:#241a06}
  .step.blocked .n{background:var(--ember);color:#fff}
  .step .t{flex:1}.step .t b{font-size:14.5px}.step .t span{display:block;color:var(--faint);font-size:12.5px}
  .step button{background:var(--amber);border:0;color:#241a06;font-weight:bold;border-radius:8px;
               padding:7px 14px;cursor:pointer;font-size:13px}
  .step button:disabled{background:var(--chip);color:var(--faint);cursor:default}
  .bar{display:flex;gap:10px;margin-bottom:12px}
  .bar button{background:var(--chip);color:var(--ink);border:1px solid var(--line);border-radius:8px;
              padding:8px 16px;cursor:pointer;font-size:13px}
  .bar button.primary{background:var(--ember);border-color:var(--ember);color:#fff;font-weight:bold}
  .panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
  .panel h2{font-size:13px;letter-spacing:1.2px;color:var(--dim);text-transform:uppercase;margin-bottom:10px}
  #summary{font-size:15px;min-height:24px}
  pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px;
      font:12.5px/1.5 var(--mono);color:var(--dim);overflow:auto;max-height:320px;white-space:pre-wrap}
  #chain{max-height:300px;overflow:auto;font:12.5px/1.7 var(--mono)}
  .env{color:var(--dim)} .env b{color:var(--amber);font-weight:normal}
  .env.new{color:var(--ink)} .env.new b{color:var(--green)}
  .stat{display:flex;gap:18px;flex-wrap:wrap;font-size:13.5px;color:var(--dim)}
  .stat b{color:var(--ink)}
  .ok{color:var(--green)} .bad{color:var(--ember)}
  footer{max-width:1180px;margin:22px auto 0;color:var(--faint);font-size:13px;font-style:italic}
</style>
</head>
<body>
<header>
  <h1><b>Brevet</b> playground</h1>
  <div class="sub">The governed evolution loop, stage by stage, on a real workspace:
  real envelopes, real signatures.</div>
  <div class="ws" id="ws"></div>
</header>
<div class="wrap">
  <div>
    <div class="bar">
      <button class="primary" id="runall">Run remaining steps</button>
      <button id="reset">Reset (fresh workspace)</button>
    </div>
    <div class="steps" id="steps"></div>
  </div>
  <div>
    <div class="panel"><h2>What just happened</h2><div id="summary">Press a stage to begin.
      The agent works first; every later stage follows from the overrides experts record.</div></div>
    <div class="panel"><h2>Artefact</h2><pre id="artifact">-</pre></div>
    <div class="panel"><h2>Evidence chain (append-only)</h2><div id="chain"></div></div>
    <div class="panel"><h2>Workspace</h2><div class="stat" id="stat"></div></div>
  </div>
</div>
<footer>Agents propose deltas; evidence tests them; humans promote them;
the runtime only ever executes signed versions.</footer>
<script>
const META = {
  work:    ["Work",    "the agent drafts 6 ratings under signed harness 0.1.0"],
  override:["Override","the reviewer corrects drafts; each becomes an override"],
  dream:   ["Dream",   "recurring overrides become a candidate and eval cases"],
  block:   ["Self-promotion attempt", "dream:nightcycle tries to promote its own candidate"],
  dawn:    ["Dawn",    "the mission group decides, by name"],
  evals:   ["Evals",   "overrides replay as tests; the conservative gate"],
  release: ["Release", "ship 0.2.0 on trial, signed, with capabilities.lock"],
  recall:  ["Recall",  "recall the rule; flag every release that shipped it"],
  verify:  ["Verify",  "replay the whole evidence chain"]};
let state = null;
async function fetchState(){ state = await (await fetch('/api/state')).json(); render(); }
function render(){
  document.getElementById('ws').textContent = 'workspace: ' + state.workspace;
  const holder = document.getElementById('steps'); holder.innerHTML = '';
  state.steps.forEach((s,i)=>{
    const done = state.done.includes(s);
    const next = state.steps[state.done.length] === s;
    const div = document.createElement('div');
    div.className = 'step' + (done ? ' done':'') + (next ? ' next':'') + (s==='block' ? ' blocked':'');
    div.innerHTML = `<div class="n">${done ? '✓' : i+1}</div>
      <div class="t"><b>${META[s][0]}</b><span>${META[s][1]}</span></div>
      <button ${next ? '' : 'disabled'} onclick="runStep('${s}')">${done?'done':'run'}</button>`;
    holder.appendChild(div);
  });
  const st = state.status;
  document.getElementById('stat').innerHTML =
    `<span>version <b>${st.version}</b></span><span>channel <b>${st.channel}</b></span>` +
    `<span>capabilities <b>${JSON.stringify(st.capabilities)}</b></span>` +
    `<span>recalled <b>${st.revoked}</b></span><span>envelopes <b>${st.envelopes}</b></span>` +
    `<span>chain <b class="${st.chain_ok?'ok':'bad'}">${st.chain_ok?'intact':'BROKEN'}</b></span>`;
}
async function runStep(name){
  const r = await (await fetch('/api/step',{method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})})).json();
  if(!r.ok){ document.getElementById('summary').textContent = r.error; return; }
  state = r.state;
  document.getElementById('summary').textContent = r.summary;
  document.getElementById('artifact').textContent = JSON.stringify(r.artifact, null, 2);
  const chain = document.getElementById('chain');
  chain.querySelectorAll('.new').forEach(e=>e.classList.remove('new'));
  (r.new_envelopes||[]).forEach(e=>{
    const d = document.createElement('div'); d.className='env new';
    d.innerHTML = `${String(e.seq).padStart(3,' ')}  <b>${e.kind}</b>  ${e.ref||''}`;
    chain.appendChild(d);
  });
  chain.scrollTop = chain.scrollHeight;
  render();
  return r;
}
document.getElementById('runall').onclick = async ()=>{
  while(state.done.length < state.steps.length){
    const next = state.steps[state.done.length];
    const r = await runStep(next);
    if(!r || !r.ok) break;
    await new Promise(res=>setTimeout(res, 650));
  }
};
document.getElementById('reset').onclick = async ()=>{
  state = await (await fetch('/api/reset',{method:'POST',
    headers:{'Content-Type':'application/json'}, body:'{}'})).json();
  document.getElementById('chain').innerHTML='';
  document.getElementById('artifact').textContent='-';
  document.getElementById('summary').textContent='Fresh workspace. Press Work to begin.';
  render();
};
fetchState();
</script>
</body>
</html>
"""
