"""WS-G2 Risk Watcher (ADR 0033).

Scans per-client state for trouble signals and writes structured rows
to ``agent_outputs.risk_flags``. Profiles are tuples of ``Signal``
objects; ``RiskWatcher._run`` evaluates each signal against a
``ClientState`` snapshot and emits flags that fire.

PR-A (this PR): contracts + base class + empty e-commerce profile +
writer. PR-B fills in the e-commerce signals; PR-C wires routing.
"""
