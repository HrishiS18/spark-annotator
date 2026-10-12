"""Add dilemmas from a dataset file to config/studies.json.

    .venv/bin/python tools/import_dilemmas.py data.csv --text-col text --title-col title --limit 20
    .venv/bin/python tools/import_dilemmas.py data.jsonl --text-col selftext --title-col title --id-col id

Works with CSV or JSONL exports of Scruples, the Moral Dilemma Dataset, AdvisorQA or the
r/Advice corpus: point --text-col at the field holding the dilemma. Items are appended to
the `dilemma` study (or --study) and de-duplicated by id. Restart the server afterwards.
"""

import argparse
import csv
import hashlib
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "config", "studies.json")


def rows(path):
    if path.endswith((".jsonl", ".json")):
        with open(path, encoding="utf-8") as f:
            if path.endswith(".json"):
                data = json.load(f)
                yield from (data if isinstance(data, list) else data.get("items", []))
            else:
                for line in f:
                    if line.strip():
                        yield json.loads(line)
    else:
        with open(path, newline="", encoding="utf-8") as f:
            yield from csv.DictReader(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--text-col", required=True)
    ap.add_argument("--title-col")
    ap.add_argument("--id-col")
    ap.add_argument("--study", default="dilemma")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--min-words", type=int, default=25)
    ap.add_argument("--max-words", type=int, default=400)
    a = ap.parse_args()

    cfg = json.load(open(CFG))
    study = next(s for s in cfg["studies"] if s["key"] == a.study)
    have = {p["id"] for p in study["prompts"]}
    added = 0
    for r in rows(a.path):
        text = str(r.get(a.text_col) or "").strip()
        n = len(text.split())
        if not (a.min_words <= n <= a.max_words):
            continue
        pid = str(r.get(a.id_col)) if a.id_col and r.get(a.id_col) else hashlib.sha1(text.encode()).hexdigest()[:10]
        pid = f"x-{pid}"
        if pid in have:
            continue
        title = str(r.get(a.title_col) or "").strip() if a.title_col else ""
        study["prompts"].append({"id": pid, "title": (title or text[:48] + "…")[:80], "text": text,
                                 "source": os.path.basename(a.path)})
        have.add(pid)
        added += 1
        if added >= a.limit:
            break
    with open(CFG, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    print(f"Added {added} prompts to study '{a.study}' ({len(study['prompts'])} total).")


if __name__ == "__main__":
    main()
