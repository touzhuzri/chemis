"""The chemistry engine worker. Runs in its own environment.

This is the only file in the repository that imports AiZynthFinder, and it runs
under a Python that is not the one CREA runs under. That separation is the
point: AiZynthFinder 4.4.1 requires `rdkit<2024.0.0`, and CREA's chemistry core
is verified against `rdkit==2026.3.6`. Both are true only if neither process
sees the other's packages.

Protocol: one JSON request on stdin, one JSON response on stdout, then exit.
See `crea/engine/protocol.py`.

Everything it reports about itself -- versions, paths, what it could and could
not import -- is measured here rather than assumed by the caller.
"""

from __future__ import annotations

import json
import sys
import time
import traceback


def _versions() -> dict:
    """What actually answered. Not what a lock file promised."""
    out: dict = {"python": sys.version.split()[0]}
    try:
        import rdkit

        out["rdkit"] = rdkit.__version__
    except Exception as exc:  # noqa: BLE001
        out["rdkit"] = f"unavailable: {type(exc).__name__}"
    try:
        import aizynthfinder

        out["aizynthfinder"] = _engine_version()
    except Exception as exc:  # noqa: BLE001
        out["aizynthfinder"] = f"unavailable: {type(exc).__name__}"
    return out


def _engine_version() -> str:
    """The engine's own version.

    4.4.1 does not expose `__version__` on the module, so reading the
    attribute yields "unknown" and a caller comparing versions has nothing to
    compare. The installed distribution's metadata is the authoritative
    answer, so that is where this looks.
    """
    import aizynthfinder

    version = getattr(aizynthfinder, "__version__", None)
    if version:
        return str(version)
    try:
        from importlib.metadata import version as _dist_version

        return _dist_version("aizynthfinder")
    except Exception:  # noqa: BLE001
        return "unknown"


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


#: Echoed back on every response so a caller can match an answer to the line
#: that asked for it. One request per process today, so nothing depends on it.
_REQUEST_ID = ""


def _log(message: str) -> None:
    """Diagnostics go to stderr, never stdout.

    stdout is the protocol channel. A single print() of a warning would land in
    the middle of a JSON line and turn a working engine into a protocol error.
    """
    sys.stderr.write(str(message).rstrip() + "\n")
    sys.stderr.flush()


def _ok(result: dict, environment: dict) -> None:
    _emit({"protocol": "crea-engine/1", "ok": True,
           "result": result, "error": None, "environment": environment,
           "request_id": _REQUEST_ID})


def _fail(message: str, error_type: str, environment: dict,
          detail: dict | None = None) -> None:
    _emit({"protocol": "crea-engine/1", "ok": False, "result": {},
           "error": {"error_type": error_type, "message": message,
                      "detail": detail or {}},
           "environment": environment, "request_id": _REQUEST_ID})


# --- operations -------------------------------------------------------------


def op_info(env: dict) -> None:
    problem = _dependency_problem(env)
    result = {"capabilities": ["retrosynthesize"], "requires_config": True}
    if problem:
        result["usable"] = False
        result["problem"] = problem
    else:
        result["usable"] = True
    _ok(result, env)


def op_ping(env: dict) -> None:
    if env.get("aizynthfinder", "").startswith("unavailable"):
        _fail(f"aizynthfinder is not importable here: {env['aizynthfinder']}",
              "unavailable", env)
        return
    _ok({"alive": True}, env)


#: What AiZynthFinder 4.4.1 declares, read from its own metadata rather than
#: assumed. Asking the installed package is the only version of this question
#: that stays true when upstream changes.
def _dependency_problem(env: dict) -> str | None:
    """Why the engine could not run here, or None if it can.

    Two failures that look identical from the outside and must not be
    conflated: the engine is not installed, and the engine is installed
    alongside a dependency it cannot work with. The second is the one that
    would tempt somebody to "fix" it by downgrading RDKit.
    """
    if env.get("aizynthfinder", "").startswith("unavailable"):
        return "unavailable"

    try:
        from importlib.metadata import requires
    except Exception:  # noqa: BLE001
        return None

    try:
        declared = requires("aizynthfinder") or []
    except Exception:  # noqa: BLE001
        return None

    for requirement in declared:
        name, spec = _split_requirement(requirement)
        if name != "rdkit":
            continue
        if _satisfied(env.get("rdkit", ""), spec):
            continue
        return (f"aizynthfinder {env.get('aizynthfinder')} requires "
                f"{requirement}, but this environment has rdkit {env.get('rdkit')}")
    return None


def _split_requirement(requirement: str) -> tuple[str, str]:
    """`(name, version specifier)` from a `requires()` line.

    `importlib.metadata` returns whole requirement strings, not specifiers:
    `"rdkit (<2024.0.0,>=2023.9.1)"`. The name and the parentheses both have to
    come off before what is left is a specifier. `packaging` is the correct
    parser and is a pip dependency, so it is there; the manual path is only
    for the environment where it somehow is not.
    """
    text = requirement.strip()
    try:
        from packaging.requirements import Requirement

        parsed = Requirement(text)
        return parsed.name.lower(), str(parsed.specifier)
    except Exception:  # noqa: BLE001
        pass

    name = text.split("(", 1)[0].split("<", 1)[0].split(">", 1)[0]
    name = name.split("=", 1)[0].split("!", 1)[0].split("~", 1)[0]
    name = name.split(";", 1)[0].strip().strip("[]").strip()
    spec = text.split(";", 1)[0]
    if "(" in spec:
        spec = spec[spec.index("(") + 1:]
    spec = spec.rstrip(")").strip()
    return name.lower(), spec


def _satisfied(version: str, spec: str) -> bool:
    """`version` against a `>=x,<y` spec, without importing a resolver.

    Only the operators that appear in a runtime dependency pin are handled;
    anything else is reported as unsatisfied rather than assumed to be fine.

    RDKit version-stamps itself zero-padded (`2023.09.6`) while package
    metadata pins say `2023.9.1`. Comparing the strings would call a matching
    version different from its own pin, and reporting a working environment as
    broken is worse than not checking at all -- it invites somebody to "fix"
    something that is not broken.
    """
    import re

    if not version or "unavailable" in version:
        return False

    spec = spec.split(";", 1)[0].strip().strip("()").strip()
    if not spec:
        return True

    for part in spec.split(","):
        match = re.match(r"^(>=|<=|==|!=|>|<|~=)\s*([0-9][\w.]*)$", part.strip())
        if not match:
            return False
        operator, want = match.group(1), match.group(2)
        left, right = _tuple(version), _tuple(want)
        if operator == ">=" and not left >= right:
            return False
        if operator == "<=" and not left <= right:
            return False
        if operator == ">" and not left > right:
            return False
        if operator == "<" and not left < right:
            return False
        if operator == "==" and left != right:
            return False
        if operator == "!=" and left == right:
            return False
        if operator == "~=" and not (left >= right and left[:2] == right[:2]):
            return False
    return True


def _tuple(version: str) -> tuple[int, ...]:
    """`2023.09.6` and `2023.9.6` must compare equal.

    RDKit pads the month; the pins in package metadata do not. Comparing the
    strings would call a matching version different from its own pin.
    """
    parts: list[int] = []
    for chunk in version.strip().split(".")[:3]:
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def op_retrosynthesize(request: dict, env: dict) -> None:
    target = (request.get("target_smiles") or "").strip()
    if not target:
        _fail("no target smiles given", "invalid_request", env)
        return

    try:
        from aizynthfinder.aizynthfinder import AiZynthFinder
    except Exception as exc:  # noqa: BLE001
        _fail(f"aizynthfinder is not importable here: {exc}", "unavailable", env)
        return

    # imports, but will it work? A version conflict is its own answer, and
    # saying so here is what stops a caller from "fixing" it by downgrading
    # the RDKit the rest of CREA depends on.
    problem = _dependency_problem(env)
    if problem:
        _fail(problem, "incompatible_dependency", env,
              {"aizynthfinder": env.get("aizynthfinder"),
               "rdkit": env.get("rdkit")})
        return

    config_file = request.get("config_file")
    try:
        finder = AiZynthFinder(configfile=config_file or None)
    except Exception as exc:  # noqa: BLE001
        # A missing policy, stock or config is its own answer, not a crash.
        _fail(
            f"aizynthfinder could not be configured "
            f"({type(exc).__name__}: {exc}). It needs a stock file and an "
            f"expansion policy; see docs/RETROSYNTHESIS_RUNTIME.md.",
            "not_configured", env,
            {"config_file": config_file or None},
        )
        return

    started = time.perf_counter()
    try:
        finder.target_smiles = target
        max_score = finder.tree_search(show_progress=False)
        finder.build_routes()
    except Exception as exc:  # noqa: BLE001
        # "No expansion policy selected" is not a broken search. It is the
        # engine telling us it has no model, which is the same class of answer
        # as having no config -- and reporting it as a failure would tell a
        # caller to retry something that will never succeed without a policy.
        if _is_configuration_error(exc):
            _fail(
                f"the engine has no data to search with ({type(exc).__name__}: "
                f"{exc}). It needs an expansion policy and a stock file; see "
                f"docs/RETROSYNTHESIS_RUNTIME.md.",
                "not_configured", env,
                {"config_file": config_file or None},
            )
            return
        _fail(f"the tree search failed: {type(exc).__name__}: {exc}",
              "search_failed", env,
              {"traceback": traceback.format_exc()[-1200:]})
        return

    search_seconds = round(time.perf_counter() - started, 3)
    routes = [_route_to_dict(raw, env) for raw in list(getattr(finder, "routes", []) or [])]
    routes = routes[: int(request.get("max_routes") or 3)]

    _ok({
        "target_smiles": target,
        "routes": routes,
        "route_count": len(routes),
        "max_score": _number(max_score),
        "search_seconds": search_seconds,
        "config_file": config_file,
        "stock_info": _safe(lambda: dict(finder.stock_info())),
    }, env)


def _is_configuration_error(exc: Exception) -> bool:
    """Is this the engine saying it has no policy or stock, rather than failing?

    The engine raises these for missing data. Answering "the search failed"
    would be false -- the search never started -- and it would send a caller
    looking for a transient problem that a retry cannot fix.

    Recognised by type where the engine names one, and by message otherwise,
    because an import of the exception classes is not guaranteed to succeed
    in an environment too broken to configure.
    """
    name = type(exc).__name__
    if name in ("PolicyException", "StockException"):
        return True
    try:
        from aizynthfinder.utils.exceptions import PolicyException, StockException

        if isinstance(exc, (PolicyException, StockException)):
            return True
    except Exception:  # noqa: BLE001
        pass

    text = str(exc).lower()
    return any(phrase in text for phrase in (
        "no expansion policy",
        "no filter policy",
        "policyexception",
        "stockexception",
        "no stock",
    ))


def _route_to_dict(raw: dict, env: dict) -> dict:
    """Convert one upstream route, keeping its own score and shape.

    Nothing is invented here: a reaction SMILES is copied when the engine gave
    one and stays absent when it did not.
    """
    steps = []
    node = raw
    index = 0
    while node is not None and index < 64:
        index += 1
        reaction = getattr(node, "reaction", None)
        reactants = _side(reaction, "reactants")
        products = _side(reaction, "products")
        reaction_smiles = _safe(lambda r=reaction: r.reaction_smiles())
        steps.append({
            "index": index,
            "reactants": reactants,
            "products": products,
            "reaction_smiles": reaction_smiles,
            "smarts": _safe(lambda r=reaction: getattr(r, "smarts", None)),
            "score": _number(getattr(node, "score", None)),
            "is_solved": _safe(lambda n=node: bool(n.is_solved)),
            "n_children": len(getattr(node, "children", []) or []),
        })
        node = getattr(node, "parent", None)

    return {
        "route_index": getattr(raw, "route_index", None),
        "score": _number(getattr(raw, "score", None)),
        "max_transforms": _number(getattr(raw, "max_transforms", None)),
        "is_solved": _safe(lambda r=raw: bool(r.is_solved)),
        "steps": steps,
    }


def _side(reaction, name: str) -> list[str]:
    if reaction is None:
        return []
    try:
        groups = getattr(reaction, name)()
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for group in groups or ():
        for molecule in group:
            smiles = getattr(molecule, "smiles", None)
            if smiles:
                out.append(smiles)
    return out


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- entry ------------------------------------------------------------------


def main() -> int:
    global _REQUEST_ID

    env = _versions()
    _log(f"worker starting: {env}")
    line = sys.stdin.readline()
    if not line.strip():
        _fail("no request received on stdin", "empty_request", env)
        return 1
    try:
        request = json.loads(line)
    except ValueError as exc:
        _fail(f"the request was not JSON: {exc}", "invalid_request", env)
        return 1
    if not isinstance(request, dict):
        _fail(f"the request was {type(request).__name__}, not an object",
              "invalid_request", env)
        return 1

    _REQUEST_ID = str(request.get("request_id") or "")
    # `op` is the field this project has always sent; `method` is the same
    # thing under the name the protocol spec uses, and both are accepted so a
    # caller written to either does not get a bare "unknown op".
    op = request.get("op") or request.get("method")
    _log(f"worker received op={op!r} request_id={_REQUEST_ID!r}")
    try:
        if op == "ping":
            op_ping(env)
        elif op == "info":
            op_info(env)
        elif op == "retrosynthesize":
            op_retrosynthesize(request, env)
        else:
            _fail(f"unknown op {op!r}", "unknown_op", env,
                  {"supported": ["ping", "info", "retrosynthesize"]})
    except Exception as exc:  # noqa: BLE001 - the worker must never die silently
        _fail(f"unhandled worker error: {type(exc).__name__}: {exc}",
              "worker_exception", env,
              {"traceback": traceback.format_exc()[-1200:]})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
