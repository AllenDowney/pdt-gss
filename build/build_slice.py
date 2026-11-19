"""Build the GSS slice's metadata records from gssrdoc and the Stata data file.

Usage:
    python build/build_slice.py

Reads build/slice.yaml, data/interim/gssrdoc-0.9.json (made by `make gssrdoc`
from gssrdoc's gss_doc.rda, pinned in sources.yaml) and each release's Stata
file in data/raw/. Writes metadata/releases/<release>/variables.jsonl for every
release in slice.yaml,
metadata/groups.jsonl (batteries, and gssrdoc's subjects as `subject` groups),
metadata/derived/scale_signature.jsonl and build/report.md. Groups and derived
properties come from gssrdoc alone, so they are the same for every release.

gssrdoc 0.9 documents Release 3. A release whose documentation is another
release's (`documented_by` in slice.yaml, as for 7224r3a) says so in each
record's provenance.

Corrections (metadata/corrections.jsonl) are written by hand and never touched
here.

Where each field comes from:
- variable label, which extended missing codes occur, value range: the Stata file
- question text, value labels, availability (years and ballots), NORC ids,
  subjects: gssrdoc
- measure, top-coding notes: build/slice.yaml (hand)
- missing types, stem/item split, storage type, groups, scale signatures:
  rules in this file

The Stata file is read only through pandas' public API, as CultureWar does.
"""

import collections
import json
import re
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
META = ROOT / "metadata"

GSSRDOC = "gssrdoc"
GSSRDOC_JSON = ROOT / "data" / "interim" / "gssrdoc-0.9.json"
RULE_VERSION = "0.1"
MISSING_RULE_VERSION = "0.2"  # gss_extended_missing_types; 0.2 types ".z" (see codebook) as unspecified

# ---------------------------------------------------------------- rules

# Rule gssrdoc_text: gssrdoc doubles every apostrophe (an escaping artifact),
# so "''" becomes "'". Then, if the question ends with a lettered item ("G. The
# woman wants it for any reason?"), split it into stem and item. The item
# letter usually follows a newline, but sometimes directly follows "?" or ":".
ITEM_AFTER_NEWLINE = re.compile(r"\n\s*([A-Z])\.\s+(?=\S)")
ITEM_AFTER_PUNCT = re.compile(r"(?<=[?:])\s*([A-Z])\.\s+(?=\S)")


def clean_text(text):
    return text.replace("''", "'")


def split_stem_item(text):
    """Return (stem, item, letter), or (None, None, None) if there's no lettered item."""
    matches = list(ITEM_AFTER_NEWLINE.finditer(text)) or list(ITEM_AFTER_PUNCT.finditer(text))
    if not matches:
        return None, None, None
    m = matches[-1]
    stem = text[: m.start()].strip()
    if not stem:
        return None, None, None
    return stem, text[m.start():].strip(), m.group(1)


# gssrdoc writes value labels as one string: "[1] yes / [2] no / [NA(d)] don't know / ...".
VALUE_LABEL = re.compile(r"\[([^\]]+)\] (.*?)(?= / \[|$)")


def parse_value_labels(text):
    """Return ({code: label} for valid codes, {'.d': label} for extended missing values)."""
    valid, missing = {}, {}
    for code, label in VALUE_LABEL.findall(text or ""):
        m = re.fullmatch(r"NA\(([a-z])\)", code)
        if m:
            missing["." + m.group(1)] = clean_text(label)
        else:
            number = float(code)
            valid[int(number) if number.is_integer() else number] = clean_text(label)
    return valid, missing


# Rule gss_extended_missing_types: missing type from the label NORC gives each
# extended missing value. The data file says the code is missing; this rule
# only says why. ".z" "see codebook" gives no reason, so it's "unspecified".
GSS_MISSING_TYPES = {
    "don't know": ["dk"],
    "iap": ["inap"],
    "I don't have a job": ["inap"],
    "dk, na, iap": ["dk", "no_answer", "inap"],
    "no answer": ["no_answer"],
    "not imputable": ["uncodeable"],
    "refused": ["refused"],
    "skipped on web": ["skipped"],
    "uncodeable": ["uncodeable"],
    "not available in this release": ["not_available"],
    "not available in this year": ["not_asked"],
    "see codebook": ["unspecified"],
}

# Labels on ordinary numeric codes that look like missing values. Those codes
# are stored as valid numbers, so the build doesn't reclassify them; it marks
# them unresolved and reports them.
SUSPECT_LABEL = re.compile(r"\b(dk|don'?t know|refused|no answer|iap|inap|not applicable|na)\b", re.I)

# Rule scale_signature_from_valid_labels: a name for common response formats.
SCALE_SIGNATURES = {
    ("yes", "no"): "yes_no",
    ("too little", "about right", "too much"): "spending_3",
}

# The default origin for every missing code (provenance.codes).
def codes_origin(stata_source):
    return {"origin": "rule", "rule": "gss_extended_missing_types", "rule_version": MISSING_RULE_VERSION,
            "source": stata_source,
            "note": "Stata extended missing values that occur in this variable; types from their gssrdoc labels."}

# --------------------------------------------------------------- inputs


def load_slice():
    spec = yaml.safe_load((ROOT / "build" / "slice.yaml").read_text())
    names = [n for group in spec["variables"].values() for n in group]
    assert len(names) == len(set(names)), "duplicate names in slice.yaml"
    measure = {n: m for m, ns in spec["measure"].items() for n in ns}
    return spec, names, measure


def load_gssrdoc():
    rows = json.loads(GSSRDOC_JSON.read_text())
    return {row[0]["variable"]: row[0] for row in rows}


def load_stata(names, filename):
    """Return (variable labels, frame with extended missing values kept)."""
    path = RAW / filename
    with pd.io.stata.StataReader(path) as reader:
        var_labels = reader.variable_labels()
    frame = pd.read_stata(path, columns=names, convert_missing=True, convert_categoricals=False)
    return var_labels, frame


def code_of(value):
    """'.d' for a pandas StataMissingValue, None for an ordinary value."""
    return getattr(value, "string", None)


# ---------------------------------------------------------------- build


def availability_from_gssrdoc(entry):
    """(wave, form) entries from gssrdoc's per-year counts and ballots."""
    yrtab = json.loads(entry["var_yrtab_json"] or "[]")
    ballots = {int(r["year"]): r["ballots"] for r in json.loads(entry["yrballot_df_json"] or "[]")}
    out = []
    for row in yrtab:
        year = int(row["year"])
        n = sum(v for k, v in row.items() if k != "year" and k not in GSS_MISSING_TYPES)
        if n <= 0:
            continue
        letters = [b for b in (ballots.get(year) or "").split("/") if b and b != "-"]
        if letters:
            out.extend({"wave": year, "form": b} for b in letters)
        else:
            out.append({"wave": year, "n": n})
    return out


def build_variable(name, spec, measure, gdoc, stata, release, rel_spec):
    var_labels, frame = stata
    stata_source = rel_spec["stata_source"]
    codes = codes_origin(stata_source)
    entry = gdoc[name]
    col = frame[name]
    report = []
    hand = {"origin": "hand", "author": spec["author"], "date": spec["date"]}

    occurring = collections.Counter(code_of(v) for v in col if code_of(v) is not None)
    present = pd.to_numeric(col.map(lambda v: None if code_of(v) else v), errors="coerce").dropna()

    # Value set: gssrdoc's valid codes, then the extended missing codes that occur.
    valid_labels, missing_labels = parse_value_labels(entry.get("value_labels"))
    values = []
    for code, label in sorted(valid_labels.items()):
        if SUSPECT_LABEL.search(label):
            values.append({"code": code, "label": label, "kind": "unresolved",
                           "origin": {"origin": "upstream_package", "source": GSSRDOC,
                                      "note": "Stored as an ordinary value, but the label suggests a missing code."}})
            report.append(f"`{name}` code {code} ({label!r}): label looks like a missing code; marked unresolved")
        else:
            values.append({"code": code, "label": label, "kind": "valid"})
    for code in sorted(occurring):
        label = missing_labels.get(code)
        types = GSS_MISSING_TYPES.get(label)
        if types is None:
            values.append({"code": code, "label": label or "", "kind": "unresolved",
                           "origin": {**codes, "note": f"Occurs {occurring[code]} times; no label, "
                                                              "or a label the rule doesn't know."}})
            report.append(f"`{name}` {code}: unlabeled or unknown label {label!r}; marked unresolved")
        else:
            # The record's provenance.codes is the origin (STANDARD.md §4.2).
            values.append({"code": code, "label": label, "kind": "missing", "missing_types": types})

    record = {"dataset": "gss", "release": release, "name": name}
    if var_labels.get(name):
        record["label"] = var_labels[name]

    text = clean_text(entry.get("question") or "")
    if text.strip():
        question = {"text": text}
        stem, item, _ = split_stem_item(text)
        if stem:
            question["stem"], question["item"] = stem, item
        record["questions"] = [question]

    if len(present):
        record["storage_type"] = "integer" if (present % 1 == 0).all() else "float"
    if name in measure:
        record["measure"] = measure[name]
    if values:
        record["values"] = values

    unlabeled = sorted(set(present.astype(float)) - {float(c) for c in valid_labels})
    if unlabeled and measure.get(name) in {"continuous", "weight", "identifier", "count"}:
        rng = {"min": float(present.min()), "max": float(present.max())}
        rng = {k: int(v) if v.is_integer() else v for k, v in rng.items()}
        if name in spec.get("top_coded", {}):
            rng["top_coded"] = True
            rng["note"] = spec["top_coded"][name]
        record["valid_range"] = rng
    elif unlabeled:
        report.append(f"`{name}`: {len(unlabeled)} unlabeled values occur in the data, e.g. {unlabeled[:5]}")

    avail = availability_from_gssrdoc(entry)
    avail_origin = None
    if not avail and entry.get("var_yrtab_json") is None:
        # gssrdoc has no per-year counts (e.g. weights): count answers in the data.
        ok = col.map(lambda v: code_of(v) is None and pd.notna(v))
        counts = frame.loc[ok, "year"].astype(int).value_counts().sort_index()
        avail = [{"wave": int(y), "n": int(k)} for y, k in counts.items()]
        avail_origin = {"origin": "data_file", "source": stata_source,
                        "note": "gssrdoc has no per-year counts for this variable; years and n counted in the data."}
    if avail:
        record["availability"] = avail
    record["source_location"] = {"source": GSSRDOC, "url": entry["norc_url"], "upstream_id": int(entry["norc_id"])}

    fields = {
        "label": {"origin": "data_file", "source": stata_source},
        "values": {"origin": "upstream_package", "source": GSSRDOC,
                   "note": "Valid codes and labels from gssrdoc; missing codes are the Stata extended "
                           "missing values that occur in this variable in the data file."},
    }
    if "storage_type" in record:
        fields["storage_type"] = {"origin": "rule", "rule": "storage_type_from_values", "rule_version": RULE_VERSION,
                                  "source": stata_source, "note": "integer if every value in the data is whole, else float."}
    if "questions" in record:
        fields["questions"] = {"origin": "rule", "rule": "gssrdoc_text", "rule_version": RULE_VERSION,
                               "source": GSSRDOC,
                               "note": "gssrdoc's question text with doubled apostrophes undoubled; "
                                       "stem and item split by rule."}
    if "measure" in record:
        fields["measure"] = hand
    if avail_origin:
        fields["availability"] = avail_origin
    if "valid_range" in record:
        fields["valid_range"] = {"origin": "data_file", "source": stata_source,
                                 "note": "Min and max of the values that occur; top-coding from build/slice.yaml."}
    record_origin = {"origin": "upstream_package", "source": GSSRDOC}
    if rel_spec.get("documented_by"):
        record_origin["note"] = (
            f"gssrdoc 0.9 documents release {rel_spec['documented_by']}, not {release}: question text, value "
            f"labels, availability and subjects describe {rel_spec['documented_by']}. The variable label and "
            f"the missing codes that occur come from this release's Stata file. NORC's release notes list "
            f"what changed (metadata/links.jsonl).")
    record["provenance"] = {"record": record_origin, "fields": fields}
    if any(v["kind"] != "valid" for v in values):
        record["provenance"]["codes"] = codes

    modules = [v for row in json.loads(entry["module_df_json"] or "[]") for v in row.values()]
    if modules:
        record["extensions"] = {"gss": {"modules": modules}}
    return record, report


BALLOT_LETTER = {1: "A", 2: "B", 3: "C", 4: "D"}


def check_availability(record, frame):
    """C5: compare documented (year, ballot) availability with the data."""
    name = record["name"]
    ok = frame[name].map(lambda v: code_of(v) is None and pd.notna(v))
    notes = []

    data_years = set(frame.loc[ok, "year"].astype(int))
    doc_years = {a["wave"] for a in record.get("availability", [])}
    if data_years != doc_years:
        notes.append(f"`{name}`: years with data but not documented {sorted(data_years - doc_years)}; "
                     f"documented but no data {sorted(doc_years - data_years)}")

    doc_forms = {(a["wave"], a["form"]) for a in record.get("availability", []) if "form" in a}
    if doc_forms:
        form_years = {w for w, _ in doc_forms}
        sub = frame.loc[ok, ["year", "ballot"]]
        data_forms = {(int(y), BALLOT_LETTER.get(b)) for y, b in zip(sub["year"], sub["ballot"])
                      if code_of(b) is None and int(y) in form_years}
        data_forms.discard((None, None))
        data_forms = {(y, f) for y, f in data_forms if f}
        if data_forms != doc_forms:
            notes.append(f"`{name}`: ballots with data but not documented {sorted(data_forms - doc_forms)}; "
                         f"documented but no data {sorted(doc_forms - data_forms)}")

    counts = frame.loc[ok, "year"].astype(int).value_counts()
    diffs = [(a["wave"], a["n"], int(counts.get(a["wave"], 0))) for a in record.get("availability", [])
             if "n" in a and a["n"] != counts.get(a["wave"], 0)]
    if diffs:
        notes.append(f"`{name}`: gssrdoc's n differs from the data in {len(diffs)} years, "
                     f"e.g. (year, gssrdoc, data) {diffs[:3]}")
    return notes


def subject_groups(names, gdoc):
    """gssrdoc's subjects (from GSS Data Explorer) as groups of kind `subject` (STANDARD.md §5.1).

    Members are the slice's variables with that subject, in slice order, because
    a group lists only variables this repo has records for.
    """
    members = collections.defaultdict(list)
    for name in names:
        for row in json.loads(gdoc[name]["subject_df_json"] or "[]"):
            for subject in row.values():
                members[subject].append(name)
    groups = []
    for subject in sorted(members):
        slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")
        groups.append({
            "id": f"subject-{slug}", "kind": "subject", "label": subject,
            "members": [{"name": n} for n in members[subject]], "ordered": False,
            "origin": {"origin": "upstream_package", "source": GSSRDOC,
                       "note": "A GSS Data Explorer subject, as gssrdoc records it. Members are limited to "
                               "the variables in this slice."}})
    return groups


def build_groups(names, gdoc):
    """Batteries by rule: variables whose cleaned question text has the same stem."""
    by_stem = collections.defaultdict(list)
    for name, entry in gdoc.items():
        stem, _, letter = split_stem_item(clean_text(entry.get("question") or ""))
        if stem:
            by_stem[stem].append((letter, name))
    in_slice = set(names)
    groups, partial = [], []
    for stem, members in by_stem.items():
        member_names = [n for _, n in sorted(members)]
        if len(members) < 2 or not in_slice & set(member_names):
            continue
        if set(member_names) <= in_slice:
            groups.append({
                "id": f"battery-{member_names[0]}", "kind": "battery", "stem": stem,
                "members": [{"name": n} for n in member_names], "ordered": True,
                "origin": {"origin": "rule", "rule": "battery_from_shared_stem", "rule_version": RULE_VERSION,
                           "source": GSSRDOC,
                           "note": "Variables whose question text, after the gssrdoc_text rule, has an "
                                   "identical stem; ordered by item letter."}})
        else:
            partial.append((member_names, sorted(in_slice & set(member_names))))
    return groups, partial


def main():
    spec, names, measure = load_slice()
    gdoc = load_gssrdoc()
    missing = [n for n in names if n not in gdoc]
    assert not missing, f"not in gssrdoc: {missing}"

    by_release = {}
    for release, rel_spec in spec["releases"].items():
        stata = load_stata(names, rel_spec["stata"])
        frame = stata[1]
        records, notes, c5 = [], [], []
        for name in names:
            record, report = build_variable(name, spec, measure, gdoc, stata, release, rel_spec)
            records.append(record)
            notes.extend(report)
            if name not in {"year", "id", "ballot"}:
                c5.extend(check_availability(record, frame))
        write_jsonl(META / "releases" / release / "variables.jsonl", records)
        by_release[release] = (records, notes, c5)

    groups, partial = build_groups(names, gdoc)
    subjects = subject_groups(names, gdoc)
    write_jsonl(META / "groups.jsonl", groups + subjects)
    old = META / "annotations" / "gssrdoc_subjects.jsonl"
    if old.exists():
        old.unlink()  # subjects were annotations before STANDARD.md §5.1 made them groups

    # Valid labels come from gssrdoc, so every release gives the same signatures.
    records = next(iter(by_release.values()))[0]
    signatures = []
    for r in records:
        valid = tuple(v["label"] for v in r.get("values", []) if v["kind"] == "valid")
        if valid in SCALE_SIGNATURES:
            signatures.append({"target": {"variable": r["name"]}, "property": "scale_signature",
                               "value": SCALE_SIGNATURES[valid],
                               "rule": "scale_signature_from_valid_labels", "rule_version": RULE_VERSION})
    write_jsonl(META / "derived" / "scale_signature.jsonl", signatures)

    write_report(spec, by_release, groups, partial, subjects, signatures)
    print(f"{len(by_release)} releases x {len(records)} variables, {len(groups)} batteries, "
          f"{len(subjects)} subject groups, {len(signatures)} scale signatures; see build/report.md")


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_report(spec, by_release, groups, partial, subjects, signatures):
    lines = [
        f"# Build report: GSS slice, releases {', '.join(by_release)}",
        "",
        "Generated by `build/build_slice.py`. Do not edit.",
        "",
        "Documentation for every release: gssrdoc 0.9 (Release 3). "
        + "".join(f"Release {r} is documented by {s['documented_by']}'s text. "
                  for r, s in spec["releases"].items() if s.get("documented_by")),
        "",
        f"- Groups (complete batteries): {len(groups)}",
        f"- Subject groups from gssrdoc: {len(subjects)}",
        f"- Scale signatures: {len(signatures)}",
        "",
        "## Batteries only partly in the slice (no group written)",
        "",
        *([f"- {len(m)} members, {len(s)} in the slice ({', '.join(s)}): {', '.join(m)}" for m, s in partial]
          or ["None."]),
        "",
    ]
    for release, (records, notes, c5) in by_release.items():
        n_missing = collections.Counter(v["code"] for r in records for v in r.get("values", [])
                                        if v["kind"] == "missing")
        lines += [
            f"## Release {release}",
            "",
            f"- Variables: {len(records)}",
            f"- With question text: {sum('questions' in r for r in records)}",
            f"- With a stem/item split: {sum(any('stem' in q for q in r.get('questions', [])) for r in records)}",
            "",
            "### Missing codes that occur, by number of variables",
            "",
            "| Code | Variables |",
            "|---|---|",
            *[f"| `{c}` | {k} |" for c, k in sorted(n_missing.items())],
            "",
            "### Values and labels to look at",
            "",
            *([f"- {n}" for n in notes] or ["None."]),
            "",
            f"### C5: availability in gssrdoc vs the {release} data",
            "",
            *([f"- {n}" for n in c5] or ["No differences."]),
            "",
        ]
    (ROOT / "build" / "report.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
