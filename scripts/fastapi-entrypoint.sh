#!/bin/sh
set -e

# =============================================================================
# FastAPI Entrypoint — Docker Socket Permission Handler
# =============================================================================
# Runs as root at container startup, then drops to appuser via gosu.
# Detects the Docker socket GID at runtime and grants appuser access.
#
# Platform support:
#   Docker Desktop (macOS/Windows) — socket GID is typically 0 (root)
#   Linux                          — socket GID varies (999, 998, 133, etc.)
#   No socket mounted              — gracefully skips, runs as appuser directly
#
# This is the same pattern used by official Docker images (postgres, redis, mysql).
# =============================================================================

# Only modify groups if a Docker socket is mounted
if [ -S /var/run/docker.sock ]; then
    SOCK_GID=$(stat -c '%g' /var/run/docker.sock 2>/dev/null || echo "")

    if [ -n "$SOCK_GID" ]; then
        # Find or create a group matching the socket's GID
        EXISTING_GROUP=$(getent group "$SOCK_GID" 2>/dev/null | cut -d: -f1 || echo "")

        if [ -z "$EXISTING_GROUP" ]; then
            groupadd -g "$SOCK_GID" dockersock 2>/dev/null || true
            EXISTING_GROUP="dockersock"
        fi

        # Grant appuser access to the Docker socket via group membership
        usermod -aG "$EXISTING_GROUP" appuser 2>/dev/null || true
    fi
fi

# Drop privileges to appuser and exec the CMD
exec gosu appuser "$@"
