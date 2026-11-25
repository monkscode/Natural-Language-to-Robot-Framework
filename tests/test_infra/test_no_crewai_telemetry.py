"""The test session never sends crewai telemetry.

The defect (measured 2026-09-27 on crewai 1.8.1): crewai pinged api.scarf.sh from
a thread started at `import crewai`, and its module-level EventListener made
crewai's own TracerProvider the process's global one, so every span a test
opened (create_workflow_span included) was exported to telemetry.crewai.com.

On crewai 1.15.44 the scarf ping and the global-provider takeover are both gone
(`Telemetry.set_tracer` no longer installs a global provider; no "scarf" in
crewai or crewai_core). What remains: `Telemetry()` builds a private
TracerProvider whose BatchSpanProcessor exports to telemetry.crewai.com:4319,
and crewai's own spans (crew, flow, tool, ...) go through it. That path is gated
by crewai's Telemetry._is_telemetry_disabled(), which reads
CREWAI_DISABLE_TELEMETRY; tests/conftest.py forces it to "true".

crewai is imported ONLY in child processes that refuse every non-loopback DNS
lookup and every connect except to a socket the child itself opened (asyncio's
Windows self-pipe), with inherited `*_proxy` variables removed and `no_proxy=*`
forced (so the Windows/macOS system proxy is not consulted either) — so
neither the check nor its control can send anything, even through a local or
system proxy.

Referenced by: none (leaf test module).
Depends on: tests/conftest.py (the CREWAI_DISABLE_TELEMETRY pin), crewai 1.15.44.
`_crewai_in_child` forces LITELLM_LOCAL_MODEL_COST_MAP=True in the child's env
so litellm's own GitHub cost-map fetch never runs there, keeping each child's
attempt list about crewai alone.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap

# Records every DNS lookup and connect, REFUSES every DNS lookup that is not
# loopback and every connect except to a port the child itself bound (asyncio's
# Windows self-pipe), imports crewai, has crewai record one span through its own
# telemetry and flushes its exporter, then reports what crewai did.
_CHILD = textwrap.dedent("""
    import json, os, socket, threading, time
    LOOPBACK = ("127.0.0.1", "::1", "localhost")
    attempts = []
    _gai = socket.getaddrinfo
    def gai(host, *a, **k):
        h = host.decode() if isinstance(host, bytes) else host
        if h not in LOOPBACK:
            attempts.append(h)
            raise socket.gaierror(socket.EAI_NONAME, "refused by the test")
        return _gai(host, *a, **k)
    socket.getaddrinfo = gai
    _connect = socket.socket.connect
    own_ports = set()
    _bind = socket.socket.bind
    def bind(self, addr):
        _bind(self, addr)
        own_ports.add(self.getsockname()[1])
    socket.socket.bind = bind
    def connect(self, addr):
        if not (isinstance(addr, tuple) and addr[0] in LOOPBACK and addr[1] in own_ports):
            attempts.append(str(addr))
            raise ConnectionRefusedError("refused by the test")
        return _connect(self, addr)
    socket.socket.connect = connect
    import crewai  # noqa: F401
    from crewai.telemetry.telemetry import Telemetry
    from opentelemetry import trace
    tel = Telemetry()
    # One span through crewai's own span path (a no-op when telemetry is
    # disabled), then flush its private provider: the export worker resolves
    # telemetry.crewai.com here, where the guard records and refuses it.
    tel.flow_creation_span("probe")
    if tel.ready:
        # The export retries with back-off for ~15 s against the refused host,
        # so flush from a daemon thread and wait only for the first lookup.
        threading.Thread(target=tel.provider.force_flush, daemon=True).start()
        for _ in range(100):
            if attempts:
                break
            time.sleep(0.05)
    resource = getattr(trace.get_tracer_provider(), "resource", None)
    print(json.dumps({
        "attempts": sorted(set(attempts)),
        "disabled": Telemetry._is_telemetry_disabled(),
        "ready": tel.ready,
        "global_service": resource.attributes.get("service.name") if resource is not None else None,
    }), flush=True)
    # Skip crewai's atexit flush and the still-retrying export thread.
    os._exit(0)
""")


def _crewai_in_child(env: dict) -> dict:
    # Drop every inherited *_proxy variable (including no_proxy) first, so an
    # env-configured proxy can never carry crewai's ping out of the child.
    # urllib reads the Windows/macOS system proxy only when no *_proxy variable
    # is set, so also force no_proxy=* to switch that lookup off too, and every
    # request in the child goes direct (to the guard).
    # LiteLLM (imported by crewai) downloads its model list from GitHub at
    # import unless told to use its bundled copy. That download is not
    # telemetry; the bundled copy keeps these attempt lists about crewai alone.
    env = {k: v for k, v in env.items() if not k.lower().endswith("_proxy")}
    env["no_proxy"] = "*"
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    # Run outside the repo: __main__ has no __file__ for a `python -c` script,
    # so crewai's load_dotenv (crewai/llm.py:104) and litellm search upward
    # from cwd instead, and would load the repo root's .env into the child.
    result = subprocess.run([sys.executable, "-c", _CHILD], cwd=tempfile.gettempdir(), env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_session_forces_crewai_telemetry_off():
    """Forced, not setdefault: a shell that exported "false" must not reach the tests."""
    assert os.environ.get("CREWAI_DISABLE_TELEMETRY") == "true"


def test_crewai_under_the_session_env_is_silent():
    """A child started with the session env (as every subprocess a test starts is)."""
    seen = _crewai_in_child(dict(os.environ))
    assert seen == {"attempts": [], "disabled": True, "ready": False, "global_service": None}


def _unpinned_env() -> dict:
    return {k: v for k, v in os.environ.items()
            if k not in ("CREWAI_DISABLE_TELEMETRY", "OTEL_SDK_DISABLED", "CREWAI_DISABLE_TRACKING")}


def test_the_control_without_the_pin_would_have_sent():
    """Proves the test above can fail: without the pin crewai builds its telemetry
    and its exporter looks up telemetry.crewai.com (refused by the child's guard,
    so nothing is sent). crewai 1.15 no longer makes its provider the global one,
    so the global provider stays unset here too."""
    seen = _crewai_in_child(_unpinned_env())
    assert "telemetry.crewai.com" in seen["attempts"]
    assert seen["ready"] is True
    assert seen["global_service"] is None


def test_the_control_cannot_leave_through_a_proxy():
    """An inherited HTTPS_PROXY cannot carry the control's telemetry export out of
    the child: `_crewai_in_child` strips it before the child runs, so the export
    goes direct and is refused at DNS (exactly like the plain control above),
    and nothing may ever reach a listener bound at the proxy's address.

    Honest scope: this test proves the env-proxy path only. A Windows/macOS
    system proxy is switched off instead by the forced `no_proxy=*` (urllib
    only reads the system proxy when no `*_proxy` variable is set); the
    connect guard remains the backstop either way. A system proxy is not
    simulated here.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    try:
        port = srv.getsockname()[1]
        env = _unpinned_env()
        for k in list(env):
            if k.lower() in ("no_proxy", "https_proxy"):
                del env[k]
        env["HTTPS_PROXY"] = f"http://127.0.0.1:{port}"
        seen = _crewai_in_child(env)

        srv.settimeout(0.5)
        try:
            conn, _ = srv.accept()
            conn.close()
            reached = True
        except socket.timeout:
            reached = False
        assert not reached, "the control's ping reached the proxy listener"
        assert "telemetry.crewai.com" in seen["attempts"]
    finally:
        srv.close()
