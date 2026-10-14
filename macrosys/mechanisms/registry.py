"""Implementation registry. ``@mechanism("B03")`` binds a class to a short id (spec §3.1, §3.3)."""

from __future__ import annotations

from typing import Any

_REGISTRY: dict[str, Any] = {}
_loaded = False


def mechanism(code: str):
    """Register ``cls`` under ``code`` (the ``A01`` / ``B03`` / ``C12`` prefix of the YAML id).

    The class must provide ``apply(inputs, params)`` and ``applicable(state, bindings)``
    (spec §8.5 ``Mechanism`` protocol, §3.4 rule 5).
    """

    def deco(cls):
        if code in _REGISTRY:
            raise RuntimeError(f"duplicate mechanism implementation {code}")
        obj = cls()
        obj.mechanism_id = code
        _REGISTRY[code] = obj
        return cls

    return deco


def load_impls() -> None:
    """Import implementation modules so their decorators run (spec §3.3)."""
    global _loaded
    if _loaded:
        return
    from macrosys.mechanisms import impl  # noqa: F401

    _loaded = True


def get_impl(spec_id: str):
    """Return the implementation for a full mechanism id such as ``B03_bottleneck_pricing``."""
    load_impls()
    code = spec_id.split("_", 1)[0]
    try:
        return _REGISTRY[code]
    except KeyError:
        raise KeyError(f"no implementation registered for {spec_id} (code {code})") from None


def registered_codes() -> set[str]:
    """Short ids that currently have an implementation (spec §3.1)."""
    load_impls()
    return set(_REGISTRY)
