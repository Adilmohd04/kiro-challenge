"""Read-only FastAPI audit dashboard for SentinelNow.

Renders a live timeline of the agent actions SentinelNow has recorded — each
entry's effect and risk score — alongside the kill-switch status and the writes
awaiting human approval. It is a window onto the gateway, nothing more.

Run with:  python -m sentinelnow.dashboard

SECURITY: this app is strictly READ-ONLY. Every endpoint only reads from the
gateway (audit log, pending approvals, kill-switch state); none mutate anything.
Approving/rejecting a write and engaging/releasing the kill switch stay in the
MCP tools (server.py) — the dashboard merely DISPLAYS pending approvals and the
kill-switch banner, it never acts on them. The audit log already carries only the
closed, secret-free key set (no ``WriteRequest.fields`` values, no credentials),
so the JSON it serves cannot leak secrets.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .gateway import Gateway
from .server import build_gateway

# Default bind address for `python -m sentinelnow.dashboard`. Local-only by
# default so the read-only view is not exposed off the host unintentionally.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


# Single inline page: no build step, no external assets. It polls the three
# read-only JSON endpoints on an interval and renders a kill-switch banner, a
# pending-approvals list (display only — no approve/reject controls), and a
# timeline table of actions. All rendering happens client-side from the JSON.
_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>SentinelNow — Audit Dashboard</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; margin: 0; padding: 1.5rem; }
  h1 { font-size: 1.4rem; margin: 0 0 1rem; }
  .banner { padding: 0.6rem 1rem; border-radius: 6px; font-weight: 600; margin-bottom: 1rem; }
  .banner.ok { background: #e6f4ea; color: #1e7e34; }
  .banner.engaged { background: #fdecea; color: #b71c1c; }
  section { margin-bottom: 1.5rem; }
  table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
  th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #ccc3; }
  th { font-weight: 600; }
  .effect-ALLOW { color: #1e7e34; }
  .effect-DENY { color: #b71c1c; }
  .effect-NEEDS_APPROVAL { color: #b26a00; }
  .muted { opacity: 0.7; font-size: 0.85rem; }
  ul { margin: 0; padding-left: 1.2rem; }
</style>
</head>
<body>
<h1>SentinelNow — Audit Dashboard <span class="muted">(read-only)</span></h1>
<div id="killswitch" class="banner ok">Kill switch: loading…</div>

<section>
  <h2>Pending approvals</h2>
  <ul id="pending"><li class="muted">loading…</li></ul>
</section>

<section>
  <h2>Action timeline</h2>
  <table>
    <thead>
      <tr>
        <th>Timestamp</th><th>Agent</th><th>Table</th><th>Operation</th>
        <th>Effect</th><th>Risk</th><th>Applied</th>
      </tr>
    </thead>
    <tbody id="audit"><tr><td colspan="7" class="muted">loading…</td></tr></tbody>
  </table>
</section>

<script>
function esc(v) {
  return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}

async function refresh() {
  try {
    const [ksRes, pendRes, auditRes] = await Promise.all([
      fetch("/api/killswitch"),
      fetch("/api/pending"),
      fetch("/api/audit"),
    ]);
    const ks = await ksRes.json();
    const pending = await pendRes.json();
    const audit = await auditRes.json();

    const banner = document.getElementById("killswitch");
    if (ks.engaged) {
      banner.className = "banner engaged";
      banner.textContent = "Kill switch: ENGAGED — all writes blocked";
    } else {
      banner.className = "banner ok";
      banner.textContent = "Kill switch: released — writes follow policy";
    }

    const pend = document.getElementById("pending");
    if (!pending.length) {
      pend.innerHTML = '<li class="muted">No writes awaiting approval.</li>';
    } else {
      pend.innerHTML = pending.map(function (a) {
        return "<li><code>" + esc(a.approval_id) + "</code> — " +
          esc(a.operation) + " on " + esc(a.table) + " by " + esc(a.agent_id) +
          " (risk " + esc(a.risk_score) + ") — " + esc(a.reason) + "</li>";
      }).join("");
    }

    const rows = audit.slice().reverse().map(function (e) {
      return "<tr>" +
        "<td>" + esc(e.timestamp) + "</td>" +
        "<td>" + esc(e.agent_id) + "</td>" +
        "<td>" + esc(e.table) + "</td>" +
        "<td>" + esc(e.operation) + "</td>" +
        '<td class="effect-' + esc(e.effect) + '">' + esc(e.effect) + "</td>" +
        "<td>" + esc(e.risk_score) + "</td>" +
        "<td>" + (e.applied ? "yes" : "no") + "</td>" +
        "</tr>";
    }).join("");
    document.getElementById("audit").innerHTML =
      rows || '<tr><td colspan="7" class="muted">No actions recorded yet.</td></tr>';
  } catch (err) {
    // Leave the last-rendered state in place on a transient fetch error.
  }
}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


def create_app(gateway: Gateway) -> FastAPI:
    """Build the read-only dashboard app bound to ``gateway``.

    None of the registered handlers mutate the gateway or the instance: they only
    read the audit log, the pending approvals, and the kill-switch state. The
    approve/reject and kill-switch actions deliberately live only in the MCP tools.
    """
    app = FastAPI(title="SentinelNow Audit Dashboard", docs_url=None, redoc_url=None)

    @app.get("/api/audit")
    async def api_audit() -> list[dict[str, object]]:
        """Return the append-only audit log as a list of audit-entry objects."""
        return [entry.model_dump() for entry in gateway.audit_log()]

    @app.get("/api/pending")
    async def api_pending() -> list[dict[str, object]]:
        """Return the audit-safe summaries of writes awaiting human approval."""
        return [approval.model_dump() for approval in gateway.list_pending_approvals()]

    @app.get("/api/killswitch")
    async def api_killswitch() -> dict[str, bool]:
        """Return the current kill-switch state."""
        return {"engaged": gateway.kill_switch_engaged()}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        """Serve the single inline dashboard page (no build step, no assets)."""
        return HTMLResponse(content=_INDEX_HTML)

    return app


# Module-level app for uvicorn (`uvicorn sentinelnow.dashboard:app`). Built from
# build_gateway() so the dashboard reflects the same config/stores as the server.
app = create_app(build_gateway())


def main() -> None:
    """Run the read-only dashboard with uvicorn on the default local host/port."""
    import uvicorn

    uvicorn.run(app, host=DEFAULT_HOST, port=DEFAULT_PORT)


if __name__ == "__main__":
    main()
