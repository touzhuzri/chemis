# Retrosynthesis: engine, licence, models, and what is actually available

Everything here was read from upstream on **2026-10-01**. Where a fact could
not be established it says `UNKNOWN` rather than carrying a plausible guess.

---

## 1. The engine we looked at

**AiZynthFinder** — <https://github.com/MolecularAI/aizynthfinder>

| Fact | Value | How established |
|---|---|---|
| version examined | **4.4.1** | PyPI JSON (`pypi.org/pypi/aizynthfinder/json`) |
| licence | **MIT** | PyPI classifier *and* the GitHub repo's licence field — two independent sources agree |
| Python | **3.10–3.12** | README ("python 3.10 - 3.12") and `requires_python <3.13,>=3.10` |
| install | `pip install aizynthfinder` (or `[all]`); README recommends conda | README |
| CLI | `aizynthcli --config config.yml --smiles smiles.txt`, `aizynthapp --config config.yml` | README |
| Python API | `AiZynthFinder(configfile=None, configdict=None)`; `.target_smiles`; `.tree_search(show_progress=False) -> float`; `.build_routes(selection=None, scorer=None)`; `.routes` | `aizynthfinder/aizynthfinder.py`, read from the repository |
| node structure | `.is_solved`, `.children`, `.score`, `.reaction` | `search/mcts/node.py` |
| reaction structure | `RetroReaction.reaction_smiles()`, `.reactants()`, `.smiles()` | `chem/reaction.py` |
| requires | a stock file, an expansion policy, optionally a filter policy | README |
| model/data source | figshare, or `download_public_data <folder>` which writes a `config.yml` | README |
| GPU | not stated as required | — |
| fully offline | possible once models and stock are downloaded | inferred from the download step, **not verified** |

## 2. It cannot be installed in this project

**`aizynthfinder` 4.4.1 declares `rdkit>=2023.9.1,<2024.0.0`.
This project pins `rdkit==2026.3.6`.**

That is a two-major-release gap, and the Chemical Core is verified against the
newer RDKit: the aromatic-versus-aliphatic bond comparison in
`core/policy/document.py`, the MCS parameter choices in `chem/align.py`, and
the o/m/p-xylene attachment results in Phase 1 all depend on current behaviour.

Installing AiZynthFinder would mean downgrading RDKit and re-verifying all of
Phase 1. **That is a decision for a person, so it has not been made.**

> **How this was resolved in Phase 3.4.** The person decided: *not* to downgrade.
> The engine runs in a separate environment and is reached over a pipe, so both
> facts hold at once — CREA stays on `rdkit==2026.3.6`, and the engine gets the
> RDKit it needs. See `docs/RETROSYNTHESIS_RUNTIME.md` for the runtime, and
> `sources/aizynthfinder.py` below for the in-process adapter this avoids.
> The decision about the *data* — the policy and stock licences — is still
> open, and is recorded as `UNKNOWN` rather than assumed.

`AiZynthFinderAdapter` therefore reports **`UNAVAILABLE`**, with the reason, and
returns no routes. `FakeRetrosynthesisProvider` — a fixture table, explicitly
not an engine — carries the offline suite.

Three states, all tested:

```
AVAILABLE         installed, configured, and it ran
UNAVAILABLE       not importable
NOT_CONFIGURED    importable, but no config / stock / policy
```

`FAILED` covers a run that started and errored, and also returns no routes.

## 3. What the model and data pieces are, and what we do not know

| Piece | Source | Licence |
|---|---|---|
| the software | PyPI / GitHub | **MIT**, confirmed |
| the expansion policy model | figshare (linked from the README) | **UNKNOWN** — a trained artefact is not the code's licence and we did not read the figshare terms |
| the filter policy model (optional) | a second figshare dataset, linked from the README | **UNKNOWN** |
| the stock compound list | distributed with the models | **UNKNOWN** |

Whether the model weights and the stock list permit commercial use is
**UNKNOWN**. We did not read the figshare terms, and a training artefact's
rights are not the code's rights. Anyone deploying this commercially should
settle that before relying on the engine.

The package was **not installed** and none of the model data was downloaded, so
nothing about the engine's runtime behaviour is verified here. Its *interface*
is verified against the repository source.

## 4. Optional dependency policy

AiZynthFinder is **not** in `pyproject.toml`'s core dependencies. It is not even
in an extra yet, because pinning a version that cannot resolve against this
project's RDKit would be worse than leaving it out. When the RDKit conflict is
resolved — by an AiZynthFinder release that accepts a current RDKit, or by
running the engine in a separate environment — add:

```toml
[project.optional-dependencies]
retrosynthesis = ["aizynthfinder==<version verified at the time>"]
```

The adapter already supports `config_file`, so a configured engine needs no code
change.

## 5. Running it elsewhere

If AiZynthFinder is installed in an environment the process can reach:

```python
from crea.sources.aizynthfinder import AiZynthFinderAdapter

adapter = AiZynthFinderAdapter(
    config_file="path/to/config.yml",
    stock_file="path/to/stock.csv.gdb",
    model_version="<what figshare gave you>",
)
```

Then `crea research "分析 … 的合成路线"` drives it. The adapter converts and
returns; it does not search, reason, read, or cite.

## 6. Known limitations

1. **The real engine has never run here.** The adapter's conversion code is
   written against the upstream source, and the offline suite exercises it
   through a scripted client. **The live engine path is UNVERIFIED.**
2. `transformation_class` is `UNKNOWN` for every route. AiZynthFinder reports
   the template, not a reaction class, and filling one in would be invention.
3. Stock flags are recorded as *the provider's stock list saying something*.
   Nothing here claims a vendor can supply a compound.
4. A route score is the engine's own Monte-Carlo heuristic. It is reported
   under `score_source` and never as a confidence.
5. `max_routes` truncation is the provider's ordering, not a ranking we applied.
6. Route comparison is a table of facts side by side. There is no combined
   score and no winner, deliberately — ranking candidate routes is a different
   decision from describing them, and nothing here has earned a verdict.