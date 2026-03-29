#!/bin/sh
if [ "${HEALING_SESSION}" = "true" ]; then
  export DISPLAY=:99
  Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset &
  sleep 1
fi
exec "$@"
