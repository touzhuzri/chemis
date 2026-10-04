"""Policy: pure decision logic, no I/O, independently testable.

Kept out of `services` so that a rule like "a URL is not an authorisation" can
be tested without standing up a provider, and cannot be quietly relocated into
an adapter.
"""
