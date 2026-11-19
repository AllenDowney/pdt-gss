"""Check this repo against the PublicDataToolkit standard (a stopgap for `pdt check`).

Usage:
    python tests/check_metadata.py

Runs checks C1, C2, C6, C7, C9 and C10 from STANDARD.md §9. C3-C5 need the
data and run in build/build_slice.py (see build/report.md); C8 and C11 wait
for the generated views and the shared library.

Schemas come from a PublicDataToolkit checkout next to this repo, or from
$PDT_SCHEMA.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = Path(os.environ.get("PDT_SCHEMA", ROOT.parent / "PublicDataToolkit" / "schema"))

FAILURES = []
REPORTS = []


def fail(check, message):
    FAILURES.append(f"{check}: {message}")


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def validators():
    schemas, resources = {}, []
    for path in SCHEMA_DIR.glob("*.schema.json"):
        schema = json.loads(path.read_text())
        schemas[path.name] = schema
        resources.append((schema["$id"], Resource.from_contents(schema)))
    registry = Registry().with_resources(resources)
    return {name: Draft202012Validator(s, registry=registry, format_checker=FormatChecker())
            for name, s in schemas.items()}


def resolve_pointer(record, pointer):
    node = record
    for part in pointer.strip("/").split("/"):
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def main():
    v = validators()
    dataset = yaml.safe_load((ROOT / "dataset.yaml").read_text())
    sources = yaml.safe_load((ROOT / "sources.yaml").read_text())
    releases = {r["id"] for r in dataset["releases"]}
    source_ids = {s["id"] for s in sources["sources"]}

    variables = {r: jsonl(ROOT / "metadata" / "releases" / r / "variables.jsonl") for r in releases}
    groups = jsonl(ROOT / "metadata" / "groups.jsonl")
    links = jsonl(ROOT / "metadata" / "links.jsonl")
    corrections = jsonl(ROOT / "metadata" / "corrections.jsonl")
    annotations = [a for p in sorted((ROOT / "metadata" / "annotations").glob("*.jsonl")) for a in jsonl(p)]
    derived = [d for p in sorted((ROOT / "metadata" / "derived").glob("*.jsonl")) for d in jsonl(p)]

    # C1: schemas
    files = [("dataset.schema.json", [dataset]), ("sources.schema.json", [sources]),
             ("group.schema.json", groups), ("link.schema.json", links),
             ("correction.schema.json", corrections), ("annotation.schema.json", annotations),
             ("derived.schema.json", derived)]
    files += [("variable.schema.json", recs) for recs in variables.values()]
    for schema, records in files:
        for i, record in enumerate(records):
            for e in v[schema].iter_errors(record):
                where = "/".join(map(str, e.absolute_path)) or "(record)"
                fail("C1", f"{schema} record {i}: {where}: {e.message[:150]}")

    # C2: unique ids and resolvable references
    names = {}
    for release, recs in variables.items():
        seen = [r["name"] for r in recs]
        dupes = {n for n in seen if seen.count(n) > 1}
        if dupes:
            fail("C2", f"duplicate names in {release}: {sorted(dupes)}")
        names[release] = set(seen)
        for r in recs:
            if r["release"] != release or r["dataset"] != dataset["id"]:
                fail("C2", f"{r['name']}: dataset/release don't match the file's location")
    all_names = set().union(*names.values())
    for kind, items in [("group", groups), ("correction", corrections)]:
        ids = [x["id"] for x in items]
        if len(ids) != len(set(ids)):
            fail("C2", f"duplicate {kind} ids")
    for g in groups:
        for m in g["members"]:
            if m["name"] not in names.get(m.get("release"), all_names):
                fail("C2", f"group {g['id']}: member {m['name']} has no record")
    for c in corrections:
        if c["target"]["name"] not in all_names:
            fail("C2", f"correction {c['id']}: no variable {c['target']['name']}")
    for a in annotations + derived:
        t = a["target"]
        if "variable" in t and t["variable"] not in all_names:
            fail("C2", f"annotation/derived target {t['variable']} has no record")
        if "group" in t and t["group"] not in {g["id"] for g in groups}:
            fail("C2", f"annotation/derived target group {t['group']} doesn't exist")
    for r in dataset["releases"]:
        for s in r["sources"]:
            if s not in source_ids:
                fail("C2", f"release {r['id']}: unknown source {s}")
    text = json.dumps([variables, groups, links, annotations])
    for s in set(re.findall(r'"source": "([^"]+)"', text)):
        if s not in source_ids:
            fail("C2", f"provenance names unknown source {s}")

    # C6: provenance of sources; report gaps
    used = {s for r in dataset["releases"] for s in r["sources"]}
    for s in sources["sources"]:
        if s["id"] in used and not (s.get("sha256") and s.get("fetched_on")):
            fail("C6", f"source {s['id']} has no sha256 or fetched_on")
        if s["kind"] == "upstream_package" and not s.get("version"):
            REPORTS.append(f"C6: upstream package {s['id']} has no pinned version")
    recs = [r for rs in variables.values() for r in rs]
    for field in ["label", "questions", "values"]:
        n = sum(field not in r for r in recs)
        if n:
            REPORTS.append(f"C6: {n} of {len(recs)} records have no {field}")
    unresolved = sum(v["kind"] == "unresolved" for r in recs for v in r.get("values", []))
    REPORTS.append(f"C6: {unresolved} unresolved codes")
    unreviewed = text.count('"origin": "model"') and sum(
        1 for o in re.findall(r'"reviewed": (true|false)', text) if o == "false")
    REPORTS.append(f"C6: {unreviewed or 0} unreviewed model-made facts")

    # C7: corrections still apply
    by_name = {(r["release"], r["name"]): r for r in recs}
    for c in corrections:
        targets = [rel for rel in (c["target"].get("releases") or releases)]
        for rel in targets:
            record = by_name.get((rel, c["target"]["name"]))
            if record is None:
                continue
            try:
                current = resolve_pointer(record, c["target"]["field"])
            except (KeyError, IndexError, ValueError):
                fail("C7", f"{c['id']}: field {c['target']['field']} not found in {rel}")
                continue
            if current != c["original"]:
                fail("C7", f"{c['id']}: original no longer matches in {rel} (fixed upstream?)")

    # C9: terms
    terms = dataset["terms"]
    if terms["llm_processing"] != "allowed" and '"origin": "model"' in text:
        fail("C9", "records with origin model, but llm_processing isn't allowed")
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    for s in sources["sources"]:
        if s["redistribution"] != "ship" and s.get("path") in tracked:
            fail("C9", f"{s['path']} is {s['redistribution']} but tracked in git")
    if terms["metadata"] != "ship":
        REPORTS.append(f"C9: metadata terms are {terms['metadata']}; records must not be published")

    # C10: extension keys documented
    ext_doc = (ROOT / "EXTENSIONS.md").read_text() if (ROOT / "EXTENSIONS.md").exists() else ""
    keys = {f"{ns}.{k}" for r in recs for ns, d in r.get("extensions", {}).items() for k in d}
    for key in sorted(keys):
        if f"`{key}`" not in ext_doc:
            fail("C10", f"extension {key} isn't listed in EXTENSIONS.md")

    for line in REPORTS:
        print("report", line)
    for line in FAILURES:
        print("FAIL  ", line)
    print(f"\n{len(recs)} variables, {len(groups)} groups, {len(corrections)} corrections, "
          f"{len(annotations)} annotations, {len(derived)} derived; {len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
