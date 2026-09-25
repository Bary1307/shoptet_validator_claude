import streamlit as st
from lxml import etree
import pandas as pd
import json
import re
from copy import deepcopy
from urllib.parse import quote

st.set_page_config(page_title="Univerzální Feed → Shoptet Validator", layout="wide")

# =============================================================================
# SLOVNÍK SHOPTET TAGŮ
# =============================================================================
SHOPTET_DICT = {
    "CODE": {"desc": "Unikátní kód / SKU produktu.", "mandatory": True},
    "NAME": {"desc": "Název produktu.", "mandatory": True},
    "SHORT_DESCRIPTION": {"desc": "Krátký popis.", "mandatory": False},
    "DESCRIPTION": {"desc": "Detailní popis produktu, podporuje HTML.", "mandatory": False},
    "MANUFACTURER": {"desc": "Značka / výrobce.", "mandatory": False},
    "EAN": {"desc": "EAN / čárový kód.", "mandatory": False},
    "PART_NUMBER": {"desc": "Katalogové číslo výrobce.", "mandatory": False},
    "CURRENCY": {"desc": "Měna ceny (CZK, EUR...).", "mandatory": True},
    "PRICE": {"desc": "Cena produktu.", "mandatory": True},
    "VAT": {"desc": "Sazba DPH v procentech (např. 21).", "mandatory": False},
    "STANDARD_PRICE": {"desc": "Původní/přeškrtnutá cena před naší slevou (zobrazí se v Shoptetu jako přeškrtnutá).", "mandatory": False},
    "CATEGORY": {"desc": "Cesta kategorie.", "mandatory": True},
    "IMAGE": {"desc": "Přímá veřejná URL adresa obrázku.", "mandatory": False},
    "WEIGHT": {"desc": "Hmotnost v kg.", "mandatory": False},
    "VISIBILITY": {"desc": "'visible' nebo 'hidden'.", "mandatory": False},
    "AVAILABILITY_IN_STOCK": {"desc": "Text dostupnosti, když je produkt skladem (např. 'Skladem').", "mandatory": False},
    "AVAILABILITY_OUT_OF_STOCK": {"desc": "Text dostupnosti, když produkt není skladem - musí odpovídat jedné z předdefinovaných hodnot Shoptetu.", "mandatory": False},
    "PARAMETERS/PARAMETER": {"desc": "POUZE pro velikosti (oblečení/obuv).", "mandatory": False},
    "INFORMATION_PARAMETERS/INFORMATION_PARAMETER": {"desc": "Obecné/technické parametry produktu.", "mandatory": False},
}

DEFAULT_PROFILE = {
    "profile_name": "Nová šablona",
    "item_tag": "",
    "fields": {t: {"source": ""} for t in ["CODE", "NAME", "DESCRIPTION", "MANUFACTURER", "EAN", "PART_NUMBER"]},
    "price": {"mode": "direct", "source": "", "price_type_attr": "", "price_type_value": ""},
    "currency": "CZK",
    "category": {"mode": "fixed", "fixed_value": "Nezařazeno", "source": "", "sep": " > "},
    "weight": {"mode": "none", "source": "", "feature_element": "", "fname_field": "", "fvalue_field": "",
               "funit_field": "", "names": "Hmotnost, Weight"},
    "images": {"mode": "none", "source": "", "mime_element": "", "purpose_field": "", "purpose_value": "normal",
               "source_field": "", "max_images": 8},
    "params": {"repeat_element": "", "classifier_scope": "", "classifier_field": "", "fname_field": "", "fvalue_field": "",
               "funit_field": "", "value_map": {}, "marketing_target": "SHORT_DESCRIPTION"},
    "visibility_default": "visible",
    "name_prefix": "",
    "name_suffix": "",
    "description_format": {"enabled": False, "paragraphs": True, "extract_packaging": True,
                            "headings": True, "bold_numbers": True},
    "price_adjust": {"discount_percent": 0, "add_vat": False, "vat_percent": "21"},
    "availability": {"enabled": False, "in_stock": "Skladem", "out_of_stock": "2-3 dny"},
}

STEPS = [
    "1. Nahrání feedu",
    "2. Základní mapování polí",
    "3. Kategorie",
    "4. Parametry",
    "5. Obrázky",
    "6. Výběr produktů",
    "7. Kontrola a ruční opravy",
    "8. Export a šablona",
]


# =============================================================================
# POMOCNÉ FUNKCE
# =============================================================================
def gtext(el, tag):
    if not tag:
        return None
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
    return "shoptet" if root.tag.split("}")[-1] == "SHOP" else "other"


def local_tag(el):
    return el.tag.split("}")[-1] if isinstance(el.tag, str) else str(el.tag)


def candidate_item_tags(root, min_count=2):
    counts, parent_refs, child_tag_sets = {}, {}, {}
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
        ratio = c / n_parents
        if ratio < 1.5:
            continue
        candidates.append((t, c, ratio, len(child_tag_sets.get(t, set()))))
    candidates.sort(key=lambda x: (-x[3], -x[2], -x[1]))
    return [(t, c) for t, c, _, _ in candidates]


def descendant_tags(item_el):
    tags = sorted(set(local_tag(e) for e in item_el.iter()))
    return [t for t in tags if t != local_tag(item_el)]


def aggregate_descendant_tags(items, sample_size=30):
    """Sjednotí podřazené tagy napříč VÍC produkty, ne jen prvním - různé produkty
    ve stejném feedu můžou mít různě neúplnou strukturu (chybějící cena, jiné MIME...)."""
    tags = set()
    for item in items[:sample_size]:
        tags.update(local_tag(e) for e in item.iter())
    tags.discard(local_tag(items[0])) if items else None
    return sorted(tags)


def repeat_children_candidates(item_el, min_count=2):
    """Najde opakující se podřazené elementy uvnitř JEDNOHO produktu (kandidáty na
    'jeden parametr/vlastnost'), např. FEATURE."""
    counts, child_tag_sets = {}, {}
    for el in item_el.iter():
        if el is item_el:
            continue
        t = local_tag(el)
        counts[t] = counts.get(t, 0) + 1
        s = child_tag_sets.setdefault(t, set())
        for child in el:
            s.add(local_tag(child))
    candidates = [(t, c) for t, c in counts.items() if c >= min_count and len(child_tag_sets.get(t, set())) >= 1]
    candidates.sort(key=lambda x: -x[1])
    return candidates


def classifier_field_candidates(root, item_tag, repeat_tag, sample_size=30):
    """Najde kandidáty na 'rozlišovací pole' druhu parametru - buď uvnitř samotné
    opakující se vlastnosti (repeat_tag, např. FDESCR uvnitř FEATURE), NEBO na jejím
    přímém rodiči (např. REFERENCE_FEATURE_GROUP_NAME na obalu ARTICLE_FEATURES,
    jak to dělá třeba Metabo). Vrací (scope, field) dvojice, scope je 'self'/'parent'."""
    items = root.xpath(f"//*[local-name()='{item_tag}']")[:sample_size]
    self_values, parent_values = {}, {}
    for item in items:
        for feat in item.xpath(f".//*[local-name()='{repeat_tag}']"):
            for child in feat:
                fname = local_tag(child)
                if child.text and child.text.strip():
                    self_values.setdefault(fname, []).append(child.text.strip())
            parent = feat.getparent()
            if parent is not None:
                for child in parent:
                    if local_tag(child) == repeat_tag:
                        continue
                    fname = local_tag(child)
                    if child.text and child.text.strip():
                        parent_values.setdefault(fname, []).append(child.text.strip())

    def score_all(values_dict):
        scored = []
        for f, vals in values_dict.items():
            distinct = len(set(vals))
            if 1 < distinct <= max(20, len(vals) * 0.3):
                scored.append((f, distinct))
        scored.sort(key=lambda x: x[1])
        return [f for f, _ in scored]

    # rodičovské (skupinové) pole preferujeme jako první tipy, protože je to častější
    # a spolehlivější vzor (jedna skupina = jeden druh parametru) než pole uvnitř samotné vlastnosti
    result = [("parent", f) for f in score_all(parent_values)]
    result += [("self", f) for f in score_all(self_values)]
    return result


def get_classifier_values(root, item_tag, repeat_tag, scope, classifier_field, sample_size=200):
    items = root.xpath(f"//*[local-name()='{item_tag}']")[:sample_size]
    counts = {}
    for item in items:
        for feat in item.xpath(f".//*[local-name()='{repeat_tag}']"):
            if scope == "parent":
                parent = feat.getparent()
                v = gtext(parent, classifier_field) if parent is not None else None
            else:
                v = gtext(feat, classifier_field)
            if v:
                counts[v] = counts.get(v, 0) + 1
    return sorted(counts.items(), key=lambda x: -x[1])


def guess_bucket(value):
    v = (value or "").lower()
    if "market" in v:
        return "marketing"
    if "techni" in v or "technic" in v:
        return "technical"
    if "velikost" in v or "size" in v:
        return "size"
    return "ignore"


# =============================================================================
# FORMÁTOVÁNÍ DESCRIPTION - z jednoho souvislého bloku textu udělá strukturu
# =============================================================================
PACKAGING_RE = re.compile(r"(balen[ií]|krabice|karton|obal(?:u|em)?|\d+\s*[×x]\s*\S)", re.IGNORECASE)
NUMBER_UNIT_RE = re.compile(
    r"(?<![A-Za-z0-9])(\d[\d\s\u00a0.,]*\d|\d)\s?(W|kg|g|mm|cm|km|V|Ah|mAh|°C|°F|l|ml|Nm|min|kWh)(?![A-Za-z0-9])"
)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ0-9])")


def format_description_html(text, opts):
    if not text or not opts or not opts.get("enabled"):
        return text
    text = text.strip()
    packaging_html = ""

    if opts.get("extract_packaging"):
        chunks = SENTENCE_SPLIT_RE.split(text)
        keep, packaging = [], []
        for c in chunks:
            (packaging if PACKAGING_RE.search(c) else keep).append(c)
        if packaging:
            text = " ".join(keep).strip()
            packaging_html = "<h3>Obsah balení</h3><p>" + " ".join(p.strip() for p in packaging) + "</p>"

    if opts.get("bold_numbers"):
        text = NUMBER_UNIT_RE.sub(r"<strong>\1 \2</strong>", text)

    if opts.get("paragraphs"):
        sentences = [s for s in SENTENCE_SPLIT_RE.split(text) if s.strip()]
        paras, group = [], []
        for s in sentences:
            group.append(s)
            if len(group) >= 2:
                paras.append(" ".join(group))
                group = []
        if group:
            paras.append(" ".join(group))
        body_html = "".join(f"<p>{p}</p>" for p in paras) if paras else f"<p>{text}</p>"
    else:
        body_html = f"<p>{text}</p>"

    html = "<h3>Popis</h3>" if opts.get("headings") else ""
    html += body_html + packaging_html
    return html


# =============================================================================
# KATEGORIE - stromový resolver (pro BMEcat styl, volitelný)
# =============================================================================
def build_tree_category_map(root, cfg):
    cats = {}
    for cs in root.xpath(f"//*[local-name()='{cfg['struct_element']}']"):
        gid = gtext(cs, cfg["group_id"])
        cats[gid] = {"name": gtext(cs, cfg["group_name"]), "parent": gtext(cs, cfg["parent_id"]), "type": cs.get("type")}
    return cats


def build_tree_links(root, cfg):
    m = {}
    for link in root.xpath(f"//*[local-name()='{cfg['link_element']}']"):
        art_id = gtext(link, cfg["id_field"])
        gid = gtext(link, cfg["group_id_field"])
        if art_id:
            m.setdefault(art_id, []).append(gid)
    return m


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


# =============================================================================
# APLIKACE PROFILU -> SHOP/SHOPITEM (čistá funkce, žádný vedlejší stav)
# =============================================================================
def apply_profile(root, profile):
    log = []
    item_tag = profile["item_tag"]
    items_src = root.xpath(f"//*[local-name()='{item_tag}']") if item_tag else []
    shop = etree.Element("SHOP")
    if not item_tag:
        return shop, ["⚠️ Ještě není vybraný element produktu (krok 2)."]

    log.append(f"Nalezeno {len(items_src)} produktů podle elementu <{item_tag}>.")

    n_price_missing = n_img = n_marketing = n_technical = n_size = 0

    for src in items_src:
        item = etree.SubElement(shop, "SHOPITEM")

        for target, fcfg in profile["fields"].items():
            if fcfg.get("source"):
                val = gtext_deep(src, fcfg["source"])
                if val:
                    etree.SubElement(item, target).text = val

        desc_el = item.xpath("./*[local-name()='DESCRIPTION']")
        if desc_el and profile.get("description_format", {}).get("enabled"):
            desc_el[0].text = format_description_html(desc_el[0].text, profile["description_format"])

        code_val = gtext(item, "CODE")

        # cena
        pcfg = profile["price"]
        price_val = None
        if pcfg["mode"] == "direct" and pcfg["source"]:
            price_val = gtext_deep(src, pcfg["source"])
        elif pcfg["mode"] == "price_type" and pcfg["source"]:
            cands = src.xpath(
                f".//*[local-name()='ARTICLE_PRICE'][@{pcfg['price_type_attr']}='{pcfg['price_type_value']}']"
            ) if pcfg.get("price_type_attr") else src.xpath(".//*[local-name()='ARTICLE_PRICE']")
            if not cands:
                cands = src.xpath(".//*[local-name()='ARTICLE_PRICE']")
            if cands:
                price_val = gtext_deep(cands[0], pcfg["source"])
        if price_val:
            etree.SubElement(item, "CURRENCY").text = profile["currency"]
            try:
                etree.SubElement(item, "PRICE").text = f"{float(price_val.replace(',', '.')):.2f}"
            except ValueError:
                etree.SubElement(item, "PRICE").text = price_val
        else:
            n_price_missing += 1

        # kategorie
        ccfg = profile["category"]
        cats_el = etree.SubElement(item, "CATEGORIES")
        if ccfg["mode"] == "fixed":
            etree.SubElement(cats_el, "CATEGORY").text = ccfg["fixed_value"]
        elif ccfg["mode"] == "direct" and ccfg["source"]:
            for e in gall_deep(src, ccfg["source"]):
                if e.text and e.text.strip():
                    etree.SubElement(cats_el, "CATEGORY").text = e.text.strip()
        if len(cats_el) == 0:
            item.remove(cats_el)

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
                                v /= 1000.0
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
        base = icfg.get("public_base_url")
        if base:
            base = base if base.endswith("/") else base + "/"
            images = [img if img.lower().startswith(("http://", "https://")) else base + quote(img) for img in images]
        if images:
            n_img += len(images)
            images_el = etree.SubElement(item, "IMAGES")
            item_name = gtext(item, "NAME") or ""
            for i, f in enumerate(images, start=1):
                img_el = etree.SubElement(images_el, "IMAGE")
                img_el.text = f
                if icfg.get("add_description", True) and item_name:
                    desc = item_name if len(images) == 1 or i == 1 else f"{item_name} {i}"
                    img_el.set("description", desc)

        # parametry - klasifikace podle hodnoty vybraného pole
        pcfg2 = profile["params"]
        marketing_bullets, technical_pairs, size_pairs = [], [], []
        if pcfg2["repeat_element"] and pcfg2["fname_field"] and pcfg2["fvalue_field"]:
            for feat in src.xpath(f".//*[local-name()='{pcfg2['repeat_element']}']"):
                cls_val = None
                if pcfg2.get("classifier_field"):
                    if pcfg2.get("classifier_scope") == "parent":
                        parent = feat.getparent()
                        cls_val = gtext(parent, pcfg2["classifier_field"]) if parent is not None else None
                    else:
                        cls_val = gtext(feat, pcfg2["classifier_field"])
                bucket = pcfg2["value_map"].get(cls_val, "ignore")
                fname = gtext(feat, pcfg2["fname_field"])
                fvals = [e.text.strip() for e in feat.xpath(f"./*[local-name()='{pcfg2['fvalue_field']}']") if e.text and e.text.strip()]
                funit = gtext(feat, pcfg2["funit_field"]) if pcfg2["funit_field"] else None
                if bucket == "marketing" and (fvals or fname):
                    marketing_bullets.extend(fvals if fvals else [fname])
                elif bucket == "technical" and fname and fvals:
                    technical_pairs.append((fname, "; ".join(fvals), funit))
                elif bucket == "size" and fname and fvals:
                    size_pairs.append((fname, "; ".join(fvals), funit))

        if marketing_bullets:
            n_marketing += len(marketing_bullets)
            text = " • ".join(marketing_bullets)
            if pcfg2["marketing_target"] == "SHORT_DESCRIPTION":
                existing = item.xpath("./*[local-name()='SHORT_DESCRIPTION']")
                if existing:
                    existing[0].text = (existing[0].text or "") + " • " + text
                else:
                    etree.SubElement(item, "SHORT_DESCRIPTION").text = text
            else:
                desc = item.xpath("./*[local-name()='DESCRIPTION']")
                bullets_html = "<ul>" + "".join(f"<li>{b}</li>" for b in marketing_bullets) + "</ul>"
                if desc:
                    desc[0].text = (desc[0].text or "") + bullets_html
                else:
                    etree.SubElement(item, "DESCRIPTION").text = bullets_html

        if technical_pairs:
            n_technical += len(technical_pairs)
            params_el = etree.SubElement(item, "INFORMATION_PARAMETERS")
            for fname, fval, funit in technical_pairs:
                p = etree.SubElement(params_el, "INFORMATION_PARAMETER")
                etree.SubElement(p, "NAME").text = fname
                etree.SubElement(p, "VALUE").text = f"{fval} {funit}".strip() if funit else fval

        if size_pairs:
            n_size += len(size_pairs)
            params_el = etree.SubElement(item, "PARAMETERS")
            for fname, fval, funit in size_pairs:
                p = etree.SubElement(params_el, "PARAMETER")
                etree.SubElement(p, "NAME").text = fname
                etree.SubElement(p, "VALUE").text = f"{fval} {funit}".strip() if funit else fval

        etree.SubElement(item, "VISIBILITY").text = profile["visibility_default"]

    if n_price_missing:
        log.append(f"⚠️ {n_price_missing} produktů nemá dohledatelnou cenu.")
    if n_img:
        log.append(f"Nalezeno {n_img} obrázkových položek.")
    if n_marketing:
        log.append(f"{n_marketing} marketingových textů zapsáno do popisu.")
    if n_technical:
        log.append(f"{n_technical} technických parametrů do <INFORMATION_PARAMETERS>.")
    if n_size:
        log.append(f"{n_size} velikostních parametrů do <PARAMETERS>.")
    return shop, log


def apply_name_affixes(shop_root, prefix, suffix):
    if not prefix and not suffix:
        return shop_root
    for item in shop_root.xpath("./*[local-name()='SHOPITEM']"):
        name_el = item.xpath("./*[local-name()='NAME']")
        if name_el and name_el[0].text:
            parts = [p for p in [(prefix or "").strip(), name_el[0].text.strip(), (suffix or "").strip()] if p]
            name_el[0].text = " ".join(parts)
    return shop_root


OUT_OF_STOCK_OPTIONS = [
    "2-3 dny",
    "Obvykle do 7 dní",
    "Produkt již není v prodeji",
    "Předprodej",
    "Na dotaz",
]


def apply_availability(shop_root, cfg):
    """Hromadně nastaví text dostupnosti skladem/nedostupné pro všechny produkty."""
    if not cfg or not cfg.get("enabled"):
        return shop_root
    in_stock = (cfg.get("in_stock") or "").strip()
    out_of_stock = (cfg.get("out_of_stock") or "").strip()
    for item in shop_root.xpath("./*[local-name()='SHOPITEM']"):
        if in_stock:
            el = item.xpath("./*[local-name()='AVAILABILITY_IN_STOCK']")
            (el[0] if el else etree.SubElement(item, "AVAILABILITY_IN_STOCK")).text = in_stock
        if out_of_stock:
            el = item.xpath("./*[local-name()='AVAILABILITY_OUT_OF_STOCK']")
            (el[0] if el else etree.SubElement(item, "AVAILABILITY_OUT_OF_STOCK")).text = out_of_stock
    return shop_root


def apply_price_adjustment(shop_root, cfg):
    """Hromadná úprava ceny.
    Cílem je, aby VÝSLEDNÁ zobrazená cena s DPH v Shoptetu vyšla na celé číslo.
    Protože si Shoptet DPH k <PRICE> dopočítává sám (bere <PRICE> jako cenu bez DPH a
    násobí ji podle <VAT>), nejde do <PRICE> poslat rovnou kulatou cenu s DPH - to by
    Shoptet vzal jako základ a k tomu ještě připočetl DPH podruhé.
    Proto se to počítá "pozpátku": nejdřív se spočte cílová KULATÁ cena s DPH, a do
    <PRICE> (resp. <STANDARD_PRICE>) se zapíše taková cena BEZ DPH, aby po Shoptetově
    vlastním přičtení DPH vyšla přesně ta kulatá částka.
    Čistá funkce na hotovém stromu, takže funguje nezávisle na tom, odkud cena původně přišla."""
    if not cfg:
        return shop_root
    discount = cfg.get("discount_percent") or 0
    add_vat = cfg.get("add_vat", False)
    try:
        vat = float(str(cfg.get("vat_percent", "21")).replace(",", "."))
    except (TypeError, ValueError):
        vat = 0
    if not discount and not add_vat:
        return shop_root
    for item in shop_root.xpath("./*[local-name()='SHOPITEM']"):
        price_el = item.xpath("./*[local-name()='PRICE']")
        if not price_el or not price_el[0].text:
            continue
        try:
            p = float(price_el[0].text.replace(",", "."))
        except ValueError:
            continue

        # cílové (kulaté) zobrazené ceny s DPH
        target_list = round(p * (1 + vat / 100.0)) if add_vat else round(p)
        target_final = round(target_list * (1 - discount / 100.0)) if discount else target_list

        if add_vat:
            # zapisujeme cenu BEZ DPH, aby Shoptet po připočtení DPH dostal přesně cílovou kulatou cenu
            fed_list = target_list / (1 + vat / 100.0)
            fed_final = target_final / (1 + vat / 100.0)

            def fmt(v):
                return f"{v:.4f}"
        else:
            fed_list, fed_final = target_list, target_final

            def fmt(v):
                return str(int(v))

        price_el[0].text = fmt(fed_final)
        if discount:
            std_el = item.xpath("./*[local-name()='STANDARD_PRICE']")
            if std_el:
                std_el[0].text = fmt(fed_list)
            else:
                etree.SubElement(item, "STANDARD_PRICE").text = fmt(fed_list)
        if add_vat:
            vat_el = item.xpath("./*[local-name()='VAT']")
            if vat_el:
                vat_el[0].text = str(cfg.get("vat_percent", "21"))
            else:
                etree.SubElement(item, "VAT").text = str(cfg.get("vat_percent", "21"))
    return shop_root


def apply_overrides(shop_root, overrides):
    """Ruční opravy z kroku 7 - aplikují se navrch čerstvě vygenerovaného stromu,
    takže přežijí návrat na dřívější krok a přepočítání."""
    if not overrides:
        return shop_root
    for item in shop_root.xpath("./*[local-name()='SHOPITEM']"):
        code = gtext(item, "CODE") or ""
        for tag, value in overrides.get(code, {}).items():
            if tag == "CATEGORY":
                cats_el = item.xpath("./*[local-name()='CATEGORIES']")
                if not cats_el:
                    cats_el = [etree.SubElement(item, "CATEGORIES")]
                etree.SubElement(cats_el[0], "CATEGORY").text = value
                continue
            existing = item.xpath(f"./*[local-name()='{tag}']")
            if existing:
                existing[0].text = value
            else:
                etree.SubElement(item, tag).text = value
    return shop_root


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
            issues.append({"tag": tag, "mandatory": mandatory, "code": code, "name": name, "item": item,
                            "doc_pos": pos, "message": message})

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
        if not item.xpath("./*[local-name()='IMAGES']/*[local-name()='IMAGE']"):
            missing("IMAGES", False, "Produkt nemá žádný obrázek.")
        else:
            for img in item.xpath("./*[local-name()='IMAGES']/*[local-name()='IMAGE']"):
                txt = (img.text or "").strip()
                if txt and not txt.lower().startswith(("http://", "https://")):
                    issues.append({"tag": "IMAGE_URL", "mandatory": True, "code": code, "name": name, "item": item,
                                   "doc_pos": order.get(img, pos), "message": f"IMAGE '{txt}' není platná URL."})
        if not gtext(item, "VISIBILITY"):
            missing("VISIBILITY", False, "Chybí VISIBILITY.")
    issues.sort(key=lambda x: (not x["mandatory"], -x["doc_pos"]))
    return issues


def shop_to_bytes(shop_root):
    return etree.tostring(shop_root, xml_declaration=True, encoding="utf-8", pretty_print=True)


# =============================================================================
# SESSION STATE
# =============================================================================
ss = st.session_state
ss.setdefault("raw_root", None)
ss.setdefault("profile", json.loads(json.dumps(DEFAULT_PROFILE)))
ss.setdefault("overrides", {})
ss.setdefault("excluded_codes", set())
ss.setdefault("step", 1)


def compute_shop():
    if ss.raw_root is None:
        return None, []
    if detect_feed_type(ss.raw_root) == "shoptet":
        shop = deepcopy(ss.raw_root)
        log = ["Feed byl už ve formátu Shoptet, načten beze změny."]
    else:
        shop, log = apply_profile(ss.raw_root, ss.profile)
    shop = apply_name_affixes(shop, ss.profile.get("name_prefix", ""), ss.profile.get("name_suffix", ""))
    shop = apply_price_adjustment(shop, ss.profile.get("price_adjust", {}))
    shop = apply_availability(shop, ss.profile.get("availability", {}))
    shop = apply_overrides(shop, ss.overrides)
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
    up_col1, up_col2 = st.columns(2)
    with up_col1:
        uploaded_file = st.file_uploader("Nahraj vstupní XML feed", type=["xml"])
    with up_col2:
        profile_file = st.file_uploader("Nebo nahraj uloženou šablonu (.json) a pak i feed", type=["json"])

    if uploaded_file is not None:
        data = uploaded_file.read()
        if ss.get("_last_filename") != uploaded_file.name:
            ss.raw_root = parse_xml_bytes(data)
            ss._last_filename = uploaded_file.name
            ss.overrides = {}
            ss.excluded_codes = set()

    if profile_file is not None and not ss.get("_profile_loaded"):
        try:
            ss.profile = json.load(profile_file)
            ss._profile_loaded = True
            st.success(f"Šablona '{ss.profile.get('profile_name', '')}' načtena.")
        except Exception as e:
            st.error(f"Nepodařilo se načíst šablonu: {e}")

    if ss.raw_root is not None:
        st.success("Feed nahrán.")
        n = len(ss.raw_root.xpath("//*[local-name()!='']")) if ss.raw_root is not None else 0
        st.caption(f"Typ: {detect_feed_type(ss.raw_root)}")
    nav_buttons()

elif ss.raw_root is None:
    st.warning("Nejdřív nahraj feed v kroku 1.")
    if st.button("⬅️ Jít na krok 1"):
        ss.step = 1
        st.rerun()

# =============================================================================
# KROK 2 - ZÁKLADNÍ MAPOVÁNÍ
# =============================================================================
elif step == 2:
    st.header(STEPS[1])
    root = ss.raw_root
    if detect_feed_type(root) == "shoptet":
        st.success("Tenhle feed už je ve formátu Shoptet - mapování polí není potřeba, pokračuj dál.")
    else:
        prof = ss.profile
        prof["profile_name"] = st.text_input("Název šablony", value=prof.get("profile_name", "Nová šablona"))

        candidates = candidate_item_tags(root)
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

        with st.expander("📝 Formátování DESCRIPTION (z jednoho bloku textu udělat strukturu)", expanded=False):
            dfcfg = prof.get("description_format", {})
            dfcfg["enabled"] = st.checkbox("Zapnout formátování popisu", value=dfcfg.get("enabled", False))
            if dfcfg["enabled"]:
                c1, c2 = st.columns(2)
                with c1:
                    dfcfg["paragraphs"] = st.checkbox("Rozdělit do odstavců", value=dfcfg.get("paragraphs", True))
                    dfcfg["headings"] = st.checkbox("Přidat nadpis 'Popis'", value=dfcfg.get("headings", True))
                with c2:
                    dfcfg["extract_packaging"] = st.checkbox("Vytáhnout obsah balení do vlastní sekce",
                                                               value=dfcfg.get("extract_packaging", True))
                    dfcfg["bold_numbers"] = st.checkbox("Zvýraznit klíčová čísla tučně (2 000 W, 0,6 kg...)",
                                                          value=dfcfg.get("bold_numbers", True))
                prof["description_format"] = dfcfg
                desc_source = prof["fields"].get("DESCRIPTION", {}).get("source")
                if desc_source and sample_items:
                    sample_raw = gtext_deep(sample_items[0], desc_source)
                    if sample_raw:
                        st.caption("Náhled na prvním produktu:")
                        st.markdown(format_description_html(sample_raw, dfcfg), unsafe_allow_html=True)
            else:
                prof["description_format"] = dfcfg

        st.markdown("#### Cena")
        pcfg = prof["price"]
        price_mode = st.radio("Zdroj ceny", ["Přímé pole", "BMEcat styl (ARTICLE_PRICE dle typu)"],
                               index=0 if pcfg["mode"] != "price_type" else 1, horizontal=True)
        prof["currency"] = st.text_input("Měna (CURRENCY)", value=prof.get("currency", "CZK"))
        if price_mode == "Přímé pole":
            sel = tag_select("Zdrojový tag ceny", pcfg.get("source", ""), "price_src")
            prof["price"] = {"mode": "direct", "source": "" if sel == "-- nevybráno --" else sel}
        else:
            attr = st.text_input("Atribut typu ceny (prázdné = ber první)", value=pcfg.get("price_type_attr", "price_type"))
            val = st.text_input("Hodnota atributu (např. net_list)", value=pcfg.get("price_type_value", "net_list"))
            sel = tag_select("Tag částky uvnitř ARTICLE_PRICE", pcfg.get("source", "PRICE_AMOUNT") or "PRICE_AMOUNT", "price_amt")
            prof["price"] = {"mode": "price_type", "source": "" if sel == "-- nevybráno --" else sel,
                              "price_type_attr": attr, "price_type_value": val}

        st.markdown("#### Hmotnost (nepovinné)")
        wcfg = prof["weight"]
        weight_mode = st.radio("Zdroj hmotnosti", ["Nevyplňovat", "Přímé pole", "FEATURE podle názvu (BMEcat styl)"],
                                index={"none": 0, "direct": 1, "feature": 2}.get(wcfg["mode"], 0), horizontal=True)
        if weight_mode == "Nevyplňovat":
            prof["weight"] = {"mode": "none"}
        elif weight_mode == "Přímé pole":
            sel = tag_select("Zdrojový tag hmotnosti", wcfg.get("source", ""), "weight_src")
            prof["weight"] = {"mode": "direct", "source": "" if sel == "-- nevybráno --" else sel}
        else:
            with st.expander("Nastavení FEATURE hmotnosti", expanded=True):
                fe = st.text_input("Element vlastnosti", value=wcfg.get("feature_element", "FEATURE"))
                fn = st.text_input("Pole s názvem", value=wcfg.get("fname_field", "FNAME"))
                fv = st.text_input("Pole s hodnotou", value=wcfg.get("fvalue_field", "FVALUE"))
                fu = st.text_input("Pole s jednotkou", value=wcfg.get("funit_field", "FUNIT"))
                names = st.text_input("Název(y) = hmotnost (odděl čárkou)", value=wcfg.get("names", "Hmotnost, Weight"))
            prof["weight"] = {"mode": "feature", "feature_element": fe, "fname_field": fn, "fvalue_field": fv,
                               "funit_field": fu, "names": names}

    render_live_preview()
    nav_buttons()

# =============================================================================
# KROK 3 - KATEGORIE
# =============================================================================
elif step == 3:
    st.header(STEPS[2])
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
# KROK 4 - PARAMETRY
# =============================================================================
elif step == 4:
    st.header(STEPS[3])
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
        st.warning("Nejdřív nastav element produktu v kroku 2.")
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

    render_live_preview()
    nav_buttons()

# =============================================================================
# KROK 5 - OBRÁZKY
# =============================================================================
elif step == 5:
    st.header(STEPS[4])
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

    render_live_preview()
    nav_buttons()

# =============================================================================
# KROK 6 - VÝBĚR PRODUKTŮ
# =============================================================================
elif step == 6:
    st.header(STEPS[5])
    shop, _ = compute_shop()
    all_items = shop.xpath("./*[local-name()='SHOPITEM']") if shop is not None else []
    st.caption("Nezaškrtnuté položky se nedostanou do finálního exportu.")
    rows = [{"Zahrnout": (gtext(i, "CODE") or "") not in ss.excluded_codes,
             "CODE": gtext(i, "CODE") or "", "NAME": gtext(i, "NAME") or ""} for i in all_items]
    df_sel = pd.DataFrame(rows)
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("Zaškrtnout vše"):
            ss.excluded_codes = set()
            st.rerun()
    with c2:
        if st.button("Odškrtnout vše"):
            ss.excluded_codes = set(df_sel["CODE"])
            st.rerun()
    with c3:
        st.metric("Vybráno", f"{len(all_items) - len(ss.excluded_codes)} / {len(all_items)}")
    edited = st.data_editor(df_sel, use_container_width=True, disabled=["CODE", "NAME"], hide_index=True, key="sel_editor")
    if st.button("💾 Uložit výběr", type="primary"):
        ss.excluded_codes = set(edited.loc[~edited["Zahrnout"], "CODE"])
        st.rerun()

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
        st.caption(
            "Cílem je vždy CELÉ číslo jako výsledná zobrazená cena v Shoptetu. Shoptet si u tohoto "
            "typu feedu DPH k <PRICE> dopočítává sám, takže když je DPH zapnuté, appka do <PRICE> a "
            "<STANDARD_PRICE> zapíše záměrně cenu BEZ DPH s desetinnými místy - je vypočítaná tak, "
            "aby po Shoptetově vlastním přičtení DPH vyšla přesně kulatá částka. Pokud je zadaná "
            "i naše sleva, cena PŘED slevou jde do <STANDARD_PRICE> (přeškrtnutá cena v Shoptetu) "
            "a cena PO slevě do <PRICE>."
        )
        pa = prof.get("price_adjust", {"discount_percent": 0, "add_vat": False, "vat_percent": "21"})
        c1, c2 = st.columns(2)
        with c1:
            pa["discount_percent"] = st.number_input(
                "NAŠE SLEVA (%) - odečte se od ceny s DPH", min_value=0.0, max_value=100.0,
                value=float(pa.get("discount_percent", 0) or 0), step=0.5,
            )
        with c2:
            pa["add_vat"] = st.checkbox("Přidat DPH k ceně (výsledná cena s DPH vyjde na celé číslo)", value=pa.get("add_vat", False))
            if pa["add_vat"]:
                pa["vat_percent"] = st.text_input(
                    "Sazba DPH (%) - textově, ať jde snadno změnit při změně legislativy",
                    value=str(pa.get("vat_percent", "21")),
                )

        prof["price_adjust"] = pa

        if pa["discount_percent"] or pa["add_vat"]:
            shop_preview, _ = compute_shop()
            first_price = first_standard = None
            if shop_preview is not None:
                pe = shop_preview.xpath(".//*[local-name()='SHOPITEM'][1]/*[local-name()='PRICE']")
                if pe and pe[0].text:
                    first_price = pe[0].text
                std = shop_preview.xpath(".//*[local-name()='SHOPITEM'][1]/*[local-name()='STANDARD_PRICE']")
                first_standard = std[0].text if std and std[0].text else None
            if first_price:
                if pa["add_vat"]:
                    vat_frac = 1 + float(str(pa.get("vat_percent", "21")).replace(",", ".")) / 100.0
                    displayed = round(float(first_price) * vat_frac)
                    line = f"Do feedu se zapíše `<PRICE>` = **{first_price}** (bez DPH) → v Shoptetu se zobrazí jako **{displayed} {prof.get('currency', '')}**"
                    if first_standard:
                        displayed_std = round(float(first_standard) * vat_frac)
                        line += f" (přeškrtnutá: ~~{displayed_std} {prof.get('currency', '')}~~)"
                else:
                    line = f"Náhled ceny prvního produktu po úpravě: **{first_price} {prof.get('currency', '')}**"
                    if first_standard:
                        line += f" (přeškrtnutá: ~~{first_standard} {prof.get('currency', '')}~~)"
                st.caption(line)

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
    all_items = shop.xpath("./*[local-name()='SHOPITEM']") if shop is not None else []
    export_shop = etree.Element("SHOP")
    n_excluded = 0
    for item in all_items:
        code = gtext(item, "CODE") or ""
        if code in ss.excluded_codes:
            n_excluded += 1
            continue
        export_shop.append(deepcopy(item))
    st.caption(f"Do exportu jde {len(all_items) - n_excluded} z {len(all_items)} produktů.")

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
    st.code(xml_bytes.decode("utf-8")[:20000], language="xml")
    if len(xml_bytes) > 20000:
        st.caption("(náhled zkrácen, stažený soubor je kompletní)")
    nav_buttons()
