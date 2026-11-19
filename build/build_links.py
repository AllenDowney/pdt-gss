"""Build metadata/links.jsonl from NORC's release notes.

Usage:
    python build/build_links.py

Reads build/release_notes_7224.yaml (a hand transcription of "Release Notes for
GSS 1972-2024 Release 3a", which repeats the notes for Releases 3, 2 and 1), the
records in metadata/releases/, and, when they are in data/raw/, the Stata files
for Releases 2, 3 and 3a (variable names only).

Writes:
- metadata/links.jsonl: the links whose two ends both have records here
  (STANDARD.md §5.2; pdt check fails a link to a missing record), plus a
  same_item link by rule for each name in two consecutive releases that the
  notes don't mention;
- build/links_report.md: every link the notes support, and which are pending
  because a release has no records yet.

Each link's origin is the publisher (the release notes, with the page). The
relations are our reading of the entry's text; the link's note says so and
quotes NORC's fix.
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
NOTES = ROOT / "build" / "release_notes_7224.yaml"

# Release id -> Stata file, for checking that a linked name exists in that release.
STATA_FILES = {
    "7224r2": "gss7224_r2.dta.gz",
    "7224r3": "gss7224_r3.dta.gz",
    "7224r3a": "gss7224_r3a.dta.gz",
}

RULE_VERSION = "0.1"

RANGE = re.compile(r"^([A-Z_]*?[A-Z_])(\d+)-\1(\d+)$")


def expand(names, fixes=None):
    """Lower-case names, with ranges like INTRACE1-INTRACE3 expanded and misprints fixed."""
    out = []
    for n in names:
        n = (fixes or {}).get(n, {}).get("name", n)
        m = RANGE.match(n)
        if m:
            prefix, lo, hi = m.group(1), int(m.group(2)), int(m.group(3))
            out += [f"{prefix}{i}".lower() for i in range(lo, hi + 1)]
        else:
            out.append(n.lower())
    return out


def load_names():
    """Variable names in each release's Stata file, for the files we have."""
    names = {}
    for release, fname in STATA_FILES.items():
        path = RAW / fname
        if path.exists():
            with pd.io.stata.StataReader(path) as reader:
                names[release] = set(reader.variable_labels())
    return names


def load_records():
    recs = set()
    for path in sorted((META / "releases").glob("*/**/variables.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                recs.add((r["release"], r["name"]))
    return recs


def candidate_links(notes):
    """Every link the transcribed entries support, with the entry it came from."""
    releases = notes["releases"]
    for entry in notes["entries"]:
        to_rel = entry["release"]
        prev = releases[to_rel]["previous"]
        for spec in entry.get("links", []):
            if "each" in spec:
                pairs = [(n, n) for n in expand(entry["variables"], notes.get("name_fixes"))]
                relations = spec["each"]
                frm, to = prev, to_rel
            else:
                pairs = [tuple(p) for p in spec["pairs"]]
                relations = spec["relations"]
                frm, to = spec.get("from_release", prev), spec.get("to_release", to_rel)
            for a, b in pairs:
                yield entry, (frm, a), (to, b), relations, "each" in spec


def make_link(notes, entry, frm, to, relations):
    label = notes["releases"][entry["release"]]["label"]
    years = entry["years"]
    years = ", ".join(str(y) for y in years) if isinstance(years, list) else years
    fix = " ".join(entry["fix"].split())
    return {
        "from": {"release": frm[0], "name": frm[1]},
        "to": {"release": to[0], "name": to[1]},
        "relations": relations,
        "origin": {"origin": "publisher", "source": notes["source"],
                   "location": f"p. {entry['page']}, notes for {label}"},
        "note": f"Years affected: {years}. NORC's fix: {fix}. Relations assigned by hand "
                f"from the entry ({notes['author']}, {notes['transcribed_on']}; "
                f"build/release_notes_7224.yaml).",
    }


def main():
    notes = yaml.safe_load(NOTES.read_text())
    names = load_names()
    records = load_records()
    slice_names = {n for _, n in records}

    written, pending, skipped_new, unknown = [], [], [], []
    for entry, frm, to, relations, each in candidate_links(notes):
        if frm[0] is None:
            pending.append((entry, frm, to, relations, "previous release not named"))
            continue
        missing_ends = [end for end in (frm, to) if end[0] in names and end[1] not in names[end[0]]]
        if missing_ends:
            # An `each` link for a variable that is new in this release has nothing to
            # link from; anything else not in the data file is a transcription problem.
            if each and missing_ends == [frm]:
                skipped_new.append((entry, frm, to))
            else:
                unknown.append((entry, frm, to, missing_ends))
            continue
        link = make_link(notes, entry, frm, to, relations)
        if frm in records and to in records:
            written.append(link)
        else:
            absent = [end[0] for end in (frm, to) if end not in records]
            pending.append((entry, frm, to, relations, "no records for " + ", ".join(sorted(set(absent)))))

    # Rule gss_same_name_same_item: between consecutive releases that both have
    # records, a name the release notes don't mention is the same item. NORC
    # keeps a variable's name and wording while the item is unchanged ("not
    # altering question wording for questions that have been previously asked",
    # What's New R3a p. 1), and the release notes list the exceptions.
    noted = {(l["from"]["release"], l["from"]["name"], l["to"]["release"]) for l in written}
    noted |= {(p[1][0], p[1][1], p[2][0]) for p in pending}
    by_release = collections.defaultdict(set)
    for rel, name in records:
        by_release[rel].add(name)
    by_rule = []
    for rel, info in notes["releases"].items():
        prev = info["previous"]
        for name in sorted(by_release.get(rel, set()) & by_release.get(prev, set())):
            if (prev, name, rel) not in noted:
                by_rule.append({
                    "from": {"release": prev, "name": name}, "to": {"release": rel, "name": name},
                    "relations": ["same_item"],
                    "origin": {"origin": "rule", "rule": "gss_same_name_same_item", "rule_version": RULE_VERSION,
                               "source": notes["source"],
                               "note": f"Same name in {prev} and {rel}, and NORC's notes for {info['label']} "
                                       "don't mention it. NORC keeps a name while the item is unchanged and lists "
                                       "the exceptions in its release notes."}})
    written += by_rule

    (META / "links.jsonl").write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in written))
    write_report(notes, names, written, pending, skipped_new, unknown, slice_names)
    print(f"links written: {len(written)}; pending: {len(pending)}; "
          f"new variables (no link): {len(skipped_new)}; names not found: {len(unknown)}")


def write_report(notes, names, written, pending, skipped_new, unknown, slice_names):
    by_pair = collections.Counter((p[1][0] or "?", p[2][0]) for p in pending)
    lines = [
        "# Links report: GSS release notes",
        "",
        "Generated by `build/build_links.py` from `build/release_notes_7224.yaml`. Do not edit.",
        "",
        f"- Entries transcribed: {len(notes['entries'])}",
        f"- Links written to `metadata/links.jsonl` (both ends have records): {len(written)}, of which "
        f"{sum(l['origin']['origin'] == 'rule' for l in written)} by rule gss_same_name_same_item "
        "(same name, not in the notes)",
        f"- Links pending (a release has no records here): {len(pending)}",
        f"- Variables new in a release, so nothing to link from: {len(skipped_new)}",
        f"- Linked names not found in that release's data file: {len(unknown)}",
        f"- Data files checked for names: {', '.join(sorted(names)) or 'none'}",
        f"- Misprinted names fixed by hand: "
        + (", ".join(f"{k} → {v['name']}" for k, v in notes.get("name_fixes", {}).items()) or "none"),
        "",
        "## Pending links, by release pair",
        "",
        "| From | To | Links |",
        "|---|---|---|",
    ]
    lines += [f"| {a} | {b} | {n} |" for (a, b), n in sorted(by_pair.items())]
    lines += ["", "## Pending links for variables in the slice", "",
              "These are written once the other release has records.", ""]
    for entry, frm, to, relations, why in pending:
        if frm[1] in slice_names or to[1] in slice_names:
            lines.append(f"- {frm[0]}/{frm[1]} → {to[0]}/{to[1]}: {', '.join(relations)} "
                         f"(p. {entry['page']}; {why})")
    lines += ["", "## Names not found in the data file", ""]
    lines += [f"- {frm[0]}/{frm[1]} → {to[0]}/{to[1]} (p. {entry['page']}): not in "
              + ", ".join(f"{e[0]}" for e in ends) for entry, frm, to, ends in unknown] or ["None."]
    lines += ["", "## Variables new in a release (no link)", ""]
    news = collections.defaultdict(list)
    for entry, frm, to in skipped_new:
        news[to[0]].append(to[1])
    lines += [f"- {rel}: {', '.join(sorted(v))}" for rel, v in sorted(news.items())] or ["None."]
    (ROOT / "build" / "links_report.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
