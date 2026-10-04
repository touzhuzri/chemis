# Phase 3.6 — DECIMER licence and runtime record

Four questions, four answers, kept apart on purpose. §13's rule is the reason
this file exists rather than a row in `docs/LICENSES.md`:

> 不要因为代码 MIT 就自动认为所有 weights/data 都是 MIT.

A software licence and a model licence are different grants from different
parties covering different artifacts. Collapsing them is how a project ends up
redistributing weights nobody cleared. So each of the four below carries its
own state, its own evidence, and its own consequence — and none of them borrows
another's.

The states match `docs/LICENSES.md`: **CONFIRMED** / **UNVERIFIED** /
**DEFERRED** / **EXCLUDED**. A state is never upgraded on the strength of a
reputation or a badge alone.

---

## 1. Software licence (the source code)

| Component | State | Licence | How it was established |
|---|---|---|---|
| `DECIMER.ai` (platform, Steinbeck-Lab) | **CONFIRMED** | MIT | Repository README badge `License: MIT`, and the README's own statement "MIT licensed for academic and commercial use". Copyright (c) 2022 Otto Brinkhaus |
| `DECIMER-Image-to-SMILES` (the OCSR model package) | **CONFIRMED** | MIT (code only) | Upstream `LICENSE` file, MIT text, Copyright (c) 2022 Otto Brinkhaus |
| `DECIMER-Image-Segmentation` (Kohulan/DECIMER-Image-Segmentation) | **UNVERIFIED** | believed MIT | Not read file-by-file in this phase |
| `DECIMER-Image_Transformer` (Kohulan/DECIMER-Image_Transformer) | **UNVERIFIED** | believed MIT | Not read file-by-file in this phase |
| `DECIMER-Image-Classifier` (Steinbeck-Lab/DECIMER-Image-Classifier) | **UNVERIFIED** | believed MIT | Not read file-by-file in this phase |

**Scope of this row.** MIT on the code grants use of the *source*. It says
nothing about the trained models, which are separate artifacts downloaded
separately and are answered in §2 below.

**Note on `docs/LICENSES.md`.** Phase 1 recorded `DECIMER-Image-to-SMILES` as
**EXCLUDED / NOASSERTION** ("No clear licence grant. Not used"). That row is
not silently upgraded here: what changed is that the `LICENSE` file was read
and is MIT **for the code**. The reason it was excluded — no clearance for the
weights — still stands and is restated in §3. Until §2 and §3 are cleared, the
package is **not used** in any distribution of CREA, and `docs/LICENSES.md`'s
EXCLUDED row remains the governing record for redistribution.

---

## 2. Model licence (the trained networks)

| Artifact | State | Licence | How it was established |
|---|---|---|---|
| DECIMER OCSR transformer weights (`DECIMER-Image-to-SMILES` releases) | **UNVERIFIED** | **not asserted** | The code is MIT; no separate grant for the weights was read. The weights ship as release assets, not as part of the MIT-licensed tree |
| DECIMER Segmentation Mask R-CNN weights (`Kohulan/DECIMER-Image-Segmentation`) | **UNVERIFIED** | **not asserted** | Same: code licence read, weights licence not |
| DECIMER Image Classifier CNN weights (`Steinbeck-Lab/DECIMER-Image-Classifier`) | **UNVERIFIED** | **not asserted** | Same |
| DECIMER.ai Docker image contents | **UNVERIFIED** | **not asserted** | A Docker image bundles code, weights and data; the bundle inherits whichever of the three is least cleared |

**This is the row §13 is about.** "MIT licensed for academic and commercial
use" in a README describes the project. It does not grant the weights, and a
README badge is not a licence. Until each weight set is read and cleared,
treat them as **not cleared for redistribution**.

**Consequence for this phase.** Nothing in this repository redistributes or
vendors a DECIMER weight file. The worker asks a locally-installed runtime for
a reading; the weights stay wherever that runtime's installer put them. This is
the same posture as `aizynthfinder`'s USPTO-derived weights (`docs/LICENSES.md`
DEFERRED).

---

## 3. Weights / training data provenance

| Artifact | State | Licence | How it was established |
|---|---|---|---|
| Training images and segmentation masks behind the OCSR model | **UNVERIFIED** | **not asserted** | Not reviewed in this phase; provenance documented in the DECIMER publications, not in a licence file |
| Training data behind the segmentation Mask R-CNN | **UNVERIFIED** | **not asserted** | Same |
| Training data behind the image classifier | **UNVERIFIED** | **not asserted** | Same |
| USPTO-derived reaction data (comparator, `aizynthfinder`) | **DEFERRED** | patent-derived | Already recorded in `docs/LICENSES.md` |

Training data is a third grant again. A model can be MIT and trained on data
that cannot be redistributed; a reader who treats the model file as
self-contained inherits the data question anyway, because the weights are a
derivative of it. Recorded here so the question is visible even though the
answer is not yet known.

---

## 4. Runtime requirements

Recorded because §13 separates *licence* from *requirements*, and because the
requirement is the reason this component is isolated rather than installed.

| Requirement | Value | Source |
|---|---|---|
| Isolation | Own Python environment / own Docker stack; **never** installed into the CREA main env | §12, AiZynthFinder-style worker boundary |
| Python | 3.9+ (platform README) | DECIMER.ai README badges |
| TensorFlow | 2.15.0 (README badge) | DECIMER.ai README |
| RDKit | Platform ships one; **not** the CREA pin | DECIMER.ai README badge shows RDKit 2024 |
| RAM | 8 GB minimum, 16 GB+ recommended | DECIMER.ai README requirements table |
| Storage | 10 GB minimum, 20 GB+ recommended | DECIMER.ai README requirements table |
| Docker | Required for the full DECIMER.ai platform | DECIMER.ai README requirements table |
| Model warm-up | 5–10 minutes on first start (large networks load) | DECIMER.ai README |
| Network at recognition time | **None** | Worker design; recognition is local |
| Network at install time | Yes, to fetch packages and weights | Out of scope for this phase |

**The RDKit row is the one that matters for CREA.** The platform's own badge
says RDKit 2024; CREA's main environment is pinned to `rdkit==2026.3.6` and
verified against a test suite. They must not share an environment, which is
exactly what §0 and §12 require and what the worker boundary enforces. No
dependency of this phase touches that pin.

**What was not installed.** Nothing. §12 says the DECIMER runtime is optional
and must not be force-installed into the main environment; Phase 3.6 ships the
adapter and the worker protocol and stops there. If `runtime/decimer-env` (or
`CREA_DECIMER_PYTHON`) is absent, `crea.providers.decimer.DecimerImageReader`
reports `Availability.MISSING_KEY`, `IdentityResolutionService.resolve_by_image`
reports `UNAVAILABLE` with `NO_RECOGNIZER`, and every other input kind is
unaffected. Installation is a later, separate, licence-gated act.

---

## 5. What this file does not claim

- It does not claim any DECIMER weights are MIT.
- It does not claim the training data is redistributable.
- It does not claim the Docker image is redistributable.
- It does not clear `docs/LICENSES.md`'s EXCLUDED row for `DECIMER-Image-to-SMILES`.
- It does not claim OCSR output is evidence of anything. That is a §15/§23
  question about provenance, not a licence question, and it is answered in
  `src/crea/core/models/identity.py`.
