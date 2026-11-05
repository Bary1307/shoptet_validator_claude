# Automatická aktualizace feedů

Nová verze aplikace `shoptet_validator_app_17.py` umí dvě věci:

- **Jednorázový feed** – funguje jako verze 16 (nahraješ soubor).
- **Aktualizovaný feed z URL** – feed se stáhne z odkazu dodavatele a z nastavení vznikne *úloha* (`.json`),
  podle které ho GitHub pravidelně stáhne, převede a uloží do `feeds/`. Shoptet si hotový feed stahuje z odkazu.

Verze 16 a starší zůstaly beze změny.

## Co je v repozitáři

| Soubor | K čemu |
|---|---|
| `feed_core.py` | převodní logika bez Streamlitu (převzatá z verze 16) + stahování z URL, přihlášení, pojistky |
| `run_feed.py` | spouštěč bez rozhraní: stáhne feed, převede, zvaliduje, uloží |
| `shoptet_validator_app_17.py` | webová aplikace s úvodní volbou režimu a exportem úlohy |
| `.github/workflows/update_feed.yml` | plán běhu (každé 3 hodiny + ruční spuštění) |
| `requirements-feed.txt` | závislosti pro automatický běh (bez Streamlitu) |
| `jobs/*.json` | úlohy – vznikají v kroku 8 aplikace |
| `feeds/*.xml` | hotové feedy pro Shoptet – vznikají automaticky |

Pro nasazení na streamlit.app změň hlavní soubor aplikace na `shoptet_validator_app_17.py`
(`feed_core.py` musí ležet vedle něj).

## Postup

1. V aplikaci zvol **Aktualizovaný feed – stahování z URL**, zadej odkaz (a přihlášení, pokud ho feed vyžaduje),
   stáhni feed a nastav mapování jako obvykle (kroky 2–7).
2. V kroku 8 vyplň název úlohy, co dělat s novými produkty a (pokud ceny berou z ceníku) odkaz na ceník.
   Stáhni úlohu a nahraj ji do repozitáře do složky `jobs/`.
3. V GitHubu přidej secrety (Settings → Secrets and variables → Actions) – jejich názvy aplikace vypíše
   (`FEED_USER`, `FEED_PASSWORD`, `FEED_TOKEN`, `FEED_URL`, `PRICE_…`). Hesla se do úloh **neukládají**.
4. Na záložce Actions spusť „Aktualizace feedů“ ručně (Run workflow). Výsledek je v `feeds/<název>.xml`.
5. V Shoptetu nastav import z odkazu
   `https://raw.githubusercontent.com/<uživatel>/<repozitář>/main/feeds/<název>.xml`.

## Pojistky

Výstup se **nepřepíše** (na Shoptetu zůstane poslední dobrý feed) a běh skončí chybou, když:

- stažení nebo přihlášení selže, nebo server místo feedu vrátí webovou stránku (např. přihlašovací),
- chybí secret,
- výsledek je prázdný,
- víc než 20 % produktů má povinnou chybu (např. chybí cena),
- počet produktů klesl o víc než 30 % proti minulému běhu (obejít jde ručním spuštěním s volbou *force*).

Produkty s povinnou chybou se z výstupu vyřadí a uvedou v reportu běhu. Limity jdou změnit v kroku 8.
Při chybě GitHub pošle e-mail o selhání workflow.

## Na co si dát pozor

- **Repozitář je veřejný** – feed i profil v něm uvidí každý. Ceny z B2B portálu dodavatele tam dávej
  jen, pokud to jeho podmínky dovolují; jinak repozitář zprivátni a feed publikuj jinudy (např. FTP).
- **Kategorie při opakovaném importu:** před zapnutím pravidelného importu ověř v nastavení importu
  v Shoptetu, které údaje se u existujících produktů přepisují, ať se ručně zařazené produkty nevrátí do „Nezařazeno“.
- **Fotky** se aktualizací nenahrávají. Přímé URL z feedu se přenesou beze změny (Shoptet je stáhne, jen pokud jsou
  veřejně dostupné). U Bosche (názvy souborů + vaše základní URL) zůstávají fotky u stávajících produktů, nové je třeba nahrát jako dnes.
- U veřejných repozitářů GitHub po 60 dnech bez aktivity plánované běhy vypíná. Pokud se feed mění a běh commituje,
  měl by repozitář zůstat aktivní; kdyby běhy přestaly, zapni je na záložce Actions.
- **Dostupnost/sklad po produktu** aplikace zatím neumí (dostupnost se nastavuje hromadně pro všechny produkty).
