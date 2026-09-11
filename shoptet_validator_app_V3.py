import streamlit as st
from lxml import etree
import pandas as pd
import json
from copy import deepcopy
from urllib.parse import quote

st.set_page_config(page_title="Univerzální Feed → Shoptet Validator", layout="wide")

# =============================================================================
# SLOVNÍK SHOPTET TAGŮ
# =============================================================================
SHOPTET_DICT = {
    "SHOP": {"desc": "Kořenový element celého feedu.", "mandatory": False},
    "SHOPITEM": {"desc": "Obalovací element jednoho produktu.", "mandatory": False},
    "CODE": {"desc": "Unikátní kód / SKU produktu v rámci e-shopu.", "mandatory": True},
    "NAME": {"desc": "Název produktu.", "mandatory": True},
    "SHORT_DESCRIPTION": {"desc": "Krátký popis.", "mandatory": False},
    "DESCRIPTION": {"desc": "Detailní popis produktu, podporuje HTML.", "mandatory": False},
    "MANUFACTURER": {"desc": "Značka / výrobce.", "mandatory": False},
    "EAN": {"desc": "EAN / čárový kód.", "mandatory": False},
    "PART_NUMBER": {"desc": "Katalogové číslo výrobce.", "mandatory": False},
    "WARRANTY": {"desc": "Délka záruky.", "mandatory": False},
    "CURRENCY": {"desc": "Měna ceny (CZK, EUR...).", "mandatory": True},
    "PRICE": {"desc": "Cena produktu.", "mandatory": True},
    "VAT": {"desc": "Sazba DPH v %.", "mandatory": False},
    "CATEGORIES": {"desc": "Obalovací element cest kategorií.", "mandatory": False},
    "CATEGORY": {"desc": "Jedna celá cesta kategorie (např. 'Nářadí > Aku nářadí').", "mandatory": True},
    "IMAGES": {"desc": "Obalovací element obrázků.", "mandatory": False},
    "IMAGE": {"desc": "Přímá veřejná URL adresa obrázku (musí začínat http/https).", "mandatory": False},
    "LOGISTIC": {"desc": "Obalovací element logistiky.", "mandatory": False},
    "WEIGHT": {"desc": "Hmotnost v kg.", "mandatory": False},
    "VISIBILITY": {"desc": "'visible' nebo 'hidden'.", "mandatory": False},
    "STOCK": {"desc": "Obalovací element skladu.", "mandatory": False},
    "AMOUNT": {"desc": "Počet kusů skladem.", "mandatory": False},
    "AVAILABILITY": {"desc": "Textová dostupnost.", "mandatory": False},
    "PARAMETERS": {"desc": "POUZE pro velikosti (oblečení/obuv). Pro běžné technické parametry NEPOUŽÍVAT.", "mandatory": False},
    "PARAMETER": {"desc": "Jeden parametr velikosti, obsahuje NAME a VALUE.", "mandatory": False},
    "INFORMATION_PARAMETERS": {"desc": "Obalovací element obecných/technických parametrů produktu.", "mandatory": False},
    "INFORMATION_PARAMETER": {"desc": "Jeden obecný/technický parametr, obsahuje NAME a VALUE.", "mandatory": False},
    "VALUE": {"desc": "Hodnota parametru.", "mandatory": False},
    "URL": {"desc": "Odkaz na produkt v e-shopu.", "mandatory": False},
}

DEFAULT_PROFILE = {
    "profile_name": "Nová šablona",
    "item_tag": "",
    "fields": {
        "CODE": {"mode": "direct", "source": ""},
        "NAME": {"mode": "direct", "source": ""},
        "DESCRIPTION": {"mode": "direct", "source": ""},
        "MANUFACTURER": {"mode": "direct", "source": ""},
        "EAN": {"mode": "direct", "source": ""},
        "PART_NUMBER": {"mode": "direct", "source": ""},
    },
    "price": {"mode": "direct", "source": "", "price_type_attr": "", "price_type_value": "net_list"},
    "currency": "CZK",
    "category": {"mode": "direct", "source": "", "sep": " > ", "link_element": "", "id_field": "", "group_id_field": "",
                 "struct_element": "", "group_id": "", "group_name": "", "parent_id": "", "root_type_value": "root"},
    "weight": {"mode": "none", "source": "", "feature_element": "", "fname_field": "", "fvalue_field": "", "funit_field": "", "names": "Hmotnost, Weight"},
    "images": {"mode": "direct", "source": "", "mime_element": "", "purpose_field": "", "purpose_value": "normal", "source_field": "", "max_images": 8},
    "params": {"mode": "none", "group_element": "", "group_name_field": "", "allowed_groups": "",
               "feature_element": "", "fname_field": "", "fvalue_field": "", "funit_field": ""},
    "visibility_default": "visible",
}


def gtext(el, tag):
    r = el.xpath(f"./*[local-name()='{tag}']")
    return r[0].text.strip() if r and r[0].text else None


def gtext_deep(el, tag):
    if not tag:
        return None
    r = el.xpath(f".//*[local-name()='{tag}']")
    return r[0].text.strip() if r and r[0].text else None


def gall_deep(el, tag):
    if not tag:
        return []
    return el.xpath(f".//*[local-name()='{tag}']")


def parse_xml_bytes(data):
    parser = etree.XMLParser(recover=True, huge_tree=True)
    return etree.fromstring(data, parser=parser)


def detect_feed_type(root):
    tag = root.tag.split("}")[-1]
    if tag == "SHOP":
        return "shoptet"
    return "other"


def local_tag(el):
    return el.tag.split("}")[-1] if isinstance(el.tag, str) else str(el.tag)


def candidate_item_tags(root, min_count=2):
    """Odhadne, který tag představuje 'jeden produkt'. Klíčový signál: skuteční
    'položky seznamu' mají SPOLEČNÉHO rodiče (jsou sourozenci pod jedním
    obalovacím elementem) - na rozdíl od vnořených podzáznamů (jako detail
    uvnitř jednoho produktu), které mají pokaždé jiného rodiče.
    Řadí primárně podle poměru (počet výskytů / počet různých rodičů),
    sekundárně podle počtu různých podřazených polí."""
    counts = {}
    parent_refs = {}
    child_tag_sets = {}
    for el in root.iter():
        t = local_tag(el)
        counts[t] = counts.get(t, 0) + 1
        parent = el.getparent()
        if parent is not None:
            parent_refs.setdefault(t, set()).add(parent)
        s = child_tag_sets.setdefault(t, set())
        for child in el:
            s.add(local_tag(child))

    candidates = []
    for t, c in counts.items():
        if c < min_count or len(child_tag_sets.get(t, set())) < 2 or t == local_tag(root):
            continue
        n_parents = max(len(parent_refs.get(t, set())), 1)
        siblings_ratio = c / n_parents
        if siblings_ratio < 1.5:
            continue  # není to opakující se POLOŽKA seznamu, spíš 1:1 vnořený podzáznam
        candidates.append((t, c, siblings_ratio, len(child_tag_sets.get(t, set()))))

    candidates.sort(key=lambda x: (-x[3], -x[2], -x[1]))
    return [(t, c) for t, c, _, _ in candidates]


def descendant_tags(item_el):
    tags = sorted(set(local_tag(e) for e in item_el.iter()))
    return [t for t in tags if t != local_tag(item_el)]


# =============================================================================
# KATEGORIE - obecný stromový resolver (funguje pro BMEcat i podobně strukturované feedy)
# =============================================================================
def build_tree_category_map(root, cfg):
    cats = {}
    for cs in root.xpath(f"//*[local-name()='{cfg['struct_element']}']"):
        gid = gtext(cs, cfg["group_id"])
        cats[gid] = {
            "name": gtext(cs, cfg["group_name"]),
            "parent": gtext(cs, cfg["parent_id"]),
            "type": cs.get("type"),
        }
    return cats


def tree_category_path(gid, cats, sep, root_type_value):
    chain, seen, cur = [], set(), gid
    while cur and cur in cats and cur not in seen:
        seen.add(cur)
        node = cats[cur]
        if node["type"] != root_type_value:
            chain.append(node["name"])
        cur = node["parent"]
    chain.reverse()
    return sep.join([c for c in chain if c])


def build_tree_links(root, cfg):
    m = {}
    for link in root.xpath(f"//*[local-name()='{cfg['link_element']}']"):
        art_id = gtext(link, cfg["id_field"])
        gid = gtext(link, cfg["group_id_field"])
        if art_id:
            m.setdefault(art_id, []).append(gid)
    return m


# =============================================================================
# APLIKACE PROFILU (ŠABLONY) NA VSTUPNÍ XML -> SHOP/SHOPITEM
# =============================================================================
def apply_profile(root, profile):
    log = []
    item_tag = profile["item_tag"]
    items_src = root.xpath(f"//*[local-name()='{item_tag}']") if item_tag else []
    log.append(f"Nalezeno {len(items_src)} produktů podle elementu <{item_tag}>.")

    shop = etree.Element("SHOP")

    cat_cfg = profile["category"]
    tree_cats, tree_links = {}, {}
    if cat_cfg["mode"] == "tree":
        tree_cats = build_tree_category_map(root, cat_cfg)
        tree_links = build_tree_links(root, cat_cfg)

    n_price_missing = n_cat_missing = n_img = n_params = 0

    for src in items_src:
        item = etree.SubElement(shop, "SHOPITEM")

        # jednoduchá pole
        for target, fcfg in profile["fields"].items():
            if fcfg["mode"] == "direct" and fcfg["source"]:
                val = gtext_deep(src, fcfg["source"])
                if val:
                    etree.SubElement(item, target).text = val

        code_val = gtext(item, "CODE")

        # cena
        pcfg = profile["price"]
        price_val = None
        if pcfg["mode"] == "direct" and pcfg["source"]:
            price_val = gtext_deep(src, pcfg["source"])
        elif pcfg["mode"] == "price_type" and pcfg["source"]:
            candidates = src.xpath(
                f".//*[local-name()='ARTICLE_PRICE'][@{pcfg['price_type_attr']}='{pcfg['price_type_value']}']"
            ) if pcfg["price_type_attr"] else src.xpath(".//*[local-name()='ARTICLE_PRICE']")
            if not candidates:
                candidates = src.xpath(".//*[local-name()='ARTICLE_PRICE']")
            if candidates:
                price_val = gtext_deep(candidates[0], pcfg["source"])
        if price_val:
            etree.SubElement(item, "CURRENCY").text = profile["currency"]
            try:
                etree.SubElement(item, "PRICE").text = f"{float(price_val.replace(',', '.')):.2f}"
            except ValueError:
                etree.SubElement(item, "PRICE").text = price_val
        else:
            n_price_missing += 1

        # kategorie
        paths = []
        if cat_cfg["mode"] == "direct" and cat_cfg["source"]:
            for e in gall_deep(src, cat_cfg["source"]):
                if e.text and e.text.strip():
                    paths.append(e.text.strip())
        elif cat_cfg["mode"] == "tree" and code_val:
            for gid in tree_links.get(code_val, []):
                p = tree_category_path(gid, tree_cats, cat_cfg["sep"], cat_cfg["root_type_value"])
                if p:
                    paths.append(p)
        if paths:
            cats_el = etree.SubElement(item, "CATEGORIES")
            for p in dict.fromkeys(paths):  # unikátní, zachovat pořadí
                etree.SubElement(cats_el, "CATEGORY").text = p
        else:
            n_cat_missing += 1

        # váha
        wcfg = profile["weight"]
        w = None
        if wcfg["mode"] == "direct" and wcfg["source"]:
            w = gtext_deep(src, wcfg["source"])
        elif wcfg["mode"] == "feature":
            names = set(n.strip().lower() for n in wcfg["names"].split(","))
            for feat in src.xpath(f".//*[local-name()='{wcfg['feature_element']}']"):
                fname = gtext(feat, wcfg["fname_field"])
                if fname and fname.strip().lower() in names:
                    val = gtext(feat, wcfg["fvalue_field"])
                    unit = (gtext(feat, wcfg["funit_field"]) or "kg").lower() if wcfg["funit_field"] else "kg"
                    if val:
                        try:
                            v = float(val.replace(",", "."))
                            if unit == "g":
                                v = v / 1000.0
                            w = str(round(v, 3))
                        except ValueError:
                            w = val
                    break
        if w:
            logistic = etree.SubElement(item, "LOGISTIC")
            etree.SubElement(logistic, "WEIGHT").text = w

        # obrázky
        icfg = profile["images"]
        images = []
        if icfg["mode"] == "direct" and icfg["source"]:
            images = [e.text.strip() for e in gall_deep(src, icfg["source"]) if e.text and e.text.strip()]
        elif icfg["mode"] == "mime":
            seen = set()
            for mime in src.xpath(f".//*[local-name()='{icfg['mime_element']}']"):
                purpose = gtext(mime, icfg["purpose_field"]) if icfg["purpose_field"] else None
                if icfg["purpose_field"] and icfg["purpose_value"] and purpose != icfg["purpose_value"]:
                    continue
                fsrc = gtext(mime, icfg["source_field"])
                if fsrc and fsrc not in seen:
                    seen.add(fsrc)
                    images.append(fsrc)
        if icfg.get("max_images"):
            images = images[: icfg["max_images"]]
        if images:
            n_img += len(images)
            images_el = etree.SubElement(item, "IMAGES")
            for f in images:
                etree.SubElement(images_el, "IMAGE").text = f

        # parametry -> vždy do INFORMATION_PARAMETERS (PARAMETERS je jen pro velikosti)
        pcfg2 = profile["params"]
        pairs = []
        if pcfg2["mode"] == "feature_groups":
            allowed = set(g.strip().lower() for g in pcfg2["allowed_groups"].split(",") if g.strip())
            for group in src.xpath(f".//*[local-name()='{pcfg2['group_element']}']"):
                gname = (gtext(group, pcfg2["group_name_field"]) or "").lower() if pcfg2["group_name_field"] else ""
                if allowed and gname not in allowed:
                    continue
                for feat in group.xpath(f"./*[local-name()='{pcfg2['feature_element']}']"):
                    fname = gtext(feat, pcfg2["fname_field"])
                    fvalues = [e.text.strip() for e in feat.xpath(f"./*[local-name()='{pcfg2['fvalue_field']}']") if e.text]
                    funit = gtext(feat, pcfg2["funit_field"]) if pcfg2["funit_field"] else None
                    if fname and len(fvalues) == 1:
                        pairs.append((fname, fvalues[0], funit))
        elif pcfg2["mode"] == "pairs":
            for feat in src.xpath(f".//*[local-name()='{pcfg2['feature_element']}']"):
                fname = gtext(feat, pcfg2["fname_field"])
                fval = gtext(feat, pcfg2["fvalue_field"])
                funit = gtext(feat, pcfg2["funit_field"]) if pcfg2["funit_field"] else None
                if fname and fval:
                    pairs.append((fname, fval, funit))
        if pairs:
            n_params += len(pairs)
            params_el = etree.SubElement(item, "INFORMATION_PARAMETERS")
            for fname, fval, funit in pairs:
                p = etree.SubElement(params_el, "INFORMATION_PARAMETER")
                etree.SubElement(p, "NAME").text = fname
                etree.SubElement(p, "VALUE").text = f"{fval} {funit}".strip() if funit else fval

        etree.SubElement(item, "VISIBILITY").text = profile["visibility_default"]

    log.append("Vygenerována jednoduchá pole (CODE/NAME/DESCRIPTION/...) dle nastaveného mapování.")
    if pcfg["mode"] != "none":
        log.append("Cena a měna doplněny dle nastaveného zdroje ceny.")
    if n_price_missing:
        log.append(f"⚠️ {n_price_missing} produktů nemá dohledatelnou cenu.")
    if cat_cfg["mode"] != "none":
        log.append("Kategorie doplněny dle nastaveného zdroje (přímé pole / stromová struktura).")
    if n_cat_missing:
        log.append(f"⚠️ {n_cat_missing} produktů nemá žádnou kategorii.")
    if n_img:
        log.append(f"Nalezeno {n_img} obrázkových položek (u BMEcat zatím jen NÁZVY souborů bez URL) – doplň v tabu Obrázky.")
    if n_params:
        log.append(f"Vygenerováno {n_params} technických parametrů do <INFORMATION_PARAMETERS>.")
    elif profile["params"]["mode"] != "none":
        log.append("⚠️ Nastavení parametrů je zapnuté, ale nenašly se žádné odpovídající hodnoty - zkontroluj mapování v šabloně.")
    log.append(f"VISIBILITY nastaveno na '{profile['visibility_default']}' u všech produktů.")

    return shop, log


def assign_doc_order(root):
    return {el: i for i, el in enumerate(root.iter())}


def validate_shop(shop_root):
    order = assign_doc_order(shop_root)
    issues = []
    for item in shop_root.xpath("./*[local-name()='SHOPITEM']"):
        code = gtext(item, "CODE") or "(bez kódu)"
        name = gtext(item, "NAME") or "(bez názvu)"
        pos = order.get(item, 0)

        def missing(tag, mandatory, message):
            issues.append({"tag": tag, "mandatory": mandatory, "code": code, "name": name,
                            "item": item, "doc_pos": pos, "message": message})

        if not gtext(item, "CODE"):
            missing("CODE", True, "Chybí povinný CODE.")
        if not gtext(item, "NAME"):
            missing("NAME", True, "Chybí povinný NAME.")
        if not gtext(item, "PRICE"):
            missing("PRICE", True, "Chybí povinná cena PRICE.")
        if not gtext(item, "CURRENCY"):
            missing("CURRENCY", True, "Chybí povinná měna CURRENCY.")
        if not item.xpath("./*[local-name()='CATEGORIES']/*[local-name()='CATEGORY']"):
            missing("CATEGORY", True, "Produkt nemá žádnou kategorii.")
        if not gtext(item, "MANUFACTURER"):
            missing("MANUFACTURER", False, "Doporučeno vyplnit výrobce.")
        if not gtext(item, "EAN"):
            missing("EAN", False, "Doporučeno vyplnit EAN.")
        if not item.xpath("./*[local-name()='DESCRIPTION']"):
            missing("DESCRIPTION", False, "Chybí popis produktu.")
        if not item.xpath("./*[local-name()='IMAGES']/*[local-name()='IMAGE']"):
            missing("IMAGES", False, "Produkt nemá žádný obrázek.")
        else:
            for img in item.xpath("./*[local-name()='IMAGES']/*[local-name()='IMAGE']"):
                txt = (img.text or "").strip()
                if txt and not txt.lower().startswith(("http://", "https://")):
                    issues.append({"tag": "IMAGE_URL", "mandatory": True, "code": code, "name": name,
                                   "item": item, "doc_pos": order.get(img, pos),
                                   "message": f"IMAGE '{txt}' není platná URL."})
        if not gtext(item, "VISIBILITY"):
            missing("VISIBILITY", False, "Chybí VISIBILITY.")

    issues.sort(key=lambda x: (not x["mandatory"], -x["doc_pos"]))
    return issues


def shop_to_bytes(shop_root):
    return etree.tostring(shop_root, xml_declaration=True, encoding="utf-8", pretty_print=True)


# =============================================================================
# STREAMLIT UI
# =============================================================================
st.title("🛠️ Univerzální Feed → Shoptet Validator")
st.caption(
    "Nahraj libovolný dodavatelský XML feed, namapuj pole na Shoptet strukturu (nebo nahraj "
    "už hotovou šablonu), zkontroluj a doprav do validní podoby pro Shoptet."
)

ss = st.session_state
ss.setdefault("raw_root", None)
ss.setdefault("profile", json.loads(json.dumps(DEFAULT_PROFILE)))
ss.setdefault("shop_root", None)
ss.setdefault("conversion_log", [])
ss.setdefault("excluded_codes", set())

with st.sidebar:
    st.header("📖 Slovník Shoptet tagů")
    search_term = st.text_input("Hledat tag / popis:", "").lower()
    for tag, info in SHOPTET_DICT.items():
        if search_term in tag.lower() or search_term in info["desc"].lower():
            badge = "🔴 POVINNÝ" if info["mandatory"] else "⚪ nepovinný"
            st.markdown(f"**`<{tag}>`** — {badge}")
            st.caption(info["desc"])

up_col1, up_col2 = st.columns(2)
with up_col1:
    uploaded_file = st.file_uploader("1. Nahraj vstupní XML feed", type=["xml"])
with up_col2:
    profile_file = st.file_uploader("2. (Volitelně) Nahraj uloženou šablonu mapování (.json)", type=["json"])

if uploaded_file is not None:
    data = uploaded_file.read()
    if ss.raw_root is None or ss.get("_last_filename") != uploaded_file.name:
        ss.raw_root = parse_xml_bytes(data)
        ss._last_filename = uploaded_file.name
        ss.shop_root = None
        ss.excluded_codes = set()

if profile_file is not None and not ss.get("_profile_loaded"):
    try:
        ss.profile = json.load(profile_file)
        ss._profile_loaded = True
        st.success(f"Šablona '{ss.profile.get('profile_name', '')}' načtena.")
    except Exception as e:
        st.error(f"Nepodařilo se načíst šablonu: {e}")

if ss.raw_root is None:
    st.info("⬆️ Nahraj feed výše pro začátek.")
    st.stop()

root = ss.raw_root
feed_type = detect_feed_type(root)

if feed_type == "shoptet":
    st.success("Vstupní feed už je ve formátu Shoptet SHOP/SHOPITEM - žádné mapování není potřeba.")
    if ss.shop_root is None:
        ss.shop_root = root
        ss.conversion_log = ["Feed byl už ve formátu Shoptet, načten beze změny."]
else:
    tabs_top = st.tabs(["🗂️ 0) Šablona / mapování polí", "ℹ️ Nápověda"])
    with tabs_top[0]:
        prof = ss.profile
        prof["profile_name"] = st.text_input("Název šablony", value=prof.get("profile_name", "Nová šablona"))

        candidates = candidate_item_tags(root)
        cand_names = [f"{t} ({c}×)" for t, c in candidates]
        cand_map = {f"{t} ({c}×)": t for t, c in candidates}
        default_idx = 0
        if prof.get("item_tag"):
            for i, (t, c) in enumerate(candidates):
                if t == prof["item_tag"]:
                    default_idx = i
                    break
        chosen = st.selectbox(
            "Element představující JEDEN produkt (odhad seřazený podle počtu výskytů)",
            cand_names, index=default_idx if cand_names else 0,
        )
        prof["item_tag"] = cand_map.get(chosen, prof.get("item_tag", ""))

        sample_items = root.xpath(f"//*[local-name()='{prof['item_tag']}']") if prof["item_tag"] else []
        avail_tags = descendant_tags(sample_items[0]) if sample_items else []
        tag_options = ["-- nevybráno --"] + avail_tags

        def tag_select(label, current, key):
            idx = tag_options.index(current) if current in tag_options else 0
            return st.selectbox(label, tag_options, index=idx, key=key)

        st.markdown("#### Jednoduchá pole")
        cols = st.columns(3)
        simple_targets = ["CODE", "NAME", "DESCRIPTION", "MANUFACTURER", "EAN", "PART_NUMBER"]
        for i, target in enumerate(simple_targets):
            with cols[i % 3]:
                cur = prof["fields"].get(target, {"mode": "direct", "source": ""})["source"]
                sel = tag_select(f"{target}", cur, f"f_{target}")
                prof["fields"][target] = {"mode": "direct", "source": "" if sel == "-- nevybráno --" else sel}

        st.markdown("#### Cena")
        pcfg = prof["price"]
        price_mode = st.radio("Zdroj ceny", ["Přímé pole", "BMEcat styl (ARTICLE_PRICE dle typu)"],
                               index=0 if pcfg["mode"] == "direct" else 1, key="price_mode", horizontal=True)
        prof["currency"] = st.text_input("Měna (CURRENCY)", value=prof.get("currency", "CZK"))
        if price_mode == "Přímé pole":
            sel = tag_select("Zdrojový tag ceny", pcfg.get("source", ""), "price_src")
            prof["price"] = {"mode": "direct", "source": "" if sel == "-- nevybráno --" else sel}
        else:
            attr = st.text_input("Atribut typu ceny na ARTICLE_PRICE (prázdné = ber první)", value=pcfg.get("price_type_attr", "price_type"))
            val = st.text_input("Hodnota atributu (např. net_list)", value=pcfg.get("price_type_value", "net_list"))
            sel = tag_select("Zdrojový tag částky uvnitř ARTICLE_PRICE", pcfg.get("source", "PRICE_AMOUNT") or "PRICE_AMOUNT", "price_amount_src")
            prof["price"] = {"mode": "price_type", "source": "" if sel == "-- nevybráno --" else sel,
                              "price_type_attr": attr, "price_type_value": val}

        st.markdown("#### Kategorie")
        ccfg = prof["category"]
        cat_mode = st.radio("Zdroj kategorií", ["Přímé pole (hotová cesta jako text)", "Stromová struktura (BMEcat CATALOG_STRUCTURE styl)"],
                             index=0 if ccfg["mode"] in ("direct", "none") else 1, key="cat_mode", horizontal=True)
        sep = st.text_input("Oddělovač úrovní kategorie", value=ccfg.get("sep", " > "))
        if cat_mode.startswith("Přímé"):
            sel = tag_select("Zdrojový tag kategorie (může se opakovat víckrát)", ccfg.get("source", ""), "cat_src")
            prof["category"] = {"mode": "direct", "source": "" if sel == "-- nevybráno --" else sel, "sep": sep}
        else:
            with st.expander("Nastavení stromové struktury kategorií", expanded=True):
                c1, c2 = st.columns(2)
                with c1:
                    link_el = st.text_input("Element propojení produkt↔kategorie", value=ccfg.get("link_element", "ARTICLE_TO_CATALOGGROUP_MAP"))
                    id_field = st.text_input("Pole s ID produktu v propojení", value=ccfg.get("id_field", "ART_ID"))
                    group_id_field = st.text_input("Pole s ID kategorie v propojení", value=ccfg.get("group_id_field", "CATALOG_GROUP_ID"))
                with c2:
                    struct_el = st.text_input("Element definice kategorie ve stromu", value=ccfg.get("struct_element", "CATALOG_STRUCTURE"))
                    group_id = st.text_input("Pole ID kategorie ve stromu", value=ccfg.get("group_id", "GROUP_ID"))
                    group_name = st.text_input("Pole názvu kategorie", value=ccfg.get("group_name", "GROUP_NAME"))
                    parent_id = st.text_input("Pole ID rodičovské kategorie", value=ccfg.get("parent_id", "PARENT_ID"))
                    root_val = st.text_input("Hodnota atributu 'type' pro kořenový uzel (vynechá se z cesty)", value=ccfg.get("root_type_value", "root"))
            prof["category"] = {"mode": "tree", "sep": sep, "link_element": link_el, "id_field": id_field,
                                 "group_id_field": group_id_field, "struct_element": struct_el, "group_id": group_id,
                                 "group_name": group_name, "parent_id": parent_id, "root_type_value": root_val}

        st.markdown("#### Hmotnost")
        wcfg = prof["weight"]
        weight_mode = st.radio("Zdroj hmotnosti", ["Nevyplňovat", "Přímé pole", "Vlastnost/FEATURE podle názvu (BMEcat styl)"],
                                index={"none": 0, "direct": 1, "feature": 2}.get(wcfg["mode"], 0), key="weight_mode", horizontal=True)
        if weight_mode == "Nevyplňovat":
            prof["weight"] = {"mode": "none"}
        elif weight_mode == "Přímé pole":
            sel = tag_select("Zdrojový tag hmotnosti", wcfg.get("source", ""), "weight_src")
            prof["weight"] = {"mode": "direct", "source": "" if sel == "-- nevybráno --" else sel}
        else:
            with st.expander("Nastavení FEATURE hmotnosti", expanded=True):
                fe = st.text_input("Element jedné vlastnosti", value=wcfg.get("feature_element", "FEATURE"))
                fn = st.text_input("Pole s názvem vlastnosti", value=wcfg.get("fname_field", "FNAME"))
                fv = st.text_input("Pole s hodnotou", value=wcfg.get("fvalue_field", "FVALUE"))
                fu = st.text_input("Pole s jednotkou", value=wcfg.get("funit_field", "FUNIT"))
                names = st.text_input("Název(y) vlastnosti = hmotnost (odděl čárkou)", value=wcfg.get("names", "Hmotnost, Weight"))
            prof["weight"] = {"mode": "feature", "feature_element": fe, "fname_field": fn, "fvalue_field": fv,
                               "funit_field": fu, "names": names}

        st.markdown("#### Obrázky")
        icfg = prof["images"]
        img_mode = st.radio("Zdroj obrázků", ["Nevyplňovat", "Přímé pole (URL/název souboru)", "MIME_INFO/MIME (BMEcat styl)"],
                             index={"none": 0, "direct": 1, "mime": 2}.get(icfg["mode"], 0), key="img_mode", horizontal=True)
        max_images = st.number_input("Max. počet obrázků na produkt (0 = bez omezení)", min_value=0,
                                      value=icfg.get("max_images", 8) or 0, step=1)
        if img_mode == "Nevyplňovat":
            prof["images"] = {"mode": "none", "max_images": max_images or None}
        elif img_mode.startswith("Přímé"):
            sel = tag_select("Zdrojový tag obrázku (může se opakovat)", icfg.get("source", ""), "img_src")
            prof["images"] = {"mode": "direct", "source": "" if sel == "-- nevybráno --" else sel, "max_images": max_images or None}
        else:
            with st.expander("Nastavení MIME obrázků", expanded=True):
                me = st.text_input("Element jednoho obrázku", value=icfg.get("mime_element", "MIME"))
                pf = st.text_input("Pole účelu obrázku (prázdné = nefiltrovat)", value=icfg.get("purpose_field", "MIME_PURPOSE"))
                pv = st.text_input("Požadovaná hodnota účelu", value=icfg.get("purpose_value", "normal"))
                sf = st.text_input("Pole s názvem souboru", value=icfg.get("source_field", "MIME_SOURCE"))
            prof["images"] = {"mode": "mime", "mime_element": me, "purpose_field": pf, "purpose_value": pv,
                               "source_field": sf, "max_images": max_images or None}

        st.markdown("#### Technické parametry → `<INFORMATION_PARAMETERS>`")
        st.caption("Necháno defaultně vypnuté - doporučujeme vždy zkontrolovat/zapnout ručně a ověřit výsledek v Živém náhledu.")
        pcfg2 = prof["params"]
        par_mode = st.radio("Zdroj parametrů", ["Nevygenerovat", "Skupiny vlastností s whitelistem (BMEcat styl)", "Opakující se páry název/hodnota"],
                             index={"none": 0, "feature_groups": 1, "pairs": 2}.get(pcfg2["mode"], 0), key="par_mode", horizontal=True)
        if par_mode == "Nevygenerovat":
            prof["params"] = {"mode": "none"}
        elif par_mode.startswith("Skupiny"):
            with st.expander("Nastavení skupin vlastností", expanded=True):
                ge = st.text_input("Element skupiny vlastností", value=pcfg2.get("group_element", "ARTICLE_FEATURES"))
                gnf = st.text_input("Pole s identifikátorem/názvem skupiny", value=pcfg2.get("group_name_field", "REFERENCE_FEATURE_SYSTEM_NAME"))
                ag = st.text_input("Whitelist hodnot skupiny (odděl čárkou, prázdné = všechny)", value=pcfg2.get("allowed_groups", "udf_BOSCH_PMDB-0.1"))
                fe = st.text_input("Element jedné vlastnosti", value=pcfg2.get("feature_element", "FEATURE"), key="par_fe")
                fn = st.text_input("Pole s názvem vlastnosti", value=pcfg2.get("fname_field", "FNAME"), key="par_fn")
                fv = st.text_input("Pole s hodnotou vlastnosti", value=pcfg2.get("fvalue_field", "FVALUE"), key="par_fv")
                fu = st.text_input("Pole s jednotkou (nepovinné)", value=pcfg2.get("funit_field", "FUNIT"), key="par_fu")
            prof["params"] = {"mode": "feature_groups", "group_element": ge, "group_name_field": gnf,
                               "allowed_groups": ag, "feature_element": fe, "fname_field": fn,
                               "fvalue_field": fv, "funit_field": fu}
        else:
            with st.expander("Nastavení párů název/hodnota", expanded=True):
                fe = st.text_input("Element jednoho páru", value=pcfg2.get("feature_element", ""), key="par2_fe")
                fn = st.text_input("Pole s názvem", value=pcfg2.get("fname_field", ""), key="par2_fn")
                fv = st.text_input("Pole s hodnotou", value=pcfg2.get("fvalue_field", ""), key="par2_fv")
                fu = st.text_input("Pole s jednotkou (nepovinné)", value=pcfg2.get("funit_field", ""), key="par2_fu")
            prof["params"] = {"mode": "pairs", "feature_element": fe, "fname_field": fn, "fvalue_field": fv, "funit_field": fu}

        prof["visibility_default"] = st.text_input("Výchozí VISIBILITY", value=prof.get("visibility_default", "visible"))

        st.markdown("---")
        colA, colB = st.columns(2)
        with colA:
            if st.button("▶️ Spustit / aktualizovat převod podle šablony výše", type="primary", use_container_width=True):
                shop, log = apply_profile(root, prof)
                ss.shop_root = shop
                ss.conversion_log = log
                ss.excluded_codes = set()
                st.rerun()
        with colB:
            st.download_button(
                "💾 Stáhnout tuto šablonu (.json) pro příští použití",
                data=json.dumps(prof, ensure_ascii=False, indent=2).encode("utf-8"),
                file_name=f"{prof['profile_name'].replace(' ', '_')}.json",
                mime="application/json",
                use_container_width=True,
            )

    with tabs_top[1]:
        st.write(
            "1. Vyber element jednoho produktu.\n"
            "2. Namapuj jednoduchá pole (CODE, NAME...) na sloupce zjištěné ve vzorovém produktu.\n"
            "3. Cena/kategorie/hmotnost/obrázky/parametry mají 'Přímý' režim (pro ploché feedy) "
            "a specializovaný BMEcat-styl režim (pro feedy se stromem kategorií a FEATURE vlastnostmi).\n"
            "4. Klikni na 'Spustit převod' a šablonu si stáhni - příště ji jen nahraješ přes pole "
            "'Nahraj uloženou šablonu' a rovnou spustíš převod bez ručního mapování."
        )

if ss.shop_root is None:
    st.warning("Nastav šablonu mapování v tabu **0) Šablona / mapování polí** a klikni na 'Spustit převod'.")
    st.stop()

shop_root = ss.shop_root
all_items = shop_root.xpath("./*[local-name()='SHOPITEM']")

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "1️⃣ Log převodu",
    "2️⃣ Validace a oprava",
    "3️⃣ Výběr produktů do exportu",
    "4️⃣ Sloučení / hromadné úpravy",
    "5️⃣ Obrázky",
    "6️⃣ Živý náhled a export",
])

with tab1:
    st.write(f"Počet produktů (SHOPITEM): **{len(all_items)}**")
    st.markdown("**Co bylo provedeno automaticky:**")
    for line in ss.conversion_log:
        if line.startswith("⚠️"):
            st.warning(line)
        else:
            st.write("• " + line)

with tab2:
    issues = validate_shop(shop_root)
    n_mandatory = sum(1 for i in issues if i["mandatory"])
    n_optional = len(issues) - n_mandatory
    c1, c2, c3 = st.columns(3)
    c1.metric("Produktů celkem", len(all_items))
    c2.metric("Povinných chyb", n_mandatory)
    c3.metric("Doporučených chyb", n_optional)

    if not issues:
        st.success("🎉 Feed neobsahuje žádné chybějící povinné ani doporučené elementy.")
    else:
        st.write("Chyby: **nejdřív povinné**, pak doporučené; uvnitř skupiny **sestupně podle pozice v XML**.")
        grouped = {}
        for i in issues:
            grouped.setdefault(i["tag"], []).append(i)
        group_order = sorted(grouped.keys(), key=lambda t: (not grouped[t][0]["mandatory"], -max(x["doc_pos"] for x in grouped[t])))

        for tag in group_order:
            group = grouped[tag]
            mand = group[0]["mandatory"]
            label = f"{'🔴 POVINNÉ' if mand else '⚪ Doporučené'} — <{tag}> chybí u {len(group)} produktů"
            with st.expander(label, expanded=mand):
                info = SHOPTET_DICT.get(tag if tag != "IMAGE_URL" else "IMAGE", {})
                if info:
                    st.caption(info.get("desc", ""))
                df = pd.DataFrame([{"CODE": g["code"], "NAME": g["name"], "Nová hodnota": ""} for g in group])

                bcol1, bcol2 = st.columns([3, 1])
                with bcol1:
                    bulk_value = st.text_input(f"Hromadná hodnota pro všechny chybějící <{tag}>", key=f"bulk_{tag}")
                with bcol2:
                    st.write("")
                    st.write("")
                    apply_bulk = st.button("Použít na všechny", key=f"apply_bulk_{tag}")

                edited = st.data_editor(df, key=f"editor_{tag}", use_container_width=True, num_rows="fixed", disabled=["CODE", "NAME"])
                apply_individual = st.button("Uložit individuální hodnoty výše", key=f"apply_ind_{tag}")

                def set_value(item_el, tag_name, value):
                    if tag_name == "IMAGE_URL":
                        return
                    if tag_name == "CATEGORY":
                        cats_el = item_el.xpath("./*[local-name()='CATEGORIES']")
                        if not cats_el:
                            cats_el = [etree.SubElement(item_el, "CATEGORIES")]
                        etree.SubElement(cats_el[0], "CATEGORY").text = value
                        return
                    existing = item_el.xpath(f"./*[local-name()='{tag_name}']")
                    if existing:
                        existing[0].text = value
                    else:
                        etree.SubElement(item_el, tag_name).text = value

                if apply_bulk and bulk_value:
                    for g in group:
                        set_value(g["item"], tag, bulk_value)
                    st.success(f"Hodnota '{bulk_value}' nastavena u {len(group)} produktů.")
                    st.rerun()
                if apply_individual:
                    count = 0
                    for idx, row in edited.iterrows():
                        if row["Nová hodnota"]:
                            set_value(group[idx]["item"], tag, row["Nová hodnota"])
                            count += 1
                    if count:
                        st.success(f"Uloženo {count} individuálních hodnot.")
                        st.rerun()

with tab3:
    st.subheader("✅ Vyber, které produkty se mají dostat do výsledného feedu")
    st.caption("Nezaškrtnuté položky se do staženého XML nezahrnou (v appce samotné zůstávají, jen se vyfiltrují při exportu).")
    rows = []
    for item in all_items:
        code = gtext(item, "CODE") or ""
        rows.append({"Zahrnout": code not in ss.excluded_codes, "CODE": code, "NAME": gtext(item, "NAME") or ""})
    df_sel = pd.DataFrame(rows)

    csel1, csel2, csel3 = st.columns(3)
    with csel1:
        if st.button("Zaškrtnout vše"):
            ss.excluded_codes = set()
            st.rerun()
    with csel2:
        if st.button("Odškrtnout vše"):
            ss.excluded_codes = set(df_sel["CODE"])
            st.rerun()
    with csel3:
        st.metric("Vybráno k exportu", f"{len(all_items) - len(ss.excluded_codes)} / {len(all_items)}")

    edited_sel = st.data_editor(
        df_sel, use_container_width=True, num_rows="fixed", disabled=["CODE", "NAME"], key="sel_editor",
        hide_index=True,
    )
    if st.button("💾 Uložit výběr", type="primary"):
        ss.excluded_codes = set(edited_sel.loc[~edited_sel["Zahrnout"], "CODE"])
        st.success(f"Výběr uložen. Do exportu půjde {len(all_items) - len(ss.excluded_codes)} produktů.")
        st.rerun()

with tab4:
    st.subheader("🔗 Obecné sloučení dvou elementů")
    present_tags = sorted(set(el.tag for el in shop_root.iter() if isinstance(el.tag, str)))
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        tag_first = st.selectbox("První element (hodnota):", ["--"] + present_tags, key="m_first")
    with col_b:
        tag_second = st.selectbox("Druhý element (jednotka):", ["--"] + present_tags, key="m_second")
    with col_c:
        target_tag = st.selectbox("Cílový tag:", ["VALUE", "NAME", "DESCRIPTION"] + list(SHOPTET_DICT), key="m_target")
    col_d, col_e = st.columns(2)
    with col_d:
        sep = st.text_input("Oddělovač", value=" ", key="m_sep")
    with col_e:
        remove_second = st.checkbox("Po sloučení smazat druhý element", value=True, key="m_remove")

    if st.button("Sloučit v celém feedu", type="primary"):
        if tag_first == "--" or tag_second == "--":
            st.error("Vyber oba elementy.")
        else:
            merged = 0
            for parent in shop_root.iter():
                first = parent.find(tag_first)
                second = parent.find(tag_second)
                if first is not None and second is not None:
                    v1, v2 = (first.text or "").strip(), (second.text or "").strip()
                    first.tag = target_tag
                    first.text = f"{v1}{sep}{v2}" if v1 and v2 else (v1 or v2)
                    if remove_second:
                        parent.remove(second)
                    merged += 1
            st.success(f"Sloučeno {merged} výskytů <{tag_first}> + <{tag_second}> → <{target_tag}>.")
            st.rerun()

    st.markdown("---")
    st.subheader("🧩 Hromadná úprava libovolného tagu")
    bulk_tag = st.selectbox("Tag k úpravě", present_tags, key="bt_tag")
    bulk_action = st.radio("Akce", ["Nastavit pevnou hodnotu všem výskytům", "Najít a nahradit text uvnitř hodnoty"], key="bt_action")
    if bulk_action == "Nastavit pevnou hodnotu všem výskytům":
        new_val = st.text_input("Nová hodnota", key="bt_val")
        if st.button("Aplikovat na celý feed", key="bt_apply1"):
            cnt = 0
            for el in shop_root.iter(bulk_tag):
                el.text = new_val
                cnt += 1
            st.success(f"Nastaveno u {cnt} výskytů <{bulk_tag}>.")
            st.rerun()
    else:
        find_txt = st.text_input("Najít", key="bt_find")
        replace_txt = st.text_input("Nahradit za", key="bt_replace")
        if st.button("Aplikovat na celý feed", key="bt_apply2"):
            cnt = 0
            for el in shop_root.iter(bulk_tag):
                if el.text and find_txt in el.text:
                    el.text = el.text.replace(find_txt, replace_txt)
                    cnt += 1
            st.success(f"Upraveno {cnt} výskytů <{bulk_tag}>.")
            st.rerun()

with tab5:
    st.subheader("🖼️ Obrázky produktů")
    st.info("Shoptet stahuje obrázky přímo z URL v <IMAGE>. Pokud feed obsahuje jen názvy souborů, "
            "zadej URL složky, kde jsou obrázky veřejně nahrané pod původními názvy.")
    image_els = shop_root.xpath(".//*[local-name()='IMAGE']")
    raw_count = sum(1 for e in image_els if e.text and not e.text.strip().lower().startswith(("http://", "https://")))
    st.write(f"Obrázků celkem: **{len(image_els)}**, z toho bez platné URL: **{raw_count}**")

    base_url = st.text_input(
        "URL složky s obrázky (appka sama připojí originální název souboru)",
        value="https://TVUJ-HOSTING.cz/obrazky/",
        help="Zadej URL složky, kde máš obrázky nahrané pod původními názvy (přesně jako v ZIPu). "
             "K ní se jen připojí název souboru z feedu - žádné přejmenovávání.",
    )
    if st.button("Aplikovat URL složky na všechny obrázky bez URL", type="primary"):
        base = base_url if base_url.endswith("/") else base_url + "/"
        cnt = 0
        for el in image_els:
            txt = (el.text or "").strip()
            if txt and not txt.lower().startswith(("http://", "https://")):
                el.text = base + quote(txt)
                cnt += 1
        st.success(f"Doplněna URL u {cnt} obrázků.")
        st.rerun()

    with st.expander("Náhled prvních 10 obrázkových hodnot"):
        for e in image_els[:10]:
            st.code(e.text or "")

with tab6:
    st.subheader("Živý náhled výsledného Shoptet XML")
    export_shop = etree.Element("SHOP")
    n_excluded = 0
    for item in all_items:
        code = gtext(item, "CODE") or ""
        if code in ss.excluded_codes:
            n_excluded += 1
            continue
        export_shop.append(deepcopy(item))
    st.caption(f"Do exportu jde {len(all_items) - n_excluded} z {len(all_items)} produktů (nastaveno v tabu 3).")

    xml_bytes = shop_to_bytes(export_shop)
    st.download_button("📥 Stáhnout výsledný Shoptet XML feed", data=xml_bytes, file_name="shoptet_feed.xml",
                        mime="application/xml", type="primary")
    st.code(xml_bytes.decode("utf-8")[:20000], language="xml")
    if len(xml_bytes) > 20000:
        st.caption("(náhled zkrácen na prvních ~20 000 znaků, stažený soubor je kompletní)")
