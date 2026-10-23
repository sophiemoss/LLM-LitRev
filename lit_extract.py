"""PRISMA 2020 literature review helper.

Gives every number in the PRISMA 2020 flow diagram for new systematic reviews
(Page MJ, et al. BMJ 2021;372:n71), from three runs of the same command:

  1. IDENTIFICATION   --searchterms FILE  [--other-records CSV]
       Searches every source in the file (PubMed, Europe PMC, ClinicalTrials.gov), removes
       duplicates, saves the records and prints the identification numbers. Nothing is sent
       to the model.
  2. SCREENING        ... --screening FILE
       The model screens every title/abstract (or trial registry summary) against YOUR
       criteria. Writes results_<name>_for_review.csv: the records YOU need to look at,
       with empty columns for your decisions.
  3. ELIGIBILITY      ... --screening FILE --eligibility
       After you have filled in the review sheet (retrieved? eligible? reason?), counts the
       reports not retrieved, assessed, excluded (with reasons) and included, and writes
       results_<name>_included.csv: the papers in your review.

SEARCH TERMS FILE (one search per line, a real TAB between columns):
    source<TAB>name<TAB>query      source = pubmed, europepmc or clinicaltrials
    name<TAB>query                 (no source given: PubMed)
  - Lines starting with # are ignored; end a line with \\ to continue it on the next line.
  - Each source has its own query syntax (see README and queries/). Use UPPERCASE AND/OR/NOT.
  - All searches run; records are merged and de-duplicated across sources (on PMID, DOI,
    NCT number, then title). The 'sources' and 'searches' columns show where each was found.

SCREENING FILE (your inclusion criteria, one per line: code<TAB>criterion; see criteria/).
A record is kept only if it meets ALL criteria. 'unclear' records and records with no
abstract are kept for you to decide. The model's exclusions are reported as "excluded by
automation tool" (the PRISMA footnote asks for human and automation exclusions separately).

OTHER RECORDS (--other-records CSV): records found by other methods (websites, organisations,
citation searching). Columns: source, title, and optionally abstract, doi, pmid, nct_id, year,
url. They go straight to the review sheet (in PRISMA they skip title/abstract screening).

DATE RANGE (--date-from / --date-to): YYYY, YYYY-MM or YYYY-MM-DD. Either can be left out.
PubMed and Europe PMC use publication date (--date-type edat: date added to the database);
ClinicalTrials.gov always uses the date the trial was first submitted.

Outputs (<name> = search terms file name, or --out):
  results_<name>_papers.json        the saved records (step 1)
  results_<name>_search_log.json    sources, searches, dates, hit counts, date run
  results_<name>_prisma.txt/.json   the PRISMA 2020 numbers (filled in as far as each step allows)
  results_<name>_screening.csv      step 2: every record's screening decision, reason, abstract
  results_<name>_exclusion_reasons.csv  step 2: every record and which criteria (C1, C2...) it failed
  results_<name>_for_review.csv     step 2: the records you need to decide on (your worksheet)
  results_<name>_included.csv       step 3: the reports included in the review

RETRIEVING EVERYTHING: by default every match is retrieved (--max-papers N keeps the top N per
search instead, but then the review is not complete). PubMed returns at most 9,999 records per
query, so above that the date range is split automatically. When the searches find 1000 or
more records (change with --warn-records) you are warned and asked y/n before anything is sent
to the model. Large screening runs also ask first (--yes to skip, needed in batch jobs), and
progress is saved: if a run stops, rerun the same command to resume (--fresh starts over).

Usage:
    export ANTHROPIC_API_KEY=...
    export NCBI_EMAIL=you@example.org        # polite use of the NCBI API
    python lit_extract.py --searchterms queries/vaccine_efficacy_searchterms.txt --date-from 2015
    python lit_extract.py --searchterms queries/vaccine_efficacy_searchterms.txt --date-from 2015 \\
        --screening criteria/vaccine_efficacy_screening.txt
    # fill in results_vaccine_efficacy_searchterms_for_review.csv, then:
    python lit_extract.py --searchterms queries/vaccine_efficacy_searchterms.txt --date-from 2015 \\
        --screening criteria/vaccine_efficacy_screening.txt --eligibility
"""
import argparse
import calendar
import csv
import datetime
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path

DEFAULT_MODEL = "claude-sonnet-5-5"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
CTGOV = "https://clinicaltrials.gov/api/v2/studies"
USER_AGENT = "lit-review-tool/1.0 (PRISMA screening helper)"
PUBMED_MAX = 9999   # PubMed esearch cannot return more than this for one query
PAGE = 1000         # records per page when retrieving everything
CONFIRM_ABOVE = 200  # ask before making this many or more API calls

# source name in the search terms file -> (PRISMA box, label)
SOURCES = {
    "pubmed": ("database", "PubMed"),
    "europepmc": ("database", "Europe PMC"),
    "clinicaltrials": ("register", "ClinicalTrials.gov"),
}


# ---------- Search terms file ----------
def parse_query_file(path):
    """Read an editable search file into [(name, source, query), ...]. See module docstring."""
    lines, pending = [], ""
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if pending:
            line, pending = pending + " " + line, ""
        if line.endswith("\\"):
            pending = line[:-1].strip()
            continue
        lines.append(line)
    if pending:
        lines.append(pending)

    searches, names = [], set()
    for i, line in enumerate(lines, 1):
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) >= 3:
            source, name, query = parts[0].lower(), parts[1], " ".join(parts[2:])
            if source not in SOURCES:
                raise ValueError(f"Unknown source '{parts[0]}' in search '{name}': "
                                 f"use {', '.join(SOURCES)}")
        elif len(parts) == 2:
            source, name, query = "pubmed", parts[0], parts[1]
        else:
            source, name, query = "pubmed", f"search{i}", parts[0]
        if not query:
            raise ValueError(f"Search '{name}' has no query text")
        if name in names:
            raise ValueError(f"Duplicate search name '{name}': names must be unique")
        names.add(name)
        searches.append((name, source, query))
    if not searches:
        raise ValueError(f"No searches found in {path}")
    return searches


def check_query_syntax(query):
    """Raise on unbalanced quotes/brackets; return a list of warnings for likely mistakes."""
    if query.count('"') % 2:
        raise ValueError(f"Unbalanced double quotes in query: {query}")
    outside_quotes = re.sub(r'"[^"]*"', "", query)
    if outside_quotes.count("(") != outside_quotes.count(")"):
        raise ValueError(f"Unbalanced brackets in query: {query}")
    warnings = []
    if re.search(r"\s(and|or|not)\s", outside_quotes):
        warnings.append("lower-case and/or/not found: searches need UPPERCASE AND / OR / NOT")
    return warnings


# ---------- Date range ----------
def parse_date(value, end=False):
    """Accept YYYY, YYYY-MM or YYYY-MM-DD (or with /). Return PubMed's YYYY/MM/DD.

    A missing month/day becomes the first day (start) or last day (end) of the period.
    """
    match = re.fullmatch(r"(\d{4})(?:[-/](\d{1,2}))?(?:[-/](\d{1,2}))?", value.strip())
    if not match:
        raise ValueError(f"Bad date '{value}': use YYYY, YYYY-MM or YYYY-MM-DD")
    year, month, day = match.groups()
    month = int(month) if month else (12 if end else 1)
    if not 1 <= month <= 12:
        raise ValueError(f"Bad month in date '{value}'")
    last = calendar.monthrange(int(year), month)[1]
    day = int(day) if day else (last if end else 1)
    if not 1 <= day <= last:
        raise ValueError(f"Bad day in date '{value}'")
    return f"{year}/{month:02d}/{day:02d}"


def resolve_dates(date_from, date_to):
    """Return (mindate, maxdate) in PubMed format, or (None, None) if no dates were given."""
    if not date_from and not date_to:
        return None, None
    start = parse_date(date_from) if date_from else "1900/01/01"
    end = parse_date(date_to, end=True) if date_to else datetime.date.today().strftime("%Y/%m/%d")
    if start > end:
        raise ValueError(f"Start date {start} is after end date {end}")
    return start, end


# ---------- Records ----------
RECORD_FIELDS = ("pmid", "doi", "nct_id", "title", "year", "abstract", "url")


def make_record(source, record_id=None, **fields):
    """One record in a common shape, whichever source it came from."""
    rec = {k: str(fields.get(k) or "").strip() for k in RECORD_FIELDS}
    rec["source"] = source
    rec["record_id"] = (f"PMID:{rec['pmid']}" if rec["pmid"] else record_id or rec["nct_id"]
                        or (f"DOI:{rec['doi'].lower()}" if rec["doi"] else ""))
    return rec


def _strip_tags(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def _get(url, params):
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8")


# ---------- PubMed ----------
def _eutils(endpoint, **params):
    params["tool"] = "lit-review-tool"
    if os.environ.get("NCBI_EMAIL"):
        params["email"] = os.environ["NCBI_EMAIL"]
    if os.environ.get("NCBI_API_KEY"):
        params["api_key"] = os.environ["NCBI_API_KEY"]
    return _get(EUTILS + endpoint, params)


def _esearch(query, retmax, retstart, mindate, maxdate, datetype):
    params = dict(db="pubmed", term=query, retmax=retmax, retstart=retstart,
                  retmode="json", sort="relevance")
    if mindate and maxdate:
        params.update(datetype=datetype, mindate=mindate, maxdate=maxdate)
    result = json.loads(_eutils("esearch.fcgi", **params))["esearchresult"]
    return list(result["idlist"]), int(result.get("count", 0))


def _to_date(text):
    year, month, day = map(int, text.split("/"))
    return datetime.date(year, month, day)


def _collect_all(query, mindate, maxdate, datetype):
    """Every PMID matching the query in the date range.

    PubMed will not return more than PUBMED_MAX records per query, so if there are more
    matches the date range is split in half repeatedly until each piece fits.
    Returns (ids, total_matches_reported_by_pubmed).
    """
    _, count = _esearch(query, 0, 0, mindate, maxdate, datetype)
    if count <= PUBMED_MAX:
        ids = []
        for start in range(0, count, PAGE):
            page, _ = _esearch(query, PAGE, start, mindate, maxdate, datetype)
            ids.extend(page)
            time.sleep(0.4)
        return ids, count
    start = _to_date(mindate or "1900/01/01")
    end = _to_date(maxdate) if maxdate else datetime.date.today()
    if start >= end:  # a single day with more matches than PubMed will return
        ids, _ = _esearch(query, PUBMED_MAX, 0, mindate or "1900/01/01", maxdate or
                          end.strftime("%Y/%m/%d"), datetype)
        return ids, count
    mid = start + (end - start) // 2
    fmt = lambda d: d.strftime("%Y/%m/%d")
    left, _ = _collect_all(query, fmt(start), fmt(mid), datetype)
    right, _ = _collect_all(query, fmt(mid + datetime.timedelta(days=1)), fmt(end), datetype)
    return left + right, count


def search_pubmed(query, max_papers, mindate=None, maxdate=None, datetype="pdat"):
    """Return {'ids': unique PMIDs, 'count': total PubMed matches}. Records are fetched later.

    max_papers=None retrieves every match (paged, with date splitting beyond PUBMED_MAX);
    otherwise only the top max_papers by relevance.
    """
    if max_papers is None:
        ids, count = _collect_all(query, mindate, maxdate, datetype)
    else:
        ids, count = _esearch(query, min(max_papers, PUBMED_MAX), 0, mindate, maxdate, datetype)
    return {"ids": list(dict.fromkeys(ids)), "count": count}  # unique, order kept


def fetch_papers(pmids):
    papers = []
    for i in range(0, len(pmids), 50):  # batch politely
        batch = pmids[i:i + 50]
        xml_text = _eutils("efetch.fcgi", db="pubmed", id=",".join(batch),
                           rettype="abstract", retmode="xml")
        papers.extend(parse_pubmed_xml(xml_text))
        if len(pmids) > 200 and (i // 50) % 10 == 9:
            print(f"  fetched {min(i + 50, len(pmids))}/{len(pmids)} PubMed records")
        time.sleep(0.4)
    return papers


def parse_pubmed_xml(xml_text):
    root = ET.fromstring(xml_text)
    papers = []
    for art in root.findall(".//PubmedArticle"):
        title_el = art.find(".//ArticleTitle")
        parts = []
        for at in art.findall(".//Abstract/AbstractText"):
            text = "".join(at.itertext()).strip()
            label = at.get("Label")
            parts.append(f"{label}: {text}" if label else text)
        year = art.findtext(".//JournalIssue/PubDate/Year") or \
            (art.findtext(".//JournalIssue/PubDate/MedlineDate") or "")[:4]
        doi = ""
        for aid in art.findall(".//PubmedData/ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi":
                doi = aid.text or ""
        pmid = art.findtext(".//MedlineCitation/PMID") or ""
        papers.append(make_record(
            "pubmed", pmid=pmid, doi=doi, year=year, abstract=" ".join(parts),
            title="".join(title_el.itertext()) if title_el is not None else "",
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"))
    return papers


# ---------- Europe PMC (journal articles, preprints, and more) ----------
def _europepmc_record(r):
    src, rid = r.get("source", ""), r.get("id", "")
    rec = make_record("europepmc", record_id=f"{src}:{rid}", pmid=r.get("pmid"), doi=r.get("doi"),
                      title=_strip_tags(r.get("title")), year=r.get("pubYear"),
                      abstract=_strip_tags(r.get("abstractText")),
                      url=f"https://europepmc.org/article/{src}/{rid}")
    if src == "PPR":
        rec["preprint"] = "yes"
    return rec


def search_europepmc(query, max_papers, mindate=None, maxdate=None, datetype="pdat"):
    """Return {'ids', 'count', 'records'}: Europe PMC sends the abstracts with the search."""
    if mindate:
        field = "CREATION_DATE" if datetype == "edat" else "FIRST_PDATE"
        query = f"({query}) AND {field}:[{mindate.replace('/', '-')} TO {maxdate.replace('/', '-')}]"
    records, cursor, count = [], "*", 0
    while True:
        want = PAGE if max_papers is None else min(PAGE, max_papers - len(records))
        if want <= 0:
            break
        data = json.loads(_get(EUROPEPMC, {"query": query, "format": "json", "resultType": "core",
                                           "pageSize": want, "cursorMark": cursor}))
        count = int(data.get("hitCount", 0))
        page = data.get("resultList", {}).get("result", [])
        records += [_europepmc_record(r) for r in page]
        nxt = data.get("nextCursorMark")
        if not page or not nxt or nxt == cursor or len(records) >= count:
            break
        cursor = nxt
        time.sleep(0.2)
    return {"ids": [r["record_id"] for r in records], "count": count, "records": records}


# ---------- ClinicalTrials.gov (trial register) ----------
CTGOV_FIELDS = ("NCTId,BriefTitle,OfficialTitle,BriefSummary,Condition,InterventionName,Phase,"
                "OverallStatus,StudyType,StudyFirstSubmitDate")


def _ctgov_record(study):
    p = study.get("protocolSection", {})
    ident, status, design = (p.get("identificationModule", {}), p.get("statusModule", {}),
                             p.get("designModule", {}))
    nct = ident.get("nctId", "")
    interventions = [i.get("name", "") for i in p.get("armsInterventionsModule", {}).get("interventions", [])]
    summary = [
        p.get("descriptionModule", {}).get("briefSummary", ""),
        "Conditions: " + "; ".join(p.get("conditionsModule", {}).get("conditions", [])),
        "Interventions: " + "; ".join(interventions),
        f"Study type: {design.get('studyType', '')}; phase: {', '.join(design.get('phases', [])) or 'not stated'}; "
        f"status: {status.get('overallStatus', '')}",
    ]
    return make_record("clinicaltrials", nct_id=nct,
                       title=ident.get("officialTitle") or ident.get("briefTitle"),
                       year=status.get("studyFirstSubmitDate", "")[:4],
                       abstract="\n".join(s for s in summary if s.strip()),
                       url=f"https://clinicaltrials.gov/study/{nct}")


def search_clinicaltrials(query, max_papers, mindate=None, maxdate=None, datetype="pdat"):
    """Return {'ids', 'count', 'records'}. Dates filter on the trial's first-submitted date."""
    params = {"query.term": query, "format": "json", "countTotal": "true", "fields": CTGOV_FIELDS}
    if mindate:
        params["filter.advanced"] = (f"AREA[StudyFirstSubmitDate]RANGE[{mindate.replace('/', '-')},"
                                     f"{maxdate.replace('/', '-')}]")
    records, count = [], 0
    while True:
        want = PAGE if max_papers is None else min(PAGE, max_papers - len(records))
        if want <= 0:
            break
        data = json.loads(_get(CTGOV, {**params, "pageSize": want}))
        count = int(data.get("totalCount", count))   # only sent with the first page
        page = data.get("studies", [])
        records += [_ctgov_record(s) for s in page]
        if not page or not data.get("nextPageToken"):
            break
        params["pageToken"] = data["nextPageToken"]
        time.sleep(0.2)
    return {"ids": [r["record_id"] for r in records], "count": count, "records": records}


SEARCH_FNS = {"pubmed": search_pubmed, "europepmc": search_europepmc,
              "clinicaltrials": search_clinicaltrials}


# ---------- Running the searches ----------
def run_searches(searches, max_papers, mindate, maxdate, datetype, search_fns=None):
    """Run every search. Return ({source: {id: [search names]}}, {source: {id: record}}, log)."""
    fns = {**SEARCH_FNS, **(search_fns or {})}
    hits, prefetched, log = {}, {}, []
    for name, source, query in searches:
        result = fns[source](query, max_papers, mindate, maxdate, datetype)
        for rid in result["ids"]:
            hits.setdefault(source, {}).setdefault(rid, []).append(name)
        for rec in result.get("records", []):
            prefetched.setdefault(source, {})[rec["record_id"]] = rec
        log.append({"name": name, "source": source, "query": query,
                    "matched": result["count"], "retrieved": len(result["ids"])})
    return hits, prefetched, log


def gather_papers(searches, max_papers, mindate, maxdate, datetype,
                  search_fns=None, fetch_fn=fetch_papers, before_fetch=None):
    """Every record from every source, one per source (not yet de-duplicated across sources)."""
    hits, prefetched, log = run_searches(searches, max_papers, mindate, maxdate, datetype, search_fns)
    if before_fetch:  # e.g. warn about a very large result set before downloading PubMed records
        before_fetch(sum(len(ids) for ids in hits.values()), log)
    records = []
    for source, ids in hits.items():
        if source == "pubmed":
            for rec in fetch_fn(list(ids)):
                rec["searches"] = ";".join(ids.get(rec["pmid"], []))
                records.append(rec)
        else:
            records += [dict(prefetched[source][rid], searches=";".join(names))
                        for rid, names in ids.items()]
    return records, log


# ---------- Cache (invalidated automatically when searches or dates change) ----------
def search_signature(searches, mindate, maxdate, datetype, max_papers):
    payload = json.dumps([searches, mindate, maxdate, datetype, max_papers], sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def load_cache(path, signature):
    """Return (papers, log) if the cache matches the current searches, else None."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("signature") != signature:
        return None
    return data["papers"], data["search_log"]


def save_cache(path, signature, papers, log):
    Path(path).write_text(json.dumps(
        {"signature": signature, "papers": papers, "search_log": log}, indent=1))


# ---------- De-duplication ----------
def _title_key(title):
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())


def _merge(a, b):
    return ";".join(dict.fromkeys([s for s in (a or "").split(";") + (b or "").split(";") if s]))


def _dedupe_keys(paper):
    """Keys that identify the same record. Title only counts with the same year (so a review and
    its update are kept apart), and preprints are only matched with other preprints: a preprint
    and its journal article are two reports of one study, not duplicates."""
    title = _title_key(paper.get("title"))
    keys = [("pmid", paper.get("pmid")), ("doi", (paper.get("doi") or "").lower()),
            ("nct", paper.get("nct_id")), ("title", f"{title}|{paper.get('year', '')}" if title else "")]
    kind = "preprint_" if paper.get("preprint") else ""
    return [(kind + name, value) for name, value in keys if value]


def dedupe_papers(papers):
    """Keep one copy of each record (match on PMID, DOI, NCT number, then title and year).

    'sources' and 'searches' are merged, and gaps (e.g. a missing abstract) are filled from
    the duplicate.
    """
    index, unique = {}, []
    for paper in papers:
        keys = _dedupe_keys(paper)
        kept = next((index[k] for k in keys if k in index), None)
        if kept is None:
            kept = dict(paper, sources=paper.get("sources") or paper.get("source", ""))
            unique.append(kept)
        else:
            kept["sources"] = _merge(kept["sources"], paper.get("sources") or paper.get("source"))
            kept["searches"] = _merge(kept.get("searches"), paper.get("searches"))
            for field in RECORD_FIELDS:
                if not kept.get(field) and paper.get(field):
                    kept[field] = paper[field]
        for k in keys:
            index.setdefault(k, kept)
    return unique


# ---------- Records found by other methods (websites, organisations, citation searching) ----------
OTHER_COLUMNS = ("abstract", "doi", "pmid", "nct_id", "year", "url")


def load_other_records(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError(f"No records found in {path}")
    missing = {"source", "title"} - set(rows[0])
    if missing:
        raise ValueError(f"{path} needs the columns source and title (missing: {', '.join(sorted(missing))})")
    records = []
    for i, row in enumerate(rows, 2):
        source, title = (row.get("source") or "").strip(), (row.get("title") or "").strip()
        if not source or not title:
            raise ValueError(f"{path} line {i}: every record needs a source and a title")
        rec = make_record("other", title=title, **{k: row.get(k) for k in OTHER_COLUMNS})
        rec.update(record_id=rec["record_id"] or f"OTHER:{i - 1}", other_source=source,
                   sources=f"other: {source}", searches="")
        records.append(rec)
    return records


def split_other_records(other, database_records):
    """Drop other-method records already found via databases/registers (or listed twice).

    Returns (new records, number not counted again)."""
    index = {k for r in database_records for k in _dedupe_keys(r)}
    new = []
    for rec in other:
        keys = _dedupe_keys(rec)
        if any(k in index for k in keys):
            continue
        index.update(keys)
        new.append(rec)
    return new, len(other) - len(new)


# ---------- Model replies and quote checks ----------
def parse_json_object(text):
    """Pull the first JSON object out of a model reply; {} if there isn't a valid one."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[\"'‘’“”]", "", text)).strip().lower()


def verify_quote(quote, abstract):
    """True only if the quote appears verbatim (ignoring case, spacing, quote marks)."""
    q = _norm(quote or "")
    return len(q) >= 15 and q in _norm(abstract)


# ---------- Screening (title/abstract) ----------
SCREEN_DECISIONS = ("include", "exclude", "unclear")
TO_HUMAN = ("include", "unclear", "no_abstract")   # not excluded by the model: you decide on these
SCREEN_COLUMNS = ["record_id", "sources", "searches", "decision", "failed_criterion",
                  "failed_criteria", "unclear_criteria", "reason", "evidence_quote",
                  "quote_verified", "title", "year", "pmid", "doi", "nct_id", "url", "abstract"]
VERDICTS = ("met", "not_met", "unclear")


def parse_criteria_file(path):
    """Read inclusion criteria: one per line as code<TAB>text (no tab: C1, C2, ...)."""
    criteria, codes = [], set()
    lines = [ln.strip() for ln in Path(path).read_text().splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    for i, line in enumerate(lines, 1):
        code, text = line.split("\t", 1) if "\t" in line else (f"C{i}", line)
        code, text = code.strip(), text.strip()
        if not text:
            raise ValueError(f"Criterion '{code}' has no text")
        if code in codes:
            raise ValueError(f"Duplicate criterion code '{code}'")
        codes.add(code)
        criteria.append((code, text))
    if not criteria:
        raise ValueError(f"No criteria found in {path}")
    return criteria


def build_screening_prompt(criteria, paper):
    listed = "\n".join(f"{code}: {text}" for code, text in criteria)
    shape = {"decision": "include | exclude | unclear",
             "criteria": {code: "met | not_met | unclear" for code, _ in criteria},
             "reason": "one short sentence",
             "evidence_quote": "verbatim phrase from the text supporting the decision, or empty"}
    return (
        "You are screening one record (a title and abstract, or a trial registry summary) for a "
        "systematic review.\n"
        f"Include it only if it meets ALL of these criteria:\n{listed}\n\n"
        "Decide: include (all criteria met), exclude (at least one criterion clearly not met) or "
        "unclear (the text does not say enough to decide). If torn between include and "
        "exclude, answer unclear. Judge EVERY criterion separately (not_met = the text clearly "
        "shows it is not met), even after finding one that fails. Use ONLY the text below; do "
        "not use outside knowledge.\n\n"
        f"Return only JSON in this shape: {json.dumps(shape)}\n\n"
        f"Title: {paper['title']}\n\nAbstract:\n{paper['abstract']}"
    )


def _screen_row(paper, decision, failed, reason, quote, verdicts=None):
    """verdicts = {code: met/not_met/unclear}; failed = the first criterion not met (PRISMA reason)."""
    verdicts = verdicts or {}
    row = {k: paper.get(k, "") for k in SCREEN_COLUMNS}
    row.update(decision=decision, failed_criterion=failed, reason=reason, evidence_quote=quote,
               quote_verified=verify_quote(quote, paper.get("abstract", "")) if quote else "",
               criteria=verdicts,
               failed_criteria="; ".join(c for c, v in verdicts.items() if v == "not_met") or failed,
               unclear_criteria="; ".join(c for c, v in verdicts.items() if v == "unclear"))
    return row


def screen_paper(client, model, criteria, paper):
    """Screen one record. Anything the model gets wrong or garbles is kept as 'unclear'."""
    if not paper.get("abstract"):
        return _screen_row(paper, "no_abstract", "", "No abstract or summary: screen the title yourself", "")
    resp = client.messages.create(
        model=model, max_tokens=600,
        messages=[{"role": "user", "content": build_screening_prompt(criteria, paper)}],
    )
    data = parse_json_object("".join(b.text for b in resp.content if b.type == "text"))
    decision = str(data.get("decision", "")).strip().lower()
    reason = str(data.get("reason", "")).strip()
    if decision not in SCREEN_DECISIONS:
        decision, reason = "unclear", "Model reply could not be parsed: kept for human review"
    given = data.get("criteria") if isinstance(data.get("criteria"), dict) else {}
    verdicts = {}
    for code, _ in criteria:
        v = str(given.get(code, "")).strip().lower().replace(" ", "_")
        verdicts[code] = v if v in VERDICTS else "unclear"
    not_met = [c for c, v in verdicts.items() if v == "not_met"]
    failed = not_met[0] if not_met else str(data.get("failed_criterion", "")).strip()
    if decision != "exclude":
        failed = ""
    elif failed not in verdicts:
        failed = "unspecified"
    return _screen_row(paper, decision, failed, reason, str(data.get("evidence_quote", "")),
                       verdicts if given else None)


DECISION_ORDER = {"exclude": 0, "unclear": 1, "no_abstract": 2, "include": 3}


def exclusion_reason_rows(screening, criteria):
    """Every screened record with why it did (not) pass: one column per criterion.

    Sorted excluded first (grouped by first failed criterion), then unclear, no abstract, included.
    Records screened before per-criterion verdicts existed only show their first failed criterion.
    """
    rows = []
    for r in screening:
        verdicts = r.get("criteria") or {}
        row = {k: r.get(k, "") for k in REASONS_COLUMNS}
        row["failed_criteria"] = r.get("failed_criteria") or r.get("failed_criterion", "")
        row["failed_criteria_text"] = " | ".join(
            f"{c}: {dict(criteria).get(c, '')}".rstrip(": ") for c in row["failed_criteria"].split("; ") if c)
        row.update({code: verdicts.get(code, "") for code, _ in criteria})
        rows.append(row)
    return sorted(rows, key=lambda x: (DECISION_ORDER.get(x["decision"], 9), x["failed_criterion"]))


REASONS_COLUMNS = ["record_id", "title", "decision", "failed_criterion", "failed_criteria",
                   "failed_criteria_text", "unclear_criteria", "reason", "evidence_quote",
                   "year", "pmid", "doi", "nct_id", "url"]


# ---------- Your review sheet (step 2 writes it, you fill it in, step 3 reads it) ----------
HUMAN_COLUMNS = ["human_screening", "retrieved", "eligible", "exclusion_reason", "study_id"]
REVIEW_COLUMNS = (["record_id", "title"] + HUMAN_COLUMNS +
                  ["path", "model_decision", "model_reason", "sources", "year", "pmid", "doi",
                   "nct_id", "url", "abstract"])
YES, NO = ("yes", "y"), ("no", "n")


def review_rows(screening, other):
    """The records you need to decide on: those the model did not exclude, plus other methods."""
    rows = []
    for r in screening:
        if r["decision"] in TO_HUMAN:
            rows.append({**{k: r.get(k, "") for k in REVIEW_COLUMNS}, "path": "databases",
                         "model_decision": r["decision"], "model_reason": r["reason"]})
    for o in other:
        rows.append({**{k: o.get(k, "") for k in REVIEW_COLUMNS}, "path": "other",
                     "model_decision": "not screened (other methods)", "model_reason": ""})
    for row in rows:
        row.update({k: "" for k in HUMAN_COLUMNS})
    return rows


def load_review(path, expected):
    """Read your filled-in review sheet. expected = {record_id: 'databases' or 'other'}.

    Raises ValueError listing every problem, so the sheet can be fixed in one go.
    """
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    problems, undecided, out, seen = [], [], [], set()
    for line, row in enumerate(rows, 2):
        rid = (row.get("record_id") or "").strip()
        val = lambda col: (row.get(col) or "").strip()
        if rid not in expected:
            problems.append(f"line {line}: record '{rid}' is not in the current screening results "
                            "(same search terms, dates, criteria and --other-records as step 2?)")
            continue
        if rid in seen:
            problems.append(f"line {line}: record '{rid}' appears twice")
            continue
        seen.add(rid)
        entry = dict(row, record_id=rid, path=expected[rid], human_exclude=False, retrieved=None,
                     eligible=None, reason=val("exclusion_reason"), study_id=val("study_id") or rid)
        screening, retrieved, eligible = (val(c).lower() for c in ("human_screening", "retrieved", "eligible"))
        if screening in ("exclude", "excluded"):
            if entry["path"] == "other":
                problems.append(f"line {line} ({rid}): records from other methods skip title/abstract "
                                "screening; use retrieved/eligible instead")
            entry["human_exclude"] = True
        elif screening not in ("", "include", "keep"):
            problems.append(f"line {line} ({rid}): human_screening must be empty or 'exclude'")
        elif retrieved not in YES + NO or (retrieved in YES and eligible not in YES + NO):
            undecided.append(rid)
        elif retrieved in YES and eligible in NO and not entry["reason"]:
            problems.append(f"line {line} ({rid}): eligible is 'no', so give an exclusion_reason")
        else:
            entry["retrieved"] = retrieved in YES
            entry["eligible"] = (eligible in YES) if entry["retrieved"] else None
        out.append(entry)
    missing = sorted(set(expected) - seen)
    if missing:
        problems.append(f"{len(missing)} records from the screening results are missing from the "
                        f"sheet, e.g. {', '.join(missing[:5])}")
    if undecided:
        problems.append(f"{len(undecided)} records have no decision yet (retrieved yes/no, and if "
                        f"retrieved, eligible yes/no), e.g. {', '.join(undecided[:5])}")
    if problems:
        raise ValueError(f"The review sheet {path} is not complete:\n  " + "\n  ".join(problems))
    return out


# ---------- PRISMA 2020 numbers ----------
def _retrieval_counts(rows):
    """Reports sought, not retrieved, assessed and excluded (with reasons) for one PRISMA column."""
    sought = [r for r in rows if not r["human_exclude"]]
    assessed = [r for r in sought if r["retrieved"]]
    reasons = Counter(r["reason"] for r in assessed if not r["eligible"])
    return {
        "reports_sought_for_retrieval": len(sought),
        "reports_not_retrieved": len(sought) - len(assessed),
        "reports_assessed_for_eligibility": len(assessed),
        "reports_excluded": sum(reasons.values()),
        "reports_excluded_reasons": dict(reasons.most_common()),
    }


def compute_prisma(search_log, per_source, n_unique, criteria=None, screening=None,
                   other=(), other_not_counted=0, review=None):
    """Every number in the PRISMA 2020 flow diagram for new systematic reviews.

    per_source: {source: records retrieved} BEFORE de-duplication. screening (step 2) and
    review (step 3) are None until those steps are done; the boxes they fill are then None.
    """
    identified = sum(per_source.values())
    boxes = lambda kind: [{"source": SOURCES[s][1], "n": n} for s, n in per_source.items()
                          if SOURCES[s][0] == kind]
    p = {
        "identification": {
            "databases": boxes("database"),
            "databases_total": sum(b["n"] for b in boxes("database")),
            "registers": boxes("register"),
            "registers_total": sum(b["n"] for b in boxes("register")),
            "searches": [{k: e[k] for k in ("name", "source", "matched", "retrieved")} for e in search_log],
            "truncated_searches": [e["name"] for e in search_log if e["matched"] > e["retrieved"]],
            "duplicate_records_removed": identified - n_unique,
            "records_marked_ineligible_by_automation_tools": 0,
            "records_removed_for_other_reasons": 0,
            "other_methods": [{"source": s, "n": n}
                              for s, n in Counter(o["other_source"] for o in other).items()],
            "other_methods_already_found_not_counted": other_not_counted,
        },
        "screening": {"records_screened": None, "records_excluded": None,
                      "records_excluded_by_automation_tool": None, "records_excluded_by_human": None,
                      "automation_exclusions_by_criterion": {}, "records_passed_to_human": None,
                      "reports_sought_for_retrieval": None, "reports_not_retrieved": None,
                      "reports_assessed_for_eligibility": None, "reports_excluded": None,
                      "reports_excluded_reasons": {}},
        "other_methods": {"reports_sought_for_retrieval": len(other) if other else None,
                          "reports_not_retrieved": None, "reports_assessed_for_eligibility": None,
                          "reports_excluded": None, "reports_excluded_reasons": {}},
        "included": {"studies_included_in_review": None, "reports_of_included_studies": None},
    }
    if screening is not None:
        counts = Counter(r["decision"] for r in screening)
        failed = Counter(r["failed_criterion"] for r in screening if r["decision"] == "exclude")
        p["screening"].update(
            records_screened=len(screening),
            records_excluded_by_automation_tool=counts["exclude"],
            automation_exclusions_by_criterion={
                code: {"n": n, "criterion": dict(criteria or []).get(code, "")}
                for code, n in sorted(failed.items())},
            records_passed_to_human=len(screening) - counts["exclude"],
            of_which_unclear=counts["unclear"], of_which_no_abstract=counts["no_abstract"])
    if review is not None:
        db = [r for r in review if r["path"] == "databases"]
        human = sum(r["human_exclude"] for r in db)
        p["screening"].update(records_excluded_by_human=human,
                              records_excluded=p["screening"]["records_excluded_by_automation_tool"] + human,
                              **_retrieval_counts(db))
        if other:
            p["other_methods"].update(_retrieval_counts([r for r in review if r["path"] == "other"]))
        included = [r for r in review if r["retrieved"] and r["eligible"]]
        p["included"] = {"studies_included_in_review": len({r["study_id"] for r in included}),
                         "reports_of_included_studies": len(included)}
    return p


def render_prisma(p, model=None):
    """The numbers laid out box by box, in the order of the PRISMA 2020 flow diagram."""
    n = lambda v: "?" if v is None else v
    ident, scr, oth, inc = p["identification"], p["screening"], p["other_methods"], p["included"]

    def retrieval(box):
        lines = [f"    Reports sought for retrieval (n = {n(box['reports_sought_for_retrieval'])})",
                 f"    Reports not retrieved (n = {n(box['reports_not_retrieved'])})",
                 f"    Reports assessed for eligibility (n = {n(box['reports_assessed_for_eligibility'])})",
                 f"    Reports excluded: (n = {n(box['reports_excluded'])})"]
        return lines + [f"      {reason} (n = {k})" for reason, k in box["reports_excluded_reasons"].items()]

    lines = ["PRISMA 2020 flow diagram for new systematic reviews: n for each box",
             "('?' = not known yet: complete the next step)", "",
             "IDENTIFICATION",
             "  Identification of studies via databases and registers",
             "    Records identified from*:",
             f"      Databases (n = {ident['databases_total']})"]
    lines += [f"        {b['source']} (n = {b['n']})" for b in ident["databases"]]
    lines += [f"      Registers (n = {ident['registers_total']})"]
    lines += [f"        {b['source']} (n = {b['n']})" for b in ident["registers"]]
    lines += ["    Records removed before screening:",
              f"      Duplicate records removed (n = {ident['duplicate_records_removed']})",
              f"      Records marked as ineligible by automation tools "
              f"(n = {ident['records_marked_ineligible_by_automation_tools']})",
              f"      Records removed for other reasons (n = {ident['records_removed_for_other_reasons']})"]
    if ident["other_methods"]:
        lines += ["  Identification of studies via other methods", "    Records identified from:"]
        lines += [f"      {o['source']} (n = {o['n']})" for o in ident["other_methods"]]
        if ident["other_methods_already_found_not_counted"]:
            lines.append(f"      (not counted: {ident['other_methods_already_found_not_counted']} "
                         "already found via databases/registers or listed twice)")
    lines += ["", "SCREENING", "  Databases and registers",
              f"    Records screened (n = {n(scr['records_screened'])})",
              f"    Records excluded** (n = {n(scr['records_excluded'])})",
              f"      by automation tool (n = {n(scr['records_excluded_by_automation_tool'])})"]
    lines += [f"        {code}: {info['n']}  ({info['criterion']})"
              for code, info in scr["automation_exclusions_by_criterion"].items()]
    lines += [f"      by human reviewer (n = {n(scr['records_excluded_by_human'])})"]
    lines += retrieval(scr)
    if ident["other_methods"]:
        lines += ["  Other methods"] + retrieval(oth)
    lines += ["", "INCLUDED",
              f"    Studies included in review (n = {n(inc['studies_included_in_review'])})",
              f"    Reports of included studies (n = {n(inc['reports_of_included_studies'])})",
              "", "*  Records per database and register are listed above.",
              f"** Automation tool: {model or 'an LLM'} screening titles/abstracts against your criteria "
              "(PRISMA asks for human and automation exclusions separately)."]
    if ident["truncated_searches"]:
        lines.append("\nWARNING: more records matched than were retrieved for: "
                     + ", ".join(ident["truncated_searches"])
                     + ". Use --max-papers all, or the review is incomplete.")
    return "\n".join(lines)


# ---------- Filling in the PRISMA 2020 flow diagram (the Word template) ----------
PRISMA_TEMPLATE = Path(__file__).with_name("PRISMA_2020_flow_diagram_new_SRs_v2.docx")
OTHER_COLUMN_EMU = 5_000_000   # text boxes further right than this are the "other methods" column
EMU_PER_PT = 12700


def _xml_text(xml):
    return "".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", xml))


def _paragraph(template, text):
    """A paragraph holding text, with the template paragraph's formatting (kept as is if unchanged)."""
    if _xml_text(template) == text:
        return template
    opening = re.sub(r'\s+w14:(?:paraId|textId)="[^"]*"', "", re.match(r"<w:p\b[^>]*>", template).group(0))
    ppr = re.search(r"<w:pPr\b.*?</w:pPr>|<w:pPr/>", template, re.S)
    rest = template[ppr.end():] if ppr else template
    rpr = re.search(r"<w:r\b[^>]*>\s*(<w:rPr>.*?</w:rPr>)", rest, re.S)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (opening + (ppr.group(0) if ppr else "") + "<w:r>" + (rpr.group(1) if rpr else "")
            + f'<w:t xml:space="preserve">{text}</w:t></w:r></w:p>')


def _box_lines(text, column, p):
    """The lines for one box of the diagram, or None if the box holds no numbers."""
    ident, inc = p["identification"], p["included"]
    box = p["screening"] if column == "databases" else p["other_methods"]
    n = lambda v: 0 if v is None else v
    sources = lambda items: [f"    {b['source']} (n = {b['n']})" for b in items]
    if text.startswith("Records identified from*"):
        return (["Records identified from*:", f"Databases (n = {ident['databases_total']})"]
                + sources(ident["databases"]) + [f"Registers (n = {ident['registers_total']})"]
                + sources(ident["registers"]))
    if text.startswith("Records identified from"):
        other = ident["other_methods"] or [{"source": s, "n": 0}
                                           for s in ("Websites", "Organisations", "Citation searching")]
        return ["Records identified from:"] + [f"{o['source']} (n = {o['n']})" for o in other]
    if text.startswith("Records removed before screening"):
        return ["Records removed before screening:",
                f"Duplicate records removed (n = {ident['duplicate_records_removed']})",
                f"Records marked as ineligible by automation tools "
                f"(n = {ident['records_marked_ineligible_by_automation_tools']})",
                f"Records removed for other reasons (n = {ident['records_removed_for_other_reasons']})"]
    if text.startswith("Records screened"):
        return ["Records screened", f"(n = {n(box['records_screened'])})"]
    if text.startswith("Records excluded"):
        return ["Records excluded**", f"(n = {n(box['records_excluded'])})",
                f"Automation tool {n(box['records_excluded_by_automation_tool'])}; "
                f"human {n(box['records_excluded_by_human'])}"]
    for label, key in (("Reports sought for retrieval", "reports_sought_for_retrieval"),
                       ("Reports not retrieved", "reports_not_retrieved"),
                       ("Reports assessed for eligibility", "reports_assessed_for_eligibility")):
        if text.startswith(label):
            return [label, f"(n = {n(box[key])})"]
    if text.startswith("Reports excluded"):
        reasons = [f"{r} (n = {k})" for r, k in box["reports_excluded_reasons"].items()]
        return ["Reports excluded:"] + (reasons or [f"(n = {n(box['reports_excluded'])})"])
    if text.startswith("Studies included in review"):
        return ["Studies included in review", f"(n = {n(inc['studies_included_in_review'])})",
                "Reports of included studies", f"(n = {n(inc['reports_of_included_studies'])})"]
    return None


def _font_size_that_fits(lines, cx, cy, default=18):
    """Largest font size (in half-points, down to 6 pt) at which the lines should fit a fixed box.

    An estimate: Calibri averages about half an em per character, and the template's text boxes
    have 0.1 inch side margins and 0.05 inch top/bottom margins. Returns (size, fits)."""
    width, height = cx / EMU_PER_PT - 14.4, cy / EMU_PER_PT - 7.2
    for half_points in (default, 16, 14, 12):
        pt = half_points / 2
        per_line = max(1, int(width / (pt * 0.5)))
        needed = sum(max(1, -(-len(line) // per_line)) for line in lines)
        if needed * pt * 1.22 <= height:
            return half_points, True
    return 12, False


def fill_prisma_docx(template, out_path, p):
    """Write a copy of the PRISMA 2020 Word template with every (n = ) filled in.

    Returns a list of problems (boxes whose text may not fit, or boxes left unfilled)."""
    with zipfile.ZipFile(template) as z:
        files = {info: z.read(info) for info in z.infolist()}
    doc = next(info for info in files if info.filename == "word/document.xml")
    xml = files[doc].decode("utf-8")

    def fill_shape(match):
        shape = match.group(0)
        if "<w:txbxContent" not in shape:
            return shape
        offset = re.search(r"<wp:positionH[^>]*>\s*<wp:posOffset>(-?\d+)", shape)
        column = "other" if offset and int(offset.group(1)) > OTHER_COLUMN_EMU else "databases"
        extent = re.search(r'<wp:extent cx="(\d+)" cy="(\d+)"', shape)

        def fill_box(box):   # each box is stored twice (modern Word and fallback): fill both
            paras = re.findall(r"<w:p\b[^>]*>.*?</w:p>", box.group(2), re.S)
            lines = _box_lines(_xml_text(box.group(2)), column, p) if paras else None
            if lines is None:
                return box.group(0)
            body = "".join(_paragraph(paras[i] if i < len(paras) else paras[min(1, len(paras) - 1)], line)
                           for i, line in enumerate(lines))
            if extent:
                size, fits = _font_size_that_fits(lines, int(extent.group(1)), int(extent.group(2)))
                if not fits:
                    problems.append(f"'{lines[0].rstrip(':*')}' ({column}) may be too full to show everything")
                if size != 18:
                    body = re.sub(r'(<w:sz(?:Cs)? w:val=")\d+(")', rf"\g<1>{size}\2", body)
            return box.group(1) + body + "</w:txbxContent>"

        return re.sub(r"(<w:txbxContent[^>]*>)(.*?)</w:txbxContent>", fill_box, shape, flags=re.S)

    problems = []
    xml = re.sub(r"<mc:AlternateContent>.*?</mc:AlternateContent>", fill_shape, xml, flags=re.S)
    files[doc] = xml.encode("utf-8")
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in files.items():
            z.writestr(info, data)
    if "(n = )" in _xml_text(xml):
        problems.append("some boxes still show (n = ): the template may differ from PRISMA 2020 v2")
    return list(dict.fromkeys(problems))   # each box is stored twice: report it once


# ---------- Asking the user ----------
def ask_yes_no(question, input_fn=None):
    """Keep asking until the answer is y or n."""
    while True:
        answer = (input_fn or input)(question).strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")


def confirm_or_exit(message, assume_yes):
    """Show a warning and ask 'continue? y/n'. Exits if the answer is n (or no one can answer)."""
    print(message)
    if assume_yes:
        return
    if not sys.stdin.isatty():
        sys.exit("Not running interactively: rerun with --yes to confirm.")
    if not ask_yes_no("Would you like to continue? [y/n]: "):
        sys.exit("Stopped. Narrow the search terms or date range in your search terms file and rerun.")


# ---------- Resumable long runs ----------
class Checkpoint:
    """Append-only JSONL file of finished work, so a long run can be resumed after a crash.

    Entries made with a different signature (other criteria or model) are ignored.
    """

    def __init__(self, path, signature):
        self.path, self.signature, self.done = Path(path), signature, {}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:  # half-written last line after a crash
                    continue
                if rec.get("signature") == self.signature:
                    self.done[rec["key"]] = rec["value"]

    def add(self, key, value):
        self.done[key] = value
        with open(self.path, "a") as fh:
            fh.write(json.dumps({"signature": self.signature, "key": key, "value": value}) + "\n")


def make_signature(*parts):
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:12]


def parse_max_papers(value):
    """'all' (or 0) means every match; otherwise a positive whole number up to PUBMED_MAX."""
    if str(value).strip().lower() in ("all", "0"):
        return None
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("use a whole number or 'all'")
    if not 0 < n <= PUBMED_MAX:
        raise argparse.ArgumentTypeError(f"must be 1-{PUBMED_MAX}; use 'all' for everything")
    return n


def write_csv(rows, columns, path):
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--searchterms", required=True, metavar="FILE",
                        help="step 1: searches, one per line: source<TAB>name<TAB>query")
    parser.add_argument("--other-records", metavar="CSV",
                        help="records found by other methods (websites, organisations, citation "
                             "searching): columns source, title, and optionally abstract, doi, pmid, "
                             "nct_id, year, url")
    parser.add_argument("--screening", metavar="FILE",
                        help="step 2: your inclusion criteria for title/abstract screening, one per "
                             "line: code<TAB>criterion")
    parser.add_argument("--eligibility", nargs="?", const=True, metavar="CSV",
                        help="step 3: your filled-in review sheet (default: the "
                             "results_<name>_for_review.csv written by step 2)")
    parser.add_argument("--prisma-template", metavar="DOCX", default=str(PRISMA_TEMPLATE),
                        help="step 3 fills in a copy of this PRISMA 2020 flow diagram (Word) "
                             f"(default: {PRISMA_TEMPLATE.name} next to this script)")
    parser.add_argument("--date-from", help="YYYY, YYYY-MM or YYYY-MM-DD")
    parser.add_argument("--date-to", help="YYYY, YYYY-MM or YYYY-MM-DD")
    parser.add_argument("--date-type", choices=["pdat", "edat"], default="pdat",
                        help="pdat = publication date (default); edat = date added to the database "
                             "(ClinicalTrials.gov always uses the first-submitted date)")
    parser.add_argument("--max-papers", type=parse_max_papers, default=None,
                        help="'all' (default, needed for a complete review) or the top N per search")
    parser.add_argument("--warn-records", type=int, default=1000, metavar="N",
                        help="warn and ask y/n when N or more records are found (default 1000; 0 turns it off)")
    parser.add_argument("--yes", action="store_true",
                        help="don't ask for confirmation before many API calls (needed in batch jobs)")
    parser.add_argument("--fresh", action="store_true",
                        help="ignore and delete saved screening progress instead of resuming")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="model for screening (a cheaper model saves cost, but check its "
                             "sensitivity with evaluate.py)")
    parser.add_argument("--out", metavar="NAME",
                        help="start of the output file names (default results_<searchterms file name>)")
    parser.add_argument("--cache", help="JSON file of fetched records (default <out>_papers.json; "
                                        "auto-refreshed if searches/dates change)")
    args = parser.parse_args()
    if args.eligibility and not args.screening:
        parser.error("--eligibility needs --screening (the same criteria file you screened with)")

    searches = parse_query_file(args.searchterms)
    for name, _, query in searches:
        for warning in check_query_syntax(query):
            print(f"WARNING [{name}]: {warning}")
    mindate, maxdate = resolve_dates(args.date_from, args.date_to)
    base = args.out or "results_" + Path(args.searchterms).stem
    stem = base[:-4] if base.endswith(".csv") else base
    cache_path = args.cache or stem + "_papers.json"
    ckpt_path = Path(stem + "_screening.ckpt.jsonl")
    review_path = stem + "_for_review.csv"
    acknowledged = False

    def check_record_count(n_records, log):
        """Warn when the searches found a lot of records, before screening any."""
        nonlocal acknowledged
        if not args.warn_records or n_records < args.warn_records:
            return
        lines = [f"\n{n_records} records found, suggest you narrow the search criteria."]
        lines += [f"  {e['name']}: {e['retrieved']} retrieved of {e['matched']} matching" for e in log]
        confirm_or_exit("\n".join(lines), args.yes)
        acknowledged = True

    # --- step 1: identification
    signature = search_signature(searches, mindate, maxdate, args.date_type, args.max_papers)
    cached = load_cache(cache_path, signature)
    if cached:
        records, search_log = cached
        print(f"Using saved records from {cache_path} (searches and dates unchanged).")
    else:
        if Path(cache_path).exists():
            print("Saved records are out of date (searches or dates changed): searching again.")
        records, search_log = gather_papers(searches, args.max_papers, mindate, maxdate, args.date_type,
                                            before_fetch=check_record_count)
        save_cache(cache_path, signature, records, search_log)
    papers = dedupe_papers(records)
    if cached:
        check_record_count(len(papers), search_log)
    other, other_not_counted = [], 0
    if args.other_records:
        other, other_not_counted = split_other_records(load_other_records(args.other_records), papers)
    per_source = Counter(r["source"] for r in records)

    for entry in search_log:
        note = "" if entry["matched"] <= entry["retrieved"] else \
            "  <- more matches than retrieved: use --max-papers all to get them"
        print(f"[{SOURCES[entry['source']][1]}: {entry['name']}] {entry['matched']} matches, "
              f"{entry['retrieved']} retrieved{note}")
    print(f"{len(papers)} unique records after removing {sum(per_source.values()) - len(papers)} duplicates"
          + (f"; {len(other)} from other methods" if args.other_records else "") + ".")

    log_path, prisma_txt, prisma_json = (stem + "_search_log.json", stem + "_prisma.txt",
                                         stem + "_prisma.json")
    Path(log_path).write_text(json.dumps({
        "date_run": datetime.date.today().isoformat(),
        "sources": sorted({SOURCES[s][1] for _, s, _ in searches}),
        "date_type": args.date_type if mindate else None,
        "date_from": mindate, "date_to": maxdate,
        "max_papers_per_search": args.max_papers or "all",
        "searches": search_log,
        "other_records_file": args.other_records,
        "screening_criteria_file": args.screening,
        "screening_model": args.model if args.screening else None,
        "unique_records": len(papers),
    }, indent=2))

    def finish(prisma, criteria=None):
        text = render_prisma(prisma, args.model if args.screening else None)
        Path(prisma_txt).write_text(text + "\n")
        Path(prisma_json).write_text(json.dumps(
            {"criteria": [{"code": c, "text": t} for c, t in criteria or []], **prisma}, indent=2))
        print("\n" + text + f"\n\nPRISMA numbers: {prisma_txt} (+ .json)\nSearch log:     {log_path}")

    if not args.screening:
        finish(compute_prisma(search_log, per_source, len(papers), other=other,
                              other_not_counted=other_not_counted))
        print(f"\nRecords saved to {cache_path}. To screen them, run the same command again and add "
              f"--screening FILE (nothing is searched again).")
        return

    # --- step 2: screening (title/abstract) by the model, against your criteria
    criteria = parse_criteria_file(args.screening)
    if args.fresh:
        ckpt_path.unlink(missing_ok=True)
    ckpt = Checkpoint(ckpt_path, make_signature("screen", criteria, args.model))
    todo = [p for p in papers if p["record_id"] not in ckpt.done]
    remaining = sum(1 for p in todo if p["abstract"])
    if todo and args.eligibility:
        sys.exit(f"{len(todo)} records have not been screened with these criteria and model. "
                 "Run step 2 (without --eligibility) first.")
    if remaining >= CONFIRM_ABOVE and not acknowledged:
        confirm_or_exit(f"\n{len(papers)} unique records. About to make roughly {remaining} screening "
                        f"API calls.", args.yes)
    if todo:
        import anthropic  # imported here so steps 1 and 3 run without the package or an API key
        client = anthropic.Anthropic(max_retries=5)
        print(f"\nScreening {remaining} titles/abstracts against {len(criteria)} criteria with "
              f"{args.model} ({len(ckpt.done)} already done from a previous run)...")
        try:
            for i, paper in enumerate(todo, 1):
                ckpt.add(paper["record_id"], screen_paper(client, args.model, criteria, paper))
                if i % 10 == 0:
                    print(f"  screened {i}/{len(todo)}")
        except BaseException:
            print("\nStopped early. Progress is saved: rerun the same command to resume "
                  "(use --fresh to start over).")
            raise
    screening = [ckpt.done[p["record_id"]] for p in papers]
    screening_path = stem + "_screening.csv"
    write_csv(screening, SCREEN_COLUMNS, screening_path)
    reasons_path = stem + "_exclusion_reasons.csv"
    write_csv(exclusion_reason_rows(screening, criteria),
              REASONS_COLUMNS + [code for code, _ in criteria], reasons_path)
    sheet = review_rows(screening, other)

    if not args.eligibility:
        if Path(review_path).exists():
            note = (f"{review_path} already exists, so it was NOT overwritten (it may hold your "
                    "decisions). Delete or rename it to get a fresh sheet.")
        else:
            write_csv(sheet, REVIEW_COLUMNS, review_path)
            note = f"Review sheet ({len(sheet)} records for you): {review_path}"
        finish(compute_prisma(search_log, per_source, len(papers), criteria, screening,
                              other, other_not_counted), criteria)
        print(f"Screening:      {screening_path}\nWhy excluded:   {reasons_path}\n{note}")
        print("\nNext: for each row of the review sheet fill in\n"
              "  human_screening   leave empty, or 'exclude' if you reject it on title/abstract\n"
              "  retrieved         yes / no (could you get the full report?)\n"
              "  eligible          yes / no, after reading the full report\n"
              "  exclusion_reason  if eligible is no (e.g. 'C3: no efficacy outcome'); same wording = one reason\n"
              "  study_id          optional: give reports of the same study the same id\n"
              "then run the same command again and add --eligibility.")
        return

    # --- step 3: eligibility (your decisions) and included studies
    sheet_path = review_path if args.eligibility is True else args.eligibility
    if not Path(sheet_path).exists():
        sys.exit(f"Review sheet {sheet_path} not found: run step 2 first, then fill it in.")
    review = load_review(sheet_path, {r["record_id"]: r["path"] for r in sheet})
    included = [r for r in review if r["retrieved"] and r["eligible"]]
    included_path = stem + "_included.csv"
    write_csv(included, ["study_id"] + [c for c in REVIEW_COLUMNS if c not in HUMAN_COLUMNS], included_path)
    prisma = compute_prisma(search_log, per_source, len(papers), criteria, screening, other,
                            other_not_counted, review)
    finish(prisma, criteria)
    print(f"Screening:      {screening_path}\nIncluded:       {included_path} "
          f"({len(included)} reports of {len({r['study_id'] for r in included})} studies)")
    template = Path(args.prisma_template)
    if not template.exists():
        print(f"PRISMA template {template} not found, so no Word diagram was made (see --prisma-template).")
        return
    docx_path = stem + "_prisma.docx"
    problems = fill_prisma_docx(template, docx_path, prisma)
    print(f"PRISMA diagram: {docx_path}")
    for problem in problems:
        print(f"  WARNING: {problem}. Check the diagram; every number is also in {prisma_txt}.")
    if not other:
        print("No --other-records were given: the 'other methods' column shows 0. If you didn't search "
              "other sources, you can use the PRISMA 2020 template for databases and registers only.")


if __name__ == "__main__":
    main()
