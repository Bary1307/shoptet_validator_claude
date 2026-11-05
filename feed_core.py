"""Jádro převodu feedů dodavatelů do formátu Shoptet - bez Streamlitu.

Používá ho webová aplikace (shoptet_validator_app_17.py) i spouštěč pro automatický běh (run_feed.py).
Řádky níže od 'SLOVNÍK SHOPTET TAGŮ' po 'shop_to_bytes' jsou převzaté beze změny z shoptet_validator_app_16.py.
"""
import io
import gzip
import json
import os
import re
import unicodedata
import zipfile
from copy import deepcopy
from urllib.parse import quote

import pandas as pd
from lxml import etree

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
    "WIDTH": {"desc": "Šířka produktu v cm.", "mandatory": False},
    "HEIGHT": {"desc": "Výška produktu v cm.", "mandatory": False},
    "DEPTH": {"desc": "Hloubka/délka produktu v cm (element LENGTH Shoptet nepovoluje).", "mandatory": False},
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
               "funit_field": "", "names": "Hmotnost, Weight",
               "width_names": "", "height_names": "", "length_names": ""},
    "images": {"mode": "none", "source": "", "mime_element": "", "purpose_field": "", "purpose_value": "normal",
               "source_field": "", "max_images": 8},
    "params": {"repeat_element": "", "classifier_scope": "", "classifier_field": "", "fname_field": "", "fvalue_field": "",
               "funit_field": "", "value_map": {}, "marketing_target": "SHORT_DESCRIPTION", "marketing_roles": {}},
    "visibility_default": "visible",
    "videos": {"mode": "none", "mime_element": "MIME", "type_field": "MIME_TYPE", "type_value": "url",
               "descr_field": "MIME_DESCR", "descr_match": "video", "source_field": "MIME_SOURCE",
               "wrapper_tag": "RELATED_VIDEOS", "add_text": False},
    "references": {"enabled": False, "reference_element": "ARTICLE_REFERENCE", "type_mode": "attr", "type_attr": "type",
                   "type_field": "", "id_field": "ART_ID_TO", "included_types": [], "alt_types": [],
                   "only_existing": False, "wrapper_tag": "RELATED_PRODUCTS", "item_tag": "CODE"},
    "name_prefix": "",
    "name_suffix": "",
    "description_format": {"enabled": False, "paragraphs": True, "extract_packaging": True,
                            "headings": True, "bold_numbers": True, "dedupe": True, "dedupe_level": 50},
    "name_cleanup": {"enabled": False, "strip_after_semicolon": True, "strip_own_code_parens": True},
    "text_tools": {"enabled": False, "collapse_spaces": True, "name_case": "none", "short_desc_max": 0,
                   "replace_rules": []},
    "price_adjust": {"enabled": False, "source_mode": "net", "discount_percent": 0, "vat_percent": "21"},
    "price_precision": "2dp",
    "availability": {"enabled": False, "in_stock": "Skladem", "out_of_stock": "2-3 dny"},
}

STEPS = [
    "1. Nahrání feedu",
    "2. Výběr produktů",
    "3. Základní mapování polí",
    "4. Kategorie",
    "5. Parametry",
    "6. Obrázky",
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


YOUTUBE_ID_RE = re.compile(r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|embed/|shorts/|v/)|youtu\.be/)([A-Za-z0-9_-]{11})")


def extract_youtube_id(url):
    """Shoptet chce u videa jen kód YouTube videa (YOUTUBE_VIDEO_CODE), ne celou URL."""
    m = YOUTUBE_ID_RE.search(url or "")
    return m.group(1) if m else None


def get_attribute_values(root, item_tag, element_local_name, attr_name, sample_size=100):
    """Zjistí, jaké hodnoty atributu (např. type=...) se u daného elementu v praxi vyskytují -
    pro auto-detekci typů ARTICLE_REFERENCE (accessories/cross_selling apod.)."""
    items = root.xpath(f"//*[local-name()='{item_tag}']")[:sample_size]
    counts = {}
    for it in items:
        for el in it.xpath(f".//*[local-name()='{element_local_name}']"):
            v = el.get(attr_name)
            if v:
                counts[v] = counts.get(v, 0) + 1
    return sorted(counts.items(), key=lambda x: -x[1])


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
    if "scope" in v or "deliver" in v or "lieferumfang" in v or "obsah bal" in v:
        return "packaging"
    if "techni" in v or "technic" in v:
        return "technical"
    if "velikost" in v or "size" in v:
        return "size"
    return "ignore"


# =============================================================================
# FORMÁTOVÁNÍ DESCRIPTION - z jednoho souvislého bloku textu udělá strukturu
# =============================================================================
PACKAGING_RE = re.compile(
    r"(obsah\s+bal[eě]ní|v\s+balení|součást[íi]\s+(?:dodávky|balení)|rozsah\s+dodávky|balení\s+obsahuje|"
    r"dodávka\s+obsahuje|^\s*\d+\s*[×x]\s+\S)", re.IGNORECASE)
NUMBER_UNIT_RE = re.compile(
    r"(?<![A-Za-z0-9])(\d[\d\s\u00a0.,]*\d|\d)\s?(W|kg|g|mm|cm|km|V|Ah|mAh|°C|°F|l|ml|Nm|min|kWh)(?![A-Za-z0-9])"
)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ0-9])")


def clean_name_text(text, code_value, opts):
    """Očistí NAME od interního kódu v závorce (přesně shodného s CODE produktu) a od
    textu za středníkem (typicky typ balení), pokud si to appka nechá zapnuté. Např.
    'Aku utahovák SSW 18 LTX 550 BL (602404850); Lepenková krabice' + CODE=602404850
    -> 'Aku utahovák SSW 18 LTX 550 BL'."""
    if not text or not opts or not opts.get("enabled"):
        return text
    t = text
    if opts.get("strip_after_semicolon"):
        t = t.split(";")[0]
    if opts.get("strip_own_code_parens") and code_value:
        t = re.sub(r"\s*\(\s*" + re.escape(code_value) + r"\s*\)", "", t)
    return t.strip()


def apply_text_rules(text, field, cfg):
    """Obecné úpravy textu pole (NAME / SHORT_DESCRIPTION / DESCRIPTION) podle cfg['text_tools']."""
    if not text or not cfg or not cfg.get("enabled"):
        return text
    t = text
    for rule in cfg.get("replace_rules", []):
        f = (rule.get("field") or "").strip()
        find = rule.get("find") or ""
        if not find or f not in ("", "VŠECHNA", field):
            continue
        repl = rule.get("replace") or ""
        try:
            if rule.get("regex"):
                t = re.sub(find, repl, t)
            else:
                t = t.replace(find, repl)
        except re.error:
            pass
    if cfg.get("collapse_spaces"):
        t = re.sub(r"[ \t\u00a0]+", " ", t)
        t = re.sub(r" *\n *", "\n", t).strip()
    if field == "NAME":
        mode = cfg.get("name_case", "none")
        if mode == "first_upper" and t:
            t = t[0].upper() + t[1:]
        elif mode == "upper":
            t = t.upper()
        elif mode == "lower":
            t = t.lower()
        elif mode == "sentence" and t:
            t = t[0].upper() + t[1:].lower()
    if field == "SHORT_DESCRIPTION":
        mx = int(cfg.get("short_desc_max") or 0)
        if mx > 0:
            plain = re.sub(r"<[^>]+>", " ", t)
            plain = re.sub(r"\s+", " ", plain).strip()
            if len(plain) > mx:
                cut = plain[:mx].rsplit(" ", 1)[0].rstrip(",;:.- ")
                t = cut + "…"
            else:
                t = plain
    return t


def apply_text_tools(shop, profile):
    cfg = profile.get("text_tools")
    if not cfg or not cfg.get("enabled"):
        return
    for item in shop.iterchildren("SHOPITEM"):
        for field in ("NAME", "SHORT_DESCRIPTION", "DESCRIPTION"):
            el = item.find(field)
            if el is not None and el.text:
                el.text = apply_text_rules(el.text, field, cfg)


def _norm_words(t):
    t = re.sub(r"<[^>]+>", " ", t or "").lower().replace("\u00a0", " ")
    return re.findall(r"[0-9a-záčďéěíňóřšťúůýž]+", t)


def _stems(t):
    return {w[:5] for w in _norm_words(t) if len(w) > 2 or w.isdigit()}


def is_duplicate_text(candidate, references, threshold=0.6):
    """True, pokud je candidate (odrážka) už v podstatě obsažen v některé z vět v references.
    Porovnává kmeny slov (prvních 5 znaků), takže zvládne české skloňování i drobné přeformulování.
    U odrážek tvaru 'Nadpis: věta' se porovnává i samotná věta za dvojtečkou."""
    variants = [candidate]
    if ":" in candidate:
        variants.append(candidate.split(":", 1)[1])
    for var in variants:
        cw = " ".join(_norm_words(var))
        if not cw:
            continue
        cs = _stems(var)
        for ref in references:
            rw = " ".join(_norm_words(ref))
            if cw in rw:
                return True
            if len(cs) >= 3:
                rs = _stems(ref)
                if rs and len(cs & rs) / len(cs) >= threshold:
                    return True
    return False


def dedupe_against(items, references, threshold):
    """Vrátí items bez těch, které jsou duplicitní vůči references nebo vůči už ponechaným."""
    out = []
    for it in items:
        if is_duplicate_text(it, references, threshold) or is_duplicate_text(it, out, 0.9):
            continue
        out.append(it)
    return out


def split_packaging_sentence(sentence):
    s = re.sub(r"^\s*obsah\s+balení\s*:?\s*", "", sentence.strip(), flags=re.IGNORECASE)
    return [p.strip(" .") for p in re.split(r";|\n", s) if p.strip(" .")]


def format_description_html(text, opts, bullets=None, packaging_items=None, extra_paragraphs=None):
    """Složí DESCRIPTION v pevném pořadí:
       popis (+ případné doplňkové odstavce) → odrážky → Obsah balení (VŽDY úplně na konci).
    Obsah balení se sbírá z jednoho místa: z vět v textu (když je zapnuto 'vytáhnout balení')
    a z položek přiřazených v kroku 5; vypíše se jednou, bez duplicit.
    Kontrola duplicit vynechá odrážky, které už v textu jsou."""
    opts = opts or {}
    fmt = bool(opts.get("enabled"))
    bullets = [b for b in (bullets or []) if b and b.strip()]
    packaging_items = [b for b in (packaging_items or []) if b and b.strip()]
    extra = [b for b in (extra_paragraphs or []) if b and b.strip()]
    text = (text or "").strip()
    if not (text or bullets or packaging_items or extra):
        return text
    if not fmt and not (bullets or packaging_items or extra):
        return text

    thr = float(opts.get("dedupe_level", 50)) / 100.0
    sentences = [x.strip() for x in SENTENCE_SPLIT_RE.split(text) if x.strip()] if text else []
    if fmt and opts.get("extract_packaging"):
        keep = []
        for c in sentences:
            if PACKAGING_RE.search(c):
                packaging_items = split_packaging_sentence(c) + packaging_items
            else:
                keep.append(c)
        sentences = keep
    for e in extra:
        sentences.extend(x.strip() for x in SENTENCE_SPLIT_RE.split(e) if x.strip())

    if opts.get("dedupe", True):
        sentences = dedupe_against(sentences, [], thr)
        bullets = dedupe_against(bullets, sentences, thr)
    packaging_items = dedupe_against(packaging_items, [], 0.9)

    body = " ".join(sentences)
    if fmt and opts.get("bold_numbers") and body:
        body = NUMBER_UNIT_RE.sub(r"<strong>\1 \2</strong>", body)
    if fmt and opts.get("paragraphs") and body:
        sents = [x for x in SENTENCE_SPLIT_RE.split(body) if x.strip()]
        body_html = "".join(f"<p>{' '.join(sents[i:i + 2])}</p>" for i in range(0, len(sents), 2))
    elif fmt and body:
        body_html = f"<p>{body}</p>"
    else:
        body_html = body

    heads = fmt and opts.get("headings")
    html = ("<h3>Popis</h3>" if heads and body else "") + body_html
    if bullets:
        html += ("<h3>Vlastnosti</h3>" if heads else "") + "<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>"
    if packaging_items:   # vždy úplně na konci
        html += "<h3>Obsah balení</h3><ul>" + "".join(f"<li>{b}</li>" for b in packaging_items) + "</ul>"
    return html


MARKETING_ROLES = {
    "ignore": "Ignorovat",
    "bullets": "Odrážka (Vlastnosti)",
    "packaging": "Obsah balení (úplný konec popisu)",
    "text": "Odstavec textu v popisu",
}


def guess_marketing_role(fname):
    n = (fname or "").lower()
    if any(k in n for k in ("rozsah dodávky", "obsah balení", "lieferumfang", "scope of delivery", "included")):
        return "packaging"
    if any(k in n for k in ("výhody", "vyhody", "benefit", "advantage", "key feature", "highlights")):
        return "bullets"
    return "ignore"


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
def apply_profile(root, profile, external_prices=None, excluded_codes=None):
    log = []
    external_prices = external_prices or {}
    excluded_codes = excluded_codes or set()
    item_tag = profile["item_tag"]
    items_src = root.xpath(f"//*[local-name()='{item_tag}']") if item_tag else []
    shop = etree.Element("SHOP")
    if not item_tag:
        return shop, ["⚠️ Ještě není vybraný element produktu (krok 2 nebo 3)."]

    log.append(f"Nalezeno {len(items_src)} produktů podle elementu <{item_tag}>.")

    n_price_missing = n_img = n_marketing = n_technical = n_size = n_video = n_video_skipped = 0

    code_src_field = profile["fields"].get("CODE", {}).get("source")
    for src in items_src:
        if excluded_codes and code_src_field and (gtext_deep(src, code_src_field) or "") in excluded_codes:
            continue  # produkt vyřazený ve výběru - vůbec se nezpracovává
        item = etree.SubElement(shop, "SHOPITEM")

        for target, fcfg in profile["fields"].items():
            if fcfg.get("source"):
                val = gtext_deep(src, fcfg["source"])
                if val:
                    etree.SubElement(item, target).text = val

        code_val = gtext(item, "CODE")

        name_el = item.xpath("./*[local-name()='NAME']")
        if name_el and profile.get("name_cleanup", {}).get("enabled"):
            name_el[0].text = clean_name_text(name_el[0].text, code_val, profile["name_cleanup"])

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
        elif pcfg["mode"] == "external":
            match_key = pcfg.get("match_key", "CODE")
            ident = code_val if match_key == "CODE" else gtext(item, "EAN")
            if ident:
                price_val = external_prices.get(normalize_id(ident))
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

        # váha a rozměry -> LOGISTIC
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

        dims = {}
        if wcfg["mode"] == "feature":
            # Shoptet pro rozměry používá DEPTH / WIDTH / HEIGHT (element LENGTH není povolen),
            # hodnoty v cm - proto se mm/m z feedu přepočítají
            for target, names_key in (("DEPTH", "length_names"), ("WIDTH", "width_names"), ("HEIGHT", "height_names")):
                names_raw = wcfg.get(names_key, "")
                if not names_raw:
                    continue
                dim_names = set(n.strip().lower() for n in names_raw.split(",") if n.strip())
                for feat in src.xpath(f".//*[local-name()='{wcfg['feature_element']}']"):
                    fname = gtext(feat, wcfg["fname_field"])
                    if fname and fname.strip().lower() in dim_names:
                        val = gtext(feat, wcfg["fvalue_field"])
                        dunit = (gtext(feat, wcfg["funit_field"]) or "").strip().lower() if wcfg.get("funit_field") else ""
                        if val:
                            try:
                                dv = float(val.replace(",", "."))
                                if dunit == "mm":
                                    dv /= 10.0
                                elif dunit == "m":
                                    dv *= 100.0
                                dims[target] = ("%.2f" % dv).rstrip("0").rstrip(".")
                            except ValueError:
                                dims[target] = val
                        break

        if w or dims:
            logistic = etree.SubElement(item, "LOGISTIC")
            for target in ("DEPTH", "WIDTH", "HEIGHT"):
                if target in dims:
                    etree.SubElement(logistic, target).text = dims[target]
            if w:
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

        # videa
        vcfg = profile.get("videos", {})
        if vcfg.get("mode") == "mime":
            vids, seen_v = [], set()
            for mime in src.xpath(f".//*[local-name()='{vcfg.get('mime_element', 'MIME')}']"):
                type_field = vcfg.get("type_field")
                mtype = gtext(mime, type_field) if type_field else None
                descr_field = vcfg.get("descr_field")
                descr = (gtext(mime, descr_field) or "") if descr_field else ""
                src_val = gtext(mime, vcfg.get("source_field", "MIME_SOURCE"))
                if not src_val:
                    continue
                type_ok = (not vcfg.get("type_value")) or (mtype == vcfg["type_value"])
                descr_ok = (not vcfg.get("descr_match")) or (vcfg["descr_match"].lower() in descr.lower())
                looks_like_video = any(h in src_val.lower() for h in ("youtube.com", "youtu.be", "vimeo.com"))
                if type_ok and (descr_ok or looks_like_video):
                    yid = extract_youtube_id(src_val)
                    if not yid:
                        n_video_skipped += 1  # ne-YouTube odkaz - Shoptet umí jen YOUTUBE_VIDEO_CODE
                        continue
                    if yid not in seen_v:
                        seen_v.add(yid)
                        vids.append(yid)
            if vids:
                wrapper = vcfg.get("wrapper_tag") or "RELATED_VIDEOS"
                if wrapper == "VIDEOS":  # starý výchozí název z dřívějších šablon
                    wrapper = "RELATED_VIDEOS"
                wrap = etree.SubElement(item, wrapper)
                vtext = gtext(item, "NAME") or ""
                for yid in vids:
                    rv = etree.SubElement(wrap, "RELATED_VIDEO")
                    if vcfg.get("add_text") and vtext:
                        etree.SubElement(rv, "TEXT").text = vtext
                    etree.SubElement(rv, "YOUTUBE_VIDEO_CODE").text = yid
                n_video += len(vids)

        # související / podobné produkty - element, pole s kódem a pole s typem jsou volitelné (krok 6)
        rcfg = profile.get("references", {})
        if rcfg.get("enabled") and rcfg.get("reference_element") and rcfg.get("id_field"):
            tmode = rcfg.get("type_mode", "attr")
            rel_codes, alt_codes, seen_r = [], [], set()
            for ref in src.xpath(f".//*[local-name()='{rcfg['reference_element']}']"):
                if tmode == "attr":
                    rtype = ref.get(rcfg.get("type_attr", "type"))
                elif tmode == "field":
                    rtype = gtext_deep(ref, rcfg.get("type_field", ""))
                else:
                    rtype = None
                if tmode != "none" and (rtype is None or rtype not in rcfg.get("included_types", [])):
                    continue
                idval = gtext_deep(ref, rcfg["id_field"])
                if idval and idval not in seen_r:
                    seen_r.add(idval)
                    (alt_codes if rtype in rcfg.get("alt_types", []) else rel_codes).append(idval)
            for codes_out, wtag in ((rel_codes, rcfg.get("wrapper_tag") or "RELATED_PRODUCTS"),
                                    (alt_codes, "ALTERNATIVE_PRODUCTS")):
                if codes_out:
                    wrap = etree.SubElement(item, wtag)
                    for c in codes_out:
                        etree.SubElement(wrap, rcfg.get("item_tag") or "CODE").text = c

        # parametry - klasifikace podle hodnoty vybraného pole
        pcfg2 = profile["params"]
        marketing_bullets, technical_pairs, size_pairs, packaging_bullets, text_paragraphs = [], [], [], [], []
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
                if bucket == "marketing" and fvals:
                    role = pcfg2.get("marketing_roles", {}).get(fname, guess_marketing_role(fname))
                    if role == "bullets":
                        marketing_bullets.extend(fvals)
                    elif role == "packaging":
                        packaging_bullets.extend(fvals)
                    elif role == "text":
                        text_paragraphs.extend(fvals)
                elif bucket == "packaging" and (fvals or fname):
                    packaging_bullets.extend(fvals if fvals else [fname])
                elif bucket == "technical" and fname and fvals:
                    technical_pairs.append((fname, "; ".join(fvals), funit))
                elif bucket == "size" and fname and fvals:
                    size_pairs.append((fname, "; ".join(fvals), funit))

        desc_bullets = []
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
                desc_bullets = list(marketing_bullets)

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

        # DESCRIPTION: popis + odrážky + (úplně na konci) obsah balení, s kontrolou duplicit
        dcfg = profile.get("description_format", {})
        desc_el = item.xpath("./*[local-name()='DESCRIPTION']")
        raw_desc = desc_el[0].text if desc_el else ""
        if dcfg.get("enabled") or desc_bullets or packaging_bullets or text_paragraphs:
            new_desc = format_description_html(raw_desc, dcfg, desc_bullets, packaging_bullets, text_paragraphs)
            if new_desc:
                if desc_el:
                    desc_el[0].text = new_desc
                else:
                    etree.SubElement(item, "DESCRIPTION").text = new_desc

        etree.SubElement(item, "VISIBILITY").text = profile["visibility_default"]

    rcfg_post = profile.get("references", {})
    if rcfg_post.get("enabled"):
        out_codes = {gtext(i, "CODE") for i in shop.iterchildren("SHOPITEM")}
        n_rel = n_dropped = 0
        for item in shop.iterchildren("SHOPITEM"):
            for wtag in {rcfg_post.get("wrapper_tag") or "RELATED_PRODUCTS", "ALTERNATIVE_PRODUCTS"}:
                for wrap in item.findall(wtag):
                    for c in list(wrap):
                        if rcfg_post.get("only_existing") and (c.text or "") not in out_codes:
                            wrap.remove(c)
                            n_dropped += 1
                        else:
                            n_rel += 1
                    if len(wrap) == 0:
                        item.remove(wrap)
        log.append(f"Související/alternativní produkty: {n_rel} odkazů zapsáno"
                   + (f", {n_dropped} vynecháno (kód není v tomto feedu)." if n_dropped else "."))
    if n_price_missing:
        log.append(f"⚠️ {n_price_missing} produktů nemá dohledatelnou cenu.")
    if n_img:
        log.append(f"Nalezeno {n_img} obrázkových položek.")
    if n_video:
        log.append(f"{n_video} YouTube videí zapsáno do <RELATED_VIDEOS>.")
    if n_video_skipped:
        log.append(f"⚠️ {n_video_skipped} video odkazů přeskočeno - nejsou z YouTube (Shoptet umí jen YOUTUBE_VIDEO_CODE).")
    if n_marketing:
        log.append(f"{n_marketing} marketingových textů zapsáno do popisu.")
    if n_technical:
        log.append(f"{n_technical} technických parametrů do <INFORMATION_PARAMETERS>.")
    if n_size:
        log.append(f"{n_size} velikostních parametrů do <PARAMETERS>.")
    apply_text_tools(shop, profile)
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


def best_net_for_gross(gross, vat_percent):
    """Cena BEZ DPH na max. 2 desetinná místa, jejíž cena s DPH (po Shoptetově přičtení) je
    nejblíž cílové částce 'gross'. Zkouší i sousední haléře, protože prosté zaokrouhlení
    G/(1+DPH) na 2 místa občas vyjde o 0,01 vedle."""
    f = 1 + vat_percent / 100.0
    base = round(gross / f, 2)
    cands = [round(base + d / 100.0, 2) for d in (-1, 0, 1)]
    return min(cands, key=lambda n: (abs(round(n * f, 2) - gross), abs(n * f - gross)))


def apply_price_precision(shop_root, mode):
    """Finální průchod při exportu: PRICE a STANDARD_PRICE mají nejvýš 2 desetinná místa
    ('2dp'), nebo se zaokrouhlí na celá čísla ('int')."""
    for item in shop_root.xpath("./*[local-name()='SHOPITEM']"):
        for tag in ("PRICE", "STANDARD_PRICE"):
            el = item.xpath(f"./*[local-name()='{tag}']")
            if el and el[0].text:
                try:
                    v = float(el[0].text.replace(",", "."))
                except ValueError:
                    continue
                el[0].text = str(int(v + 0.5)) if mode == "int" else f"{v:.2f}"
    return shop_root


def apply_price_adjustment(shop_root, cfg):
    """Hromadná úprava ceny.
    Cílem je vždy CELÉ číslo jako výsledná zobrazená cena v Shoptetu. Shoptet si k <PRICE>
    DPH podle <VAT> vždy dopočítává sám (bere <PRICE> jako cenu bez DPH), takže nejde do
    <PRICE> poslat rovnou kulatou cenu s DPH - to by Shoptet vzal jako základ a k tomu
    ještě připočetl DPH podruhé. Proto se to počítá "pozpátku": nejdřív se spočte cílová
    KULATÁ cena s DPH (podle toho, jestli zdrojová cena z feedu už DPH obsahovala, nebo ne),
    a do <PRICE> (resp. <STANDARD_PRICE>) se zapíše taková cena BEZ DPH, aby po Shoptetově
    vlastním přičtení DPH vyšla přesně ta kulatá částka.
    Čistá funkce na hotovém stromu, takže funguje nezávisle na tom, odkud cena původně přišla."""
    if not cfg or not cfg.get("enabled"):
        return shop_root
    discount = cfg.get("discount_percent") or 0
    source_mode = cfg.get("source_mode", "net")  # "net" = zdrojová cena bez DPH, "gross" = zdrojová cena s DPH (např. z ceníku)
    try:
        vat = float(str(cfg.get("vat_percent", "21")).replace(",", "."))
    except (TypeError, ValueError):
        vat = 0
    for item in shop_root.xpath("./*[local-name()='SHOPITEM']"):
        price_el = item.xpath("./*[local-name()='PRICE']")
        if not price_el or not price_el[0].text:
            continue
        try:
            p = float(price_el[0].text.replace(",", "."))
        except ValueError:
            continue

        # cílová (kulatá) zobrazená cena s DPH
        target_list = round(p) if source_mode == "gross" else round(p * (1 + vat / 100.0))
        target_final = round(target_list * (1 - discount / 100.0)) if discount else target_list

        # zapisujeme cenu BEZ DPH (max. 2 desetinná místa - víc validátor Shoptetu nepustí), vybranou
        # tak, aby po Shoptetově přičtení DPH vyšla cena co nejblíž cílové kulaté částce
        fed_list = best_net_for_gross(target_list, vat)
        fed_final = best_net_for_gross(target_final, vat)

        def fmt(v):
            return f"{v:.2f}"

        price_el[0].text = fmt(fed_final)
        if discount:
            std_el = item.xpath("./*[local-name()='STANDARD_PRICE']")
            if std_el:
                std_el[0].text = fmt(fed_list)
            else:
                etree.SubElement(item, "STANDARD_PRICE").text = fmt(fed_list)
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


def read_table_file(uploaded_file):
    """Přečte Excel/CSV do (header, rows) - header je list názvů sloupců (nebo indexy jako
    text, pokud soubor hlavičku nemá), rows je list řádků (list hodnot jako string)."""
    name = uploaded_file.name.lower()
    data = uploaded_file.read()
    rows = []
    if name.endswith(".csv"):
        import csv as csv_mod
        import io as io_mod
        text = data.decode("utf-8", errors="ignore")
        for row in csv_mod.reader(io_mod.StringIO(text)):
            rows.append([c.strip() for c in row])
    elif name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        import io as io_mod
        wb = openpyxl.load_workbook(io_mod.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        for row in ws.iter_rows(values_only=True):
            rows.append(["" if c is None else str(c).strip() for c in row])
    if not rows:
        return [], []
    header = rows[0]
    return header, rows[1:]


def normalize_id(v):
    v = str(v).strip()
    if v.endswith(".0") and v[:-2].replace("-", "").isdigit():
        v = v[:-2]
    return v


def read_all_columns(uploaded_file):
    """Přečte nahraný soubor do slovníku {název_listu: list sloupců}, každý sloupec = list hodnot (str).
    Excel se čte bez read_only (ten umí useknout řádky podle chybné dimenze listu)."""
    import io as io_mod
    name = uploaded_file.name.lower()
    data = uploaded_file.getvalue() if hasattr(uploaded_file, "getvalue") else uploaded_file.read()

    def cell_str(c):
        if c is None:
            return ""
        if isinstance(c, float) and c.is_integer():
            return str(int(c))
        return str(c)

    sheets = {}
    if name.endswith(".txt"):
        text = data.decode("utf-8-sig", errors="ignore")
        vals = [p for p in re.split(r"[\r\n,;\t]+", text)]
        sheets["TXT"] = [vals]
    elif name.endswith(".csv"):
        import csv as csv_mod
        text = data.decode("utf-8-sig", errors="ignore")
        try:
            dialect = csv_mod.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except Exception:
            dialect = csv_mod.excel
        rows = list(csv_mod.reader(io_mod.StringIO(text), dialect))
        w = max((len(r) for r in rows), default=0)
        sheets["CSV"] = [[(r[i] if i < len(r) else "") for r in rows] for i in range(w)]
    elif name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io_mod.BytesIO(data), data_only=True)
        for ws in wb.worksheets:
            rows = [[cell_str(c) for c in row] for row in ws.iter_rows(values_only=True)]
            w = max((len(r) for r in rows), default=0)
            if w:
                sheets[ws.title] = [[(r[i] if i < len(r) else "") for r in rows] for i in range(w)]
    elif name.endswith(".xls"):
        df = pd.read_excel(io_mod.BytesIO(data), header=None, dtype=str)
        sheets["List1"] = [df[c].fillna("").tolist() for c in df.columns]
    return sheets


def norm_code(v, loose=False):
    """Normalizace kódu pro porovnání. Přísná: ořez mezer (vč. nbsp), '.0' u čísel, velká písmena.
    Volná: navíc bez oddělovačů (mezera . - _ /) a bez úvodních nul (Excel je u čísel ztrácí)."""
    s = str(v if v is not None else "")
    s = s.replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "").strip()
    s = re.sub(r"^(\d+)\.0+$", r"\1", s)
    s = s.upper()
    if loose:
        s = re.sub(r"[\s.\-_/]+", "", s).lstrip("0")
    return s


def match_codes(upload_values, feed_pairs):
    """upload_values: list hodnot z Excelu. feed_pairs: list (hodnota_pole_ve_feedu, CODE).
    Vrací (set CODE, počet přesných, počet volných, list nespárovaných hodnot)."""
    strict = {}
    loose = {}
    for val, code in feed_pairs:
        if not val:
            continue
        strict.setdefault(norm_code(val), set()).add(code)
        loose.setdefault(norm_code(val, True), set()).add(code)
    matched, n_strict, n_loose, unmatched = set(), 0, 0, []
    seen = set()
    for v in upload_values:
        k = norm_code(v)
        if not k or k in seen:
            continue
        seen.add(k)
        if k in strict:
            matched |= strict[k]
            n_strict += 1
            continue
        lk = norm_code(v, True)
        if lk and lk in loose:
            matched |= loose[lk]
            n_loose += 1
        else:
            unmatched.append(str(v).strip())
    return matched, n_strict, n_loose, unmatched


def shop_to_bytes(shop_root):
    return etree.tostring(shop_root, xml_declaration=True, encoding="utf-8", pretty_print=True)


# =============================================================================
# NOVÉ: SESTAVENÍ FEEDU, STAHOVÁNÍ Z URL, ÚLOHY PRO AUTOMATICKÝ BĚH
# (nic z toho nepotřebuje Streamlit - používá to aplikace i run_feed.py)
# =============================================================================
JOB_VERSION = 1


def slugify(text):
    """'Bosch – ceník 2026' -> 'bosch_cenik_2026' (bez diakritiky, vhodné do názvu souboru)."""
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()


def codes_in_feed(root, profile):
    """Množina kódů produktů ve zdrojovém feedu (podle mapování CODE v profilu)."""
    if root is None:
        return set()
    if detect_feed_type(root) == "shoptet":
        return {(gtext(it, "CODE") or "") for it in root.xpath("./*[local-name()='SHOPITEM']")}
    item_tag = profile.get("item_tag")
    code_src = profile.get("fields", {}).get("CODE", {}).get("source")
    if not item_tag or not code_src:
        return set()
    return {(gtext_deep(s, code_src) or "") for s in root.xpath(f"//*[local-name()='{item_tag}']")}


def build_shop(root, profile, external_prices=None, excluded_codes=None, overrides=None, included_codes=None):
    """Celá převodní pipeline: zdrojový strom + profil -> (strom <SHOP>, log).
    Pořadí kroků je stejné jako dřív v compute_shop(). included_codes (pokud není None) = zpracovat
    jen tyto kódy, ostatní se vyřadí (nové produkty ve feedu se tak nezakládají)."""
    external_prices = external_prices or {}
    excluded = set(excluded_codes or ())
    if included_codes is not None:
        excluded |= codes_in_feed(root, profile) - set(included_codes)
    if detect_feed_type(root) == "shoptet":
        shop = deepcopy(root)
        for it in shop.xpath("./*[local-name()='SHOPITEM']"):
            if (gtext(it, "CODE") or "") in excluded:
                shop.remove(it)
        log = ["Feed byl už ve formátu Shoptet, načten beze změny."]
    else:
        shop, log = apply_profile(root, profile, external_prices, excluded)
    shop = apply_name_affixes(shop, profile.get("name_prefix", ""), profile.get("name_suffix", ""))
    shop = apply_price_adjustment(shop, profile.get("price_adjust", {}))
    shop = apply_availability(shop, profile.get("availability", {}))
    shop = apply_overrides(shop, overrides or {})
    shop = apply_price_precision(shop, profile.get("price_precision", "2dp"))
    return shop, log


def selection_to_args(selection):
    """Výběr produktů z úlohy -> (excluded_codes, included_codes pro build_shop)."""
    sel = selection or {"mode": "all"}
    codes = set(sel.get("codes") or [])
    if sel.get("mode") == "exclude":
        return codes, None
    if sel.get("mode") == "include_only":
        return set(), codes
    return set(), None


def drop_invalid_items(shop_root):
    """Vyřadí produkty s povinnou chybou (do Shoptetu nemají jít rozbité záznamy).
    Vrací (počet vyřazených, seznam (kód, důvod) pro report)."""
    dropped = []
    seen = set()
    for issue in validate_shop(shop_root):
        if not issue["mandatory"]:
            continue
        item = issue["item"]
        if item in seen:
            continue
        seen.add(item)
        parent = item.getparent()
        if parent is not None:
            parent.remove(item)
        dropped.append((issue["code"], issue["message"]))
    return len(dropped), dropped


# ---- tabulky (ceník) z bajtů ------------------------------------------------
class MemFile:
    """Minimální náhrada nahraného souboru (name + read/getvalue) pro funkce, co čekají Streamlit upload."""

    def __init__(self, name, data):
        self.name = name
        self._data = data

    def read(self):
        return self._data

    def getvalue(self):
        return self._data


def read_table_bytes(name, data):
    """(header, rows) z CSV/Excelu v paměti. CSV se čte s rozpoznáním oddělovače (středník z českého Excelu)."""
    sheets = read_all_columns(MemFile(name, data))
    if not sheets:
        return [], []
    cols = next(iter(sheets.values()))
    if not cols or not cols[0]:
        return [], []
    header = [(c[0] if c else "").strip() for c in cols]
    n = len(cols[0])
    rows = [[(col[i] if i < len(col) else "").strip() for col in cols] for i in range(1, n)]
    return header, rows


def price_map_from_table(header, rows, id_col, price_col):
    """Stejná logika jako tlačítko 'Načíst ceník' v aplikaci: {normalizované ID: cena jako text}."""
    price_map = {}
    for row in rows:
        if id_col < len(row) and price_col < len(row) and row[id_col]:
            price_map[normalize_id(row[id_col])] = row[price_col].replace(" ", "").replace(" ", "").replace(",", ".")
    return price_map


def find_column(header, wanted):
    """Index sloupce podle názvu v hlavičce (bez ohledu na velikost písmen a okrajové mezery)."""
    w = str(wanted).strip().lower()
    for i, h in enumerate(header):
        if str(h).strip().lower() == w:
            return i
    raise RuntimeError(f"V ceníku není sloupec '{wanted}'. Dostupné sloupce: {', '.join(h for h in header if h)}")


# ---- tajné údaje a stahování -----------------------------------------------
def get_secret(name, env=None):
    """Hodnota z proměnné prostředí, nebo z JSON v ALL_SECRETS (GitHub Actions: toJSON(secrets))."""
    env = os.environ if env is None else env
    val = env.get(name)
    if val not in (None, ""):
        return val
    blob = env.get("ALL_SECRETS")
    if blob:
        try:
            val = json.loads(blob).get(name)
        except (ValueError, AttributeError):
            val = None
        if val not in (None, ""):
            return val
    return None


def secret_names(job):
    """Názvy secretů, které úloha potřebuje (aby je šlo vypsat uživateli i zkontrolovat před během)."""
    names = set()

    def scan(spec):
        for k, v in (spec or {}).items():
            if k.endswith("_env") and v:
                names.add(v)

    src = job.get("source") or {}
    scan({"url_env": src.get("url_env")})
    scan(src.get("auth"))
    ps = job.get("price_source") or {}
    scan({"url_env": ps.get("url_env")})
    scan(ps.get("auth"))
    return sorted(names)


def resolve_auth(spec, env=None):
    """Popis přihlášení s názvy secretů -> konkrétní hodnoty. Přímo zadané hodnoty (user, password, value)
    mají přednost - používá je aplikace při interaktivním stahování."""
    spec = dict(spec or {"type": "none"})
    for key in ("user", "password", "value"):
        ref = spec.get(key + "_env")
        if ref and not spec.get(key):
            val = get_secret(ref, env)
            if val is None:
                raise RuntimeError(f"Chybí secret '{ref}' (přihlašovací údaj pro stažení feedu).")
            spec[key] = val
    return spec


class FeedFetchError(RuntimeError):
    pass


def looks_like_html(data):
    head = data[:400].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def unpack_payload(data):
    """Rozbalí gzip a ZIP s jedním souborem. Excel (.xlsx je také ZIP) nechá být."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    elif data[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if "[Content_Types].xml" not in names:
                if len(names) != 1:
                    raise FeedFetchError(f"Archiv ZIP obsahuje {len(names)} souborů, čekal jsem jeden.")
                data = zf.read(names[0])
    return data


def fetch_url(url, auth=None, timeout=(15, 180), max_bytes=500 * 1024 * 1024):
    """Stáhne feed z URL a vrátí bajty. auth: {"type": none|basic|header|form, ...} s konkrétními hodnotami.
    Chybové hlášky záměrně neobsahují URL ani údaje (v logu by mohl být token)."""
    import requests
    auth = auth or {"type": "none"}
    atype = auth.get("type", "none")
    if not url or not str(url).lower().startswith(("http://", "https://")):
        raise FeedFetchError("URL feedu musí začínat http:// nebo https://.")
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (compatible; feed-converter/1.0)"
    kwargs = {}
    try:
        if atype == "basic":
            kwargs["auth"] = (auth.get("user", ""), auth.get("password", ""))
        elif atype == "header":
            s.headers[auth.get("header_name") or "Authorization"] = auth.get("value", "")
        elif atype == "form":
            if not auth.get("login_url"):
                raise FeedFetchError("Chybí URL přihlašovacího formuláře.")
            data = dict(auth.get("extra_fields") or {})
            data[auth.get("user_field") or "username"] = auth.get("user", "")
            data[auth.get("password_field") or "password"] = auth.get("password", "")
            lr = s.post(auth["login_url"], data=data, timeout=timeout, allow_redirects=True)
            lr.raise_for_status()
        r = s.get(url, timeout=timeout, stream=True, **kwargs)
        r.raise_for_status()
        chunks, size = [], 0
        for chunk in r.iter_content(1024 * 256):
            size += len(chunk)
            if size > max_bytes:
                raise FeedFetchError("Feed je větší než povolených 500 MB.")
            chunks.append(chunk)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        hint = " - zkontroluj přihlášení." if code in (401, 403) else "."
        raise FeedFetchError(f"Server odpověděl chybou {code}{hint}") from None
    except requests.RequestException as e:
        raise FeedFetchError(f"Stažení se nepovedlo ({type(e).__name__}).") from None
    data = unpack_payload(b"".join(chunks))
    if not data.strip():
        raise FeedFetchError("Server vrátil prázdnou odpověď.")
    if looks_like_html(data):
        raise FeedFetchError("Server vrátil webovou stránku (HTML) místo feedu - nejspíš přihlášení nebo špatná URL.")
    return data


def fetch_source(spec, env=None):
    """Zdroj z úlohy ({"type": "url"|"file", ...}) -> bajty."""
    stype = (spec or {}).get("type", "url")
    if stype == "file":
        with open(spec["path"], "rb") as f:
            return unpack_payload(f.read())
    url = spec.get("url") or ""
    if spec.get("url_env"):
        url = get_secret(spec["url_env"], env)
        if not url:
            raise RuntimeError(f"Chybí secret '{spec['url_env']}' (URL feedu).")
    return fetch_url(url, resolve_auth(spec.get("auth"), env))


def load_price_map(spec, env=None):
    """Ceník z úlohy -> {ID: cena}. spec: zdroj (viz fetch_source) + format (csv|xlsx|xlsm|xls),
    id_column a price_column (názvy sloupců v hlavičce)."""
    data = fetch_source(spec, env)
    fmt = (spec.get("format") or "csv").lower().lstrip(".")
    header, rows = read_table_bytes(f"cenik.{fmt}", data)
    if not header:
        raise RuntimeError("Ceník je prázdný nebo ho nejde přečíst.")
    return price_map_from_table(header, rows, find_column(header, spec["id_column"]),
                                find_column(header, spec["price_column"]))
