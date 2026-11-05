# Univerzální Feed -> Shoptet Validator, verze 17.
# Oproti verzi 16: převodní logika je v feed_core.py (sdílí ji i automatický běh run_feed.py),
# na začátku se volí režim "jednorázový feed" / "aktualizovaný feed z URL" a v kroku 8 lze
# exportovat úlohu (job .json) pro automatickou aktualizaci přes GitHub Actions.
import streamlit as st
from lxml import etree
import pandas as pd
import json
import re
from copy import deepcopy
from urllib.parse import quote

from feed_core import *  # noqa: F401,F403 - slovník tagů, apply_profile, build_shop, fetch_url, ...

st.set_page_config(page_title="Univerzální Feed → Shoptet Validator", layout="wide")

# =============================================================================
# SESSION STATE
# =============================================================================
ss = st.session_state
ss.setdefault("raw_root", None)
ss.setdefault("profile", json.loads(json.dumps(DEFAULT_PROFILE)))
ss.setdefault("overrides", {})
ss.setdefault("excluded_codes", set())
ss.setdefault("step", 1)
ss.setdefault("external_prices", {})


def get_item_candidates(root):
    """Kandidáty na element produktu - výpočet je u velkých feedů pomalý, proto se cachuje."""
    cached = ss.get("_item_candidates")
    if cached is not None and cached[0] == ss.get("_raw_ver", 0):
        return cached[1]
    res = candidate_item_tags(root)
    ss._item_candidates = (ss.get("_raw_ver", 0), res)
    return res


def compute_shop():
    """Vrací (strom, log). Výsledek se cachuje podle nastavení - volající ho nesmí měnit."""
    if ss.raw_root is None:
        return None, []
    key = json.dumps([ss.profile, ss.overrides, sorted(ss.excluded_codes),
                      ss.get("_raw_ver", 0), ss.get("_ext_ver", 0)], sort_keys=True, default=str)
    cached = ss.get("_shop_cache")
    if cached and cached[0] == key:
        return cached[1], cached[2]
    shop, log = build_shop(ss.raw_root, ss.profile, ss.get("external_prices", {}), ss.excluded_codes, ss.overrides)
    ss._shop_cache = (key, shop, log)
    return shop, log

def render_live_preview():
    shop, _ = compute_shop()
    if shop is None:
        return
    issues = validate_shop(shop)
    n_mand = sum(1 for i in issues if i["mandatory"])
    n_opt = len(issues) - n_mand
    items = shop.xpath("./*[local-name()='SHOPITEM']")
    c1, c2, c3 = st.columns(3)
    c1.metric("Produktů", len(items))
    c2.metric("🔴 Povinné chyby", n_mand)
    c3.metric("⚪ Doporučené", n_opt)
    if n_mand:
        st.error(f"Feed zatím NENÍ validní pro Shoptet - {n_mand} povinných chyb (viz krok 7).")
    elif items:
        st.success("Feed prochází kontrolou povinných elementů.")
    with st.expander("👁️ Živý náhled XML (aktuální stav)", expanded=False):
        xml_bytes = shop_to_bytes(shop)
        st.code(xml_bytes.decode("utf-8")[:8000], language="xml")
        if len(xml_bytes) > 8000:
            st.caption("(zkráceno, plný výstup je v kroku 8 - Export)")


def nav_buttons():
    c1, c2, c3 = st.columns([1, 3, 1])
    with c1:
        if ss.step > 1 and st.button("⬅️ Zpět"):
            ss.step -= 1
            st.rerun()
    with c3:
        if ss.step < len(STEPS) and st.button("Další ➡️", type="primary"):
            ss.step += 1
            st.rerun()


# =============================================================================
# REŽIM "AKTUALIZOVANÝ FEED Z URL" - pomocné funkce rozhraní
# =============================================================================
AUTH_TYPES = {
    "none": "Bez přihlášení",
    "basic": "Jméno a heslo (HTTP Basic)",
    "header": "Token / API klíč v hlavičce",
    "form": "Přihlašovací formulář webu (např. B2B portál)",
}
DEFAULT_REPO = "Bary1307/shoptet_validator_claude"
DEFAULT_BRANCH = "main"

ss.setdefault("mode", "file")
ss.setdefault("src_cfg", {"url": "", "url_secret": False, "auth": {"type": "none"}})
ss.setdefault("job_cfg", {})
ss.setdefault("pending_include", None)


def json_default(o):
    return sorted(o) if isinstance(o, (set, frozenset)) else str(o)


def auth_form(prefix, store, key):
    """Vykreslí volbu přihlášení. store = dict v session_state (pamatuje nesecretová nastavení).
    Vrací (popis_pro_úlohu, přihlášení_pro_stažení_teď). Hesla a tokeny se nikam neukládají -
    v úloze jsou jen názvy secretů (PREFIX_USER, PREFIX_PASSWORD, PREFIX_TOKEN)."""
    types = list(AUTH_TYPES)
    atype = st.selectbox("Přihlášení", types, index=types.index(store.get("type", "none")) if store.get("type", "none") in types else 0,
                         format_func=AUTH_TYPES.get, key=f"{key}_type")
    spec, live = {"type": atype}, {"type": atype}
    if atype == "basic":
        c1, c2 = st.columns(2)
        user = c1.text_input("Jméno", key=f"{key}_user")
        pw = c2.text_input("Heslo", type="password", key=f"{key}_pw")
        spec.update(user_env=f"{prefix}_USER", password_env=f"{prefix}_PASSWORD")
        live.update(user=user, password=pw)
    elif atype == "header":
        hn = st.text_input("Název hlavičky", value=store.get("header_name", "Authorization"), key=f"{key}_hn")
        val = st.text_input("Hodnota (např. Bearer abc123)", type="password", key=f"{key}_hv")
        spec.update(header_name=hn, value_env=f"{prefix}_TOKEN")
        live.update(header_name=hn, value=val)
    elif atype == "form":
        lu = st.text_input("URL, kam se odesílá přihlášení (POST)", value=store.get("login_url", ""), key=f"{key}_lu")
        c1, c2 = st.columns(2)
        uf = c1.text_input("Název pole pro jméno", value=store.get("user_field", "username"), key=f"{key}_uf")
        pf = c2.text_input("Název pole pro heslo", value=store.get("password_field", "password"), key=f"{key}_pf")
        user = c1.text_input("Jméno", key=f"{key}_user")
        pw = c2.text_input("Heslo", type="password", key=f"{key}_pw")
        common = dict(login_url=lu, user_field=uf, password_field=pf)
        spec.update(common, user_env=f"{prefix}_USER", password_env=f"{prefix}_PASSWORD")
        live.update(common, user=user, password=pw)
    if atype != "none":
        st.caption("Přihlašovací údaje se do úlohy neukládají - uloží se jen názvy GitHub secretů, "
                   f"které pak přidáš do repozitáře ({', '.join(v for k, v in spec.items() if k.endswith('_env'))}).")
    return spec, live


def apply_pending_selection():
    """Výběr z načtené úlohy ('jen vybrané produkty') se přepočítá na vyřazené kódy, jakmile je feed k dispozici."""
    pend = ss.get("pending_include")
    if pend is not None and ss.raw_root is not None and ss.profile.get("item_tag"):
        ss.excluded_codes = codes_in_feed(ss.raw_root, ss.profile) - set(pend)
        ss.pending_include = None


def load_profile_or_job(loaded):
    """Načte šablonu (profil) nebo celou úlohu z .json. Vrací text pro uživatele."""
    if isinstance(loaded, dict) and loaded.get("job_version"):
        ss.profile = loaded["profile"]
        ss.overrides = loaded.get("overrides") or {}
        sel = loaded.get("selection") or {}
        if sel.get("mode") == "exclude":
            ss.excluded_codes = set(sel.get("codes") or [])
        elif sel.get("mode") == "include_only":
            ss.pending_include = set(sel.get("codes") or [])
        src = loaded.get("source") or {}
        ss.src_cfg = {"url": src.get("url", ""), "url_secret": bool(src.get("url_env")),
                      "auth": src.get("auth") or {"type": "none"}}
        ps = loaded.get("price_source") or {}
        chk = loaded.get("checks") or {}
        ss.job_cfg = {
            "name": loaded.get("name", ""),
            "new_products": "only_selected" if sel.get("mode") == "include_only" else "auto",
            "max_drop_pct": int(round(chk.get("max_drop_ratio", 0.30) * 100)),
            "max_invalid_pct": int(round(chk.get("max_invalid_ratio", 0.20) * 100)),
            "price_url": ps.get("url", ""), "price_url_secret": bool(ps.get("url_env")),
            "price_auth": ps.get("auth") or {"type": "none"}, "price_format": ps.get("format", "csv"),
            "price_id_col": ps.get("id_column", ""), "price_col": ps.get("price_column", ""),
        }
        ss.mode = "url"
        extra = ""
        if sel.get("mode") == "include_only":
            extra = " Výběr produktů se použije po stažení feedu."
        return f"Úloha '{loaded.get('name', '')}' načtena (profil, výběr, opravy i nastavení zdroje).{extra} Hesla je třeba zadat znovu."
    ss.profile = loaded
    return f"Šablona '{ss.profile.get('profile_name', '')}' načtena."


def render_url_source():
    """Krok 1 v režimu URL: adresa, přihlášení a tlačítko Stáhnout."""
    cfg = ss.src_cfg
    st.caption("Feed se stáhne z odkazu dodavatele. Stejný odkaz pak může každých pár hodin stahovat i GitHub "
               "a nechat feed převést sám (krok 8).")
    cfg["url"] = st.text_input("URL feedu", value=cfg.get("url", ""), placeholder="https://…").strip()
    cfg["url_secret"] = st.checkbox("URL obsahuje tajný token nebo heslo - do úlohy ji neukládat, jen jako secret FEED_URL",
                                    value=cfg.get("url_secret", False))
    cfg["auth"], live_auth = auth_form("FEED", cfg.get("auth") or {"type": "none"}, "src_auth")
    if st.button("⬇️ Stáhnout feed", type="primary", disabled=not cfg["url"]):
        try:
            with st.spinner("Stahuji feed…"):
                data = fetch_url(cfg["url"], live_auth)
                root = parse_xml_bytes(data)
            if root is None or len(root) == 0:
                raise FeedFetchError("Stažený soubor není čitelné XML.")
        except Exception as e:
            st.error(f"❌ {e}")
        else:
            if ss.get("_last_url") != cfg["url"]:
                ss.overrides = {}
                ss.excluded_codes = set()
            ss._last_url = cfg["url"]
            ss._last_filename = None
            ss.raw_root = root
            ss._raw_ver = ss.get("_raw_ver", 0) + 1
            ss._fetch_info = f"Staženo {len(data) / 1024:.0f} kB z URL."
            apply_pending_selection()
            st.rerun()


def render_job_export(export_shop):
    """Krok 8 v režimu URL: složí úlohu (job .json) pro automatický běh a vypíše, co dál."""
    st.markdown("---")
    st.subheader("🤖 Automatická aktualizace (GitHub Actions)")
    st.caption("Z nastavení, které jsi teď udělal, vznikne úloha. Nahraješ ji do repozitáře do složky `jobs/` a "
               "GitHub podle ní feed pravidelně stáhne, převede a uloží do `feeds/`. Shoptet si pak hotový feed stahuje z odkazu.")
    src, jc, prof = ss.src_cfg, ss.job_cfg, ss.profile
    problems = []
    if not src.get("url"):
        problems.append("V kroku 1 chybí URL feedu.")

    jc["name"] = st.text_input("Název úlohy (soubory jobs/<název>.json a feeds/<název>.xml)",
                               value=jc.get("name") or slugify(prof.get("profile_name", "")) or "feed")
    name = slugify(jc["name"]) or "feed"

    sel_opts = {"auto": "Zakládat nové produkty z feedu automaticky (mimo zůstanou jen ty, co jsi vyřadil v kroku 2)",
                "only_selected": "Zpracovávat jen produkty vybrané teď (nové z feedu ignorovat)"}
    jc["new_products"] = st.radio("Co s produkty, které se ve feedu objeví později?", list(sel_opts),
                                  index=list(sel_opts).index(jc.get("new_products", "auto")), format_func=sel_opts.get)
    if jc["new_products"] == "auto":
        cat = prof.get("category", {})
        where = cat.get("fixed_value") if cat.get("mode") == "fixed" else "podle mapování z kroku 4"
        st.caption(f"Nové produkty se založí do kategorie: **{where}**.")

    price_source = None
    if prof.get("price", {}).get("mode") == "external":
        st.markdown("**Ceník pro automatický běh** - profil bere ceny z ceníku, GitHub si ho proto musí umět stáhnout sám.")
        jc["price_url"] = st.text_input("URL ceníku (CSV nebo Excel)", value=jc.get("price_url", "")).strip()
        jc["price_url_secret"] = st.checkbox("URL ceníku obsahuje tajný údaj - uložit jen jako secret PRICE_URL",
                                             value=jc.get("price_url_secret", False))
        jc["price_auth"], _ = auth_form("PRICE", jc.get("price_auth") or {"type": "none"}, "ps_auth")
        c1, c2, c3 = st.columns(3)
        fmts = ["csv", "xlsx"]
        jc["price_format"] = c1.selectbox("Formát ceníku", fmts, index=fmts.index(jc.get("price_format", "csv")) if jc.get("price_format", "csv") in fmts else 0)
        jc["price_id_col"] = c2.text_input("Sloupec s kódem/EAN (název v hlavičce)", value=jc.get("price_id_col", ""))
        jc["price_col"] = c3.text_input("Sloupec s cenou (název v hlavičce)", value=jc.get("price_col", ""))
        if not (jc["price_url"] and jc["price_id_col"] and jc["price_col"]):
            problems.append("Doplň URL ceníku a názvy jeho sloupců.")
        price_source = {"type": "url", "url": "" if jc["price_url_secret"] else jc["price_url"],
                        "url_env": "PRICE_URL" if jc["price_url_secret"] else "", "auth": jc["price_auth"],
                        "format": jc["price_format"], "id_column": jc["price_id_col"], "price_column": jc["price_col"]}

    with st.expander("Pojistky proti rozbitému feedu (doporučené hodnoty)"):
        jc["max_drop_pct"] = st.slider("Zastavit, když počet produktů klesne o víc než (%)", 5, 90, int(jc.get("max_drop_pct", 30)))
        jc["max_invalid_pct"] = st.slider("Zastavit, když víc než (%) produktů má povinnou chybu", 0, 100, int(jc.get("max_invalid_pct", 20)))
        st.caption("Produkt s povinnou chybou (např. bez ceny) se z výstupu vyřadí a uvede v reportu. "
                   "Při zastavení zůstane na Shoptetu poslední dobrý feed.")

    excluded = sorted(ss.excluded_codes)
    if jc["new_products"] == "only_selected":
        selection = {"mode": "include_only", "codes": sorted(codes_in_feed(ss.raw_root, prof) - set(excluded))}
    elif excluded:
        selection = {"mode": "exclude", "codes": excluded}
    else:
        selection = {"mode": "all"}

    job = {
        "job_version": JOB_VERSION,
        "name": name,
        "source": {"type": "url", "url": "" if src.get("url_secret") else src.get("url", ""),
                   "url_env": "FEED_URL" if src.get("url_secret") else "", "auth": src.get("auth") or {"type": "none"}},
        "profile": prof,
        "selection": selection,
        "overrides": ss.overrides,
        "price_source": price_source,
        "output": {"path": f"feeds/{name}.xml"},
        "checks": {"max_drop_ratio": jc["max_drop_pct"] / 100, "max_invalid_ratio": jc["max_invalid_pct"] / 100},
    }
    for p in problems:
        st.error(p)
    st.download_button(f"💾 Stáhnout úlohu (jobs/{name}.json)", type="primary", disabled=bool(problems),
                       data=json.dumps(job, ensure_ascii=False, indent=2, default=json_default).encode("utf-8"),
                       file_name=f"{name}.json", mime="application/json")

    secrets = secret_names(job)
    repo = st.text_input("Repozitář (uživatel/název) - jen pro vypsání odkazu na feed", value=jc.get("repo", DEFAULT_REPO))
    jc["repo"] = repo
    st.markdown(
        f"**Co dál:**\n"
        f"1. Nahraj stažený soubor do repozitáře do složky `jobs/` (a `run_feed.py`, `feed_core.py`, "
        f"`requirements-feed.txt` a `.github/workflows/update_feed.yml`, pokud tam ještě nejsou).\n"
        + (f"2. V GitHubu (Settings → Secrets and variables → Actions) přidej secrety: " + ", ".join(f"`{s}`" for s in secrets) + ".\n"
           if secrets else "2. Secrety nejsou potřeba (feed je bez přihlášení).\n")
        + f"3. Na záložce Actions spusť „Aktualizace feedů“ ručně (Run workflow); dál poběží sama každé 3 hodiny.\n"
        f"4. V Shoptetu nastav import z odkazu: `https://raw.githubusercontent.com/{repo}/{DEFAULT_BRANCH}/feeds/{name}.xml`"
    )
    st.caption("⚠️ Repozitář je veřejný - feed v něm i profil uvidí každý. Ceny z B2B portálu dodavatele tam dávej jen, "
               "pokud to jeho podmínky dovolují. U Shoptetu si před zapnutím pravidelného importu ověř v nastavení importu, "
               "které údaje se u existujících produktů přepisují (hlavně kategorie, ať se ručně zařazené produkty "
               "nevrátí do „Nezařazeno“).")


# =============================================================================
# HLAVIČKA
# =============================================================================
st.title("🛠️ Univerzální Feed → Shoptet Validator")
progress_labels = " → ".join(f"**{s}**" if i + 1 == ss.step else s for i, s in enumerate(STEPS))
st.caption(progress_labels)
step_jump = st.selectbox("Přeskočit na krok", STEPS, index=ss.step - 1, label_visibility="collapsed")
if STEPS.index(step_jump) + 1 != ss.step:
    ss.step = STEPS.index(step_jump) + 1
    st.rerun()
st.markdown("---")

with st.sidebar:
    st.header("📖 Slovník Shoptet tagů")
    search_term = st.text_input("Hledat tag / popis:", "").lower()
    for tag, info in SHOPTET_DICT.items():
        if search_term in tag.lower() or search_term in info["desc"].lower():
            badge = "🔴 POVINNÝ" if info["mandatory"] else "⚪ nepovinný"
            st.markdown(f"**`<{tag}>`** — {badge}")
            st.caption(info["desc"])

step = ss.step

# =============================================================================
# KROK 1 - NAHRÁNÍ
# =============================================================================
if step == 1:
    st.header(STEPS[0])
    mode_box = st.container()
    up_box = st.container()

    # nahrání šablony/úlohy se zpracuje dřív než volba režimu (úloha může režim přepnout)
    with up_box:
        profile_file = st.file_uploader("Uložená šablona nebo úloha (.json) - nepovinné", type=["json"])
    if profile_file is not None and not ss.get("_profile_loaded"):
        try:
            msg = load_profile_or_job(json.load(profile_file))
            ss._profile_loaded = True
            st.success(msg)
        except Exception as e:
            st.error(f"Nepodařilo se načíst šablonu: {e}")

    mode_options = {"file": "📄 Jednorázový feed - nahraju soubor",
                    "url": "🔄 Aktualizovaný feed - stahování z URL (lze automatizovat)"}
    with mode_box:
        ss.mode = st.radio("Jak chceš feed zpracovat?", list(mode_options), index=list(mode_options).index(ss.mode),
                           format_func=mode_options.get, horizontal=True)

    if ss.mode == "file":
        uploaded_file = st.file_uploader("Nahraj vstupní XML feed", type=["xml"])
        if uploaded_file is not None:
            data = uploaded_file.read()
            if ss.get("_last_filename") != uploaded_file.name:
                ss.raw_root = parse_xml_bytes(data)
                ss._last_filename = uploaded_file.name
                ss._last_url = None
                ss._fetch_info = ""
                ss._raw_ver = ss.get("_raw_ver", 0) + 1
                ss.overrides = {}
                ss.excluded_codes = set()
    else:
        render_url_source()

    apply_pending_selection()

    if ss.raw_root is not None:
        st.success("Feed nahrán." + (f" {ss.get('_fetch_info')}" if ss.mode == "url" and ss.get("_fetch_info") else ""))
        st.caption(f"Typ: {detect_feed_type(ss.raw_root)}")
    nav_buttons()

elif ss.raw_root is None:
    st.warning("Nejdřív nahraj feed v kroku 1.")
    if st.button("⬅️ Jít na krok 1"):
        ss.step = 1
        st.rerun()

# =============================================================================
# KROK 2 - VÝBĚR PRODUKTŮ (hned po nahrání - nevybrané se dál vůbec nezpracovávají)
# =============================================================================
elif step == 2:
    st.header(STEPS[1])
    root = ss.raw_root
    prof = ss.profile
    st.caption("Vyber produkty hned na začátku - nezaškrtnuté se dál vůbec nezpracovávají, takže další kroky "
               "pracují s menším množstvím dat a jsou rychlejší.")

    rows_src = []
    items_src = []
    tag_options = []
    ready = True
    if detect_feed_type(root) == "shoptet":
        for it in root.xpath("./*[local-name()='SHOPITEM']"):
            rows_src.append((gtext(it, "CODE") or "", gtext(it, "NAME") or ""))
            items_src.append(it)
    else:
        candidates = get_item_candidates(root)
        cand_names = [f"{t} ({c}×)" for t, c in candidates]
        cand_map = {f"{t} ({c}×)": t for t, c in candidates}
        default_idx = next((i for i, (t, c) in enumerate(candidates) if t == prof.get("item_tag")), 0)
        chosen = st.selectbox("Element představující JEDEN produkt", cand_names,
                               index=default_idx if cand_names else 0)
        prof["item_tag"] = cand_map.get(chosen, prof.get("item_tag", ""))
        sample_items = root.xpath(f"//*[local-name()='{prof['item_tag']}']") if prof["item_tag"] else []
        avail_tags = aggregate_descendant_tags(sample_items) if sample_items else []
        tag_options = ["-- nevybráno --"] + avail_tags
        c1, c2 = st.columns(2)
        with c1:
            cur = prof["fields"].get("CODE", {"source": ""}).get("source", "")
            sel = st.selectbox("Pole s kódem produktu (CODE)", tag_options,
                                index=tag_options.index(cur) if cur in tag_options else 0, key="f_CODE")
            prof["fields"]["CODE"] = {"source": "" if sel == "-- nevybráno --" else sel}
        with c2:
            cur = prof["fields"].get("NAME", {"source": ""}).get("source", "")
            sel = st.selectbox("Pole s názvem produktu (NAME)", tag_options,
                                index=tag_options.index(cur) if cur in tag_options else 0, key="f_NAME")
            prof["fields"]["NAME"] = {"source": "" if sel == "-- nevybráno --" else sel}
        code_src = prof["fields"]["CODE"]["source"]
        name_src = prof["fields"]["NAME"]["source"]
        if not code_src:
            ready = False
            st.info("Vyber pole s kódem produktu - podle něj se produkty vybírají.")
        else:
            for it in sample_items:
                rows_src.append((gtext_deep(it, code_src) or "", gtext_deep(it, name_src) or ""))
                items_src.append(it)

    if ready:
        all_codes = set(c for c, _ in rows_src)
        excluded_now = ss.excluded_codes & all_codes
        df_sel = pd.DataFrame([{"Zahrnout": code not in excluded_now, "CODE": code, "NAME": name}
                                for code, name in rows_src])
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Zaškrtnout vše"):
                ss.excluded_codes = set()
                st.rerun()
        with c2:
            if st.button("Odškrtnout vše"):
                ss.excluded_codes = set(all_codes)
                st.rerun()
        with c3:
            st.metric("Vybráno", f"{len(rows_src) - len(excluded_now)} / {len(rows_src)}")
        edited = st.data_editor(df_sel, use_container_width=True, disabled=["CODE", "NAME"], hide_index=True, key="sel_editor")
        if st.button("💾 Uložit výběr", type="primary"):
            ss.excluded_codes = set(edited.loc[~edited["Zahrnout"], "CODE"])
            st.rerun()

        st.markdown("---")
        st.subheader("📄 Nebo nahraj seznam kódů ze souboru (Excel/CSV/TXT)")
        st.caption("Vyber sloupec s kódy - porovná se s polem feedu, které zvolíš níže (výchozí CODE). "
                   "Porovnání ignoruje mezery, velikost písmen a koncovku .0; pokud nenajde přesnou shodu, zkusí i "
                   "variantu bez oddělovačů a úvodních nul (Excel je u čísel ztrácí).")
        code_file = st.file_uploader("Soubor se seznamem kódů", type=["xlsx", "xlsm", "xls", "csv", "txt"],
                                     key="code_file_uploader")
        if code_file is not None:
            try:
                sheets = read_all_columns(code_file)
            except Exception as e:
                sheets = {}
                st.error(f"Soubor se nepodařilo přečíst: {e}")
            if sheets:
                cs1, cs2, cs3 = st.columns(3)
                with cs1:
                    sheet_name = st.selectbox("List", list(sheets.keys()))
                cols = sheets[sheet_name]
                nonempty = [i for i, c in enumerate(cols) if any(str(x).strip() for x in c)]
                with cs2:
                    col_idx = st.selectbox(
                        "Sloupec s kódy", nonempty or [0],
                        format_func=lambda i: f"Sloupec {i + 1}  (např. {next((str(x).strip() for x in cols[i] if str(x).strip()), '')[:20]}, "
                                              f"{sum(1 for x in cols[i] if str(x).strip())} hodnot)" if cols else "-")
                feed_fields = [t for t in (tag_options[1:] if tag_options else [])]
                default_field = prof["fields"].get("CODE", {}).get("source", "") if prof.get("fields") else ""
                with cs3:
                    if feed_fields:
                        cmp_field = st.selectbox("Porovnat s polem feedu", feed_fields,
                                                 index=feed_fields.index(default_field) if default_field in feed_fields else 0)
                    else:
                        cmp_field = None
                        st.text_input("Porovnat s polem feedu", "CODE", disabled=True)
                values = list(cols[col_idx]) if cols else []
                feed_pairs = []
                for it, (code, _n) in zip(items_src, rows_src):
                    fv = gtext_deep(it, cmp_field) if cmp_field else code
                    feed_pairs.append((fv or "", code))
                # hlavička: první hodnota, která nic nenapáruje, a vypadá jako text (ne číslo)
                first = str(values[0]).strip() if values else ""
                m_first = match_codes([first], feed_pairs)[0] if first else set()
                has_header = st.checkbox("První řádek je hlavička", value=bool(first) and not m_first and not first.isdigit())
                if has_header:
                    values = values[1:]
                values = [v for v in values if str(v).strip()]
                matched, n_strict, n_loose, unmatched = match_codes(values, feed_pairs)
                n_unique = n_strict + n_loose + len(unmatched)
                st.success(f"V souboru **{n_unique}** unikátních kódů → **{n_strict + n_loose}** nalezeno ve feedu "
                           f"({n_strict} přesně, {n_loose} po volné normalizaci), **{len(unmatched)}** nenalezeno. "
                           f"Ve feedu je celkem {len(all_codes)} produktů, vybráno by jich bylo {len(matched)}.")
                if unmatched:
                    with st.expander(f"Nenalezené kódy ({len(unmatched)}) - nejsou ve feedu"):
                        st.write(", ".join(unmatched[:300]) + (" …" if len(unmatched) > 300 else ""))
                        st.caption("Zkontroluj, zda tyto kódy ve feedu opravdu existují (feed dodavatele nemusí obsahovat "
                                   "všechny položky), nebo zkus v rozbalovátku výše porovnat s jiným polem (např. EAN).")
                        sample_feed = [v for v, _c in feed_pairs[:5]]
                        st.caption(f"Ukázka hodnot ve feedu: {', '.join(sample_feed)}")
                cbtn1, cbtn2 = st.columns(2)
                with cbtn1:
                    if st.button("✅ Zaškrtnout JEN tyto kódy", type="primary"):
                        ss.excluded_codes = all_codes - matched
                        st.rerun()
                with cbtn2:
                    if st.button("🚫 Odškrtnout tyto kódy (vynechat je)"):
                        ss.excluded_codes = ss.excluded_codes | matched
                        st.rerun()
    nav_buttons()

# =============================================================================
# KROK 3 - ZÁKLADNÍ MAPOVÁNÍ
# =============================================================================
elif step == 3:
    st.header(STEPS[2])
    root = ss.raw_root
    if detect_feed_type(root) == "shoptet":
        st.success("Tenhle feed už je ve formátu Shoptet - mapování polí není potřeba, pokračuj dál.")
    else:
        prof = ss.profile
        prof["profile_name"] = st.text_input("Název šablony", value=prof.get("profile_name", "Nová šablona"))

        candidates = get_item_candidates(root)
        cand_names = [f"{t} ({c}×)" for t, c in candidates]
        cand_map = {f"{t} ({c}×)": t for t, c in candidates}
        default_idx = next((i for i, (t, c) in enumerate(candidates) if t == prof.get("item_tag")), 0)
        chosen = st.selectbox("Element představující JEDEN produkt", cand_names,
                               index=default_idx if cand_names else 0)
        prof["item_tag"] = cand_map.get(chosen, prof.get("item_tag", ""))

        sample_items = root.xpath(f"//*[local-name()='{prof['item_tag']}']") if prof["item_tag"] else []
        avail_tags = aggregate_descendant_tags(sample_items) if sample_items else []
        tag_options = ["-- nevybráno --"] + avail_tags

        def tag_select(label, current, key):
            idx = tag_options.index(current) if current in tag_options else 0
            return st.selectbox(label, tag_options, index=idx, key=key)

        st.markdown("#### Jednoduchá pole")
        cols = st.columns(3)
        for i, target in enumerate(["CODE", "NAME", "DESCRIPTION", "MANUFACTURER", "EAN", "PART_NUMBER"]):
            with cols[i % 3]:
                cur = prof["fields"].get(target, {"source": ""})["source"]
                sel = tag_select(target, cur, f"f_{target}")
                prof["fields"][target] = {"source": "" if sel == "-- nevybráno --" else sel}

        with st.expander("🧹 Čištění NAME (odstranění kódu/balení z názvu)", expanded=False):
            ncfg = prof.get("name_cleanup", {"enabled": False, "strip_after_semicolon": True, "strip_own_code_parens": True})
            ncfg["enabled"] = st.checkbox("Zapnout čištění názvu", value=ncfg.get("enabled", False))
            if ncfg["enabled"]:
                ncfg["strip_after_semicolon"] = st.checkbox(
                    "Odstranit vše za středníkem (typicky typ balení)", value=ncfg.get("strip_after_semicolon", True))
                ncfg["strip_own_code_parens"] = st.checkbox(
                    "Odstranit kód produktu v závorce, pokud je stejný jako CODE", value=ncfg.get("strip_own_code_parens", True))
                name_source = prof["fields"].get("NAME", {}).get("source")
                if name_source and sample_items:
                    sample_code_src = prof["fields"].get("CODE", {}).get("source")
                    sample_raw_name = gtext_deep(sample_items[0], name_source)
                    sample_code = gtext_deep(sample_items[0], sample_code_src) if sample_code_src else None
                    if sample_raw_name:
                        st.caption("Náhled na prvním produktu:")
                        st.write(f"„{sample_raw_name}" + "“ → „" + clean_name_text(sample_raw_name, sample_code, ncfg) + "“")
            prof["name_cleanup"] = ncfg

        with st.expander("✏️ Úpravy textu (hledat/nahradit, velikost písmen, zkrácení)", expanded=False):
            ttcfg = prof.get("text_tools") or {"enabled": False, "collapse_spaces": True, "name_case": "none",
                                               "short_desc_max": 0, "replace_rules": []}
            ttcfg["enabled"] = st.checkbox("Zapnout úpravy textu", value=ttcfg.get("enabled", False))
            if ttcfg["enabled"]:
                ttcfg["collapse_spaces"] = st.checkbox("Odstranit dvojité mezery a mezery na okrajích",
                                                       value=ttcfg.get("collapse_spaces", True))
                case_opts = {"none": "Beze změny", "first_upper": "První písmeno velké",
                             "sentence": "Jako věta (první velké, zbytek malá)", "upper": "VELKÁ PÍSMENA", "lower": "malá písmena"}
                keys = list(case_opts.keys())
                ttcfg["name_case"] = keys[st.selectbox(
                    "Velikost písmen v NAME", range(len(keys)), format_func=lambda i: case_opts[keys[i]],
                    index=keys.index(ttcfg.get("name_case", "none")) if ttcfg.get("name_case", "none") in keys else 0)]
                ttcfg["short_desc_max"] = int(st.number_input(
                    "Zkrátit SHORT_DESCRIPTION na max. znaků (0 = nezkracovat; odstraní HTML)", min_value=0, max_value=2000,
                    value=int(ttcfg.get("short_desc_max", 0)), step=10))
                st.markdown("**Hledat a nahradit** (prázdné 'Nahradit' = smazat; zaškrtni Regex pro regulární výrazy)")
                rules_df = pd.DataFrame(ttcfg.get("replace_rules") or [],
                                        columns=["field", "find", "replace", "regex"])
                if rules_df.empty:
                    rules_df = pd.DataFrame({"field": pd.Series(dtype="str"), "find": pd.Series(dtype="str"),
                                             "replace": pd.Series(dtype="str"), "regex": pd.Series(dtype="bool")})
                rules_df["regex"] = rules_df["regex"].fillna(False).astype(bool)
                edited_rules = st.data_editor(
                    rules_df, num_rows="dynamic", use_container_width=True, key="text_rules_editor",
                    column_config={
                        "field": st.column_config.SelectboxColumn("Pole", options=["VŠECHNA", "NAME", "SHORT_DESCRIPTION", "DESCRIPTION"], default="VŠECHNA"),
                        "find": st.column_config.TextColumn("Hledat"),
                        "replace": st.column_config.TextColumn("Nahradit"),
                        "regex": st.column_config.CheckboxColumn("Regex", default=False)})
                ttcfg["replace_rules"] = [
                    {"field": r["field"] or "VŠECHNA", "find": r["find"] or "", "replace": r["replace"] or "",
                     "regex": bool(r["regex"])}
                    for r in edited_rules.fillna("").to_dict("records") if (r.get("find") or "")]
                for fld in ("NAME", "SHORT_DESCRIPTION", "DESCRIPTION"):
                    src_f = prof["fields"].get(fld, {}).get("source")
                    if src_f and sample_items:
                        raw = gtext_deep(sample_items[0], src_f)
                        if raw:
                            st.caption(f"Náhled {fld} (první produkt; bez dalšího formátování popisu/čištění názvu):")
                            st.write(f"„{raw[:200]}“ → „{apply_text_rules(raw, fld, ttcfg)[:200]}“")
            prof["text_tools"] = ttcfg

        st.markdown("#### Cena")
        pcfg = prof["price"]
        price_mode = st.radio("Zdroj ceny", ["Přímé pole", "BMEcat styl (ARTICLE_PRICE dle typu)", "Ceník ze souboru (Excel/CSV, párování podle CODE/EAN)"],
                               index={"direct": 0, "price_type": 1, "external": 2}.get(pcfg["mode"], 0), horizontal=True)
        prof["currency"] = st.text_input("Měna (CURRENCY)", value=prof.get("currency", "CZK"))
        if price_mode == "Přímé pole":
            sel = tag_select("Zdrojový tag ceny", pcfg.get("source", ""), "price_src")
            prof["price"] = {"mode": "direct", "source": "" if sel == "-- nevybráno --" else sel}
        elif price_mode.startswith("BMEcat"):
            attr = st.text_input("Atribut typu ceny (prázdné = ber první)", value=pcfg.get("price_type_attr", "price_type"))
            val = st.text_input("Hodnota atributu (např. net_list)", value=pcfg.get("price_type_value", "net_list"))
            sel = tag_select("Tag částky uvnitř ARTICLE_PRICE", pcfg.get("source", "PRICE_AMOUNT") or "PRICE_AMOUNT", "price_amt")
            prof["price"] = {"mode": "price_type", "source": "" if sel == "-- nevybráno --" else sel,
                              "price_type_attr": attr, "price_type_value": val}
        else:
            match_key = st.radio("Párovat podle", ["CODE", "EAN"], index=0 if pcfg.get("match_key", "CODE") == "CODE" else 1, horizontal=True)
            price_file = st.file_uploader("Ceník (Excel .xlsx nebo .csv)", type=["xlsx", "xlsm", "csv"], key="price_file_uploader")
            if price_file is not None:
                header, rows = read_table_file(price_file)
                if header:
                    c1, c2 = st.columns(2)
                    with c1:
                        id_col = st.selectbox(f"Sloupec s {match_key}", range(len(header)), format_func=lambda i: f"{header[i]} (sloupec {i+1})")
                    with c2:
                        price_col = st.selectbox("Sloupec s cenou", range(len(header)), index=min(1, len(header) - 1), format_func=lambda i: f"{header[i]} (sloupec {i+1})")
                    if st.button("Načíst ceník", type="primary"):
                        price_map = {}
                        for row in rows:
                            if id_col < len(row) and price_col < len(row) and row[id_col]:
                                price_map[normalize_id(row[id_col])] = row[price_col].replace(" ", "").replace(",", ".")
                        ss.external_prices = price_map
                        ss._ext_ver = ss.get("_ext_ver", 0) + 1
                        st.success(f"Načteno {len(price_map)} cen z ceníku.")
                        st.rerun()
            if ss.external_prices:
                st.caption(f"Aktuálně načteno **{len(ss.external_prices)}** cen v paměti.")
            prof["price"] = {"mode": "external", "match_key": match_key}

        st.markdown("#### Hmotnost a rozměry (nepovinné) → LOGISTIC")
        wcfg = prof["weight"]
        weight_mode = st.radio("Zdroj hmotnosti/rozměrů", ["Nevyplňovat", "Přímé pole (jen hmotnost)", "FEATURE podle názvu (BMEcat styl)"],
                                index={"none": 0, "direct": 1, "feature": 2}.get(wcfg["mode"], 0), horizontal=True)
        if weight_mode == "Nevyplňovat":
            prof["weight"] = {"mode": "none"}
        elif weight_mode == "Přímé pole (jen hmotnost)":
            sel = tag_select("Zdrojový tag hmotnosti", wcfg.get("source", ""), "weight_src")
            prof["weight"] = {"mode": "direct", "source": "" if sel == "-- nevybráno --" else sel}
        else:
            with st.expander("Nastavení FEATURE hmotnosti a rozměrů", expanded=True):
                st.caption(
                    "Shoptet používá pod LOGISTIC elementy WEIGHT / DEPTH / WIDTH / HEIGHT (LENGTH není "
                    "povolen). Rozměry appka přepočítá na cm (mm → cm), hmotnost z gramů na kg."
                )
                fe = st.text_input("Element vlastnosti", value=wcfg.get("feature_element", "FEATURE"))
                fn = st.text_input("Pole s názvem", value=wcfg.get("fname_field", "FNAME"))
                fv = st.text_input("Pole s hodnotou", value=wcfg.get("fvalue_field", "FVALUE"))
                fu = st.text_input("Pole s jednotkou", value=wcfg.get("funit_field", "FUNIT"))
                names = st.text_input("Název(y) = HMOTNOST (odděl čárkou)", value=wcfg.get("names", "Hmotnost, Weight"))
                c1, c2, c3 = st.columns(3)
                with c1:
                    width_names = st.text_input("Název(y) = ŠÍŘKA (nepovinné)", value=wcfg.get("width_names", ""))
                with c2:
                    height_names = st.text_input("Název(y) = VÝŠKA (nepovinné)", value=wcfg.get("height_names", ""))
                with c3:
                    length_names = st.text_input("Název(y) = HLOUBKA/DÉLKA → DEPTH (nepovinné)", value=wcfg.get("length_names", ""))
            prof["weight"] = {"mode": "feature", "feature_element": fe, "fname_field": fn, "fvalue_field": fv,
                               "funit_field": fu, "names": names, "width_names": width_names,
                               "height_names": height_names, "length_names": length_names}

    render_live_preview()
    nav_buttons()

# =============================================================================
# KROK 4 - KATEGORIE
# =============================================================================
elif step == 4:
    st.header(STEPS[3])
    prof = ss.profile
    ccfg = prof["category"]
    st.write("Necháváš produkty roztřídit do vlastního stromu kategorií ručně, takže nejpohodlnější "
             "je poslat je všechny do jedné (klidně skryté) kategorie typu 'Nezařazeno'.")
    mode = st.radio("Způsob nastavení kategorie", ["Pevná hodnota pro všechny produkty", "Vzít ze zdrojového pole"],
                     index=0 if ccfg["mode"] == "fixed" else 1)
    if mode.startswith("Pevná"):
        fixed = st.text_input("Kategorie pro všechny produkty", value=ccfg.get("fixed_value", "Nezařazeno"))
        prof["category"] = {"mode": "fixed", "fixed_value": fixed}
    else:
        root = ss.raw_root
        sample_items = root.xpath(f"//*[local-name()='{prof['item_tag']}']") if prof.get("item_tag") else []
        avail_tags = aggregate_descendant_tags(sample_items) if sample_items else []
        tag_options = ["-- nevybráno --"] + avail_tags
        cur = ccfg.get("source", "")
        idx = tag_options.index(cur) if cur in tag_options else 0
        sel = st.selectbox("Zdrojový tag kategorie", tag_options, index=idx)
        prof["category"] = {"mode": "direct", "source": "" if sel == "-- nevybráno --" else sel}

    render_live_preview()
    nav_buttons()

# =============================================================================
# KROK 5 - PARAMETRY
# =============================================================================
elif step == 5:
    st.header(STEPS[4])
    st.write(
        "Appka nejdřív navrhne, který tag v jedné 'vlastnosti' produktu (např. `<FEATURE>`) funguje jako "
        "rozlišovač druhu parametru (marketingový text / technický parametr / velikost), a najde jeho "
        "reálné hodnoty ve feedu. Ty pak každé hodnotě přiřadíš, co se s ní má stát - appka nic neuhodne napevno."
    )
    root = ss.raw_root
    prof = ss.profile
    item_tag = prof.get("item_tag", "")
    sample_items = root.xpath(f"//*[local-name()='{item_tag}']") if item_tag else []

    if not sample_items:
        st.warning("Nejdřív nastav element produktu v kroku 2 nebo 3.")
    else:
        pcfg = prof["params"]
        repeat_candidates = repeat_children_candidates(sample_items[0])
        repeat_names = [f"{t} ({c}×)" for t, c in repeat_candidates]
        repeat_map = {f"{t} ({c}×)": t for t, c in repeat_candidates}
        default_idx = next((i for i, (t, c) in enumerate(repeat_candidates) if t == pcfg.get("repeat_element")), 0)
        chosen_repeat = st.selectbox("Element jedné 'vlastnosti' produktu (např. FEATURE)",
                                      ["-- žádné parametry v tomto feedu --"] + repeat_names,
                                      index=default_idx + 1 if repeat_names else 0)
        if chosen_repeat == "-- žádné parametry v tomto feedu --":
            pcfg["repeat_element"] = ""
        else:
            pcfg["repeat_element"] = repeat_map.get(chosen_repeat, "")

        if pcfg["repeat_element"]:
            repeat_els = []
            for si in sample_items[:30]:
                repeat_els.extend(si.xpath(f".//*[local-name()='{pcfg['repeat_element']}']"))
            field_options = aggregate_descendant_tags(repeat_els) if repeat_els else []
            field_options = ["-- nevybráno --"] + sorted(set(field_options))

            def fsel(label, current, key):
                idx = field_options.index(current) if current in field_options else 0
                return st.selectbox(label, field_options, index=idx, key=key)

            c1, c2, c3 = st.columns(3)
            with c1:
                sel = fsel("Pole s NÁZVEM parametru (FNAME)", pcfg.get("fname_field", ""), "p_fname")
                pcfg["fname_field"] = "" if sel == "-- nevybráno --" else sel
            with c2:
                sel = fsel("Pole s HODNOTOU (FVALUE)", pcfg.get("fvalue_field", ""), "p_fvalue")
                pcfg["fvalue_field"] = "" if sel == "-- nevybráno --" else sel
            with c3:
                sel = fsel("Pole s JEDNOTKOU (nepovinné)", pcfg.get("funit_field", ""), "p_funit")
                pcfg["funit_field"] = "" if sel == "-- nevybráno --" else sel

            suggested = classifier_field_candidates(root, item_tag, pcfg["repeat_element"])
            scope_labels = {"parent": " (skupina vlastností - rodič)", "self": " (uvnitř jedné vlastnosti)"}
            classifier_options = [("none", "")] + suggested
            option_labels = ["-- neklasifikovat (nic negenerovat) --"] + \
                [f"{f}{scope_labels[s]}" for s, f in suggested]
            cur_scope, cur_field = pcfg.get("classifier_scope", ""), pcfg.get("classifier_field", "")
            cur_idx = next((i for i, (s, f) in enumerate(classifier_options) if s == cur_scope and f == cur_field), 0)
            chosen_idx = st.selectbox(
                "Rozlišovací pole (podle čeho appka pozná druh parametru) - navržené kandidáty jsou nahoře, "
                "'skupina vlastností' je pole nad celým blokem vlastností (typické pro Metabo apod.), "
                "'uvnitř vlastnosti' je pole přímo u jedné hodnoty (typické pro Bosch FDESCR)",
                range(len(option_labels)), index=cur_idx, format_func=lambda i: option_labels[i],
            )
            pcfg["classifier_scope"], pcfg["classifier_field"] = classifier_options[chosen_idx]

            if pcfg["classifier_field"]:
                values = get_classifier_values(root, item_tag, pcfg["repeat_element"], pcfg["classifier_scope"], pcfg["classifier_field"])
                st.markdown("#### Přiřaď nalezeným hodnotám, co se s nimi má stát")
                bucket_labels = {
                    "ignore": "Ignorovat",
                    "marketing": "Marketingový text → do popisu",
                    "packaging": "Obsah balení → úplný konec DESCRIPTION",
                    "technical": "Technický parametr → INFORMATION_PARAMETERS",
                    "size": "Velikost → PARAMETERS",
                }
                bucket_keys = list(bucket_labels.keys())
                for val, count in values:
                    default_bucket = pcfg["value_map"].get(val, guess_bucket(val))
                    sel = st.selectbox(
                        f"„{val}" + f"“  ({count}×)",
                        bucket_keys, index=bucket_keys.index(default_bucket),
                        format_func=lambda k: bucket_labels[k], key=f"bucket_{val}",
                    )
                    pcfg["value_map"][val] = sel

                if any(b == "marketing" for b in pcfg["value_map"].values()):
                    target = st.radio("Kam zapsat marketingové texty?",
                                       ["Do SHORT_DESCRIPTION", "Na konec DESCRIPTION (jako odrážky)"],
                                       index=0 if pcfg.get("marketing_target", "SHORT_DESCRIPTION") == "SHORT_DESCRIPTION" else 1)
                    pcfg["marketing_target"] = "SHORT_DESCRIPTION" if target.startswith("Do") else "DESCRIPTION"

                    mk_names = {}
                    for si in sample_items[:30]:
                        for feat in si.xpath(f".//*[local-name()='{pcfg['repeat_element']}']"):
                            par = feat.getparent()
                            cv = (gtext(par, pcfg["classifier_field"]) if pcfg.get("classifier_scope") == "parent" and par is not None
                                  else gtext(feat, pcfg["classifier_field"]))
                            if pcfg["value_map"].get(cv) == "marketing":
                                fn = gtext(feat, pcfg["fname_field"])
                                if fn:
                                    mk_names[fn] = mk_names.get(fn, 0) + 1
                    if mk_names:
                        st.markdown("#### Co z marketingových textů použít v popisu")
                        st.caption("Marketingová skupina často obsahuje i věci, které do popisu nepatří (data, řada značky, "
                                   "hotspoty). Přiřaď každému poli, co s ním. Výchozí: jen 'Výhody' jako odrážky a "
                                   "'Rozsah dodávky' jako obsah balení, zbytek se ignoruje.")
                        roles = pcfg.setdefault("marketing_roles", {})
                        role_keys = list(MARKETING_ROLES.keys())
                        for fn, cnt in sorted(mk_names.items(), key=lambda x: -x[1]):
                            cur_role = roles.get(fn, guess_marketing_role(fn))
                            roles[fn] = st.selectbox(f"„{fn}“ ({cnt}×)", role_keys, index=role_keys.index(cur_role),
                                                     format_func=lambda k: MARKETING_ROLES[k], key=f"mrole_{fn}")

        with st.expander("📝 Formátování DESCRIPTION (struktura popisu, obsah balení na konci, kontrola duplicit)", expanded=False):
            dfcfg = prof.get("description_format", {})
            dfcfg["enabled"] = st.checkbox("Zapnout formátování popisu", value=dfcfg.get("enabled", False))
            if dfcfg["enabled"]:
                c1, c2 = st.columns(2)
                with c1:
                    dfcfg["paragraphs"] = st.checkbox("Rozdělit do odstavců", value=dfcfg.get("paragraphs", True))
                    dfcfg["headings"] = st.checkbox("Přidat nadpisy (Popis / Vlastnosti / Obsah balení)", value=dfcfg.get("headings", True))
                with c2:
                    dfcfg["extract_packaging"] = st.checkbox("Věty o balení z textu přesunout do 'Obsah balení'",
                                                               value=dfcfg.get("extract_packaging", True))
                    dfcfg["bold_numbers"] = st.checkbox("Zvýraznit klíčová čísla tučně (2 000 W, 0,6 kg...)",
                                                          value=dfcfg.get("bold_numbers", True))
            dfcfg["dedupe"] = st.checkbox("Kontrolovat duplicity (odrážky a věty, které už v popisu jsou, vynechat)",
                                           value=dfcfg.get("dedupe", True))
            if dfcfg["dedupe"]:
                dfcfg["dedupe_level"] = st.slider("Citlivost kontroly duplicit (% shodných slov)", 40, 100,
                                                   int(dfcfg.get("dedupe_level", 50)),
                                                   help="Nižší = agresivnější (vyhodí i podobné odrážky), vyšší = jen téměř shodné.")
            st.caption("Pořadí popisu: text → Vlastnosti (odrážky) → Obsah balení (vždy úplně na konci). "
                       "Výsledek uvidíš v živém náhledu XML níže.")
            prof["description_format"] = dfcfg

    render_live_preview()
    nav_buttons()

# =============================================================================
# KROK 6 - OBRÁZKY
# =============================================================================
elif step == 6:
    st.header(STEPS[5])
    st.info(
        "**Doporučený postup:** nahraj obrázky do **Shoptet Správce souborů** (Administrace → Vzhled → "
        "Správce souborů). Shoptet si při importu obrázek stáhne, uloží na vlastní disk a připraví "
        "standardizované velikosti - obrázky pak žijou přímo v Shoptetu, ne u tebe. Jakmile jsou nahrané, "
        "zkopíruj URL té složky sem."
    )
    root = ss.raw_root
    prof = ss.profile
    item_tag = prof.get("item_tag", "")
    sample_items = root.xpath(f"//*[local-name()='{item_tag}']") if item_tag else []
    icfg = prof["images"]

    if sample_items:
        img_mode = st.radio("Zdroj obrázků ve vstupním feedu", ["Nevyplňovat", "Přímé pole (URL/název souboru)", "MIME_INFO/MIME (BMEcat styl)"],
                             index={"none": 0, "direct": 1, "mime": 2}.get(icfg["mode"], 0), horizontal=True)
        max_images = st.number_input("Max. počet obrázků na produkt (0 = bez omezení)", min_value=0,
                                      value=icfg.get("max_images", 8) or 0, step=1)
        add_desc = st.checkbox("Přidat atribut description (alt text) - Shoptet ho u obrázků používá",
                                value=icfg.get("add_description", True))
        avail_tags = aggregate_descendant_tags(sample_items)
        tag_options = ["-- nevybráno --"] + avail_tags

        def tag_select(label, current, key):
            idx = tag_options.index(current) if current in tag_options else 0
            return st.selectbox(label, tag_options, index=idx, key=key)

        preserved = {k: icfg.get(k) for k in ("public_base_url",) if icfg.get(k)}
        if img_mode == "Nevyplňovat":
            new_cfg = {"mode": "none", "max_images": max_images or None}
        elif img_mode.startswith("Přímé"):
            sel = tag_select("Zdrojový tag obrázku (může se opakovat)", icfg.get("source", ""), "img_src")
            new_cfg = {"mode": "direct", "source": "" if sel == "-- nevybráno --" else sel, "max_images": max_images or None}
        else:
            with st.expander("Nastavení MIME obrázků", expanded=True):
                me = st.text_input("Element jednoho obrázku", value=icfg.get("mime_element", "MIME"))
                pf = st.text_input("Pole účelu (prázdné = nefiltrovat)", value=icfg.get("purpose_field", "MIME_PURPOSE"))
                pv = st.text_input("Požadovaná hodnota účelu", value=icfg.get("purpose_value", "normal"))
                sf = st.text_input("Pole s názvem souboru", value=icfg.get("source_field", "MIME_SOURCE"))
            new_cfg = {"mode": "mime", "mime_element": me, "purpose_field": pf, "purpose_value": pv,
                       "source_field": sf, "max_images": max_images or None}
        new_cfg["add_description"] = add_desc
        new_cfg.update(preserved)
        prof["images"] = new_cfg
        icfg = prof["images"]

    st.markdown("---")
    shop_preview, _ = compute_shop()
    image_els = shop_preview.xpath(".//*[local-name()='IMAGE']") if shop_preview is not None else []
    raw_count = sum(1 for e in image_els if e.text and not e.text.strip().lower().startswith(("http://", "https://")))
    st.write(f"Obrázků celkem: **{len(image_els)}**, z toho bez platné URL: **{raw_count}**")
    st.caption(
        "Adresa cdn.myshoptet.com se vygeneruje sama až PO importu - do feedu nedávej vymyšlenou "
        "cdn.myshoptet.com URL, ale skutečnou veřejně dostupnou adresu, kam jsi obrázky přes FTP nahrál/-a."
    )
    base_url = st.text_input(
        "URL složky, kam jsi obrázky nahrál/-a přes FTP (appka připojí originální název souboru)",
        value=icfg.get("public_base_url") or "https://TVUJ-ESHOP.cz/...",
    )
    if st.button("Aplikovat URL na všechny obrázky bez platné adresy", type="primary"):
        prof["images"]["public_base_url"] = base_url
        st.success("URL složky uložena a použita na obrázky bez platné adresy.")
        st.rerun()

    st.markdown("#### 🔍 Test dostupnosti (ověří, jestli URL reálně vedou na obrázek)")
    if st.button("Zkontrolovat prvních pár obrázkových URL"):
        import requests
        shop_check, _ = compute_shop()
        check_urls = [e.text for e in shop_check.xpath(".//*[local-name()='IMAGE']")[:5] if e.text]
        for u in check_urls:
            try:
                r = requests.head(u, timeout=6, allow_redirects=True)
                ok = r.status_code == 200 and "image" in r.headers.get("Content-Type", "")
                (st.success if ok else st.error)(f"{r.status_code} — {u}")
            except Exception as e:
                st.error(f"Nedostupné ({e}) — {u}")

    with st.expander("🎥 Videa", expanded=False):
        st.caption(
            "Shoptet bere u videa jen KÓD YouTube videa: `<RELATED_VIDEO><YOUTUBE_VIDEO_CODE>kód</YOUTUBE_VIDEO_CODE>` "
            "(volitelně s `<TEXT>`). Appka kód vytáhne z odkazu (youtube.com/watch?v=..., youtu.be/..., embed, shorts). "
            "Odkazy mimo YouTube se přeskočí. Název obalového elementu (výchozí `RELATED_VIDEOS`) je můj odhad - "
            "validátor ti případně řekne, jak se jmenuje správně."
        )
        vcfg = prof.get("videos", {"mode": "none"})
        video_on = st.checkbox("Zahrnout videa z feedu (odkazy typu URL s popiskem 'video' nebo z YouTube)",
                                value=vcfg.get("mode") == "mime")
        if video_on:
            c1, c2 = st.columns(2)
            with c1:
                wrapper_tag = st.text_input("Obalovací element videí", value=vcfg.get("wrapper_tag") or "RELATED_VIDEOS")
            with c2:
                add_text = st.checkbox("Přidat <TEXT> (popisek videa = název produktu)", value=vcfg.get("add_text", False))
            prof["videos"] = {"mode": "mime", "mime_element": vcfg.get("mime_element", "MIME"),
                               "type_field": vcfg.get("type_field", "MIME_TYPE"), "type_value": vcfg.get("type_value", "url"),
                               "descr_field": vcfg.get("descr_field", "MIME_DESCR"), "descr_match": vcfg.get("descr_match", "video"),
                               "source_field": vcfg.get("source_field", "MIME_SOURCE"),
                               "wrapper_tag": wrapper_tag, "add_text": add_text}
        else:
            prof["videos"] = {"mode": "none"}

    with st.expander("🔗 Související a podobné produkty", expanded=False):
        st.caption("Kódy odkazují na jiné produkty podle jejich CODE (Shoptet: `<RELATED_PRODUCTS><CODE>` a "
                   "`<ALTERNATIVE_PRODUCTS><CODE>`). Appka se zeptá, kde ve feedu odkazy na jiné produkty jsou.")
        rcfg = prof.get("references", {})
        item_tag_ref = prof.get("item_tag", "")
        sample_items_ref = ss.raw_root.xpath(f"//*[local-name()='{item_tag_ref}']")[:50] if item_tag_ref else []
        rcfg["enabled"] = st.checkbox("Zahrnout související/podobné produkty", value=rcfg.get("enabled", False))
        if rcfg["enabled"] and sample_items_ref:
            cnt, kids = {}, {}
            for it_ in sample_items_ref:
                for el_ in it_.iter():
                    if el_ is it_ or not isinstance(el_.tag, str) or len(el_) == 0:
                        continue
                    t_ = local_tag(el_)
                    cnt[t_] = cnt.get(t_, 0) + 1
            ref_like = sorted([t for t in cnt if "REFER" in t.upper() or "RELATED" in t.upper() or "CROSS" in t.upper()],
                              key=lambda t: -cnt[t])
            other = sorted([t for t in cnt if t not in ref_like], key=lambda t: -cnt[t])
            elem_opts = ref_like + other
            cur_el = rcfg.get("reference_element", "")
            if cur_el not in elem_opts:
                cur_el = elem_opts[0] if elem_opts else ""
            if not elem_opts:
                st.info("U produktů jsem nenašel žádný element s odkazy.")
            else:
                rcfg["reference_element"] = st.selectbox(
                    "Element jednoho odkazu na jiný produkt", elem_opts, index=elem_opts.index(cur_el),
                    format_func=lambda t: f"{t} ({cnt[t]}×)" + ("  ← nejspíš tohle" if t in ref_like[:1] else ""))
                ref_els = []
                for it_ in sample_items_ref:
                    ref_els.extend(it_.xpath(f".//*[local-name()='{rcfg['reference_element']}']"))
                attr_names = sorted({k for e in ref_els for k in e.attrib})
                child_tags = aggregate_descendant_tags(ref_els) if ref_els else []
                c1, c2 = st.columns(2)
                with c1:
                    id_guess = next((t for t in child_tags if re.search(r"(ART|PROD|SUPPLIER).*ID|ID_TO|_TO$|AID", t, re.I)),
                                    child_tags[0] if child_tags else "")
                    cur_id = rcfg.get("id_field") if rcfg.get("id_field") in child_tags else id_guess
                    rcfg["id_field"] = st.selectbox("Pole s KÓDEM odkazovaného produktu", child_tags,
                                                    index=child_tags.index(cur_id) if cur_id in child_tags else 0) if child_tags else ""
                with c2:
                    type_opts = ["-- bez rozlišení typu --"] + [f"atribut: {a}" for a in attr_names] + \
                                [f"pole: {t}" for t in child_tags if t != rcfg.get("id_field")]
                    tm, ta, tf = rcfg.get("type_mode", "attr"), rcfg.get("type_attr", "type"), rcfg.get("type_field", "")
                    cur_t = ("-- bez rozlišení typu --" if tm == "none" else
                             f"atribut: {ta}" if tm == "attr" else f"pole: {tf}")
                    if cur_t not in type_opts:
                        cur_t = type_opts[1] if len(type_opts) > 1 else type_opts[0]
                    pick = st.selectbox("Podle čeho poznat TYP odkazu", type_opts, index=type_opts.index(cur_t))
                    if pick.startswith("atribut: "):
                        rcfg["type_mode"], rcfg["type_attr"] = "attr", pick[9:]
                    elif pick.startswith("pole: "):
                        rcfg["type_mode"], rcfg["type_field"] = "field", pick[6:]
                    else:
                        rcfg["type_mode"] = "none"
                types_found = {}
                for e in ref_els:
                    if rcfg["type_mode"] == "attr":
                        v_ = e.get(rcfg.get("type_attr", "type"))
                    elif rcfg["type_mode"] == "field":
                        v_ = gtext_deep(e, rcfg.get("type_field", ""))
                    else:
                        v_ = None
                    if v_:
                        types_found[v_] = types_found.get(v_, 0) + 1
                if rcfg["type_mode"] != "none":
                    type_list = sorted(types_found, key=lambda k: -types_found[k])
                    rcfg["included_types"] = st.multiselect(
                        "Které typy zahrnout", type_list,
                        default=[t for t in (rcfg.get("included_types") or type_list) if t in type_list],
                        format_func=lambda t: f"{t} ({types_found[t]}×)")
                    rcfg["alt_types"] = st.multiselect(
                        "…z nich tyto dej do ALTERNATIVE_PRODUCTS (podobné) místo související", rcfg["included_types"],
                        default=[t for t in rcfg.get("alt_types", []) if t in rcfg["included_types"]])
                c1, c2 = st.columns(2)
                with c1:
                    rcfg["wrapper_tag"] = st.text_input("Obalovací tag", value=rcfg.get("wrapper_tag", "RELATED_PRODUCTS"))
                with c2:
                    rcfg["item_tag"] = st.text_input("Tag jednoho kódu", value=rcfg.get("item_tag", "CODE"))
                rcfg["only_existing"] = st.checkbox(
                    "Vynechat odkazy na kódy, které nejsou v tomto (vybraném) feedu",
                    value=rcfg.get("only_existing", False),
                    help="Shoptet ignoruje odkaz na neexistující produkt. Zapni, když chceš do feedu jen odkazy mezi produkty, "
                         "které importuješ společně. Vypni, pokud už jsou odkazované produkty (např. příslušenství) v eshopu z jiného importu.")
                # kontrola, kolik odkazovaných kódů je ve feedu
                code_src_ = prof["fields"].get("CODE", {}).get("source", "")
                if code_src_ and rcfg.get("id_field"):
                    feed_codes_ = {gtext_deep(i_, code_src_) for i_ in ss.raw_root.xpath(f"//*[local-name()='{item_tag_ref}']")}
                    ids_ = {gtext_deep(e, rcfg["id_field"]) for e in ref_els}
                    ids_.discard(None)
                    st.caption(f"Ve vzorku je {len(ids_)} různých odkazovaných kódů, z toho {len(ids_ & feed_codes_)} je přímo v tomto feedu.")
                if ref_els:
                    e0 = ref_els[0]
                    st.caption(f"Ukázka prvního odkazu: kód `{gtext_deep(e0, rcfg['id_field']) if rcfg['id_field'] else '?'}`"
                               + (f", typ `{e0.get(rcfg['type_attr'])}`" if rcfg['type_mode'] == 'attr' else ""))
        prof["references"] = rcfg

    render_live_preview()
    nav_buttons()

# =============================================================================
# KROK 7 - KONTROLA A RUČNÍ OPRAVY
# =============================================================================
elif step == 7:
    st.header(STEPS[6])
    prof = ss.profile

    with st.expander("🏷️ Přidat text k názvu produktu (např. značku, pokud v NAME chybí)", expanded=False):
        st.caption("Aplikuje se na všechny produkty najednou, přímo v <NAME>.")
        c1, c2 = st.columns(2)
        with c1:
            prof["name_prefix"] = st.text_input("Text PŘED název (prefix)", value=prof.get("name_prefix", ""))
        with c2:
            prof["name_suffix"] = st.text_input("Text ZA název (suffix)", value=prof.get("name_suffix", ""))

    with st.expander("💰 Hromadná úprava ceny (NAŠE SLEVA + DPH)", expanded=False):
        prec_labels = [
            "Max. 2 desetinná místa (doporučeno - validátor Shoptetu povoluje nejvýš 2)",
            "Celá čísla (pozor: Shoptet k ceně sám přičte DPH, takže cena s DPH pak nemusí být celé číslo)",
        ]
        prec_choice = st.radio(
            "Přesnost <PRICE> a <STANDARD_PRICE> v exportu", prec_labels,
            index=0 if prof.get("price_precision", "2dp") == "2dp" else 1,
        )
        prof["price_precision"] = "2dp" if prec_choice == prec_labels[0] else "int"
        st.markdown("---")
        pa = prof.get("price_adjust", {"enabled": False, "source_mode": "net", "discount_percent": 0, "vat_percent": "21"})
        pa["enabled"] = st.checkbox("Zapnout přepočet ceny (cena s DPH na celé číslo + naše sleva)", value=pa.get("enabled", False))
        if pa["enabled"]:
            st.caption(
                "Cílem je celé číslo jako výsledná zobrazená cena s DPH v Shoptetu. Shoptet si k <PRICE> "
                "DPH dopočítává VŽDY sám (bere <PRICE> jako cenu bez DPH), takže appka do <PRICE> a "
                "<STANDARD_PRICE> zapíše cenu BEZ DPH - vypočítanou tak, aby po Shoptetově přičtení DPH "
                "vyšla co nejpřesněji kulatá částka. Pokud je zadaná i naše sleva, cena PŘED slevou jde do "
                "<STANDARD_PRICE> (přeškrtnutá cena v Shoptetu) a cena PO slevě do <PRICE>."
            )
            c1, c2 = st.columns(2)
            with c1:
                mode_choice = st.radio(
                    "Zdrojová cena z feedu je",
                    ["Bez DPH", "S DPH (např. z ceníku)"],
                    index=0 if pa.get("source_mode", "net") == "net" else 1,
                )
                pa["source_mode"] = "net" if mode_choice == "Bez DPH" else "gross"
                pa["vat_percent"] = st.text_input(
                    "Sazba DPH (%) - textově, ať jde snadno změnit při změně legislativy",
                    value=str(pa.get("vat_percent", "21")),
                )
            with c2:
                pa["discount_percent"] = st.number_input(
                    "NAŠE SLEVA (%) - odečte se od ceny s DPH", min_value=0.0, max_value=100.0,
                    value=float(pa.get("discount_percent", 0) or 0), step=0.5,
                )

        prof["price_adjust"] = pa

        if pa["enabled"]:
            shop_preview, _ = compute_shop()
            first_price = first_standard = None
            if shop_preview is not None:
                pe = shop_preview.xpath(".//*[local-name()='SHOPITEM'][1]/*[local-name()='PRICE']")
                if pe and pe[0].text:
                    first_price = pe[0].text
                std = shop_preview.xpath(".//*[local-name()='SHOPITEM'][1]/*[local-name()='STANDARD_PRICE']")
                first_standard = std[0].text if std and std[0].text else None
            if first_price:
                vat_frac = 1 + float(str(pa.get("vat_percent", "21")).replace(",", ".")) / 100.0
                displayed = float(first_price) * vat_frac
                line = f"Do feedu se zapíše `<PRICE>` = **{first_price}** (bez DPH) → v Shoptetu vyjde **{displayed:.2f} {prof.get('currency', '')}**"
                if first_standard:
                    displayed_std = float(first_standard) * vat_frac
                    line += f" (přeškrtnutá: ~~{displayed_std:.2f} {prof.get('currency', '')}~~)"
                st.caption(line)
                all_prices = [float(e.text) for e in shop_preview.xpath("./*[local-name()='SHOPITEM']/*[local-name()='PRICE']") if e.text]
                off = sum(1 for pv in all_prices if abs(round(pv * vat_frac, 2) - round(pv * vat_frac)) > 0.001)
                if all_prices:
                    st.caption(f"Cena s DPH vyjde přesně na celé číslo u **{len(all_prices) - off} z {len(all_prices)}** produktů "
                               f"(u zbylých {off} je to o haléř vedle - víc než 2 desetinná místa v <PRICE> validátor nepřijme).")

    with st.expander("📦 Viditelnost zásoby (dostupnost)", expanded=False):
        st.caption("Aplikuje se hromadně na všechny produkty.")
        av = prof.get("availability", {"enabled": False, "in_stock": "Skladem", "out_of_stock": "2-3 dny"})
        av["enabled"] = st.checkbox("Nastavit dostupnost hromadně", value=av.get("enabled", False))
        if av["enabled"]:
            c1, c2 = st.columns(2)
            with c1:
                av["in_stock"] = st.text_input(
                    "Text, když je SKLADEM (AVAILABILITY_IN_STOCK)", value=av.get("in_stock", "Skladem"),
                )
            with c2:
                idx = OUT_OF_STOCK_OPTIONS.index(av.get("out_of_stock")) if av.get("out_of_stock") in OUT_OF_STOCK_OPTIONS else 0
                av["out_of_stock"] = st.selectbox(
                    "Text, když NENÍ skladem (AVAILABILITY_OUT_OF_STOCK) - musí být jedna z předdefinovaných Shoptet hodnot",
                    OUT_OF_STOCK_OPTIONS, index=idx,
                )
        prof["availability"] = av

    shop, _ = compute_shop()
    issues = validate_shop(shop) if shop is not None else []
    n_mand = sum(1 for i in issues if i["mandatory"])
    n_opt = len(issues) - n_mand
    c1, c2, c3 = st.columns(3)
    c1.metric("Produktů", len(shop.xpath("./*[local-name()='SHOPITEM']")) if shop is not None else 0)
    c2.metric("🔴 Povinné chyby", n_mand)
    c3.metric("⚪ Doporučené", n_opt)

    if not issues:
        st.success("🎉 Feed neobsahuje žádné chybějící povinné ani doporučené elementy.")
    else:
        st.write("Nejdřív **povinné** chyby (musí se opravit, jinak Shoptet feed odmítne), pak doporučené; "
                  "uvnitř skupiny sestupně podle pozice v XML.")
        grouped = {}
        for i in issues:
            grouped.setdefault(i["tag"], []).append(i)
        group_order = sorted(grouped.keys(), key=lambda t: (not grouped[t][0]["mandatory"], -max(x["doc_pos"] for x in grouped[t])))
        for tag in group_order:
            group = grouped[tag]
            mand = group[0]["mandatory"]
            label = f"{'🔴 POVINNÉ' if mand else '⚪ Doporučené'} — <{tag}> chybí u {len(group)} produktů"
            with st.expander(label, expanded=mand):
                bulk_value = st.text_input(f"Hromadná hodnota pro všechny chybějící <{tag}>", key=f"bulk_{tag}")
                if st.button("Použít na všechny", key=f"apply_bulk_{tag}") and bulk_value:
                    for g in group:
                        ss.overrides.setdefault(g["code"], {})[tag] = bulk_value
                    st.success(f"Nastaveno u {len(group)} produktů.")
                    st.rerun()
                df = pd.DataFrame([{"CODE": g["code"], "NAME": g["name"], "Nová hodnota": ss.overrides.get(g["code"], {}).get(tag, "")} for g in group])
                edited = st.data_editor(df, key=f"editor_{tag}", use_container_width=True, disabled=["CODE", "NAME"])
                if st.button("Uložit individuální hodnoty výše", key=f"apply_ind_{tag}"):
                    cnt = 0
                    for _, row in edited.iterrows():
                        if row["Nová hodnota"]:
                            ss.overrides.setdefault(row["CODE"], {})[tag] = row["Nová hodnota"]
                            cnt += 1
                    if cnt:
                        st.success(f"Uloženo {cnt} hodnot.")
                        st.rerun()

    render_live_preview()
    nav_buttons()

# =============================================================================
# KROK 8 - EXPORT
# =============================================================================
elif step == 8:
    st.header(STEPS[7])
    shop, _ = compute_shop()
    export_shop = shop if shop is not None else etree.Element("SHOP")
    n_items = len(export_shop.xpath("./*[local-name()='SHOPITEM']"))
    st.caption(f"Do exportu jde {n_items} produktů (výběr z kroku 2 se uplatnil už na začátku).")

    issues = validate_shop(export_shop)
    n_mand = sum(1 for i in issues if i["mandatory"])
    if n_mand:
        st.error(f"⚠️ Pozor: export obsahuje {n_mand} povinných chyb - vrať se do kroku 7 a doplň je.")
    else:
        st.success("Export prochází kontrolou povinných elementů.")

    xml_bytes = shop_to_bytes(export_shop)
    st.download_button("📥 Stáhnout výsledný Shoptet XML feed", data=xml_bytes, file_name="shoptet_feed.xml",
                        mime="application/xml", type="primary")
    st.download_button(
        "💾 Stáhnout šablonu (.json) pro příští použití",
        data=json.dumps(ss.profile, ensure_ascii=False, indent=2).encode("utf-8"),
        file_name=f"{ss.profile.get('profile_name', 'sablona').replace(' ', '_')}.json",
        mime="application/json",
    )
    if ss.mode == "url":
        render_job_export(export_shop)
    else:
        st.caption("💡 Chceš, aby se feed aktualizoval sám? V kroku 1 zvol režim „Aktualizovaný feed - stahování z URL“.")
    st.code(xml_bytes.decode("utf-8")[:20000], language="xml")
    if len(xml_bytes) > 20000:
        st.caption("(náhled zkrácen, stažený soubor je kompletní)")
    nav_buttons()
