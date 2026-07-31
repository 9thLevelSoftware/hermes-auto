"""Differential proof of R5: a fixed candidate behaves identically through the
gateway and called directly.

Every module here runs the *same* scripted upstream twice -- once straight from
``MockUpstream`` and once through a real supervised sidecar -- and compares the
two results under two reductions. See :mod:`tests.differential.harness`.
"""
