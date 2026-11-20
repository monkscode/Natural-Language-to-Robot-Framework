"""The SPA's nginx must look fastapi up per request, not once at start.

nginx resolves a literal `proxy_pass http://fastapi:5000;` when it loads, so after
fastapi is recreated (new IP on nlrf-network) every proxied path answers 502 until
`nginx -s reload`. A variable in proxy_pass plus a `resolver` makes nginx ask
Docker's DNS again. The resolver address comes from the image's own
NGINX_ENTRYPOINT_LOCAL_RESOLVERS hook (rendered from /etc/resolv.conf), so the
file is installed as an envsubst template.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONF = (ROOT / "src/frontend-react/nginx.conf").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "Dockerfile.frontend").read_text(encoding="utf-8")


def _proxy_passes() -> list[str]:
    return re.findall(r"^\s*(?:location[^{]*\{\s*)?proxy_pass\s+([^;]+);", CONF, flags=re.M)


def test_every_proxy_pass_uses_the_variable_and_no_uri():
    passes = _proxy_passes()
    assert len(passes) == 10  # control: no proxied location was dropped
    # URI-less on purpose: a trailing "/" would strip the location prefix.
    assert set(passes) == {"http://$fastapi_upstream:5000"}


def test_the_upstream_variable_and_resolver_are_declared():
    assert re.search(r"^\s*set \$fastapi_upstream fastapi;", CONF, flags=re.M)
    assert re.search(r"^\s*resolver \$\{NGINX_LOCAL_RESOLVERS\} valid=10s;", CONF, flags=re.M)


def test_the_image_installs_the_file_as_a_template_with_the_resolver_hook_on():
    assert "COPY src/frontend-react/nginx.conf /etc/nginx/templates/default.conf.template" in DOCKERFILE
    assert re.search(r"^ENV NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1$", DOCKERFILE, flags=re.M)
    assert "/etc/nginx/conf.d/default.conf" not in DOCKERFILE
