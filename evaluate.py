"""Score the model's abstract screening against your own blind screening of a sample.

Sensitivity and specificity of the include/exclude decisions. The gold CSV needs 'record_id'
(or 'pmid') and 'decision' (include or exclude), from your own blind screening of a random
sample of the records in results_<name>_screening.csv. Only records present in both files are
compared. The model's 'unclear' and 'no_abstract' count as include (they are kept for you). Sensitivity matters most: every
relevant paper the model wrongly excludes is lost from the review.

    python evaluate.py --pred results_vaccine_efficacy_searchterms_screening.csv --gold gold_screening.csv
"""
import argparse
import csv
import re

KEEP = {"include", "unclear", "no_abstract"}


def _norm(value):
    return re.sub(r"[\s\-_]+", "", (value or "").lower())


def _record_key(row):
    """record_id as written by lit_extract.py; a bare PMID is accepted too."""
    key = _norm(row.get("record_id") or row.get("pmid"))
    return key if not key.isdigit() else "pmid:" + key


def load_decisions(path, kind):
    """Return {record_id: 'include' or 'exclude'}. For model output, kept records count as include."""
    out = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            decision = (row.get("decision") or "").strip().lower()
            if kind == "pred":
                decision = "include" if decision in KEEP else "exclude"
            elif decision not in ("include", "exclude"):
                raise ValueError(f"Gold decision for {row.get('record_id') or row.get('pmid')} "
                                 "must be include or exclude")
            out[_record_key(row)] = decision
    return out


def score_screening(pred, gold):
    shared = sorted(set(pred) & set(gold))
    tp = sum(pred[p] == "include" and gold[p] == "include" for p in shared)
    fn = sum(pred[p] == "exclude" and gold[p] == "include" for p in shared)
    tn = sum(pred[p] == "exclude" and gold[p] == "exclude" for p in shared)
    fp = sum(pred[p] == "include" and gold[p] == "exclude" for p in shared)
    sens = tp / (tp + fn) if tp + fn else 0.0
    spec = tn / (tn + fp) if tn + fp else 0.0
    return {
        "compared": len(shared), "tp": tp, "fn": fn, "tn": tn, "fp": fp,
        "sensitivity": round(sens, 3), "specificity": round(spec, 3),
        "missed_relevant": [p for p in shared if pred[p] == "exclude" and gold[p] == "include"],
        "extra_to_check": [p for p in shared if pred[p] == "include" and gold[p] == "exclude"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pred", required=True, help="the model's results_<name>_screening.csv")
    parser.add_argument("--gold", required=True, help="your own decisions: record_id,decision")
    args = parser.parse_args()

    result = score_screening(load_decisions(args.pred, "pred"), load_decisions(args.gold, "gold"))
    print({k: v for k, v in result.items() if k not in ("missed_relevant", "extra_to_check")})
    print("\nRelevant papers the model EXCLUDED (read these first):")
    for rid in result["missed_relevant"]:
        print("  ", rid)
    print("\nPapers the model kept that you excluded:")
    for rid in result["extra_to_check"]:
        print("  ", rid)


if __name__ == "__main__":
    main()
