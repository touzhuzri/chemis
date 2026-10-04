# License status

Three states only. A status is never upgraded on the strength of a package's
reputation, a GitHub API that returned nothing, or a classifier field that came
back blank.

| Component | State | License | How it was established |
|---|---|---|---|
| `rdkit` 2026.3.6 | **CONFIRMED** | BSD-3-Clause | PyPI project metadata; wheel sha256 matched the published digest |
| `httpx` | **CONFIRMED** | BSD-3-Clause | GitHub LICENSE (encode/httpx) |
| `tenacity` | **CONFIRMED** | Apache-2.0 | GitHub LICENSE (jd/tenacity) |
| `anthropic` 1.9.0 | **CONFIRMED** | MIT | PyPI classifier |
| `pypdf` 6.19.0 | **CONFIRMED** | BSD-3-Clause | Upstream `py-pdf/pypdf` LICENSE read directly: three-clause BSD text (redistribution grant, three conditions, warranty disclaimer). `requires_python >=3.9`; pure-Python `py3-none-any` wheel; sole runtime dep is `typing_extensions>=4.0` (already present) |
| `pymupdf` 1.28.2 | **EXCLUDED** | AGPL-3.0 / commercial | PyPI metadata: dual licensed; AGPL is file- and network-copyleft, so it is kept out of the dependency set |
| `pydantic` 2.13.5 | **UNCONFIRMED** | believed MIT | Installed; the LICENSE file was not read end to end |
| `pydantic-settings` 2.15.0 | **UNCONFIRMED** | believed MIT | GitHub LICENSE observed |
| `pydantic-core` 2.46.5 | **UNCONFIRMED** | believed MIT | Not verified; this is a compiled Rust extension and the one to check first |
| `pytest` 9.1.1 | **UNCONFIRMED** | believed MIT | Not verified |
| `numpy` 2.2.6 | **UNCONFIRMED** | believed BSD-3-Clause | GitHub API returned `NOASSERTION` (rate limit, not a missing license) |
| `Pillow` 12.3.0 | **UNCONFIRMED** | believed MIT-CMU | Pulled in as a transitive RDKit dependency |
| `typing-extensions` 4.16.0 | **UNCONFIRMED** | believed PSF-2.0 | Transitive |
| `python-dotenv` 1.2.3 | **UNCONFIRMED** | believed BSD-3-Clause | Transitive via pydantic-settings |
| `iniconfig`, `packaging`, `pluggy`, `tomli`, `pygments`, `colorama`, `exceptiongroup` | **UNCONFIRMED** | various permissive | Transitive via pytest |
| `aizynthfinder` | **DEFERRED** | MIT (code) | Code MIT; default model weights are USPTO-derived and were not licence-reviewed. Phase 5+ |
| `ASKCOS` | **EXCLUDED** | MPL-2.0 + non-commercial data | Repo LICENSE is MPL-2.0 and excludes `makeit/data`; default model weights derive from Reaxys and are not free for commercial use |
| `DECIMER-Image-Transformer`, `DECIMER-Image-Segmentation` | **DEFERRED** | MIT | Phase 5+ |
| `DECIMER-Image-to-SMILES` | **EXCLUDED** | NOASSERTION | No clear licence grant. Not used |
| Web of Science / Springer TDM | **DEFERRED** | subscription | Requires institutional licence; TDM terms generally forbid bulk download and redistribution |

## Consequence for this phase

Phase 1 depends on exactly one chemistry package (`rdkit`, CONFIRMED) and
three data-model/test packages whose licences are widely considered permissive
but which this repository has not audited file by file. **Before any
distribution of this code, every `UNCONFIRMED` row must be moved to CONFIRMED
or EXCLUDED by reading the actual LICENSE file.** `pydantic-core` is the row
with the least margin: it is a compiled binary dependency and a licence
question there is not academic.

## Data, as distinct from code

- PubChem: public domain, commercially usable.
- OpenAlex metadata: CC0.
- Crossref metadata: public.
- Unpaywall: returns links only; it is never a licence to fetch closed content.
- Reaxys / Pistachio: commercial, not redistributable. Not used.
- USPTO reaction data: usable, but patent-derived; `aizynthfinder` weights
  inherit that question and are deferred with it.

## CREA 4.0 integration candidates (Stage A, 2026-10-03)

Verification method matters as much as the verdict: every CONFIRMED row below
was established by reading a LICENSE file or an authoritative POM/`METADATA`
block actually fetched -- not by reputation.

| Component | State | License | How it was established |
|---|---|---|---|
| `opsin` 2.9.0 (uk.ac.cam.ch.opsin) | **CONFIRMED** | MIT | Maven Central `opsin-2.8.0.pom` `<licenses>` block read directly (`<name>MIT License</name>`); versions through 2.9.0 in `maven-metadata.xml`. Community claims of LGPL are **wrong**. Integration plan: Java library behind an isolated worker (the AiZynthFinder pattern) |
| `rxnmapper` 0.4.3 | **CONFIRMED** (code) | MIT | PyPI wheel `rxnmapper-0.4.3.dist-info/licenses/LICENSE` read directly (RXN4Chemistry team, 2020). **Model weights**: transformer checkpoints' terms NOT reviewed -- row stays DEFERRED for the weights until read |
| `openbabel-wheel` 3.1.1.23 | **EXCLUDED** | GPL-2.0 | Wheel `dist-info/licenses/LICENSE` contains the full GNU GPL Version 2 text; classifier `License :: OSI Approved :: GNU General Public License (GPL)`. File-level copyleft conflicts with this repository's Proprietary license. Also redundant: RDKit already covers SMILES/InChI/MOL/SDF interop |
| NCI CADD Chemical Identifier Resolver (cactus.nci.nih.gov) | **DEFERRED** | service terms not obtained | Endpoint **verified live** (`phenylacetylene -> C#Cc1ccccc1`; `536-74-3 -> C#Cc1ccccc1`; names incl. CAS/EINECS), reachable without a key. The disclaimer page could not be fetched (`/disclaimer` 404) and the site blocks automated access; a human must read the service terms before caching beyond incidental use |
| CDXML / ChemDraw clipboard formats | **DEFERRED** | spec terms not obtained | See docs/CHEMICAL_CLIPBOARD.md -- no ChemDraw on the verification machine; formats must be enumerated empirically before any code assumes them |

### Consequence for Stage B

The name-to-structure gap has a licence-clean candidate (OPSIN, MIT) and a
licence-clean code base for atom mapping (RxnMapper MIT, weights pending).
Open Babel is closed out permanently unless the project relicences. NCI CIR
is technically the widest identifier resolver verified so far and must have
its service terms read before it becomes a permanent dependency path.
