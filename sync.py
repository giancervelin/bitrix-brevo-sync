import os
import json
import time
import shelve
import requests
from collections import OrderedDict
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

BITRIX_WEBHOOK_BASE = (os.getenv("BITRIX_WEBHOOK_BASE") or "").strip().rstrip("/") + "/"
BREVO_API_KEY = (os.getenv("BREVO_API_KEY") or "").strip()
BREVO_LIST_ID = int(os.getenv("BREVO_LIST_ID", "0") or "0")  # ID da lista Cotefácil

BREVO_DELAY_SECONDS = float(os.getenv("BREVO_DELAY_SECONDS", "0.15") or "0.15")
MAX_CONTACTS_PER_RUN = int(os.getenv("MAX_CONTACTS_PER_RUN", "500") or "500")

STATUS_FIELD = (os.getenv("STATUS_FIELD") or "UF_CRM_63DE3F720F62D").strip()
SEGMENTO_FIELD = (os.getenv("SEGMENTO_FIELD") or "UF_CRM_63DE3F70902E0").strip()
REDE_FIELD = (os.getenv("REDE_FIELD") or "UF_CRM_63DE4EBA09876").strip()
ESTADO_FIELD = (os.getenv("ESTADO_FIELD") or "UF_CRM_1682456907").strip()
SOFTWARE_HOUSE_FIELD = (os.getenv("SOFTWARE_HOUSE_FIELD") or "UF_CRM_63DE523E1DF34").strip()

NOME_FIELD = (os.getenv("NOME_FIELD") or "NAME").strip()
SOBRENOME_FIELD = (os.getenv("SOBRENOME_FIELD") or "LAST_NAME").strip()
NASCIMENTO_FIELD = (os.getenv("NASCIMENTO_FIELD") or "BIRTHDATE").strip()

CHECKPOINT_FILE = os.path.join(BASE_DIR, "checkpoint.json")
COMPANY_CACHE_DB = os.path.join(BASE_DIR, "company_cache.db")  # cache em disco (shelve)
MAX_COMPANY_CACHE = 5000


def convert_birthdate(birthdate_str):
    """Converte data do Bitrix (ISO com timezone) para YYYY-MM-DD (formato Brevo)"""
    if not birthdate_str:
        return ""
    try:
        return birthdate_str.split("T")[0]
    except Exception:
        return ""


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] %s" % (ts, msg), flush=True)


class LRUCache(object):
    def __init__(self, capacity):
        self.capacity = capacity
        self.cache = OrderedDict()

    def get(self, key):
        if key not in self.cache:
            return None
        self.cache.move_to_end(key)
        return self.cache[key]

    def put(self, key, value):
        if key in self.cache:
            self.cache.move_to_end(key)
        self.cache[key] = value
        if len(self.cache) > self.capacity:
            self.cache.popitem(last=False)


companies_lru = LRUCache(MAX_COMPANY_CACHE)
field_maps = {}  # {field_code: {id: label}}


def bitrix_call(method, params):
    url = "%s%s.json" % (BITRIX_WEBHOOK_BASE, method)
    r = requests.post(url, data=params or {}, timeout=60)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError("Bitrix error %s: %s" % (data.get("error"), data.get("error_description")))
    return data


def load_field_maps_cached():
    # Carrega mapeamentos para STATUS, SEGMENTO, REDE, ESTADO, SOFTWARE_HOUSE
    now = int(time.time())
    cache_file = os.path.join(BASE_DIR, "field_maps_cache.json")

    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cache = json.load(f)
            cached_at = int(cache.get("_cached_at", 0))
            if cached_at and (now - cached_at) &lt; (24 * 3600):
                return cache.get("maps", {})
        except Exception:
            pass

    resp = bitrix_call("crm.company.fields", {})
    fields = resp.get("result", {}) or {}

    maps = {}
    for field_code in [STATUS_FIELD, SEGMENTO_FIELD, REDE_FIELD, ESTADO_FIELD, SOFTWARE_HOUSE_FIELD]:
        field = fields.get(field_code, {}) or {}
        items = field.get("items", []) or []
        m = {}
        for it in items:
            _id = it.get("ID")
            _val = it.get("VALUE")
            if _id is not None and _val:
                m[str(_id)] = str(_val)
        maps[field_code] = m

    # fallback manual para SEGMENTO (mantém seu comportamento atual)
    if SEGMENTO_FIELD not in maps or not maps[SEGMENTO_FIELD]:
        maps[SEGMENTO_FIELD] = {
            "SALE": "Farma",
            "COMPLEX": "Alimentar",
            "GOODS": "Hospitalar",
            "UC_S4Y259": "Perfumaria",
            "SERVICES": "Outros",
        }

    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({"_cached_at": now, "maps": maps}, f)
    except Exception:
        pass

    return maps


def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                cp = json.load(f)
            cp.setdefault("mode", "full")
            cp.setdefault("start_offset", 0)
            cp.setdefault("last_date_modify", "")
            return cp
        except Exception:
            pass
    return {"mode": "full", "start_offset": 0, "last_date_modify": ""}


def save_checkpoint(cp):
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump(cp, f)


def extract_email(contact):
    email_field = contact.get("EMAIL")
    if not email_field:
        return ""

    if isinstance(email_field, list) and len(email_field) > 0:
        first = email_field[0]
        if isinstance(first, dict):
            return (first.get("VALUE") or "").strip().lower()
        return str(first).strip().lower()

    if isinstance(email_field, str):
        return email_field.strip().lower()

    return ""


def brevo_upsert(email, situacao, segmento, rede, estado, software_house, nome, sobrenome, nascimento):
    url = "https://api.brevo.com/v3/contacts"
    headers = {
        "api-key": BREVO_API_KEY,
        "content-type": "application/json",
        "accept": "application/json",
    }

    payload = {"email": email, "updateEnabled": True, "attributes": {}}

    if situacao:
        payload["attributes"]["SITUACAO_DO_CLIENTE"] = situacao
    if segmento:
        payload["attributes"]["SEGMENTO"] = segmento
    if rede:
        payload["attributes"]["REDE"] = rede
    if estado:
        payload["attributes"]["ESTADO"] = estado
    if software_house:
        payload["attributes"]["SOFTWARE_HOUSE"] = software_house
    if nome:
        payload["attributes"]["NOME"] = nome
    if sobrenome:
        payload["attributes"]["SOBRENOME"] = sobrenome
    if nascimento:
        payload["attributes"]["DATA_NASCIMENTO"] = nascimento

    if BREVO_LIST_ID > 0:
        payload["listIds"] = [BREVO_LIST_ID]

    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code not in (200, 201, 204):
        raise RuntimeError("Brevo HTTP %s (email=%s): %s" % (r.status_code, email, r.text))


def _ensure_company_fields_shape(d):
    """Garante que sempre exista o mesmo formato, mesmo com cache antigo."""
    base = {"situacao": "", "segmento": "", "rede": "", "estado": "", "software_house": ""}
    if not isinstance(d, dict):
        return base
    base.update(d)
    # se cache antigo tinha chaves extras/erradas, mantém, mas garante as necessárias
    return base


def get_company_fields(company_id):
    if company_id is None:
        return _ensure_company_fields_shape({})

    try:
        cid_int = int(str(company_id).strip())
    except Exception:
        return _ensure_company_fields_shape({})

    if cid_int &lt;= 0:
        return _ensure_company_fields_shape({})

    key = str(cid_int)

    # 1) cache em memória
    v = companies_lru.get(key)
    if v is not None:
        v2 = _ensure_company_fields_shape(v)
        companies_lru.put(key, v2)
        return v2

    # 2) cache em disco
    with shelve.open(COMPANY_CACHE_DB) as db:
        if key in db:
            v = db[key]
            v2 = _ensure_company_fields_shape(v)
            companies_lru.put(key, v2)
            # regrava no formato novo (corrige cache antigo automaticamente)
            db[key] = v2
            return v2

    # 3) buscar no Bitrix
    try:
        resp = bitrix_call("crm.company.get", {"id": cid_int})
        company = resp.get("result", {}) or {}

        situacao_id = company.get(STATUS_FIELD)
        segmento_id = company.get(SEGMENTO_FIELD)
        rede_id = company.get(REDE_FIELD)
        estado_id = company.get(ESTADO_FIELD)
        software_house_id = company.get(SOFTWARE_HOUSE_FIELD)

        situacao = field_maps.get(STATUS_FIELD, {}).get(str(situacao_id), "") if situacao_id else ""
        segmento = field_maps.get(SEGMENTO_FIELD, {}).get(str(segmento_id), "") if segmento_id else ""
        rede = field_maps.get(REDE_FIELD, {}).get(str(rede_id), "") if rede_id else ""
        estado = field_maps.get(ESTADO_FIELD, {}).get(str(estado_id), "") if estado_id else ""
        software_house = field_maps.get(SOFTWARE_HOUSE_FIELD, {}).get(str(software_house_id), "") if software_house_id else ""

        fields = _ensure_company_fields_shape(
            {"situacao": situacao, "segmento": segmento, "rede": rede, "estado": estado, "software_house": software_house}
        )
    except Exception as e:
        log("Aviso: falha ao buscar empresa (COMPANY_ID=%s): %s" % (key, e))
        fields = _ensure_company_fields_shape({})

    companies_lru.put(key, fields)
    with shelve.open(COMPANY_CACHE_DB) as db:
        db[key] = fields

    return fields


def main():
    if not BITRIX_WEBHOOK_BASE or BITRIX_WEBHOOK_BASE == "/":
        raise RuntimeError("BITRIX_WEBHOOK_BASE ausente no .env")
    if not BREVO_API_KEY:
        raise RuntimeError("BREVO_API_KEY ausente no .env")

    global field_maps
    field_maps = load_field_maps_cached()
    log("Field maps carregados: %s campos" % len(field_maps))

    cp = load_checkpoint()
    mode = cp.get("mode", "full")
    start = int(cp.get("start_offset", 0) or 0)
    last_date = cp.get("last_date_modify", "") or ""

    if mode == "incremental":
        start = 0
        log("Modo incremental | last_date_modify=%s" % last_date)
    else:
        log("Modo FULL | start_offset=%s" % start)

    upserts = 0
    processed = 0
    max_date_seen = last_date

    while upserts &lt; MAX_CONTACTS_PER_RUN:
        params = {
            "order[DATE_MODIFY]": "ASC",
            "start": start,
            "select[]": ["ID", "DATE_MODIFY", "COMPANY_ID", "EMAIL", NOME_FIELD, SOBRENOME_FIELD, NASCIMENTO_FIELD],
        }
        if mode == "incremental" and last_date:
            params["filter[>=DATE_MODIFY]"] = last_date

        resp = bitrix_call("crm.contact.list", params)
        rows = resp.get("result", []) or []

        if not rows:
            cp["mode"] = "incremental"
            cp["start_offset"] = 0
            cp["last_date_modify"] = max_date_seen
            save_checkpoint(cp)

            if mode == "full":
                log("FULL finalizado. Mudando para incremental. last_date_modify=%s" % max_date_seen)
            else:
                log("Incremental sem novos contatos. last_date_modify=%s" % max_date_seen)
            return

        for c in rows:
            processed += 1

            dm = c.get("DATE_MODIFY") or ""
            if dm and (not max_date_seen or dm > max_date_seen):
                max_date_seen = dm

            email = extract_email(c)
            if not email:
                continue

            company_id = c.get("COMPANY_ID")
            company_fields = get_company_fields(company_id)

            situacao = company_fields.get("situacao", "")
            segmento = company_fields.get("segmento", "")
            rede = company_fields.get("rede", "")
            estado = company_fields.get("estado", "")
            software_house = company_fields.get("software_house", "")

            nome = str(c.get(NOME_FIELD, "") or "").strip()
            sobrenome = str(c.get(SOBRENOME_FIELD, "") or "").strip()
            nascimento_raw = str(c.get(NASCIMENTO_FIELD, "") or "").strip()
            nascimento = convert_birthdate(nascimento_raw)

            brevo_upsert(email, situacao, segmento, rede, estado, software_house, nome, sobrenome, nascimento)
            upserts += 1

            if upserts % 100 == 0:
                log("Progresso: upserts=%s | processed=%s | start=%s | max_date_seen=%s" %
                    (upserts, processed, start, max_date_seen))

            time.sleep(BREVO_DELAY_SECONDS)

            if upserts >= MAX_CONTACTS_PER_RUN:
                break

        nxt = resp.get("next", None)
        if nxt is None:
            cp["mode"] = "incremental"
            cp["start_offset"] = 0
            cp["last_date_modify"] = max_date_seen
            save_checkpoint(cp)
            log("Fim da lista. Checkpoint salvo. upserts=%s | last_date_modify=%s" % (upserts, max_date_seen))
            return

        start = int(nxt)

    cp["mode"] = mode
    cp["last_date_modify"] = max_date_seen
    cp["start_offset"] = start if mode == "full" else 0
    save_checkpoint(cp)
    log("Limite por rodada atingido. upserts=%s | next_start_offset=%s | last_date_modify=%s" %
        (upserts, cp["start_offset"], max_date_seen))


if __name__ == "__main__":
    main()