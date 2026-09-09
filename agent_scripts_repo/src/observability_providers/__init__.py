"""Observability provider plugins.

Each module in this package exposes a ``Provider`` class implementing the
provider protocol consumed by :class:`observability.ObservabilityManager`:

    class Provider:
        name = "<backend>"
        def __init__(self, settings: dict, service_name: str): ...
        def setup(self) -> None: ...
        @contextmanager
        def start_span(self, name, *, input=None, metadata=None): ...   # yields a span handle
        @contextmanager
        def start_generation(self, name, *, model=None, input=None,
                            model_parameters=None, metadata=None): ...   # yields an LLM-generation handle
        def update_trace(self, *, session_id=None, user_id=None,
                         input=None, output=None, metadata=None, tags=None) -> None: ...
        def inject_context(self, carrier: dict) -> dict: ...
        @contextmanager
        def use_remote_context(self, carrier: dict): ...                # yields None
        def flush(self) -> None: ...

A span handle is any object with ``update(**kwargs)`` and ``end()`` methods. A
generation handle is the same, but its ``update`` also accepts ``usage`` (token
counts, e.g. ``{"input": .., "output": .., "total": ..}``), ``model`` and
``cost`` so the backend can record LLM token usage and cost. Backends without a
dedicated generation type may simply reuse their span implementation.

Providers are loaded by name (matching the module filename) from the
``[observability] providers`` list in ``config.ini``. To add a backend
(Langsmith, Phoenix, ...), drop a new ``<name>.py`` module here exposing a
``Provider`` class and add ``<name>`` to that config list — no change to
``observability.py`` is required.
"""
