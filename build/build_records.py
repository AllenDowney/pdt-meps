"""Build the MEPS-HC records from AHRQ's codebooks and documentation and the Stata files.

Usage (under the memory cap):
    python build/build_records.py

Reads build/slice.yaml; for each release, the full-year consolidated file (table fyc) and the
office-based visits file (table ob): codebook text (data/interim/<file>cb.txt), documentation text
(data/interim/<file>doc.txt; pdftotext -layout of the PDFs in data/raw/) and data
(data/interim/<file>.dta, unzipped from data/raw/<file>dta.zip). Writes
metadata/releases/<release>/<table>/variables.jsonl, metadata/links.jsonl,
metadata/derived/scale_signature.jsonl and build/report.md.

Where each field comes from (STANDARD.md §3):
- label, codes and labels with their counts, ranges: the codebook (publisher)
- which codes are missing, and why: the documentation's table of reserved codes (publisher,
  section 2.2, Table 1); the types are read from its definitions
- which codes occur, availability counts, storage type: the data file
- measure: build/slice.yaml (hand, Claude, Task 64), fyc variables only
- links between years: rule meps_year_suffix, from AHRQ's naming convention (documentation
  section 2.4): a name ending in the year's two digits is the same item in the next year's file
  with the next year's digits, unless an alteration renames it with a new _Myy suffix
- scale signatures: the shared library's rule (pdt.derived)
"""

import collections
import json
import re
from pathlib import Path

import pandas as pd
import pyreadstat
import yaml

try:
    from pdt.derived import scale_signatures
except ImportError:
    import os
    import sys
    sys.path.insert(0, str(Path(os.environ.get("PDT", Path(__file__).resolve().parents[2] / "PublicDataToolkit"))
                           / "lib"))
    from pdt.derived import scale_signatures

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
INTERIM = ROOT / "data" / "interim"
META = ROOT / "metadata"
DATASET = "meps"
RULE_VERSION = "0.1"

# ---------------------------------------------------------------- the documentation

# AHRQ's reserved codes (documentation section 2.2, Table 1), typed from their definitions.
RESERVED = {
    -1: (["inap"], "Inapplicable: question was not asked due to skip pattern"),
    -2: (["not_asked"], "Determined in previous round: not asked in this round because there was no change"),
    -7: (["refused"], "Refused"),
    -8: (["dk", "no_answer"], "Don't know: did not know, or the information could not be ascertained"),
    -10: (["not_available"], "Top coded: hourly wage or establishment size top-coded for confidentiality"),
    -15: (["uncodeable"], "Cannot be computed: value cannot be derived from data"),
}


def reserved_codes(doc_text):
    """The reserved codes the documentation's Table 1 lists, and its page."""
    i = doc_text.find("Reserved Code Values")
    j = doc_text.find("Codebook Format", i)
    table = doc_text[i:j]
    found = sorted({int(m) for m in re.findall(r"^\s*(-\d+)\s{2,}", table, re.M)})
    page = re.search(r"\b(C-\d+)\b", doc_text[i:i + 3000])
    return found, page.group(1) if page else None


# ---------------------------------------------------------------- the codebook

NAME = re.compile(r"^Name:\s+(\S+)\s*$")
FIELD = re.compile(r"^(Description|Format|Type|Start|End):\s+(.*?)\s*$")
NUM = r"-?\$?-?[\d,]*\.?\d+"
VALUE = re.compile(rf"^\s{{2,}}(?P<code>{NUM}(?:\s*-\s*{NUM})?)(?:\s+(?P<label>.*?))?\s{{2,}}(?P<unw>[\d,]+)\s+(?P<w>[\d,.]+)\s*$")


def parse_codebook(text):
    """{NAME: entry} from a MEPS codebook's per-variable listing."""
    lines = text.split("\n")
    entries, cur, page = {}, None, None
    for line in lines:
        line = line.replace("\f", "")
        mp = re.search(r"Page (\d+) of \d+", line)
        if mp:
            page = int(mp.group(1))
            continue
        m = NAME.match(line)
        if m:
            cur = {"name": m.group(1), "fields": {}, "values": [], "page": page}
            entries[cur["name"].upper()] = cur
            continue
        if cur is None:
            continue
        m = FIELD.match(line)
        if m:
            cur["fields"][m.group(1)] = m.group(2)
            continue
        if line.strip().startswith("Total:"):
            cur = None
            continue
        m = VALUE.match(line)
        if m:
            raw = m.group("code").strip()
            rm = re.fullmatch(rf"({NUM})\s*-\s*({NUM})", raw)
            cur["values"].append({"code": f"{rm.group(1)} - {rm.group(2)}" if rm else raw,
                                  "label": (m.group("label") or "").strip(),
                                  "n": int(m.group("unw").replace(",", ""))})
    for e in entries.values():
        if e["page"] is not None:
            e["page"] += 1          # the footer ends the page before the entry's
    return entries


def code_value(code):
    """A code as printed, without dollar signs or thousands separators ("$1,246,195" -> 1246195)."""
    x = float(code.replace("$", "").replace(",", "")) if isinstance(code, str) else float(code)
    return int(x) if x.is_integer() else x


def as_range(code):
    """(low, high) for a printed range "A - B" (quantile bins print as "$813 - $2,954")."""
    m = re.fullmatch(r"(-?\$?-?[\d,]*\.?\d+) - (-?\$?-?[\d,]*\.?\d+)", code)
    return (code_value(m.group(1)), code_value(m.group(2))) if m else None


# ---------------------------------------------------------------- build

def build_record(release, table, entry, col, sources, reserved, doc_page, measure, spec):
    rec = {"dataset": DATASET, "release": release, "table": table, "name": entry["name"]}
    if entry["fields"].get("Description"):
        rec["label"] = entry["fields"]["Description"]
    present = col.dropna()
    num = pd.to_numeric(present, errors="coerce")
    rec["storage_type"] = ("string" if entry["fields"].get("Type", "").lower().startswith("char") or num.isna().any()
                           else "integer" if len(num) == 0 or (num % 1 == 0).all() else "float")
    if measure:
        rec["measure"] = measure
    values, ranges, report = [], [], []
    for v in entry["values"]:
        rng = as_range(v["code"])
        if rng:
            ranges.append(rng)
            continue
        code = code_value(v["code"])
        item = {"code": code, "label": v["label"], "kind": "valid", "n": v["n"]}
        if code in RESERVED and code in reserved:
            item["kind"], item["missing_types"] = "missing", RESERVED[code][0]
        elif isinstance(code, int) and code < 0 and v["label"]:
            # A labeled negative code that this year's table of reserved codes doesn't list. (An
            # unlabeled negative value is a value: FAMINC23 lists a family income of -230.)
            item["kind"] = "unresolved"
            item["origin"] = {"origin": "rule", "rule": "meps_reserved_codes", "rule_version": RULE_VERSION,
                              "note": "A negative code the documentation's table of reserved codes doesn't list."}
            report.append(("unresolved", f"{release}/{table} `{entry['name']}` {code} {v['label']!r}"))
        values.append(item)
    if values:
        rec["values"] = values
    if ranges:
        rec["valid_range"] = {"min": min(a for a, _ in ranges), "max": max(b for _, b in ranges)}
    counts = collections.Counter()
    if rec["storage_type"] != "string":
        for k, c in col.value_counts(dropna=False).items():
            counts["." if pd.isna(k) else code_value(k)] += int(c)
    else:
        counts = collections.Counter({k: int(c) for k, c in col.value_counts(dropna=False).items()})
    missing = {v["code"] for v in values if v["kind"] != "valid"}
    n = sum(c for k, c in counts.items() if k not in missing and k != "." and k != "")
    rec["availability"] = [{"wave": int(release), "n": n}] if n else []
    rec["source_location"] = {"source": sources["codebook"], "page": entry["page"]}
    fields = {"storage_type": {"origin": "rule", "rule": "storage_type_from_values", "rule_version": RULE_VERSION,
                               "source": sources["data"]},
              "availability": {"origin": "data_file", "source": sources["data"],
                               "note": "n counts the cases whose code isn't a reserved code."}}
    if measure:
        fields["measure"] = {"origin": "hand", "author": spec["author"], "date": spec["date"]}
    rec["provenance"] = {"record": {"origin": "publisher", "source": sources["codebook"],
                                    "location": f"p. {entry['page']}"}, "fields": fields}
    if missing:
        rec["provenance"]["codes"] = {"origin": "publisher", "source": sources["doc"],
                                      "location": f"section 2.2, Table 1 (p. {doc_page})",
                                      "note": "AHRQ's reserved codes, typed from the table's definitions: "
                                              + "; ".join(f"{c} {RESERVED[c][1]}" for c in reserved if c in RESERVED)}
    # C4 and C5, for variables whose codebook lists their values.
    if entry["values"] and rec["storage_type"] != "string":
        listed = {v["code"] for v in values}
        extra = [(k, c) for k, c in counts.items()
                 if k not in listed and not (k != "." and any(a <= k <= b for a, b in ranges))]
        if extra:
            report.append(("undocumented", f"{release}/{table} `{entry['name']}`: {len(extra)} codes not in the "
                                           f"codebook, e.g. {sorted(extra, key=str)[:5]}"))
        for v in entry["values"]:
            rng = as_range(v["code"])
            got = (sum(c for k, c in counts.items() if k != "." and rng[0] <= k <= rng[1] and k not in listed)
                   if rng else counts.get(code_value(v["code"]), 0))
            if got != v["n"]:
                report.append(("counts", f"{release}/{table} `{entry['name']}` {v['code']}: codebook {v['n']}, data {got}"))
    return rec, report


def stem(name, year):
    """Rule meps_year_suffix: the name with this year's two digits replaced by {yy}, where they end it
    (TOTEXP24, PERWT24F, OBXP24X) or come just before a final X or F."""
    yy = f"{year % 100:02d}"
    return re.sub(rf"(?<=[A-Z]){yy}(?=[XF]?$)", "{yy}", name)


def main():
    spec = yaml.safe_load((ROOT / "build" / "slice.yaml").read_text())
    report, coverage, records = [], [], collections.defaultdict(list)
    for release, rel in spec["releases"].items():
        yy = f"{int(release) % 100:02d}"
        for table in ("fyc", "ob"):
            f = rel[table]
            sources = {"codebook": f"meps-{release}-{table}-codebook", "doc": f"meps-{release}-{table}-doc",
                       "data": f"meps-{release}-{table}-data"}
            entries = parse_codebook((INTERIM / f"{f}cb.txt").read_text())
            reserved, doc_page = reserved_codes((INTERIM / f"{f}doc.txt").read_text())
            _, meta = pyreadstat.read_dta(str(INTERIM / f"{f}.dta"), metadataonly=True)
            cols = {c.upper(): c for c in meta.column_names}
            coverage.append(f"- {release}/{table} ({f}): {meta.number_rows:,} rows, {len(cols)} columns, "
                            f"{len(entries)} codebook entries; in the data only: {sorted(set(cols) - set(entries))[:8]}; "
                            f"in the codebook only: {sorted(set(entries) - set(cols))[:8]}; reserved codes {reserved}")
            if table == "fyc":
                wanted = {t.replace("{yy}", yy).upper(): m for t, m in spec["fyc_variables"].items()}
            else:
                wanted = {n: None for n in cols}
            missing = sorted(set(wanted) - set(cols))
            if missing:
                report.append(("coverage", f"{release}/{table}: slice names not in the data: {missing}"))
            use = [cols[n] for n in wanted if n in cols]
            frame, _ = pyreadstat.read_dta(str(INTERIM / f"{f}.dta"), usecols=use)
            for c in use:
                e = entries.get(c.upper())
                if e is None:
                    report.append(("coverage", f"{release}/{table} `{c}`: no codebook entry"))
                    continue
                rec, rep = build_record(release, table, e, frame[c], sources, reserved, doc_page,
                                        wanted[c.upper()], spec)
                records[(release, table)].append(rec)
                report += rep
            write_jsonl(META / "releases" / release / table / "variables.jsonl", records[(release, table)])
            del frame
    links = year_links(records, spec)
    write_jsonl(META / "links.jsonl", links)
    write_jsonl(META / "derived" / "scale_signature.jsonl",
                scale_signatures([r for rs in records.values() for r in rs], per_release=True))
    write_report(records, links, coverage, report)
    print(f"{sum(len(v) for v in records.values())} records, {len(links)} links; see build/report.md")


def year_links(records, spec):
    releases = sorted(spec["releases"])
    links = []
    for a, b in zip(releases, releases[1:]):
        for table in ("fyc", "ob"):
            by_stem_a = {stem(r["name"], int(a)): r["name"] for r in records[(a, table)]}
            for r in records[(b, table)]:
                s = stem(r["name"], int(b))
                if s in by_stem_a:
                    old = by_stem_a[s]
                    rel = ["same_item"] + (["renamed"] if old != r["name"] else [])
                    links.append({
                        "from": {"release": a, "table": table, "name": old},
                        "to": {"release": b, "table": table, "name": r["name"]},
                        "relations": rel,
                        "origin": {"origin": "rule", "rule": "meps_year_suffix", "rule_version": RULE_VERSION,
                                   "source": f"meps-{b}-{table}-doc",
                                   "note": "AHRQ's naming convention (documentation section 2.4): a name ending in "
                                           "the year's digits names the same item each year; an alteration adds a "
                                           "new _Myy suffix, which this rule doesn't match."}})
    return links


def write_jsonl(path, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in items:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_report(records, links, coverage, report):
    by = collections.defaultdict(list)
    for kind, line in report:
        by[kind].append(line)
    lines = ["# Build report: meps", "", "Generated by `build/build_records.py`. Do not edit.", "",
             *[f"- {rel}/{tab}: {len(v)} records" for (rel, tab), v in sorted(records.items())],
             f"- Links: {len(links)}", "", "## C3: coverage", "", *coverage, *[f"- {x}" for x in by["coverage"]], ""]
    for kind, title in [("undocumented", "C4: codes in the data that the codebook doesn't list"),
                        ("counts", "C5: codebook counts vs the data"),
                        ("unresolved", "Negative codes not in the reserved-code table")]:
        lines += [f"## {title}", "", *([f"- {x}" for x in by[kind]] or ["None."]), ""]
    (ROOT / "build" / "report.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
