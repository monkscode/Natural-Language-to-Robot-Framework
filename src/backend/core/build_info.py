"""Which build is this process running?

`-develop` and `-latest` are moving tags, so the tag a container was pulled
under says nothing about the commit inside it. Without a stamp, "you are not on
the latest code" cannot be confirmed or refuted from the outside — which is how
a browser-service pin sat three releases behind for two weeks without anyone
noticing from the running system.

build-images.yml passes the commit to every image build as the GIT_SHA
build-arg; the Dockerfiles turn it into an OCI revision label (readable with
`docker inspect`) and, for the backend image, an environment variable this
module reads and the health endpoints report.

Referenced by:
    src/backend/api/health.py        — /health and /api/health
    src/backend/runner_exec/app.py   — the executor's own /health
Depends on: nothing (stdlib only — this must work before anything else does).
"""

import os

# Read at call time, not import time: the tests set it per case, and a process
# that re-execs (uvicorn --reload) must see the current environment.
_ENV_VAR = "NLRF_BUILD_SHA"

# An unset --build-arg reaches the image as an empty string rather than being
# absent, so a blank value means "not stamped" just as a missing one does.
UNKNOWN = "unknown"


def build_info() -> dict[str, str]:
    """The commit this image was built from, or 'unknown' if it was not stamped.

    'unknown' is the honest answer for a local `run.sh` checkout, which has no
    image and therefore no stamp. It is never inferred from a version string or
    a package release — those are what proved worthless here: two different
    browser-service builds both reported __version__ 1.0.32.
    """
    return {"commit": os.getenv(_ENV_VAR, "").strip() or UNKNOWN}
