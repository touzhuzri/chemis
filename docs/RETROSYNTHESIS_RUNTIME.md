# The chemistry engine runtime

How AiZynthFinder actually runs here, what it needs, and what is still unknown
about its licensing.

`docs/RETROSYNTHESIS.md` covers *whether* the engine is usable. This file covers
the runtime that Phase 3.4 built around it, and it is the file
`workers/chemistry/worker.py` points at when it has to tell somebody what is
missing.

Verified against upstream on **2026-10-01**.

---

## 1. The shape of it

```
CREA main process          rdkit==2026.3.6   (pyproject pins it; Phase 1 is
  │                                              verified against it)
  │  JSON Lines over a pipe, one process per request
  ▼
workers/chemistry/worker.py
  │  runs under its own interpreter
  ▼
runtime/aizynthfinder-env  python 3.10.11, aizynthfinder 4.4.1,
                           rdkit>=2023.9.1,<2024.0.0
  │
  ▼
AiZynthFinder
```

The main process never imports `aizynthfinder`. That is not a style preference:
`tests/unit/test_engine_protocol.py::test_nothing_in_the_main_environment_imports_the_engine`
asserts it by importing CREA in a subprocess and checking `sys.modules`, and
`::test_the_main_rdkit_is_untouched` asserts the main RDKit still reports
`2026.*`.

**Downgrading the main environment's RDKit to fit the engine is not on the
table.** The engine runs elsewhere.

## 2. Installing the runtime

```bash
# Python 3.10-3.12 only. aizynthfinder 4.4.1 declares requires_python <3.13,>=3.10
python -m venv runtime/aizynthfinder-env

runtime/aizynthfinder-env/Scripts/python.exe -m pip install aizynthfinder==4.4.1
runtime/aizynthfinder-env/Scripts/python.exe -m pip list      # check what landed
```

This pulls a large transitive set — `jupyter`, `notebook`, `matplotlib`,
`pandas`, `numpy<2`, `pyarrow`, `onnxruntime`, `tables`, `scipy` and others.
It is a slow install and nothing is wrong while it is running: pip downloads
everything before it writes anything into `site-packages`, so the directory
staying near-empty for a long time is expected, not a hang.

It is **not** an optional dependency of the project. `pyproject.toml` does not
list `aizynthfinder`, in any extra, because a pin that cannot resolve against
this project's RDKit would be worse than leaving it out. The runtime is a
separate environment, installed by hand, exactly as above.

## 3. The model and the stock

The engine does not ship the two things it needs to search. Both come from
outside the wheel, and neither is a code dependency.

| Piece | What it is | Where it comes from |
|---|---|---|
| **expansion policy** | the model that proposes bonds to break | figshare, linked from the upstream README |
| **filter policy** | optional; ranks the routes the expansion proposed | a second figshare dataset |
| **stock file** | the list of purchasable building blocks the search terminates against | distributed with the models |

Upstream ships `aizynthcli download_public_data <dir>`, which writes a
`config.yml` alongside the downloaded data. CREA does not run that, and does
not download any of it, automatically: see §4.

Give the resulting config to the adapter:

```python
from crea.engine.client import EngineClient
from crea.providers.worker_retrosynthesis import WorkerRetrosynthesisProvider

provider = WorkerRetrosynthesisProvider(
    client=EngineClient(
        python_executable="runtime/aizynthfinder-env/Scripts/python.exe",
        worker_script="workers/chemistry/worker.py",
    ),
    model_version="<what figshare told you>",
    stock_configuration="<which stock list, and which version>",
)
```

`model_version` and `stock_configuration` are **provenance fields only**. They
are recorded next to every route the engine returns. They are not used to make
a route look better supported than it is, and leaving them `None` is a normal,
honest state.

## 4. What was and was not downloaded here

| Item | State on 2026-10-01 |
|---|---|
| `aizynthfinder` 4.4.1 in the runtime | see §6 |
| expansion policy | **not downloaded** |
| filter policy | **not downloaded** |
| stock file | **not downloaded** |
| `config.yml` | **not present** |

Nothing is fetched automatically. Auto-downloading model and stock data on
first use would mean downloading artefacts whose terms this project has not
established, from a URL that could change, into an environment nobody chose to
put them in. If you want the engine to run, that download is a decision you
make, and you make it against the licence question in §5 rather than because a
tool did it for you.

Until then the engine reports itself honestly:

```
probe()  -> (NOT_CONFIGURED, "…needs a stock file and an expansion policy…")
retrosynthesize() -> RetrosynthesisResult(routes=[], status=NOT_CONFIGURED,
                                          reason_code=INVALID_CONFIGURATION)
```

## 5. Licensing — recorded separately on purpose

The code's licence and the data's licence are different questions with
different answers. Conflating them is how a project ends up shipping something
it has no right to ship.

| Piece | Licence | How established |
|---|---|---|
| **the software** | **MIT** | PyPI classifier *and* the GitHub repository's licence field — two independent sources agree |
| **expansion policy model** | **UNKNOWN** | figshare, linked from the README. We did not read the figshare terms |
| **filter policy model** | **UNKNOWN** | same |
| **stock compound list** | **UNKNOWN** | distributed with the models; its own terms were not read |
| **the data terms generally** | **UNKNOWN** | — |

**`UNKNOWN` means we did not establish it. It does not mean permissive, and it
does not mean restricted.** A trained artefact's rights are not the code's
rights, and a file being downloadable is not a grant of any right to
redistribute it or use it commercially.

Before this is used in anything commercial, someone has to read the figshare
terms for each downloaded artefact and settle it. That is a question for a
person, and Phase 3.4 did not answer it.

## 6. Runtime status as of 2026-10-01

The runtime environment exists (`runtime/aizynthfinder-env`, Python 3.10.11).

To find out what state the engine is actually in, ask it rather than reading a
note that may be out of date:

```bash
CREA_AIZYNTHFINDER_LIVE=1 pytest tests/live/test_aizynthfinder_live.py -v
```

That suite skips with a named reason for whatever is missing — no interpreter,
no engine, no policy, no stock — and never fabricates a route to pass. The
answer it gives is a measurement; this file's §4 table is a note, and a note
goes stale.

## 7. Status vocabulary

`ProviderStatus` is about whether the provider ran; `ProviderReason` is about
why, in a form code can branch on. They are not the same question.

| Status | Means |
|---|---|
| `AVAILABLE` | it ran and returned routes |
| `UNAVAILABLE` | not installed, or not importable here |
| `NOT_CONFIGURED` | installed, but no policy / stock / config |
| `FAILED` | it started and errored |

| Reason code | Raised when |
|---|---|
| `NONE` | it ran |
| `MISSING_RUNTIME` | no worker, or no interpreter for it |
| `INCOMPATIBLE_DEPENDENCY` | it imports, but a dependency beside it is the wrong version |
| `INVALID_CONFIGURATION` | it needs a policy, a stock file or a config, and we cannot say which |
| `WORKER_TIMEOUT` | it started and stopped answering |
| `WORKER_CRASH` | it started and died |
| `PROTOCOL_ERROR` | it answered with something unreadable |
| `MISSING_MODEL` | available for a caller that *knows* the policy is absent |
| `MISSING_STOCK` | available for a caller that *knows* the stock is absent |
| `UNKNOWN` | a failure we cannot name |

`MISSING_MODEL` and `MISSING_STOCK` are deliberately **not** inferred from the
worker's `not_configured` error. That error does not say which of the two was
missing, and reading it out of the message text would be a guess. The codes are
there for a caller that genuinely knows; nothing on this path pretends to.

`INCOMPATIBLE_DEPENDENCY` is checked by the worker against the installed
package's own declared requirement, not against a note written down here — so
it stays true if upstream changes its pins.

## 8. What this runtime does not do

1. **It is not a daemon.** One process per request, started, answered, exited.
   Nothing accumulates between calls and nothing to supervise.
2. **It does not rank routes.** The engine's score is reported as its score,
   under `score_source`, and is never presented as a confidence or a
   recommendation.
3. **It does not promote a step.** A route from a real engine arrives as
   `CANDIDATE` / `no_support`, and the only thing that changes that is
   evidence, in `services/route_validation.py`.
4. **`transformation_class` stays `UNKNOWN`.** AiZynthFinder reports a template,
   not a reaction class, and filling one in would be invention.
5. **Stock flags are the stock list's opinion.** Nothing here claims a vendor
   can supply a compound.
6. **The engine has never read a PDF or searched CREA's literature.** It
   proposes; the evidence layer decides.
