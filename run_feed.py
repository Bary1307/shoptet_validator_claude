#!/usr/bin/env python3
"""Automatický běh: stáhne feed dodavatele, převede ho podle uložené úlohy a uloží hotový Shoptet XML.

Použití:
    python run_feed.py jobs/bosch.json [jobs/metabo.json ...]
    python run_feed.py --all jobs            # všechny jobs/*.json
    python run_feed.py jobs/bosch.json --force   # vypnout pojistku proti poklesu počtu produktů

Úlohu (job .json) vytvoří webová aplikace v kroku 8 (režim "Aktualizovaný feed z URL").
Přihlašovací údaje nejsou v úloze, jen názvy secretů - hodnoty se berou z proměnných prostředí
nebo z JSON v ALL_SECRETS (GitHub Actions: ${{ toJSON(secrets) }}).

Pojistky: výstup se NEPŘEPÍŠE, když stažení/validace selže, výsledek je prázdný, příliš mnoho produktů
má povinnou chybu, nebo počet produktů prudce klesl proti minulému běhu.
"""
import argparse
import glob
import json
import os
import sys

from feed_core import (JOB_VERSION, build_shop, drop_invalid_items, fetch_source, get_secret,
                       load_price_map, parse_xml_bytes, secret_names, selection_to_args, shop_to_bytes)

DEFAULT_CHECKS = {"max_drop_ratio": 0.30, "max_invalid_ratio": 0.20}


class JobFailed(Exception):
    pass


def count_items(data):
    return data.count(b"<SHOPITEM")


def run_job(path, force=False):
    """Provede jednu úlohu. Vrací seznam řádků reportu; při chybě vyvolá JobFailed (výstup zůstane netknutý)."""
    with open(path, encoding="utf-8") as f:
        job = json.load(f)
    if job.get("job_version") != JOB_VERSION:
        raise JobFailed(f"Nepodporovaná verze úlohy ({job.get('job_version')!r}), čekám {JOB_VERSION}.")
    name = job.get("name") or os.path.splitext(os.path.basename(path))[0]
    out_path = (job.get("output") or {}).get("path") or f"feeds/{name}.xml"
    checks = {**DEFAULT_CHECKS, **(job.get("checks") or {})}
    report = [f"Úloha: {name}"]

    missing = [n for n in secret_names(job) if get_secret(n) is None]
    if missing:
        raise JobFailed("Chybí secrety: " + ", ".join(missing) + " (GitHub: Settings → Secrets and variables → Actions).")

    # 1) zdrojový feed
    try:
        data = fetch_source(job["source"])
    except Exception as e:
        raise JobFailed(f"Stažení feedu selhalo: {e}")
    root = parse_xml_bytes(data)
    if root is None or len(root) == 0:
        raise JobFailed("Stažený feed není čitelné XML nebo je prázdný.")
    report.append(f"Stažen feed: {len(data) / 1024:.0f} kB")

    # 2) ceník (pokud ho profil používá)
    profile = job["profile"]
    prices = {}
    if job.get("price_source"):
        try:
            prices = load_price_map(job["price_source"])
        except Exception as e:
            raise JobFailed(f"Načtení ceníku selhalo: {e}")
        if not prices:
            raise JobFailed("Ceník neobsahuje žádné ceny.")
        report.append(f"Načteno cen z ceníku: {len(prices)}")
    elif profile.get("price", {}).get("mode") == "external":
        raise JobFailed("Profil bere ceny z ceníku, ale úloha nemá nastavený zdroj ceníku (price_source).")

    # 3) převod
    excluded, included = selection_to_args(job.get("selection"))
    shop, log = build_shop(root, profile, prices, excluded, job.get("overrides"), included)
    n_total = len(shop.xpath("./*[local-name()='SHOPITEM']"))
    n_dropped, dropped = drop_invalid_items(shop)
    n_out = n_total - n_dropped
    report.append(f"Produktů ve výsledku: {n_out} (ze {n_total})")
    if n_dropped:
        report.append(f"Vyřazeno kvůli povinné chybě: {n_dropped}")
        for code, msg in dropped[:10]:
            report.append(f"  - {code}: {msg}")
        if n_dropped > 10:
            report.append(f"  … a dalších {n_dropped - 10}")

    # 4) pojistky
    if n_out == 0:
        raise JobFailed("Výsledek neobsahuje žádný produkt.")
    if n_total and n_dropped / n_total > checks["max_invalid_ratio"]:
        raise JobFailed(f"{n_dropped} z {n_total} produktů má povinnou chybu "
                        f"(limit {checks['max_invalid_ratio']:.0%}) - feed se nezveřejní.\n" + "\n".join(report))
    prev, old = None, None
    if os.path.exists(out_path):
        with open(out_path, "rb") as f:
            old = f.read()
        prev = count_items(old)
    if prev and not force and n_out < prev * (1 - checks["max_drop_ratio"]):
        raise JobFailed(f"Počet produktů klesl z {prev} na {n_out} (limit poklesu {checks['max_drop_ratio']:.0%}). "
                        f"Zkontroluj feed dodavatele; pokud je to v pořádku, spusť s --force.")

    # 5) zápis (atomicky, ať Shoptet nikdy nestáhne půlku souboru)
    new = shop_to_bytes(shop)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    changed = old != new
    if changed:
        tmp = out_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(new)
        os.replace(tmp, out_path)
    report.append(f"Výstup {out_path}: " + ("aktualizován" if changed else "beze změny")
                  + (f" (dříve {prev} produktů)" if prev is not None else " (nový soubor)"))
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("jobs", nargs="*", help="soubory úloh (.json)")
    ap.add_argument("--all", metavar="SLOŽKA", help="spustit všechny *.json ve složce")
    ap.add_argument("--force", action="store_true", help="přeskočit kontrolu poklesu počtu produktů")
    args = ap.parse_args(argv)

    paths = list(args.jobs)
    if args.all:
        paths += sorted(glob.glob(os.path.join(args.all, "*.json")))
    if not paths:
        print("Nenalezena žádná úloha (očekávám soubory jobs/*.json).", file=sys.stderr)
        return 1

    summary, failed = [], 0
    for p in paths:
        try:
            lines = run_job(p, force=args.force)
            summary.append(f"### ✅ {os.path.basename(p)}\n" + "\n".join(lines))
        except JobFailed as e:
            failed += 1
            summary.append(f"### ❌ {os.path.basename(p)}\n{e}")
        except Exception as e:  # nečekaná chyba nesmí zastavit ostatní úlohy
            failed += 1
            summary.append(f"### ❌ {os.path.basename(p)}\nNečekaná chyba: {type(e).__name__}: {e}")
        print(summary[-1], flush=True)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write("\n\n".join(summary) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
