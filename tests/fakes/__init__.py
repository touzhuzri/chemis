"""Test doubles for the provider layer.

`FakeTransport` is the offline seam; `fixtures` holds the synthetic payloads and
the stand-in parsers. Nothing here imports a network library, and
`tests/architecture/test_no_network.py` enforces that for `src/`.
"""
