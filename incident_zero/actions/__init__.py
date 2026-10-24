"""The Action API: the only way Incident Zero changes the system (REQUIREMENTS F4.1, F6.1).

  catalog.py      the runbook actions the mock exposes, and parameter validation
  client.py       the HTTP client the Planner and Executor use (swap for the jury's API: one adapter)
  mock_server.py  a stand-in Action API over Docker, run as its own process (`incident_zero action-api`)
"""
