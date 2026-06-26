"""Execution boundary: the socket-holding executor service and its client.

Referenced by: src/backend/services/workflow_service.py,
src/backend/services/dryrun_service.py, src/backend/api/endpoints.py,
docker-compose.yml (runner-exec service).
Depends on: src/backend/services/docker_service.py.
"""
