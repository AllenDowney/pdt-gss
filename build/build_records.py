"""Build the GSS metadata records for every variable of every release.

Usage:
    python build/build_records.py

Reads:
- build/releases.yaml: the releases, each with its Stata file;
- data/interim/gssrdoc-0.9.json (made by `make gssrdoc` from gssrdoc's
  gss_doc.rda, pinned in sources.yaml);
- data/interim/<stata>.dta, decompressed from data/raw/<stata>.dta.gz here;
- data/interim/codebook-2024r3a.txt (`make codebook`: pdftotext of NORC's 2024
  codebook), for variables gssrdoc doesn't document;
- build/slice.yaml: the snake_case slice, a named subset of 50 variables, with
  hand-entered measures and top-coding notes for them.

Writes metadata/releases/<release>/variables.jsonl, metadata/groups.jsonl
(batteries by rule, gssrdoc's subjects, and the slice),
metadata/derived/scale_signature.jsonl and build/report.md.

gssrdoc 0.9 documents Release 3. A release whose documentation is another
release's (`documented_by` in releases.yaml, as for 7224r3a) says so in each
record's provenance. A variable gssrdoc lacks (vote_ts and pres_ts, new in
3a) is built from the Stata file's labels and the codebook's entry.

Corrections (metadata/corrections.jsonl) are written by hand and never touched
here.

Where each field comes from:
- variable label, value labels for variables gssrdoc lacks, which extended
  missing codes occur, storage type, value range, counts: the Stata file
- question text, value labels, availability (years and ballots), NORC ids,
  subjects, modules: gssrdoc
- description and notes for variables gssrdoc lacks: the 2024 codebook
- measure, top-coding notes: build/slice.yaml (hand), slice variables only
- missing types, stem/item split, groups: rules in this file
- scale signatures: the shared library's rule (pdt.derived)

The Stata file is read with pyreadstat, which returns Stata's extended missing
values (.a to .z) as the letters "a" to "z".
"""

import collections
import gzip
import json
import re
import shutil
from pathlib import Path

import pandas as pd
import pyreadstat
import yaml

# The shared library (PublicDataToolkit lib/pdt): installed, or on PYTHONPATH;
# otherwise a checkout next to this repo, or $PDT.
try:
    from pdt.derived import scale_signatures
except ImportError:
    import os
    import sys
    sys.path.insert(0, str(Path(os.environ.get("PDT", Path(__file__).resolve().parents[2] / "PublicDataToolkit"))
                           / "lib"))
    from pdt.derived import scale_signatures

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
INTERIM = ROOT / "data" / "interim"
META = ROOT / "metadata"

GSSRDOC = "gssrdoc"
GSSRDOC_JSON = INTERIM / "gssrdoc-0.9.json"
CODEBOOK = "gss-codebook-2024r3a"
CODEBOOK_TXT = INTERIM / "codebook-2024r3a.txt"
RULE_VERSION = "0.1"
MISSING_RULE_VERSION = "0.3"  # gss_extended_missing_types; 0.2 types ".z" (see codebook) as unspecified;
# 0.3 adds "can't choose" (ISSP items) as dk
STATA_ENCODING = "latin1"  # a few labels aren't valid UTF-8; pandas falls back to latin-1 too
CHUNK = 1000  # columns read from the Stata file at a time
EXAMPLES = 40  # lines per section of the report

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


def stata_value_labels(labels):
    """The same split for a Stata value-label set, as pyreadstat returns it."""
    valid, missing = {}, {}
    for code, label in (labels or {}).items():
        if isinstance(code, str):
            missing["." + code] = label
        else:
            number = float(code)
            valid[int(number) if number.is_integer() else number] = label
    return valid, missing


# Rule gss_extended_missing_types: missing type from the label NORC gives each
# extended missing value. The data file says the code is missing; this rule
# only says why. ".z" "see codebook" gives no reason, so it's "unspecified".
GSS_MISSING_TYPES = {
    "don't know": ["dk"],
    "can't choose": ["dk"],
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

def codes_origin(stata_source, labels_from=GSSRDOC):
    return {"origin": "rule", "rule": "gss_extended_missing_types", "rule_version": MISSING_RULE_VERSION,
            "source": stata_source,
            "note": "Stata extended missing values that occur in this variable; types from their "
                    + ("gssrdoc" if labels_from == GSSRDOC else "Stata") + " labels."}


# Rule codebook_entries: an entry in NORC's 2024 codebook starts "Variable: NAME
# Type: ...", then "Label:" and "Notes:" (each may run over several lines), then
# the frequency table, whose header line contains "PCT Excl.". Line breaks from
# page width are joined with spaces. An entry continued on the next page is
# printed again; the first printing is used.
ENTRY = re.compile(r"^\s*Variable:\s+(\S+)\s+Type:", re.M)
PAGE = re.compile(r"Page (\d+)\s*$", re.M)


def parse_codebook(text):
    entries = {}
    starts = list(ENTRY.finditer(text))
    for i, m in enumerate(starts):
        name = m.group(1).lower()
        if name in entries:
            continue
        body = text[m.end(): starts[i + 1].start() if i + 1 < len(starts) else len(text)]
        body = body.split("PCT Excl.")[0]
        label = re.search(r"^\s*Label:(.*?)(?=^\s*Notes:|\Z)", body, re.M | re.S)
        notes = re.search(r"^\s*Notes:(.*)", body, re.M | re.S)
        page = PAGE.search(text, m.end())
        entries[name] = {
            "label": " ".join(label.group(1).split()) if label else "",
            "notes": " ".join(notes.group(1).split()) if notes else "",
            "page": int(page.group(1)) if page else None,
        }
    return entries


# --------------------------------------------------------------- inputs


def load_yaml(name):
    return yaml.safe_load((ROOT / "build" / name).read_text())


def load_gssrdoc():
    rows = json.loads(GSSRDOC_JSON.read_text())
    return {row[0]["variable"]: row[0] for row in rows}


def dta_path(gz_name):
    """Decompress data/raw/<name>.dta.gz to data/interim/<name>.dta once."""
    src = RAW / gz_name
    dst = INTERIM / gz_name.removesuffix(".gz")
    if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
        INTERIM.mkdir(parents=True, exist_ok=True)
        with gzip.open(src) as f, open(dst, "wb") as g:
            shutil.copyfileobj(f, g)
    return dst


def scan_stata(path, names):
    """Per-variable facts from the data, read CHUNK columns at a time.

    Returns {name: {"occurring": Counter of missing codes, "present": numeric
    values that aren't missing, "years": {year: n}, "forms": {(year, ballot)},
    "string": bool}}.
    """
    design, _ = pyreadstat.read_dta(str(path), usecols=["year", "ballot"], encoding=STATA_ENCODING)
    year = design["year"].astype(int)
    ballot = pd.to_numeric(design["ballot"], errors="coerce")
    facts = {}
    for i in range(0, len(names), CHUNK):
        cols = names[i:i + CHUNK]
        # Read twice: plain (numbers, NaN for any missing value), and with the
        # extended missing values as letters, which are needed only where the
        # plain value is NaN. That's much faster than parsing the mixed columns.
        plain, _ = pyreadstat.read_dta(str(path), usecols=cols, encoding=STATA_ENCODING)
        frame, meta = pyreadstat.read_dta(str(path), usecols=cols, user_missing=True,
                                          apply_value_formats=False, encoding=STATA_ENCODING)
        kinds = meta.readstat_variable_types
        oks = {}
        for name in cols:
            col = frame[name]
            if kinds.get(name) == "string":
                oks[name] = col.notna() & (col.astype(str).str.len() > 0)
                facts[name] = {"string": True, "occurring": collections.Counter(), "present": None}
            else:
                num = plain[name]
                gaps = col[num.isna().values]
                occurring = collections.Counter({"." + k if isinstance(k, str) else ".": int(v)
                                                 for k, v in gaps.value_counts(dropna=False).items()})
                oks[name] = num.notna()
                facts[name] = {"string": False, "occurring": occurring, "present": num[oks[name]]}
        # Counts by year, and which (year, ballot) cells have answers, for the whole chunk at once.
        ok = pd.DataFrame(oks)
        by_year = ok.groupby(year.values).sum()
        by_form = ok[ballot.notna().values].groupby([year[ballot.notna()].values,
                                                     ballot[ballot.notna()].astype(int).values]).any()
        for name in cols:
            facts[name]["years"] = {int(y): int(n) for y, n in by_year[name].items() if n}
            facts[name]["forms"] = {(int(y), int(b)) for (y, b), hit in by_form[name].items() if hit}
        print(f"  {path.name}: {min(i + CHUNK, len(names))}/{len(names)} columns")
    return facts


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


def build_variable(name, ctx, rel, fact):
    """One record, and (kind, line) pairs for the report."""
    release, rel_spec, stata_source = rel["id"], rel["spec"], rel["spec"]["stata_source"]
    spec, measure = ctx["slice"], ctx["measure"]
    entry = ctx["gdoc"].get(name)
    report = []
    hand = {"origin": "hand", "author": spec["author"], "date": spec["date"]}

    if entry is not None:
        valid_labels, missing_labels = parse_value_labels(entry.get("value_labels"))
        labels_origin = {"origin": "upstream_package", "source": GSSRDOC}
        codes = codes_origin(stata_source)
    else:
        valid_labels, missing_labels = stata_value_labels(rel["value_labels"].get(name))
        labels_origin = {"origin": "data_file", "source": stata_source}
        codes = codes_origin(stata_source, labels_from="stata")

    occurring = fact["occurring"]
    values = []
    for code, label in sorted(valid_labels.items()):
        if SUSPECT_LABEL.search(label):
            values.append({"code": code, "label": label, "kind": "unresolved",
                           "origin": {**labels_origin,
                                      "note": "Stored as an ordinary value, but the label suggests a missing code."}})
            report.append(("suspect", f"`{name}` code {code} ({label!r}): label looks like a missing code; "
                                      "marked unresolved"))
        else:
            values.append({"code": code, "label": label, "kind": "valid"})
    _, stata_missing = stata_value_labels(rel["value_labels"].get(name))
    for code in sorted(occurring):
        label = missing_labels.get(code)
        own_origin = None
        if label is None and stata_missing.get(code) and entry is not None:
            # gssrdoc gives no label for this code; the Stata value labels do.
            label = stata_missing[code]
            own_origin = codes_origin(stata_source, labels_from="stata")
        types = GSS_MISSING_TYPES.get(label)
        if types is None:
            values.append({"code": code, "label": label or "", "kind": "unresolved",
                           "origin": {**codes, "note": f"Occurs {occurring[code]} times; no label, "
                                                       "or a label the rule doesn't know."}})
            report.append(("unresolved", f"`{name}` {code}: unlabeled or unknown label {label!r}; "
                                         "marked unresolved"))
        else:
            # The record's provenance.codes is the origin (STANDARD.md §4.2), unless the label is Stata's.
            values.append({"code": code, "label": label, "kind": "missing", "missing_types": types,
                           **({"origin": own_origin} if own_origin else {})})

    record = {"dataset": "gss", "release": release, "name": name}
    if rel["var_labels"].get(name):
        record["label"] = rel["var_labels"][name]

    cb = ctx["codebook"].get(name) if entry is None else None
    if cb and cb["label"]:
        record["description"] = cb["label"]
    if cb and cb["notes"]:
        record["notes"] = cb["notes"]

    text = clean_text((entry or {}).get("question") or "")
    if text.strip():
        question = {"text": text}
        stem, item, _ = split_stem_item(text)
        if stem:
            question["stem"], question["item"] = stem, item
        record["questions"] = [question]

    present = fact["present"]
    if fact["string"]:
        record["storage_type"] = "string"
    elif present is not None and len(present):
        record["storage_type"] = "integer" if (present % 1 == 0).all() else "float"
    if name in measure:
        record["measure"] = measure[name]
    if values:
        record["values"] = values

    if present is not None and len(present):
        unlabeled = sorted(set(present.unique().astype(float)) - {float(c) for c in valid_labels})
    else:
        unlabeled = []
    if unlabeled and measure.get(name) in {"continuous", "weight", "identifier", "count"}:
        rng = {"min": float(present.min()), "max": float(present.max())}
        rng = {k: int(v) if v.is_integer() else v for k, v in rng.items()}
        if name in spec.get("top_coded", {}):
            rng["top_coded"] = True
            rng["note"] = spec["top_coded"][name]
        record["valid_range"] = rng
    elif unlabeled:
        report.append(("unlabeled", f"`{name}`: {len(unlabeled)} unlabeled values occur in the data, "
                                    f"e.g. {unlabeled[:5]}"))

    avail_origin = None
    avail = availability_from_gssrdoc(entry) if entry is not None else []
    if not avail and (entry is None or entry.get("var_yrtab_json") is None):
        # gssrdoc has no per-year counts (e.g. weights), or doesn't document the
        # variable: count answers in the data.
        avail = [{"wave": y, "n": n} for y, n in sorted(fact["years"].items())]
        avail_origin = {"origin": "data_file", "source": stata_source,
                        "note": ("gssrdoc has no per-year counts for this variable"
                                 if entry is not None else "gssrdoc doesn't document this variable")
                                + "; years and n counted in the data."}
    if avail:
        record["availability"] = avail

    if entry is not None:
        loc = {"source": GSSRDOC}
        if entry.get("norc_url"):
            loc["url"] = entry["norc_url"]
        if entry.get("norc_id") is not None:
            loc["upstream_id"] = int(entry["norc_id"])
        record["source_location"] = loc
    elif cb:
        record["source_location"] = {"source": CODEBOOK, "page": cb["page"]}

    fields = {"label": {"origin": "data_file", "source": stata_source}}
    if entry is not None:
        fields["values"] = {"origin": "upstream_package", "source": GSSRDOC,
                            "note": "Valid codes and labels from gssrdoc; missing codes are the Stata extended "
                                    "missing values that occur in this variable in the data file."}
    else:
        fields["values"] = {"origin": "data_file", "source": stata_source,
                            "note": "Valid codes and labels from the Stata value labels; missing codes are the "
                                    "extended missing values that occur in this variable."}
    if cb:
        for f in ("description", "notes"):
            if f in record:
                fields[f] = {"origin": "publisher", "source": CODEBOOK, "location": f"p. {cb['page']}",
                             "note": "Line breaks from page width joined (rule codebook_entries)."}
    if "storage_type" in record:
        fields["storage_type"] = {"origin": "rule", "rule": "storage_type_from_values", "rule_version": RULE_VERSION,
                                  "source": stata_source,
                                  "note": "string for Stata strings; else integer if every value in the data is "
                                          "whole, else float."}
    if "questions" in record:
        fields["questions"] = {"origin": "rule", "rule": "gssrdoc_text", "rule_version": RULE_VERSION,
                               "source": GSSRDOC,
                               "note": "gssrdoc's question text with doubled apostrophes undoubled; "
                                       "stem and item split by rule."}
    if "measure" in record:
        decided = spec.get("measure_decisions", {}).get(name)
        fields["measure"] = {"origin": "hand", **decided} if decided else hand
    if avail_origin:
        fields["availability"] = avail_origin
    if "valid_range" in record:
        fields["valid_range"] = {"origin": "data_file", "source": stata_source,
                                 "note": "Min and max of the values that occur; top-coding from build/slice.yaml."}

    if entry is not None:
        record_origin = {"origin": "upstream_package", "source": GSSRDOC}
        if rel_spec.get("documented_by"):
            record_origin["note"] = (
                f"gssrdoc 0.9 documents release {rel_spec['documented_by']}, not {release}: question text, value "
                f"labels, availability and subjects describe {rel_spec['documented_by']}. The variable label and "
                f"the missing codes that occur come from this release's Stata file. NORC's release notes list "
                f"what changed (metadata/links.jsonl).")
    else:
        record_origin = {"origin": "data_file", "source": stata_source,
                         "note": "Not in gssrdoc 0.9. Label, value labels and counts from the Stata file; "
                                 "description and notes from NORC's 2024 codebook"
                                 + (f" (p. {cb['page']})." if cb else ", which has no entry for it.")}
    record["provenance"] = {"record": record_origin, "fields": fields}
    if any(v["kind"] != "valid" for v in values):
        record["provenance"]["codes"] = codes

    if entry is not None:
        modules = [v for row in json.loads(entry["module_df_json"] or "[]") for v in row.values()]
        if modules:
            record["extensions"] = {"gss": {"modules": modules}}
    return record, report


BALLOT_LETTER = {1: "A", 2: "B", 3: "C", 4: "D"}


def check_availability(record, fact):
    """C5: compare documented (year, ballot) availability with the data."""
    name = record["name"]
    notes = []
    data_years = set(fact["years"])
    doc_years = {a["wave"] for a in record.get("availability", [])}
    if data_years != doc_years:
        notes.append(("years", f"`{name}`: years with data but not documented {sorted(data_years - doc_years)}; "
                               f"documented but no data {sorted(doc_years - data_years)}"))

    doc_forms = {(a["wave"], a["form"]) for a in record.get("availability", []) if "form" in a}
    if doc_forms:
        form_years = {w for w, _ in doc_forms}
        data_forms = {(y, BALLOT_LETTER[b]) for y, b in fact["forms"] if y in form_years and b in BALLOT_LETTER}
        if data_forms != doc_forms:
            notes.append(("forms", f"`{name}`: ballots with data but not documented "
                                   f"{sorted(data_forms - doc_forms)}; documented but no data "
                                   f"{sorted(doc_forms - data_forms)}"))

    diffs = [(a["wave"], a["n"], fact["years"].get(a["wave"], 0)) for a in record.get("availability", [])
             if "n" in a and a["n"] != fact["years"].get(a["wave"], 0)]
    if diffs:
        notes.append(("counts", f"`{name}`: documented n differs from the data in {len(diffs)} years, "
                                f"e.g. (year, documented, data) {diffs[:3]}"))
    return notes


def subject_groups(gdoc):
    """gssrdoc's subjects (from GSS Data Explorer) as groups of kind `subject` (STANDARD.md §5.1)."""
    members = collections.defaultdict(list)
    for name, entry in gdoc.items():
        for row in json.loads(entry["subject_df_json"] or "[]"):
            for subject in row.values():
                members[subject].append(name)
    groups, ids = [], set()
    for subject in sorted(members):
        slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")
        gid = f"subject-{slug}"
        assert gid not in ids, f"two subjects give the id {gid}"
        ids.add(gid)
        groups.append({
            "id": gid, "kind": "subject", "label": subject,
            "members": [{"name": n} for n in members[subject]], "ordered": False,
            "origin": {"origin": "upstream_package", "source": GSSRDOC,
                       "note": "A GSS Data Explorer subject, as gssrdoc records it."}})
    return groups


def battery_groups(gdoc):
    """Batteries by rule: variables whose cleaned question text has the same stem."""
    by_stem = collections.defaultdict(list)
    for name, entry in gdoc.items():
        stem, _, letter = split_stem_item(clean_text(entry.get("question") or ""))
        if stem:
            by_stem[stem].append((letter, name))
    groups = []
    for stem, members in by_stem.items():
        if len(members) < 2:
            continue
        member_names = [n for _, n in sorted(members)]
        groups.append({
            "id": f"battery-{member_names[0]}", "kind": "battery", "stem": stem,
            "members": [{"name": n} for n in member_names], "ordered": True,
            "origin": {"origin": "rule", "rule": "battery_from_shared_stem", "rule_version": RULE_VERSION,
                       "source": GSSRDOC,
                       "note": "Variables whose question text, after the gssrdoc_text rule, has an "
                               "identical stem; ordered by item letter."}})
    return sorted(groups, key=lambda g: g["id"])


def slice_group(spec, names):
    return {
        "id": "slice-snake-case", "kind": "other", "label": "The snake_case slice",
        "members": [{"name": n} for n in names], "ordered": False,
        "origin": {"origin": "hand", "author": spec["author"], "date": spec["date"],
                   "note": "Not a publisher's group: the 50 variables chosen for the snake_case spike "
                           "(build/slice.yaml), kept identifiable because PublicDataToolkit's eval keys "
                           "for the slice refer to them."}}


def main():
    spec = load_yaml("slice.yaml")
    slice_names = [n for group in spec["variables"].values() for n in group]
    assert len(slice_names) == len(set(slice_names)), "duplicate names in slice.yaml"
    measure = {n: m for m, ns in spec["measure"].items() for n in ns}
    releases = load_yaml("releases.yaml")["releases"]
    gdoc = load_gssrdoc()
    codebook = parse_codebook(CODEBOOK_TXT.read_text()) if CODEBOOK_TXT.exists() else {}
    ctx = {"slice": spec, "measure": measure, "gdoc": gdoc, "codebook": codebook}

    by_release = {}
    for release, rel_spec in releases.items():
        path = dta_path(rel_spec["stata"])
        _, meta = pyreadstat.read_dta(str(path), metadataonly=True, encoding=STATA_ENCODING)
        names = list(meta.column_names)
        missing_slice = [n for n in slice_names if n not in names]
        assert not missing_slice, f"slice variables not in {release}: {missing_slice}"
        rel = {"id": release, "spec": rel_spec,
               "var_labels": dict(zip(meta.column_names, meta.column_labels)),
               "value_labels": {n: meta.value_labels.get(lab) for n, lab in meta.variable_to_label.items()}}
        print(f"{release}: {len(names)} variables")
        facts = scan_stata(path, names)
        records, notes, c5 = [], [], []
        for name in names:
            record, report = build_variable(name, ctx, rel, facts[name])
            records.append(record)
            notes.extend(report)
            if name not in {"year", "id", "ballot"}:
                c5.extend(check_availability(record, facts[name]))
        write_jsonl(META / "releases" / release / "variables.jsonl", records)
        not_in_gdoc = sorted(n for n in names if n not in gdoc)
        by_release[release] = (records, notes, c5, not_in_gdoc)

    known = {r["name"] for recs, *_ in by_release.values() for r in recs}
    batteries = [g for g in battery_groups(gdoc) if all(m["name"] in known for m in g["members"])]
    subjects = subject_groups({n: e for n, e in gdoc.items() if n in known})
    write_jsonl(META / "groups.jsonl", batteries + subjects + [slice_group(spec, slice_names)])
    old = META / "annotations" / "gssrdoc_subjects.jsonl"
    if old.exists():
        old.unlink()  # subjects were annotations before STANDARD.md §5.1 made them groups

    # Rule scale_signature_from_valid_labels, from the shared library (Task 27).
    # A variable's valid labels are the same in every release here (gssrdoc, or
    # the Stata file for the few gssrdoc lacks), so one signature per name.
    signatures = scale_signatures([r for records, *_ in by_release.values() for r in records], per_release=False)
    write_jsonl(META / "derived" / "scale_signature.jsonl", signatures)

    write_report(releases, by_release, batteries, subjects, signatures, slice_names)
    print(f"{', '.join(f'{r}: {len(v[0])}' for r, v in by_release.items())} variables; {len(batteries)} batteries, "
          f"{len(subjects)} subject groups, {len(signatures)} scale signatures; see build/report.md")


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def section(title, items, empty="None."):
    """A report section: a count by kind, then the first EXAMPLES lines."""
    lines = [title, ""]
    if not items:
        return lines + [empty, ""]
    counts = collections.Counter(k for k, _ in items)
    lines += [", ".join(f"{k}: {n}" for k, n in sorted(counts.items())), ""]
    lines += [f"- {text}" for _, text in items[:EXAMPLES]]
    if len(items) > EXAMPLES:
        lines.append(f"- ... and {len(items) - EXAMPLES} more")
    return lines + [""]


def write_report(releases, by_release, batteries, subjects, signatures, slice_names):
    sizes = collections.Counter(len(g["members"]) for g in batteries)
    lines = [
        f"# Build report: GSS, releases {', '.join(by_release)}",
        "",
        "Generated by `build/build_records.py`. Do not edit.",
        "",
        "Documentation: gssrdoc 0.9 (Release 3). "
        + "".join(f"Release {r} is documented by {s['documented_by']}'s text. "
                  for r, s in releases.items() if s.get("documented_by")),
        "",
        f"- Batteries by rule: {len(batteries)} "
        f"(members: {', '.join(f'{k}: {n}' for k, n in sorted(sizes.items()))})",
        f"- Subject groups from gssrdoc: {len(subjects)}",
        f"- Scale signatures: {len(signatures)}",
        f"- The snake_case slice: {len(slice_names)} variables, group `slice-snake-case`",
        "",
        "C3 and C4 are the data checks below: every missing code that occurs has a type or is unresolved (C3), "
        "and every value that occurs is labeled or within a valid range (C4). C5 compares documented "
        "availability with the data.",
        "",
    ]
    for release, (records, notes, c5, not_in_gdoc) in by_release.items():
        n_missing = collections.Counter(v["code"] for r in records for v in r.get("values", [])
                                        if v["kind"] == "missing")
        lines += [
            f"## Release {release}",
            "",
            f"- Variables: {len(records)}",
            f"- With question text: {sum('questions' in r for r in records)}",
            f"- With a stem/item split: {sum(any('stem' in q for q in r.get('questions', [])) for r in records)}",
            f"- With values: {sum('values' in r for r in records)}",
            f"- Strings: {sum(r.get('storage_type') == 'string' for r in records)}",
            f"- Not in gssrdoc (built from the Stata file and codebook): {len(not_in_gdoc)}"
            + (f" ({', '.join(not_in_gdoc)})" if not_in_gdoc else ""),
            "",
            "### Missing codes that occur, by number of variables",
            "",
            "| Code | Variables |",
            "|---|---|",
            *[f"| `{c}` | {k} |" for c, k in sorted(n_missing.items())],
            "",
        ]
        lines += section("### C3: missing codes without a type (unresolved)",
                         [x for x in notes if x[0] in ("unresolved", "suspect")])
        lines += section("### C4: values that occur without a label or valid range",
                         [x for x in notes if x[0] == "unlabeled"])
        lines += section(f"### C5: documented availability vs the {release} data", c5, "No differences.")
    (ROOT / "build" / "report.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
