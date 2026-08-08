#!/bin/sh
set -eu

docker compose run --rm orchestrator \
  python -m py_compile \
  app/state.py \
  app/github_client.py \
  app/workspace.py \
  app/repository_context.py \
  app/test_runner.py \
  app/agents/planner.py \
  app/agents/coder.py \
  app/agents/reviewer.py \
  app/graph.py

docker compose run --rm orchestrator \
  python -m unittest discover -s tests -p 'test_*.py'
