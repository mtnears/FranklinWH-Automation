#!/bin/bash
cd /volume1/docker/franklin || exit 1
exec /volume1/docker/franklin/venv311/bin/python3 /volume1/docker/franklin/smart_decision.py
