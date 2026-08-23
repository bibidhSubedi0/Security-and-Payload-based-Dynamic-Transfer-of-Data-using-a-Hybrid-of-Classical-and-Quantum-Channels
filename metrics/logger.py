r"""
Structured JSON Logging Factory
==============================

Single factory every module in the project uses to obtain its logger:
metrics/logger consumers include classical.fault_injector,
transmission.echo_validation (plus its dedicated
echo_validation.SECURITY channel), server.ebit_server, and others.

-----------------
Design Contract
-----------------
Each call returns a logger that emits ONE JSON OBJECT PER EVENT on stdout:

    {"asctime": "...", "name": "fault_injector",
     "levelname": "INFO", "message": "Fault injected: bit errors",
     "packet_index": 3, "bit_errors": 5, ...}

Why JSON lines:
  - extra={...} keyword fields passed to .info()/.warning() merge into the
    record and surface as TOP-LEVEL JSON keys, so structured facts
    (packet_index, qber, outcome tokens) are machine-filterable without any
    message-string parsing by dashboards or log analysis.
  - The human-readable console print and the machine-readable stream are the
    SAME bytes: no parallel plain-text format to keep in sync.

Named-channel pattern (used by echo_validation.py):
    get_logger("echo_validation")            # normal INFO/WARNING traffic
    get_logger("echo_validation.SECURITY",   # security events
               level=logging.CRITICAL)
    In the JSON stream these produce "levelname": "CRITICAL" and
    "name": "echo_validation.SECURITY", making genuine security alerts
    visually and programmatically unmistakable next to routine INFO lines.

--------------
Relationships
--------------
  - Complements metrics.collector: the collector writes one deliberate summary
    row per transfer to transfers.jsonl; THIS module emits the high-frequency
    event stream (per-packet faults, echo phases, QKD steps) to stdout.
  - Callers own their event vocabulary; this factory owns only formatting,
    destination, level gating, and deduplication of handlers.

-----------
Integration
-----------
  - Every producer calls get_logger() once at module import and stores the
    result in a module-level `logger` variable.
  - Dependency note: requires the `pythonjsonlogger` package at import time;
    environments missing it fail here (this is the project's only direct
    consumer of that package).
"""

from __future__ import annotations

import logging
import sys
from pythonjsonlogger import jsonlogger


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    r"""
    Return a JSON-emitting logger, configuring it on FIRST call only.

    ----------
    Parameters
    ----------
    name : str
        Logger name; doubles as the "name" field in every emitted JSON line.
        Dot-separated names create the hierarchy used for channels
        ("echo_validation.SECURITY").
    level : int, default logging.INFO
        Minimum severity this logger will emit. Per-channel override lets a
        security channel gate at CRITICAL while normal traffic stays INFO.

    -------
    Returns
    -------
    logging.Logger
        Shared instance for `name`. Python's logging registry is global, so
        repeated calls anywhere in the process return the SAME object.

    -------------------------
    Idempotence Guard (why)
    -------------------------
    `if logger.handlers: return` makes re-imports and repeat calls free of
    side effects. Without it, every get_logger() call would add another
    StreamHandler, and each log event would print N copies (one per
    accumulated handler) — a classic doubled-log bug.

    ---------------------
    Configuration Details
    ---------------------
    - StreamHandler(sys.stdout): all JSON lands on stdout so the event stream
      interleaves with benchmark progress prints in one captured stream.
    - JsonFormatter fields: %(asctime)s (ISO, second resolution via datefmt,
      matching collector timestamp style), %(name)s, %(levelname)s,
      %(message)s. Any extra={} fields ride along as additional JSON keys.
    - propagate = False: records stop here instead of walking up to the root
      logger. With no root handlers configured, propagated records would fall
      through to logging's lastResort handler and print a SECOND, plain-text
      copy on stderr; disabling propagation guarantees exactly one JSON line
      per event.
    """
    logger = logging.getLogger(name)

    # First-call-only setup: guards against duplicate handlers (duplicated
    # output) when the same name is requested multiple times.
    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stdout)
    formatter = jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(level)

    # Terminal emitter: never forward to the root logger (prevents the
    # lastResort stderr fallback from duplicating every line).
    logger.propagate = False

    return logger
