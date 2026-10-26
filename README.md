## LLM-LitRev
# Using Claude API and PubMed API to speed up PRISMA style literature reviews

**`lit_extract.py`**

This tool is a helper for systematic reviews that follows the **PRISMA 2020 flow diagram for new systematic reviews** (https://www.prisma-statement.org/prisma-2020-flow-diagram)). It searches PubMed, Europe PMC and ClinicalTrials.gov with your own specified search terms, screens titles and abstracts against your own study inclusion criteria with Claude API, gives you a list of records to include for your full-text decisions, and gives you an output of the number of records found and kept at each stage of the PRISMA process. 

**`evaluate.py`** scores the model's screening against your own blind screening of a sample.

Everything is checkable: the model only sees text we retrieved from the published databases, every screening decision comes with a reason, and an evidence quote which is machine-checked against the abstract, every exclusion by the model is listed, and the final decisions are human.

## How to Setup

```bash
pip install -r requirements.txt
```

Note: You can either paste the key and your email every time, or i'd recommend adding these two lines to the end of your `~/.bashrc`, so they are set in every new terminal:

```bash
export ANTHROPIC_API_KEY=...   # the API that you get from your Claude Platform console
export NCBI_EMAIL=...          # your email for PubMed API
```

Then run `source ~/.bashrc`, or open a new terminal. Keep the real key in `~/.bashrc` only: never in this README, in scripts or in git.

## PRISMA style 2020 literature review (`lit_extract.py`)

### Three steps, one command

You run the same command three times, adding one option each time. 
Each step saves its work, so nothing is searched or screened twice.

| PRISMA stage | Command | What you get |
|---|---|---|
| **1. Identification** | `--searchterms FILE` (+ `--other-records CSV`, optional) | Records from each database and register, duplicates removed. Nothing is sent to the model. |
| **2. Screening** | add `--screening FILE` (your criteria) | Claude screens every title/abstract against your criteria. You get `results_<name>_for_review.csv`: **the records you need to look at**, with empty columns for your decisions. |
| **3. Eligibility and included** | add `--eligibility` (after filling in the sheet) | The remaining PRISMA numbers, `results_<name>_included.csv`: **the papers included in your review**, and `results_<name>_prisma.docx`: **the PRISMA 2020 flow diagram, filled in**. |

Here is a worked example: a review of published data on the efficacy of **malaria vaccines**, from 2000 onwards. The example files are in `queries/` and `criteria/`.

# 1. Identification: how many records are there?

Write your searches in a search terms file, one search per line (see [The search terms file](#the-search-terms-file) below). These are the same searches you would type into PubMed and the other sources. Then run:

```bash
python lit_extract.py --searchterms queries/vaccine_efficacy_searchterms.txt --date-from 2000
```

This step makes no API calls. It searches every source, removes duplicates, and tells you how many records were found.

Additional options:
`--other-records my_records.csv` to add records you found another way, e.g. WHO reports or reference lists (see [Sources you can search](#sources-you-can-search)).

# 2. Screening titles and abstracts only (based on your criteria file)

Write your inclusion criteria in a criteria file, one per line (see [Screening criteria file](#screening-criteria-file) below). Claude checks every title and abstract against them. Run the same command with `--screening` added:

```bash
python lit_extract.py --searchterms queries/vaccine_efficacy_searchterms.txt --date-from 2000 \
    --screening criteria/vaccine_efficacy_screening.txt
```

This step makes one API call per record. It gives you:

- `results_<name>_screening.csv`: the model's decision and reason for every record
- `results_<name>_for_review.csv`: your review sheet, listing the records the model did not exclude

Additional options:
`--max-papers N` to keep only the first N records from each search (for PubMed, the N most relevant).Only use it for a quick trial run as a PRISMA style review needs every record.

# 3. Eligibility and included studies (full-text check)

First, open `results_<name>_for_review.csv` and fill in your decisions (see [Your review sheet](#your-review-sheet) below). Then run the same command again with `--eligibility` added:

```bash
python lit_extract.py --searchterms queries/vaccine_efficacy_searchterms.txt --date-from 2000 \
    --screening criteria/vaccine_efficacy_screening.txt --eligibility
```

This step makes no API calls. It checks your sheet, and then gives you:

- `results_<name>_included.csv`: the papers included in your review
- `results_<name>_prisma.docx`: the PRISMA 2020 flow diagram with every number filled in

If your sheet has missing or inconsistent entries, it lists every problem and stops. Fix them and run it again.

Additional options:
`--eligibility my_sheet.csv` to use a review sheet saved under another name.

# 4. Validate the screening step (Step 2, Titles & Abstracts)

The model can wrongly exclude relevant papers in the screening step, so you need to validate this to check how often it does that. The number that matters is **sensitivity**: the share of relevant records that the model kept.

1. Pick a random sample of records (e.g. 100) from `results_<name>_screening.csv`.
2. Screen them yourself **without looking at the model's decisions**. Save your answers as `gold_screening.csv` with two columns, `record_id,decision`, where decision is `include` or `exclude` (copy `gold_screening_template.csv`).
3. Run:

```bash
python evaluate.py --pred results_vaccine_efficacy_searchterms_screening.csv --gold gold_screening.csv
```

The model's `unclear` and `no_abstract` decisions count as include. The output lists the relevant records that the model excluded, so read those first. If sensitivity is low, loosen your criteria or use a stronger model (`--model`).

During unit-testing offline, the searches were tested against the live PubMed, Europe PMC and ClinicalTrials.gov APIs, and screening identified an initial sensitivity of 0.981 and specificity of 0.936.

## Reference

### Your review sheet

After step 2, `results_<name>_for_review.csv` lists every record the model did **not** exclude, plus any records you added with `--other-records`. Open it in Excel (or similar), fill in these columns, and save it as CSV:

| Column | What to enter |
|---|---|
| `human_screening` | Leave empty, or `exclude` if you reject it on the title/abstract |
| `retrieved` | `yes` or `no`: could you get the full text? |
| `eligible` | `yes` or `no`, after reading the full text |
| `exclusion_reason` | Needed if `eligible` is `no`. Identical wording is counted as one reason, so start with your criterion code, e.g. `C4: no efficacy outcome` |
| `study_id` | Optional. Give the same id to reports of the same study (e.g. a trial registration, its preprint and its paper) |

Running step 2 again never overwrites a sheet that already exists, so your decisions are safe.

### Sources you can search with this tool

| Source | PRISMA box | Name in the search terms file | What it adds |
|---|---|---|---|
| PubMed | Databases | `pubmed` | MEDLINE and other biomedical journals |
| Europe PMC | Databases | `europepmc` | Preprints (medRxiv, bioRxiv, Research Square, ...), PubMed Central, Agricola and more |
| ClinicalTrials.gov | Registers | `clinicaltrials` | Registered trials, including unpublished and ongoing ones, which helps limit publication bias |
| Anything else | Other methods | `--other-records CSV` | Websites, organisations (e.g. WHO reports), and reference lists of included papers and reviews |

**Sources that are not built in:** Embase, Scopus, Web of Science and Cochrane CENTRAL need a subscription or API key. WHO ICTRP (which includes the Pan African Clinical Trials Registry) has no open search API. 

Note: A PRISMA compliant literature review would normally include Embase and CENTRAL, so you should also search them on their own websites and either add the records with `--other-records` or report them yourself.

For `--other-records`, copy `other_records_template.csv` and fill in one row per record. The columns are `source,title,abstract,doi,pmid,nct_id,year,url`.

### Files you need to make:
### Creating the search terms file

See example: `queries/vaccine_efficacy_searchterms.txt`

- One search per line: `source<TAB>name<TAB>query`, with a real tab between the columns.
- A line with only `name<TAB>query` is treated as a PubMed search.
- Every search runs, and the results are merged and de-duplicated across sources.

Each source has its own query syntax. All of them need **uppercase** `AND`, `OR`, `NOT`, `"quotes"` for phrases and `( )` for grouping:

- **PubMed:** field tags such as `[tiab]` (title/abstract), `[mh]` (MeSH) and `[pt]` (publication type).
- **Europe PMC:** `TITLE_ABS:(...)` searches the title and abstract only. Without it, the full text is searched too, which gives many more hits. `SRC:PPR` limits a search to preprints.
- **ClinicalTrials.gov:** plain terms, or `AREA[ConditionSearch]`, `AREA[InterventionSearch]` and `AREA[StudyType]` to search one field.

### Screening criteria file

See example: `criteria/vaccine_efficacy_screening.txt`

A plain text file with one criterion per line
- A record must meet **all** criteria to be included.
- For each record, the model answers `include`, `exclude` (naming the first criterion it fails) or `unclear`.
- `unclear` records, and records with no abstract or summary, are never excluded automatically. They go to your review sheet.

### Output files

`<name>` is the search terms file name, or whatever you set with `--out`.

| File | Step | What it contains |
|---|---|---|
| `results_<name>_papers.json` | 1 | All the retrieved records |
| `results_<name>_search_log.json` | 1 | Sources, searches, dates, hit counts, criteria file and model. Use it to report and repeat your search |
| `results_<name>_prisma.txt` / `.json` | all | The PRISMA numbers so far (`?` = not known yet) |
| `results_<name>_screening.csv` | 2 | **Every** record's decision, the failed criterion, a one-sentence reason, a checked evidence quote and the abstract. Read the excluded ones: this is where relevant papers get lost |
| `results_<name>_exclusion_reasons.csv` | 2 | One row per record, with a column for each criterion, showing which criteria it failed and why |
| `results_<name>_for_review.csv` | 2 | Your review sheet |
| `results_<name>_included.csv` | 3 | The included reports, with their `study_id` |
| `results_<name>_prisma.docx` | 3 | The filled-in PRISMA 2020 flow diagram |

### How the numbers fill the PRISMA diagram

| Diagram box | Where the number comes from |
|---|---|
| Records identified from databases / registers | Records found in each source, listed per source |
| Records identified from other methods | Your `--other-records` file, counted by its `source` column. Records already found in a database or register are not counted again |
| Duplicate records removed | Same PMID, DOI or NCT number, or same title and year. A preprint and its journal article are **not** duplicates: they are two reports of one study, so link them with `study_id` |
| Records marked ineligible by automation tools / removed for other reasons | Always 0: nothing is removed before screening |
| Records screened | Every unique record from databases and registers |
| Records excluded | Excluded by the model plus excluded by you, shown separately |
| Reports sought / not retrieved / assessed / excluded with reasons | Your review sheet. Other-methods records go straight to "reports sought" |
| Studies included / reports of included studies | Your review sheet: rows with `eligible` = `yes`, and how many different `study_id`s they have |

### Other options and large searches

| Option | What it does |
|---|---|
| `--date-from`, `--date-to` | Limit by date: `YYYY`, `YYYY-MM` or `YYYY-MM-DD`. PubMed and Europe PMC use the publication date. ClinicalTrials.gov always uses the date the trial was first submitted |
| `--date-type edat` | For PubMed and Europe PMC, use the date the record was added instead of the publication date |
| `--max-papers N` | Keep only the top N records per search. Not recommended: the PRISMA output will warn that the review is incomplete |
| `--warn-records N` | Ask y/n before screening if N or more records are found (default 1000; `0` turns it off) |
| `--yes` | Answer yes to every question, for batch or cluster jobs where nobody can answer |
| `--model` | Choose the Claude model used for screening |
| `--fresh` | Throw away saved screening progress and start again |
| `--out NAME` | Change the start of the output file names |

Good to know:

- **Every match is retrieved by default**, as a systematic review needs. PubMed returns at most 9,999 records per query, so larger searches are split by date automatically.
- **Screening costs one API call per record.** You are asked to confirm before 200 or more calls.
- **You can stop and resume.** Screening progress is saved as it goes (`results_<name>_screening.ckpt.jsonl`), so if a run stops, run the same command again to carry on. Changing the criteria or `--model` screens everything again.

## Known limitations

- **The tool follows the PRISMA 2020 flow, but the tool alone doesn't make a review PRISMA-compliant.** PRISMA also asks for a registered protocol, several databases (usually including Embase and CENTRAL, which aren't built in), and screening by two independent reviewers. Here a single model does the first title/abstract screen. Say so in your methods, report the model's exclusions as automation-tool exclusions (the output already does this), and check its sensitivity with `evaluate.py`.
- The model only screens titles and abstracts. Full-text eligibility is your decision, recorded in the review sheet.
- Keyword searches miss some relevant records and find some irrelevant ones.
- Trial registry records are screened on their summary, conditions and interventions. These say less than an abstract, so expect more `unclear` decisions.
- A verified evidence quote proves the sentence exists in the abstract. It doesn't prove the model interpreted it correctly.
- The scripts were unit-tested offline with a fake model. The searches were tested against the live PubMed, Europe PMC and ClinicalTrials.gov APIs, and screening identified an initial sensitivity of 0.981 and specificity of 0.936.
