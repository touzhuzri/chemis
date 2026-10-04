"""Capture real PUG REST payloads as offline fixtures.

Not run by the test suite. Run by hand, once, to refresh the fixtures, and
every payload it writes is labelled with the exact URL it came from and the
moment it was fetched. The alternative -- hand-writing JSON that *looks* like a
PubChem response -- is how a test suite ends up asserting its own imagination,
and §21 forbids it: hand-built fixtures must say `origin = SYNTHETIC` and must
never pose as a real upstream response.

So there are two kinds of fixture in this directory and the `.meta.json` says
which is which:

  origin = LIVE_CAPTURE   bytes taken from the real PUG REST endpoint
  origin = SYNTHETIC      written by hand, never claiming to be upstream

PubChem payloads are U.S. Government work and carry no copyright. The
`license` field records that fact rather than leaving the column blank.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "pubchem"
UA = "crea-chemical-research-agent/0.1 (structure ingestion; offline fixtures)"
BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"

#: name, path, query-string, description
CAPTURES = [
    ("name_phenylacetylene.json",
     "compound/name/phenylacetylene/property/"
     "SMILES,ConnectivitySMILES,InChI,InChIKey,MolecularFormula,"
     "MolecularWeight,Title/JSON",
     "single unambiguous common name"),
    ("name_vitamin_c.json",
     "compound/name/vitamin%20C/property/"
     "SMILES,ConnectivitySMILES,InChI,InChIKey,MolecularFormula,"
     "MolecularWeight,Title/JSON",
     "the spec's own example of a name with a protonation/salt family"),
    ("name_ascorbate.json",
     "compound/name/ascorbate/property/"
     "SMILES,ConnectivitySMILES,InChI,InChIKey,MolecularFormula,"
     "MolecularWeight,Title/JSON",
     "the deprotonated form; a different structure from ascorbic acid"),
    ("name_sodium_ascorbate.json",
     "compound/name/sodium%20ascorbate/property/"
     "SMILES,ConnectivitySMILES,InChI,InChIKey,MolecularFormula,"
     "MolecularWeight,Title/JSON",
     "the salt; a third distinct structure for the same informal name"),
    ("inchikey_ue_xc_jvn_btnxoe_h.json",
     "compound/inchikey/UEXCJVNBTNXOEH-UHFFFAOYSA-N/property/"
     "SMILES,ConnectivitySMILES,InChI,InChIKey,MolecularFormula,"
     "MolecularWeight,Title/JSON",
     "InChIKey lookup: the key is a lookup handle, not an invertible hash"),
    ("cid_2244_aspirin.json",
     "compound/cid/2244/property/"
     "SMILES,ConnectivitySMILES,InChI,InChIKey,MolecularFormula,"
     "MolecularWeight,Title/JSON",
     "primary-key lookup by CID"),
    ("synonyms_2244.json",
     "compound/cid/2244/synonyms/JSON",
     "the synonym list behind matched_by / matched_synonym"),
    ("smiles_phenylacetylene.json",
     "compound/smiles/property/"
     "SMILES,ConnectivitySMILES,InChI,InChIKey,MolecularFormula,"
     "MolecularWeight,Title/JSON",
     "structure lookup; body is form-encoded, see the .meta.json"),
    ("inchi_phenylacetylene.json",
     "compound/inchi/property/"
     "SMILES,ConnectivitySMILES,InChI,InChIKey,MolecularFormula,"
     "MolecularWeight,Title/JSON",
     "InChI lookup; body is form-encoded"),
    ("not_found_name.json",
     "compound/name/zzzznotarealcompoundzzzz/property/SMILES,InChI,InChIKey/JSON",
     "the shape of an honest absence: PUGREST.NotFound, not an empty list"),
    ("formula_c2h6o_waiting.json",
     "compound/formula/C2H6O/cids/JSON",
     "the asynchronous ListKey handoff PubChem returns for a broad query"),
    ("formula_c2h6o_list.json",
     "compound/listkey/__LISTKEY__/property/"
     "SMILES,ConnectivitySMILES,InChI,InChIKey,MolecularFormula,"
     "MolecularWeight,Title/JSON",
     "the multi-candidate answer: ethanol, dimethyl ether, deuterated forms"),
]

#: Form-encoded bodies, used by the POST lookups. Recorded in the meta file
#: rather than in the filename so the request is reproducible exactly.
FORM_BODIES = {
    "smiles_phenylacetylene.json": {"smiles": "C#Cc1ccccc1"},
    "inchi_phenylacetylene.json": {
        "inchi": "InChI=1S/C8H6/c1-2-8-6-4-3-5-7-8/h1,3-7H"
    },
}


def _curl(url: str, form: dict | None = None) -> tuple[int, bytes]:
    cmd = ["curl", "-sS", "-L", "--ssl-no-revoke", "--max-time", "60", "-A", UA]
    if form:
        for key, value in form.items():
            cmd += ["--data-urlencode", f"{key}={value}"]
        cmd += ["-X", "POST"]
    cmd.append(url)
    raw = subprocess.run(cmd, capture_output=True)
    return raw.returncode, raw.stdout


def _meta(path: str, url: str, description: str, form: dict | None,
          listkey: str | None = None) -> dict:
    return {
        "origin": "LIVE_CAPTURE",
        "source": "PUBCHEM",
        "endpoint": "PUG REST",
        "url": url,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "license": (
            "U.S. Government work / public domain. PubChem is a resource of the "
            "U.S. National Library of Medicine; its records carry no copyright. "
            "See https://pubchem.ncbi.nlm.nih.gov/docs/pug-rest#section=License "
            "and https://www.ncbi.nlm.nih.gov/home/about/policies/"
        ),
        "description": description,
        "request_method": "POST" if form else "GET",
        "request_form": form,
        "listkey_used": listkey,
        "tool": "tests/fixtures/providers/_gen_pubchem.py",
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    listkey: str | None = None

    for filename, path, description in CAPTURES:
        form = FORM_BODIES.get(filename)
        url_path = path
        if "__LISTKEY__" in path:
            if listkey is None:
                print(f"skip {filename}: no ListKey captured")
                continue
            url_path = path.replace("__LISTKEY__", listkey)
        url = f"{BASE}/{url_path}"

        code, body = _curl(url, form)
        text = body.decode("utf-8", errors="replace")

        # Capture the ListKey so the follow-up fixture can be fetched.
        if filename == "formula_c2h6o_waiting.json" and "ListKey" in text:
            try:
                listkey = str(json.loads(text)["Waiting"]["ListKey"])
                print(f"  listkey = {listkey}")
            except Exception:  # noqa: BLE001
                pass
            time.sleep(3)

        (OUT / filename).write_text(text, encoding="utf-8")
        # `.meta.json`, not `.json.meta.json`: the house convention is that a
        # payload `foo.json` carries `foo.meta.json` beside it.
        meta_name = filename[:-len(".json")] + ".meta.json" if filename.endswith(".json") else filename + ".meta.json"
        (OUT / meta_name).write_text(
            json.dumps(_meta(filename, url, description, form, listkey),
                       indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"{filename}: curl exit {code}, {len(text)} bytes")
        time.sleep(1)  # PubChem asks for <= 5 requests per second

    return 0


if __name__ == "__main__":
    sys.exit(main())
