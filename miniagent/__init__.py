"""miniagent — a small, bounded multi-agent collaboration system.

Layers (each file has one job):

* ``providers.py`` / ``provider_http.py`` — DeepSeek Chat Completions client and an
  offline scripted provider.
* ``tools.py`` and the tool modules — file system, code execution, shell, browser
  and web research tools, all confined to an agent's sandbox (``sandbox.py``).
* ``agent.py`` — one agent's bounded Reason–Act–Observe loop, with
  ``context.py`` (compaction) and ``loop_guard.py`` (stuck detection).
* ``workspace.py`` — private agent workspaces and the shared team project.
* ``team.py`` — lead/member collaboration: delegation, questions to the
  requester, memory.
* ``store.py`` / ``studio.py`` / ``server.py`` / ``web/`` — persistence, the
  service and the local web UI. ``cli.py`` — command line.
"""

__version__ = "1.0.0"
