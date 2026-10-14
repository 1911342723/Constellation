"""Generate the manuscript's results_macros.tex from archived experiment outputs.

Every quantitative statement in the manuscript is a macro defined here, so
the paper cannot drift from the per-document records it summarises.

Inputs (all archived in experiment_results/):
  hrdoc_2026-10-13/report/summary.json      Comp-HRDoc summary + bootstrap
  hrdoc_2026-10-13/runs/{test,gen}-*/        per-document records
  hrdoc_2026-10-13/admission.json, split.json, code_commit.txt, llm_config.json
  hrdoc_2026-10-13/fp_audit.json             false-positive audit (optional)
  paper_2026-10-14/{det,model}_frozen/       controlled tiers (acceptance), frozen code
  paper_2026-10-14/offline_paper_metrics_raw.json
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HR = ROOT / "experiment_results/hrdoc_2026-10-13"
PAPER = ROOT / "experiment_results/paper_2026-10-14"  # re-run on the frozen code
OUT = ROOT / "build/results_macros.tex"

NAMES = {"outline": "Outline", "docling": "Docling", "det": "Det", "model": "Full",
         "det-nooutline": "DetNo", "model-nooutline": "FullNo",
         "model-nooutline-nosilence": "FullNoSil", "llm_markdown": "Gen",
         "docling-numlevels": "DoclingNum", "det-nooutline-layout": "DetLay",
         "model-nooutline-layout": "FullLay"}
PAIR_NAMES = {"model-nooutline - det-nooutline": "FullNoDetNo",
              "model-nooutline - model-nooutline-nosilence": "FullNoFullNoSil",
              "model-nooutline - docling": "FullNoDocling", "model-nooutline - outline": "FullNoOutline",
              "model - det": "FullDet", "model - model-nooutline": "FullFullNo",
              "det - det-nooutline": "DetDetNo", "model-nooutline - docling-numlevels": "FullNoDoclingNum",
              "model-nooutline-layout - model-nooutline": "FullLayFullNo",
              "model-nooutline-layout - det-nooutline-layout": "FullLayDetLay",
              "model-nooutline-layout - docling-numlevels": "FullLayDoclingNum",
              "det-nooutline-layout - det-nooutline": "DetLayDetNo",
              "model-nooutline - llm_markdown": "FidGen"}


def f3(x: float) -> str:
    return f"{x:.3f}"


def signed(x: float) -> str:
    return f"{x:+.3f}".replace("+", "$+$").replace("-", "$-$")


def records(run: Path) -> dict[str, dict]:
    out = {}
    for path in run.glob("*.json"):
        r = json.loads(path.read_text(encoding="utf-8"))
        if "metrics" in r:
            out[r["doc"]] = r
    return out


def main() -> None:
    macros: dict[str, str] = {}
    admission = json.loads((HR / "admission.json").read_text())
    split = json.loads((HR / "split.json").read_text())
    macros["AdmittedDocs"] = str(sum(r["status"] == "admitted" for r in admission))
    macros["MismatchDocs"] = str(sum(r["status"] == "version_mismatch" for r in admission))
    macros["DevDocs"] = str(len(split["dev"]))
    sections = {r["doc"]: r["sections"] for r in admission}
    macros["CodeCommit"] = (HR / "code_commit.txt").read_text().strip()[:7]
    llm = json.loads((HR / "llm_config.json").read_text())  # archived run-time configuration
    recorded = {
        json.loads(path.read_text(encoding="utf-8")).get("info", {}).get("model")
        for path in (HR / "runs/test-model-nooutline").glob("*.json")
    } - {None}
    if recorded != {llm["reader_model"]}:
        raise SystemExit(f"llm_config.json model {llm['reader_model']!r} != records {recorded}")
    macros["LLMTemperature"] = f"{llm['reader_temperature']:g}"
    macros["LLMModel"] = llm["reader_model"]

    report = json.loads((HR / "report/summary.json").read_text())
    summary = report["summary"]
    test_docs = summary["model-nooutline"]["documents"]
    macros["TestDocs"] = str(test_docs)
    macros["TestSections"] = f"{sum(sections[d] for d in split['test']):,}".replace(",", "{,}")
    for key, name in NAMES.items():
        s = summary.get(key)
        if not s:
            continue
        macros[f"{name}P"] = f3(s["micro_precision"])
        macros[f"{name}R"] = f3(s["micro_recall"])
        macros[f"{name}F"] = f3(s["micro_f1"])
        macros[f"{name}Lev"] = f3(s["level_accuracy"])
        macros[f"{name}Par"] = f3(s["parent_accuracy"])
        macros[f"{name}TEDS"] = f3(s["macro_teds"])
        macros[f"{name}Docs"] = str(s["documents"])
        macros[f"{name}Failed"] = str(s.get("failed", 0))
        g = report.get("summary_generic", {}).get(key)
        if g:
            macros[f"{name}GF"] = f3(g["micro_f1"])
            macros[f"{name}GT"] = f3(g["macro_teds"])
    for pair, name in PAIR_NAMES.items():
        c = report["comparisons"].get(pair)
        if not c:
            continue
        for metric, short in (("f1", "F"), ("teds", "T")):
            macros[f"D{name}{short}"] = signed(c[metric]["delta"])
            macros[f"D{name}{short}lo"] = signed(c[metric]["lo"])
            macros[f"D{name}{short}hi"] = signed(c[metric]["hi"])

    macros["FailedTotal"] = str(sum(s.get("failed", 0) for s in summary.values()))
    retries = json.loads((HR / "retries.json").read_text())
    macros["RerunFull"] = str(len(retries["rerun"].get("test-model-nooutline", [])))
    macros["RerunTotal"] = str(sum(len(v) for v in retries["rerun"].values()))
    # Sensitivity: largest F1 change from removing front/back matter, bookmarks excluded.
    gen_summary = report.get("summary_generic", {})
    deltas = [abs(summary[k]["micro_f1"] - gen_summary[k]["micro_f1"]) for k in NAMES
              if k in summary and k in gen_summary and k != "outline"]
    macros["GenericMaxDelta"] = f3(max(deltas))
    # Development-split numbers quoted for the layout variant.
    from evaluation.hrdoc.evaluate import summarize
    for key, name in (("model-nooutline", "DevFullNoF"), ("model-nooutline-layout", "DevFullLayF")):
        dev = list(records(HR / f"runs/dev-{key}").values())
        macros[name] = f3(summarize(dev)["micro_f1"])
    rep = report.get("repeat_runs") or {}
    if rep:
        macros["RepRuns"] = str(len(rep["runs"]))
        macros["RepDocs"] = str(rep["documents"])
        for key, short in (("f1", "F"), ("teds", "T")):
            macros[f"Rep{short}min"] = f3(rep[f"{key}_min"])
            macros[f"Rep{short}max"] = f3(rep[f"{key}_max"])

    docling = records(HR / "runs/test-docling")
    levels = [p["level"] for r in docling.values() for p in r["predictions"]]
    macros["DoclingLevelOnePct"] = f"{100 * sum(lv == 1 for lv in levels) / len(levels):.0f}\\%"

    full_no = records(HR / "runs/test-model-nooutline")
    # Assembly check of Proposition 1: every provider text block appears, in order, in the export.
    pres = [r["info"]["content_preservation"] for r in full_no.values() if "error" not in r]
    macros["FullNoPresMin"] = f3(min(pres))
    macros["FullNoPresDocs"] = str(len(pres))
    outline = records(HR / "runs/test-outline")
    macros["OutlineEmpty"] = str(sum(not r["predictions"] for r in outline.values()))
    macros["PerfCommit"] = (HR / "perf_commit.txt").read_text().strip()[:7]
    layout_commit = HR / "layout_commit.txt"
    macros["LayoutCommit"] = layout_commit.read_text().strip()[:7] if layout_commit.exists() else "pending"

    # Generative sample. Heading accuracy, tokens and time of the full system come from
    # its main test run (test-model-nooutline) on the same documents; the spread over
    # the four runs is reported separately. Text preservation needs the exported
    # Markdown, which only the repeat run gen-model-nooutline-rep4 stored; the export
    # contains every block whatever the votes, so the deterministic export of the same
    # documents (gen-det-nooutline-fidelity) is measured as a check.
    gen = records(HR / "runs/gen-llm_markdown")
    fid = records(HR / "runs/gen-model-nooutline-rep4")
    main_run = records(HR / "runs/test-model-nooutline")
    det_fid = records(HR / "runs/gen-det-nooutline-fidelity")
    docs = sorted(d for d in gen if d in fid and d in main_run
                  and "error" not in gen[d] and "error" not in fid[d] and "error" not in main_run[d])
    macros["GenFailed"] = str(sum("error" in gen[d] for d in gen))
    macros["GenDocs"] = str(len(docs))

    def micro_f1(rows: list[dict]) -> float:
        tp = sum(m["tp"] for m in rows)
        p_, r_ = tp / max(sum(m["pred"] for m in rows), 1), tp / max(sum(m["gt"] for m in rows), 1)
        return 2 * p_ * r_ / max(p_ + r_, 1e-12)

    def mean(values: list[float]) -> float:
        return sum(values) / len(values)

    macros["GsGenF"] = f3(micro_f1([gen[d]["metrics"] for d in docs]))
    macros["GsGenTEDS"] = f3(mean([gen[d]["metrics"]["teds"] for d in docs]))
    macros["GsFullNoF"] = f3(micro_f1([main_run[d]["metrics"] for d in docs]))
    macros["GsFullNoTEDS"] = f3(mean([main_run[d]["metrics"]["teds"] for d in docs]))
    # Research question: how much of the generative LLM's gain over the deterministic
    # core does anchor routing keep, on the same documents?
    det_no = records(HR / "runs/test-det-nooutline")
    gs = {"gen": (micro_f1([gen[d]["metrics"] for d in docs]), mean([gen[d]["metrics"]["teds"] for d in docs])),
          "full": (micro_f1([main_run[d]["metrics"] for d in docs]), mean([main_run[d]["metrics"]["teds"] for d in docs])),
          "det": (micro_f1([det_no[d]["metrics"] for d in docs]), mean([det_no[d]["metrics"]["teds"] for d in docs]))}
    macros["GsDetNoF"], macros["GsDetNoTEDS"] = f3(gs["det"][0]), f3(gs["det"][1])
    for i, short in ((0, "F"), (1, "T")):
        share = (gs["full"][i] - gs["det"][i]) / (gs["gen"][i] - gs["det"][i])
        macros[f"GainShare{short}"] = f"{100 * share:.0f}\\%"
    # Paired bootstrap of the gain share (documents resampled, same seed as the report).
    import random
    rng, shares = random.Random(7), {0: [], 1: []}
    for _ in range(5000):
        sample = [docs[rng.randrange(len(docs))] for _ in docs]
        point = {k: (micro_f1([src[d]["metrics"] for d in sample]), mean([src[d]["metrics"]["teds"] for d in sample]))
                 for k, src in (("gen", gen), ("full", main_run), ("det", det_no))}
        for i in (0, 1):
            gap = point["gen"][i] - point["det"][i]
            if gap > 0:
                shares[i].append((point["full"][i] - point["det"][i]) / gap)
    for i, short in ((0, "F"), (1, "T")):
        values = sorted(shares[i])
        macros[f"GainShare{short}Lo"] = f"{100 * values[int(0.025 * len(values))]:.0f}\\%"
        macros[f"GainShare{short}Hi"] = f"{100 * values[int(0.975 * len(values)) - 1]:.0f}\\%"
    def wp(record: dict, key: str = "recall") -> float:
        return record["info"]["word_preservation"][key]

    g_pres = [wp(gen[d]) for d in docs]
    c_pres = [wp(fid[d]) for d in docs]
    d_pres = [wp(det_fid[d]) for d in docs if d in det_fid]
    macros["GenInOrder"] = f3(mean([wp(gen[d], "in_order") for d in docs]))
    macros["GsFullNoInOrder"] = f3(mean([wp(fid[d], "in_order") for d in docs]))
    macros["GsFullNoInOrderMin"] = f3(min(wp(fid[d], "in_order") for d in docs))
    macros["GenPres"] = f3(mean(g_pres))
    macros["GenPresPct"] = f"{100 * mean(g_pres):.1f}\\%"
    macros["GenLossPct"] = f"{100 * (1 - mean(g_pres)):.1f}\\%"
    macros["GenPresMin"] = f3(min(g_pres))
    macros["GsFullNoPres"] = f3(mean(c_pres))
    macros["GsFullNoPresPct"] = f"{100 * mean(c_pres):.1f}\\%"
    macros["GsFullNoPresMin"] = f3(min(c_pres))
    macros["GenPresMinPct"] = f"{100 * min(g_pres):.1f}\\%"
    macros["GsFullNoPresMinPct"] = f"{100 * min(c_pres):.1f}\\%"
    macros["GsDetNoPres"] = f3(mean(d_pres)) if d_pres else "--"
    macros["GenBelowFull"] = str(sum(wp(gen[d]) < wp(fid[d]) for d in docs))
    truncated = sum(gen[d]["info"]["finish_reason"] == "length" for d in docs)
    macros["GenTrunc"] = str(truncated)
    macros["GenTruncText"] = ("none of its outputs reached the output limit" if truncated == 0 else
                              f"{truncated} of its outputs were truncated at the output limit")
    macros["GenInsertPct"] = f"{100 * mean([wp(gen[d], 'inserted') for d in docs]):.1f}\\%"
    macros["GsFullNoInsertPct"] = f"{100 * mean([wp(fid[d], 'inserted') for d in docs]):.1f}\\%"
    g_in = mean([gen[d]["info"]["usage"]["prompt_tokens"] for d in docs])
    g_out = mean([gen[d]["info"]["usage"]["completion_tokens"] for d in docs])
    c_in = mean([main_run[d]["info"]["usage"]["prompt_tokens"] for d in docs])
    c_out = mean([main_run[d]["info"]["usage"]["completion_tokens"] for d in docs])
    macros["GenPTok"] = f"{g_in:,.0f}".replace(",", "{,}")
    macros["GenCTok"] = f"{g_out:,.0f}".replace(",", "{,}")
    macros["GsFullNoPTok"] = f"{c_in:,.0f}".replace(",", "{,}")
    macros["GsFullNoCTok"] = f"{c_out:,.0f}".replace(",", "{,}")
    macros["GenOutRatio"] = f"{g_out / c_out:.0f}"
    g_sec = mean([gen[d]["seconds"] for d in docs])
    c_sec = mean([main_run[d]["seconds"] for d in docs])
    macros["GenSec"] = f"{g_sec:.0f}"
    macros["GsFullNoSec"] = f"{c_sec:.0f}"
    macros["GenTimeRatio"] = f"{g_sec / c_sec:.1f}"
    # Wall time of the full system over its four runs on these documents.
    rep_secs = [c_sec] + [mean([records(HR / f"runs/gen-model-nooutline-rep{k}")[d]["seconds"] for d in docs])
                          for k in (2, 3, 4)]
    macros["GsFullNoSecMin"], macros["GsFullNoSecMax"] = f"{min(rep_secs):.0f}", f"{max(rep_secs):.0f}"
    macros["GenTimeRatioMin"] = f"{g_sec / max(rep_secs):.1f}"
    macros["GenTimeRatioMax"] = f"{g_sec / min(rep_secs):.1f}"
    raw = [gen[d]["metrics_raw"] for d in docs if "metrics_raw" in gen[d]]
    macros["GenLevRaw"] = f3(sum(m["level_correct"] for m in raw) / max(sum(m["tp"] for m in raw), 1))
    macros["GenMarkerLoss"] = str(sum(1 for d in docs if len(gen[d]["info"]["raw_headings"]) > 3
                                      and {h[1] for h in gen[d]["info"]["raw_headings"]} <= {0}))

    # Legacy tiers (acceptance runs on the frozen code).
    def tier(path: Path, name: str) -> dict:
        data = json.loads((path / "results.json").read_text(encoding="utf-8"))
        rows = [r for r in data["documents"] if r["tier"] == name]
        tp = sum(r["exact_title_snapshot_metrics"]["tp"] for r in rows)
        fp = sum(r["exact_title_snapshot_metrics"]["fp"] for r in rows)
        fn = sum(r["exact_title_snapshot_metrics"]["fn"] for r in rows)
        p, rc = tp / max(tp + fp, 1), tp / max(tp + fn, 1)
        return {"p": p, "r": rc, "f": 2 * p * rc / max(p + rc, 1e-12), "rows": rows}

    for label, run in (("Det", PAPER / "det_frozen"), ("Full", PAPER / "model_frozen")):
        mis, nos, long = tier(run, "mis_styled"), tier(run, "no_style"), tier(run, "long_docs")
        macros[f"Mis{label}F"] = f3(mis["f"])
        macros[f"NoStyle{label}F"] = f3(nos["f"])
        macros[f"NoStyle{label}R"] = f3(nos["r"])
        macros[f"Long{label}F"] = f3(long["f"])
        if label == "Full":
            calls = [r["model_calls"] for r in long["rows"]]
            macros["LongCallsPerDoc"] = f"{sum(calls) / len(calls):.1f}"
    offline = json.loads((PAPER / "offline_paper_metrics_raw.json").read_text(encoding="utf-8"))
    saves = [row["token_savings_pct"] for row in offline["long_docs"] if "token_savings_pct" in row]
    macros["SparseSave"] = f"{sum(saves) / len(saves):.0f}\\%"

    audit_path = HR / "fp_audit.json"
    if audit_path.exists():
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        # The sample was drawn before the title convention; items that the convention
        # now ignores are no longer false positives. Dropping them leaves a uniform
        # sample of the current false positives (the current pool is a subset).
        unmatched = {d: set(r["metrics"]["unmatched_pred"]) for d, r in full_no.items()}
        items = [i for i in audit["items"] if i["title"][:80] in unmatched.get(i["doc"], set())]
        macros["AuditDropped"] = str(len(audit["items"]) - len(items))
        n = len(items)

        def pct(count: int) -> str:
            return f"{100 * count / n:.0f}\\%"

        macros["AuditN"] = str(n)
        macros["AuditGenuinePct"] = pct(sum(i["verdict"] == "genuine_heading" for i in items))
        macros["AuditSplitPct"] = pct(sum(i["verdict"] == "split_or_version_text" for i in items))
        macros["AuditErrorPct"] = pct(sum(i["verdict"] == "error" for i in items))
        kinds = {"formula_or_algorithm_line": "AuditFormula", "figure_or_table_text": "AuditFigure",
                 "run_in_label_or_list_item": "AuditRunIn", "title_page_or_running_header": "AuditTitlePage",
                 "reference_entry": "AuditRefEntry", "body_text_line": "AuditBodyLine"}
        for kind, name in kinds.items():
            macros[name] = str(sum(i.get("error_kind") == kind for i in items))
        macros["AuditPool"] = str(sum(r["metrics"]["fp"] for r in full_no.values()))
        review_path = HR / "fp_audit_author_review.json"  # evaluation/hrdoc/fp_audit_review.py
        if review_path.exists():
            review = json.loads(review_path.read_text(encoding="utf-8"))
            macros["AuditChecked"] = str(review["reviewed"])
            macros["AuditAgreed"] = str(review["agreement"])

    # Sensitivity to the title-matching rule (evaluation/hrdoc/strict_matching.py).
    strict_path = HR / "report/strict_matching.json"
    if strict_path.exists():
        strict = json.loads(strict_path.read_text())
        drops_f = [v["main"]["f1"] - v["strict"]["f1"] for v in strict.values()]
        drops_t = [v["main"]["teds"] - v["strict"]["teds"] for v in strict.values()]
        macros["StrictDropFMin"], macros["StrictDropFMax"] = f3(min(drops_f)), f3(max(drops_f))
        macros["StrictDropTMin"], macros["StrictDropTMax"] = f3(min(drops_t)), f3(max(drops_t))
        for name, label in (("docling", "Docling"), ("model-nooutline-layout", "FullLay"),
                            ("model-nooutline", "FullNo"), ("det-nooutline", "DetNo"),
                            ("docling-numlevels", "DoclingNum")):
            macros[f"Strict{label}F"] = f3(strict[name]["strict"]["f1"])
            macros[f"Strict{label}TEDS"] = f3(strict[name]["strict"]["teds"])

    lines = ["% Generated by evaluation/paper_macros.py -- do not edit by hand."]
    for key in sorted(macros):
        lines.append(f"\\newcommand{{\\{key}}}{{{macros[key]}}}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(macros)} macros -> {OUT}")


if __name__ == "__main__":
    main()
