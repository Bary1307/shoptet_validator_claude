import streamlit as st
from lxml import etree
import pandas as pd
import io
import re
from copy import deepcopy

st.set_page_config(page_title="Bosch → Shoptet Feed Validator", layout="wide")

# =============================================================================
# SLOVNÍK SHOPTET TAGŮ (dodavatelský/importní XML feed)
# =============================================================================
# mandatory: True/False, group: pro řazení v tab 2 (nejdřív povinné)
SHOPTET_DICT = {
    "SHOP": {"desc": "Kořenový element celého feedu.", "mandatory": False},
    "SHOPITEM": {"desc": "Obalovací element jednoho produktu.", "mandatory": False},
    "CODE": {"desc": "Unikátní kód / SKU produktu v rámci e-shopu. Musí být neměnný a jedinečný.", "mandatory": True},
    "NAME": {"desc": "Název produktu zobrazený zákazníkovi.", "mandatory": True},
    "SHORT_DESCRIPTION": {"desc": "Krátký popis (výpisy kategorií, štítky).", "mandatory": False},
    "DESCRIPTION": {"desc": "Detailní popis produktu, podporuje HTML.", "mandatory": False},
    "MANUFACTURER": {"desc": "Značka / výrobce produktu.", "mandatory": False},
    "EAN": {"desc": "EAN / čárový kód produktu.", "mandatory": False},
    "PART_NUMBER": {"desc": "Katalogové (výrobní) číslo produktu u výrobce.", "mandatory": False},
    "WARRANTY": {"desc": "Délka záruky, např. '24 měsíců'.", "mandatory": False},
    "CURRENCY": {"desc": "Měna ceny produktu (CZK, EUR...). Bez ní Shoptet neví, v čem je PRICE.", "mandatory": True},
    "PRICE": {"desc": "Cena produktu (dle konvence dodavatelského feedu bez DPH, nebo dle domluvy s eshopem).", "mandatory": True},
    "VAT": {"desc": "Sazba DPH v procentech, např. 21.", "mandatory": False},
    "CATEGORIES": {"desc": "Obalovací element cest kategorií produktu.", "mandatory": False},
    "CATEGORY": {"desc": "Jedna celá cesta kategorie odshora dolů, oddělovač zpravidla '>' (např. 'Nářadí > Aku nářadí').", "mandatory": True},
    "IMAGES": {"desc": "Obalovací element obrázků produktu.", "mandatory": False},
    "IMAGE": {"desc": "Přímá, veřejně dostupná URL adresa obrázku (musí začínat http/https, ne jen název souboru).", "mandatory": False},
    "LOGISTIC": {"desc": "Obalovací element logistických údajů (váha, rozměry).", "mandatory": False},
    "WEIGHT": {"desc": "Hmotnost produktu v kg.", "mandatory": False},
    "VISIBILITY": {"desc": "Viditelnost produktu v e-shopu: 'visible' nebo 'hidden'.", "mandatory": False},
    "STOCK": {"desc": "Obalovací element skladových údajů.", "mandatory": False},
    "AMOUNT": {"desc": "Počet kusů skladem.", "mandatory": False},
    "AVAILABILITY": {"desc": "Textová dostupnost, např. 'Skladem'.", "mandatory": False},
    "PARAMETERS": {"desc": "Obalovací element parametrů (vlastností) produktu.", "mandatory": False},
    "PARAMETER": {"desc": "Jeden parametr produktu, obsahuje NAME a VALUE.", "mandatory": False},
    "VALUE": {"desc": "Hodnota parametru uvnitř <PARAMETER> (Shoptet chce zde 'VALUE', pozor na kolizi s jinými významy 'VALUE' v cizích feedech).", "mandatory": False},
    "URL": {"desc": "Přímý odkaz na produkt v e-shopu (pokud už existuje).", "mandatory": False},
}
MANDATORY_TAGS = [t for t, v in SHOPTET_DICT.items() if v["mandatory"]]

SHOPITEM_TAG_ORDER = [
    "CODE", "NAME", "SHORT_DESCRIPTION", "DESCRIPTION", "MANUFACTURER", "EAN",
    "PART_NUMBER", "WARRANTY", "CATEGORIES", "CURRENCY", "PRICE", "VAT",
    "IMAGES", "PARAMETERS", "LOGISTIC", "STOCK", "VISIBILITY", "URL",
]


# =============================================================================
# POMOCNÉ FUNKCE - obecné XML
# =============================================================================
def gtext(el, tag):
    r = el.xpath(f"./*[local-name()='{tag}']")
    return r[0].text.strip() if r and r[0].text else None


def gtext_deep(el, tag):
    """Jako gtext, ale hledá i ve vnořených elementech (např. EAN je uvnitř
    ARTICLE_DETAILS, ne přímo pod ARTICLE)."""
    r = el.xpath(f".//*[local-name()='{tag}']")
    return r[0].text.strip() if r and r[0].text else None


def gall(el, tag):
    return el.xpath(f"./*[local-name()='{tag}']")


def parse_xml_bytes(data):
    parser = etree.XMLParser(recover=True, huge_tree=True)
    return etree.fromstring(data, parser=parser)


def detect_feed_type(root):
    tag = root.tag.split("}")[-1]
    if tag == "BMECAT":
        return "bmecat"
    if tag == "SHOP":
        return "shoptet"
    return "unknown"


def assign_doc_order(root):
    """Přiřadí každému elementu pořadí v dokumentu - použije se pro řazení chyb
    'sestupně tak, jak jsou umístěny v XML kódu'."""
    order = {}
    for i, el in enumerate(root.iter()):
        order[id(el)] = i
    return order


# =============================================================================
# PŘEVOD Z BOSCH BMEcat DO SHOPTET
# =============================================================================
def build_category_map(root):
    cats = {}
    for cs in root.xpath("//*[local-name()='CATALOG_STRUCTURE']"):
        gid = gtext(cs, "GROUP_ID")
        cats[gid] = {
            "name": gtext(cs, "GROUP_NAME"),
            "parent": gtext(cs, "PARENT_ID"),
            "type": cs.get("type"),
        }
    return cats


def category_path(gid, cats, sep=" > "):
    chain, seen, cur = [], set(), gid
    while cur and cur in cats and cur not in seen:
        seen.add(cur)
        node = cats[cur]
        if node["type"] != "root":
            chain.append(node["name"])
        cur = node["parent"]
    chain.reverse()
    return sep.join([c for c in chain if c])


def build_article_category_links(root):
    m = {}
    for link in root.xpath("//*[local-name()='ARTICLE_TO_CATALOGGROUP_MAP']"):
        art_id = gtext(link, "ART_ID")
        gid = gtext(link, "CATALOG_GROUP_ID")
        if art_id:
            m.setdefault(art_id, []).append(gid)
    return m


def find_weight_kg(article, weight_feature_names):
    for feat in article.xpath(".//*[local-name()='FEATURE']"):
        fname = gtext(feat, "FNAME")
        if fname and fname.strip().lower() in weight_feature_names:
            val = gtext(feat, "FVALUE")
            unit = (gtext(feat, "FUNIT") or "kg").lower()
            if val is None:
                continue
            v = val.replace(",", ".").strip()
            try:
                v = float(v)
            except ValueError:
                continue
            if unit in ("g",):
                v = v / 1000.0
            return round(v, 3)
    return None


def collect_technical_features(article, allowed_groups):
    """Vrátí seznam (FNAME, FVALUE, FUNIT) jen ze skupin FEATURE, jejichž
    REFERENCE_FEATURE_SYSTEM_NAME je v allowed_groups (whitelist) - u Bosch feedů
    tak jde přeskočit marketingové bloky a HOTSPOT metadata, které mají jiný název skupiny."""
    allowed = set(g.strip().lower() for g in allowed_groups if g.strip())
    out = []
    for group in article.xpath(".//*[local-name()='ARTICLE_FEATURES']"):
        sysname = (gtext(group, "REFERENCE_FEATURE_SYSTEM_NAME") or "").lower()
        if allowed and sysname not in allowed:
            continue
        for feat in group.xpath("./*[local-name()='FEATURE']"):
            fname = gtext(feat, "FNAME")
            fvalues = [e.text.strip() for e in gall(feat, "FVALUE") if e.text]
            funit = gtext(feat, "FUNIT")
            if not fname or not fvalues:
                continue
            if len(fvalues) > 1:
                # víc hodnot = spíš marketingový výčet, ne technický parametr -> přeskočit
                continue
            out.append((fname, fvalues[0], funit))
    return out


def collect_images(article, max_images=None):
    seen, files = set(), []
    for mime in article.xpath(".//*[local-name()='MIME_INFO']/*[local-name()='MIME']"):
        purpose = gtext(mime, "MIME_PURPOSE")
        src = gtext(mime, "MIME_SOURCE")
        if not src or purpose not in (None, "normal"):
            continue
        if src not in seen:
            seen.add(src)
            files.append(src)
    if max_images:
        files = files[:max_images]
    return files


def convert_bmecat_to_shoptet(root, options):
    """Vrátí (nový <SHOP> element, log úprav [list str])."""
    log = []
    cats = build_category_map(root)
    art_links = build_article_category_links(root)
    weight_names = set(n.strip().lower() for n in options["weight_feature_names"].split(","))

    shop = etree.Element("SHOP")
    articles = root.xpath("//*[local-name()='ARTICLE']")
    log.append(f"Nalezeno {len(articles)} produktů (<ARTICLE>) ve vstupním Bosch BMEcat feedu.")

    n_price_missing, n_cat_missing, n_img = 0, 0, 0

    for art in articles:
        item = etree.SubElement(shop, "SHOPITEM")

        supplier_aid = gtext(art, "SUPPLIER_AID")
        etree.SubElement(item, "CODE").text = supplier_aid or ""

        name = gtext_deep(art, "DESCRIPTION_SHORT") or ""
        etree.SubElement(item, "NAME").text = name

        desc_long = gtext_deep(art, "DESCRIPTION_LONG")
        if desc_long:
            etree.SubElement(item, "DESCRIPTION").text = desc_long

        manufacturer = gtext_deep(art, "MANUFACTURER_NAME")
        if manufacturer:
            etree.SubElement(item, "MANUFACTURER").text = manufacturer

        ean = gtext_deep(art, "EAN")
        if ean:
            etree.SubElement(item, "EAN").text = ean

        part_number = gtext_deep(art, "MANUFACTURER_AID")
        if part_number:
            etree.SubElement(item, "PART_NUMBER").text = part_number

        # kategorie
        gids = art_links.get(supplier_aid, [])
        paths = []
        for gid in gids:
            p = category_path(gid, cats, sep=options["category_sep"])
            if p and p not in paths:
                paths.append(p)
        if paths:
            cats_el = etree.SubElement(item, "CATEGORIES")
            for p in paths:
                etree.SubElement(cats_el, "CATEGORY").text = p
        else:
            n_cat_missing += 1

        # cena
        price_el = art.xpath(".//*[local-name()='ARTICLE_PRICE'][@price_type='net_list']")
        if not price_el:
            price_el = art.xpath(".//*[local-name()='ARTICLE_PRICE']")
        if price_el:
            amount = gtext(price_el[0], "PRICE_AMOUNT")
            if amount:
                etree.SubElement(item, "CURRENCY").text = options["currency"]
                try:
                    etree.SubElement(item, "PRICE").text = f"{float(amount):.2f}"
                except ValueError:
                    etree.SubElement(item, "PRICE").text = amount
            else:
                n_price_missing += 1
        else:
            n_price_missing += 1

        # váha
        w = find_weight_kg(art, weight_names)
        if w is not None:
            logistic = etree.SubElement(item, "LOGISTIC")
            etree.SubElement(logistic, "WEIGHT").text = str(w)

        # parametry z technických FEATURE
        if options["generate_parameters"]:
            feats = collect_technical_features(art, options["technical_groups"])
            if feats:
                params_el = etree.SubElement(item, "PARAMETERS")
                for fname, fval, funit in feats:
                    p = etree.SubElement(params_el, "PARAMETER")
                    etree.SubElement(p, "NAME").text = fname
                    value = f"{fval} {funit}".strip() if funit else fval
                    etree.SubElement(p, "VALUE").text = value

        # obrázky (zatím jen názvy souborů - musí se doplnit URL šablonou v tabu Obrázky)
        images = collect_images(art, max_images=options.get("max_images"))
        if images:
            n_img += len(images)
            images_el = etree.SubElement(item, "IMAGES")
            for fname in images:
                etree.SubElement(images_el, "IMAGE").text = fname

        etree.SubElement(item, "VISIBILITY").text = "visible"

    log.append(f"Vygenerována pole CODE, NAME, DESCRIPTION, MANUFACTURER, EAN, PART_NUMBER pro {len(articles)} produktů.")
    log.append("Kategorie dopočítány z CATALOG_STRUCTURE stromu podle ARTICLE_TO_CATALOGGROUP_MAP.")
    if n_cat_missing:
        log.append(f"⚠️ {n_cat_missing} produktů nemá napojenou žádnou kategorii (chybí ARTICLE_TO_CATALOGGROUP_MAP).")
    log.append("Cena převzata z <ARTICLE_PRICE price_type='net_list'> (bez DPH, dle konvence dodavatelského feedu).")
    if n_price_missing:
        log.append(f"⚠️ {n_price_missing} produktů nemá dohledatelnou cenu.")
    log.append("Váha dopočítána z technické vlastnosti (FEATURE) odpovídající zadanému názvu (výchozí 'Hmotnost').")
    if options["generate_parameters"]:
        log.append("Technické parametry (FNAME/FVALUE/FUNIT) převedeny do <PARAMETERS><PARAMETER><NAME>/<VALUE> (jednotka sloučena do hodnoty).")
    if n_img:
        log.append(f"Nalezeno {n_img} obrázkových souborů (názvy, zatím BEZ veřejné URL) – doplňte v tabu 'Obrázky'.")
    log.append("VISIBILITY nastaveno na 'visible' u všech produktů (lze hromadně změnit v tabu Validace).")

    return shop, log


# =============================================================================
# VALIDACE VÝSLEDNÉHO SHOPTET FEEDU
# =============================================================================
def validate_shop(shop_root):
    """Vrátí list issue dictů: tag, mandatory, code, name, item, doc_pos, message"""
    order = assign_doc_order(shop_root)
    issues = []
    items = shop_root.xpath("./*[local-name()='SHOPITEM']")
    for item in items:
        code = gtext(item, "CODE") or "(bez kódu)"
        name = gtext(item, "NAME") or "(bez názvu)"
        pos = order.get(id(item), 0)

        def missing(tag, mandatory, message):
            issues.append({
                "tag": tag, "mandatory": mandatory, "code": code, "name": name,
                "item": item, "doc_pos": pos, "message": message,
            })

        if not gtext(item, "CODE"):
            missing("CODE", True, "Chybí povinný CODE.")
        if not gtext(item, "NAME"):
            missing("NAME", True, "Chybí povinný NAME.")
        if not gtext(item, "PRICE"):
            missing("PRICE", True, "Chybí povinná cena PRICE.")
        if not gtext(item, "CURRENCY"):
            missing("CURRENCY", True, "Chybí povinná měna CURRENCY.")
        if not item.xpath("./*[local-name()='CATEGORIES']/*[local-name()='CATEGORY']"):
            missing("CATEGORY", True, "Produkt nemá žádnou kategorii CATEGORY.")

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
                    issues.append({
                        "tag": "IMAGE_URL", "mandatory": True, "code": code, "name": name,
                        "item": item, "doc_pos": order.get(id(img), pos),
                        "message": f"IMAGE '{txt}' není platná URL (musí začínat http/https).",
                    })
        if not gtext(item, "VISIBILITY"):
            missing("VISIBILITY", False, "Chybí VISIBILITY (viditelnost produktu).")

    # řazení: povinné chyby první, uvnitř skupiny sestupně podle pozice v XML
    issues.sort(key=lambda x: (not x["mandatory"], -x["doc_pos"]))
    return issues


# =============================================================================
# EXPORT XML -> STRING
# =============================================================================
def shop_to_bytes(shop_root):
    return etree.tostring(
        shop_root, xml_declaration=True, encoding="utf-8", pretty_print=True
    )


# =============================================================================
# STREAMLIT UI
# =============================================================================
st.title("🛠️ Bosch → Shoptet Feed Validator")
st.caption(
    "Nahraj dodavatelský feed (Bosch BMEcat) nebo už hotový Shoptet XML feed. "
    "Nástroj se pokusí feed automaticky převést/opravit tak, aby byl validní pro Shoptet, "
    "ukáže co udělal, a zbytek necháš doladit ručně nebo hromadně."
)

if "shop_root" not in st.session_state:
    st.session_state.shop_root = None
    st.session_state.conversion_log = []
    st.session_state.source_type = None

with st.sidebar:
    st.header("⚙️ Nastavení převodu")
    category_sep = st.text_input("Oddělovač kategorií", value=" > ")
    currency = st.text_input("Měna (CURRENCY)", value="CZK")
    weight_feature_names = st.text_input(
        "Název(y) FEATURE pro váhu (odděl čárkou)", value="Hmotnost, Weight"
    )
    generate_parameters = st.checkbox("Generovat PARAMETERS z technických FEATURE", value=True)
    technical_groups = st.text_input(
        "Whitelist skupin FEATURE pro parametry (REFERENCE_FEATURE_SYSTEM_NAME, odděl čárkou; "
        "prázdné = vzít všechny skupiny)",
        value="udf_BOSCH_PMDB-0.1",
        help="U Bosch feedu takto vynecháš marketingové bloky a HOTSPOT metadata, "
             "které nejsou skutečné technické parametry.",
    )
    max_images = st.number_input(
        "Max. počet obrázků na produkt (0 = bez omezení)", min_value=0, value=8, step=1
    )

    st.markdown("---")
    st.header("📖 Slovník Shoptet tagů")
    search_term = st.text_input("Hledat tag / popis:", "").lower()
    for tag, info in SHOPTET_DICT.items():
        if search_term in tag.lower() or search_term in info["desc"].lower():
            badge = "🔴 POVINNÝ" if info["mandatory"] else "⚪ nepovinný"
            st.markdown(f"**`<{tag}>`** — {badge}")
            st.caption(info["desc"])

uploaded_file = st.file_uploader("Nahraj XML feed (Bosch BMEcat nebo Shoptet)", type=["xml"])

if uploaded_file is not None and st.session_state.shop_root is None:
    data = uploaded_file.read()
    root = parse_xml_bytes(data)
    ftype = detect_feed_type(root)
    st.session_state.source_type = ftype

    if ftype == "bmecat":
        options = {
            "category_sep": category_sep,
            "currency": currency,
            "weight_feature_names": weight_feature_names,
            "generate_parameters": generate_parameters,
            "technical_groups": technical_groups.split(","),
            "max_images": max_images if max_images > 0 else None,
        }
        shop, log = convert_bmecat_to_shoptet(root, options)
        st.session_state.shop_root = shop
        st.session_state.conversion_log = log
    elif ftype == "shoptet":
        st.session_state.shop_root = root
        st.session_state.conversion_log = ["Feed je už ve formátu Shoptet SHOP/SHOPITEM, žádný automatický převod nebyl potřeba."]
    else:
        st.error("Nerozpoznaný formát XML (očekávám kořenový element BMECAT nebo SHOP).")

if st.button("🔄 Načíst feed znovu od začátku (reset)"):
    st.session_state.shop_root = None
    st.session_state.conversion_log = []
    st.rerun()

if st.session_state.shop_root is not None:
    shop_root = st.session_state.shop_root
    items = shop_root.xpath("./*[local-name()='SHOPITEM']")

    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "1️⃣ Log automatického převodu",
        "2️⃣ Validace a oprava",
        "3️⃣ Sloučení / hromadné úpravy",
        "4️⃣ Obrázky",
        "5️⃣ Živý náhled a export",
    ])

    # -------------------------------------------------------------------
    with tab1:
        st.subheader(f"Zdrojový formát: {st.session_state.source_type}")
        st.write(f"Počet produktů (SHOPITEM): **{len(items)}**")
        st.markdown("**Co bylo provedeno automaticky:**")
        for line in st.session_state.conversion_log:
            if line.startswith("⚠️"):
                st.warning(line)
            else:
                st.write("• " + line)

    # -------------------------------------------------------------------
    with tab2:
        issues = validate_shop(shop_root)
        n_mandatory = sum(1 for i in issues if i["mandatory"])
        n_optional = len(issues) - n_mandatory

        c1, c2, c3 = st.columns(3)
        c1.metric("Produktů celkem", len(items))
        c2.metric("Povinných chyb", n_mandatory)
        c3.metric("Doporučených chyb", n_optional)

        if not issues:
            st.success("🎉 Feed neobsahuje žádné chybějící povinné ani doporučené elementy.")
        else:
            st.write(
                "Chyby jsou řazené: **nejdřív povinné, pak doporučené**, "
                "uvnitř skupiny **sestupně podle pozice v XML**."
            )
            # seskup podle tagu, zachovej pořadí první výskyt = nejvyšší pozice v dané skupině
            grouped = {}
            for i in issues:
                grouped.setdefault(i["tag"], []).append(i)

            # pořadí skupin: podle mandatory a podle max doc_pos v ní (desc)
            group_order = sorted(
                grouped.keys(),
                key=lambda t: (not grouped[t][0]["mandatory"], -max(x["doc_pos"] for x in grouped[t])),
            )

            for tag in group_order:
                group = grouped[tag]
                mand = group[0]["mandatory"]
                label = f"{'🔴 POVINNÉ' if mand else '⚪ Doporučené'} — <{tag}> chybí u {len(group)} produktů"
                with st.expander(label, expanded=mand):
                    info = SHOPTET_DICT.get(tag if tag != "IMAGE_URL" else "IMAGE", {})
                    if info:
                        st.caption(info.get("desc", ""))

                    df = pd.DataFrame([{"CODE": g["code"], "NAME": g["name"], "Nová hodnota": ""} for g in group])

                    bulk_col1, bulk_col2 = st.columns([3, 1])
                    with bulk_col1:
                        bulk_value = st.text_input(
                            f"Hromadná hodnota pro všechny chybějící <{tag}>",
                            key=f"bulk_{tag}",
                            help="Nastaví stejnou hodnotu všem produktům v této skupině naráz.",
                        )
                    with bulk_col2:
                        st.write("")
                        st.write("")
                        apply_bulk = st.button("Použít na všechny", key=f"apply_bulk_{tag}")

                    edited = st.data_editor(
                        df, key=f"editor_{tag}", use_container_width=True, num_rows="fixed",
                        disabled=["CODE", "NAME"],
                    )
                    apply_individual = st.button("Uložit individuální hodnoty výše", key=f"apply_ind_{tag}")

                    def set_value(item_el, tag_name, value):
                        if tag_name == "IMAGE_URL":
                            return  # řeší se v tabu Obrázky
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
                            val = row["Nová hodnota"]
                            if val:
                                set_value(group[idx]["item"], tag, val)
                                count += 1
                        if count:
                            st.success(f"Uloženo {count} individuálních hodnot.")
                            st.rerun()

    # -------------------------------------------------------------------
    with tab3:
        st.subheader("🔗 Obecné sloučení dvou elementů")
        st.write(
            "Najde všechny výskyty, kde má rodičovský element zároveň oba vybrané tagy, "
            "a sloučí jejich hodnoty do jednoho cílového tagu (např. hodnota + jednotka)."
        )
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
                        v1 = (first.text or "").strip()
                        v2 = (second.text or "").strip()
                        combined = f"{v1}{sep}{v2}" if v1 and v2 else (v1 or v2)
                        first.tag = target_tag
                        first.text = combined
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

    # -------------------------------------------------------------------
    with tab4:
        st.subheader("🖼️ Obrázky produktů")
        st.info(
            "Shoptet stahuje obrázky přímo z URL v <IMAGE>. Bosch BMEcat feed obsahuje jen "
            "názvy souborů (bez veřejné adresy), proto je potřeba nastavit šablonu, kam jsi "
            "obrázky nahrál/-a (vlastní hosting, S3, Google Drive přímý odkaz apod.)."
        )
        image_els = shop_root.xpath(".//*[local-name()='IMAGE']")
        raw_count = sum(1 for e in image_els if e.text and not e.text.strip().lower().startswith(("http://", "https://")))
        st.write(f"Obrázků celkem: **{len(image_els)}**, z toho bez platné URL: **{raw_count}**")

        template = st.text_input(
            "Šablona URL (použij {filename} jako zástupný znak za název souboru, mezery se automaticky zakódují)",
            value="https://TVUJ-HOSTING.cz/bosch-obrazky/{filename}",
        )
        st.caption(
            "Např. u Google Drive použij formát přímého odkazu: "
            "'https://drive.google.com/uc?export=view&id=ID_SOUBORU' — ale pozor, Drive má limity "
            "na hotlinkování a pro produkční feed se doporučuje spíš vlastní hosting/CDN."
        )
        if st.button("Aplikovat šablonu na všechny obrázky bez URL", type="primary"):
            from urllib.parse import quote
            cnt = 0
            for el in image_els:
                txt = (el.text or "").strip()
                if txt and not txt.lower().startswith(("http://", "https://")):
                    el.text = template.replace("{filename}", quote(txt))
                    cnt += 1
            st.success(f"Doplněna URL u {cnt} obrázků.")
            st.rerun()

        with st.expander("Náhled prvních 10 obrázkových hodnot"):
            for e in image_els[:10]:
                st.code(e.text or "")

    # -------------------------------------------------------------------
    with tab5:
        st.subheader("Živý náhled výsledného Shoptet XML")
        xml_bytes = shop_to_bytes(shop_root)
        st.download_button(
            "📥 Stáhnout výsledný Shoptet XML feed",
            data=xml_bytes,
            file_name="shoptet_feed.xml",
            mime="application/xml",
            type="primary",
        )
        st.code(xml_bytes.decode("utf-8")[:20000], language="xml")
        if len(xml_bytes) > 20000:
            st.caption("(náhled zkrácen na prvních ~20 000 znaků, stažený soubor je kompletní)")
else:
    st.info("⬆️ Nahraj feed výše pro začátek.")
