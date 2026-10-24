"""The nine agents. Each is a small class with one job; the orchestrator moves an incident between them.

These modules must never import incident_zero.actions.flags, incident_zero.practice, or the Docker write path
(actions.docker_control, actions.mock_server) (REQUIREMENTS R1, R3, F6.1); tests/test_actions.py enforces it.
"""
