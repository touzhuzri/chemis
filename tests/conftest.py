import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from crea.chem.normalize import normalize

# `.env` is loaded before collection, not at the point of use. Skip markers are
# evaluated during collection, and a live test that silently skips because the
# operator's flag was in a file rather than in the shell is worse than one that
# does not exist: it looks like a pass.
from crea.core.env import load_local_env

#: Live gates that must be asked for in the shell. `.env` is where credentials
#: belong; consent to spend somebody else's quota is a different kind of thing.
_LIVE_GATES = ("CREA_LIVE_TESTS", "CREA_AIZYNTHFINDER_LIVE")

_EXPLICIT_LIVE = {name: os.environ.get(name) for name in _LIVE_GATES}

load_local_env()

for _name in _LIVE_GATES:
    if _EXPLICIT_LIVE[_name] != "1":
        # `.env` is where credentials belong, and `CREA_LIVE_TESTS` is a
        # reasonable line to find in one -- an operator who filled it in once
        # should not have to export it every session. It is also how a bare
        # `pytest` ends up spending forty seconds inside a provider's rate
        # limiter and then failing on somebody else's quota.
        #
        # So the file supplies credentials and the shell supplies consent. A
        # live suite is a decision somebody made in this invocation; a file
        # that happens to be lying around is not a decision.
        # `CREA_LIVE_TESTS=1 pytest` still works, because the shell value is
        # read before the file is loaded and `load_dotenv(override=False)`
        # will not undo it.
        #
        # Providers are untouched by this: nothing in `src/crea` reads either
        # of these. They are test-suite gates and are treated as one.
        os.environ.pop(_name, None)


@pytest.fixture(autouse=True, scope="session")
def _no_network():
    """Make the default suite unable to reach the network.

    The requirement is that `pytest` is offline by default, not that it happens
    to be. Rather than auditing for accidental calls, this removes the
    possibility: an outbound connection raises, and the test that triggered it
    fails.

    `CREA_LIVE_TESTS=1 pytest -m live` lifts the guard, because the live suite
    in `tests/live/` is the one thing entitled to reach a provider. Lifting it
    is a whole-session decision and is a little blunt: the offline tests still
    run under it, but they inject a `FakeTransport` and assert on it, so they
    cannot silently start making real calls.

    Loopback is deliberately allowed either way. On Windows `asyncio.run` builds
    a ProactorEventLoop whose self-pipe is a real TCP connection to 127.0.0.1, so
    a blanket block fires during event-loop setup and is not a network call at
    all. Blocking non-loopback traffic keeps the guarantee that matters -- no
    request can leave the machine -- at the cost of not being able to stop a
    test from talking to a server running on localhost. Nothing here does.
    """
    # One name, shared with the live tests. They used to disagree -- the guard
    # watched CREA_LIVE_TESTS while the tests watched CREA_LIVE_TESTS -- so an
    # operator who enabled live tests had them blocked by the guard instead.
    if os.environ.get("CREA_LIVE_TESTS") == "1":
        yield
        return

    loopback = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}

    def is_loopback(address) -> bool:
        if isinstance(address, (tuple, list)) and address:
            host = str(address[0])
            return host in loopback or host.startswith("127.")
        return not isinstance(address, (tuple, list))   # AF_UNIX and friends

    def guard(original):
        def wrapper(sock, address, *args, **kwargs):
            if is_loopback(address):
                return original(sock, address, *args, **kwargs)
            raise AssertionError(
                f"the default test suite attempted an outbound connection to "
                f"{address!r}. Live calls belong in tests/live/ behind "
                f"CREA_LIVE_TESTS."
            )
        return wrapper

    def guard_create_connection(original):
        def wrapper(address, *args, **kwargs):
            if is_loopback(address):
                return original(address, *args, **kwargs)
            raise AssertionError(
                f"the default test suite attempted an outbound connection to "
                f"{address!r}. Live calls belong in tests/live/ behind "
                f"CREA_LIVE_TESTS."
            )
        return wrapper

    originals = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection)
    socket.socket.connect = guard(originals[0])
    socket.socket.connect_ex = guard(originals[1])
    socket.create_connection = guard_create_connection(originals[2])
    try:
        yield
    finally:
        (socket.socket.connect, socket.socket.connect_ex,
         socket.create_connection) = originals


# Real molecules. Every expectation in the suite refers to these, so a change in
# RDKit behaviour shows up as a failing test rather than as a silently different
# number in a report.
DIPHENYLACETYLENE = "c1ccccc1C#Cc1ccccc1"
DIPHENYL_BUTADIYNE = "c1ccccc1C#CC#Cc2ccccc2"
PHENYLACETYLENE = "C#Cc1ccccc1"
BENZENE = "c1ccccc1"
TOLUENE = "Cc1ccccc1"
CYCLOHEXANE = "C1CCCCC1"
PYRIDINE = "c1ccncc1"
O_XYLENE = "Cc1ccccc1C"
M_XYLENE = "Cc1cccc(C)c1"
P_XYLENE = "Cc1ccc(C)cc1"
ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"


@pytest.fixture
def diyne():
    return normalize(DIPHENYL_BUTADIYNE)


@pytest.fixture
def diphenylacetylene():
    return normalize(DIPHENYLACETYLENE)
