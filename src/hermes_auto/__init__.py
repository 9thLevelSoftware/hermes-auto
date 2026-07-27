"""Hermes Auto Router: capability-, cost-, cache-, and outcome-aware model routing.

An out-of-tree distribution that gives Hermes Agent a virtual "Auto" model via a
model-provider plugin, a control plugin, and a local OpenAI-compatible routing
gateway supervised as a sidecar (design.md §1).
"""

from .version import __version__

__all__ = ["__version__"]
