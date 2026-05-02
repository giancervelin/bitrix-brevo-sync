import os
import time
import json
import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

BITRIX_WEBHOOK_BASE = (os.getenv("BITRIX_WEBHOOK_BASE") or "").strip().rstrip("/") + "/"
CHECKPOINT_PATH = os.path.join(BASE_DIR, "repair_checkpoint.json")

SLEEP_BETWEEN_COMPANIES = 1.5
TIMEOUT_SECONDS = 90
RETRY_DELAY = 300

# Ajuste aqui se sua conexão cai muito:
# 300 = ~lote menor, 500 = médio, 1000 = maior (recomendado começar com 500)
MAX_PROCESS = 1000

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] %s" % (ts, msg), flush=True)

def bitrix_call(method, params):
    log("Chamando %s" % method)
    url = "%s%s.json" % (BITRIX_WEBHOOK_BASE, method)
    r = requests.post(url, json=params or {}, timeout=TIMEOUT_SECONDS)
    try:
        data = r.json()
    except Exception:
        data = {"http_status": r.status_code, "raw": r.text}
    if isinstance(data, dict) and data.get("error") == "OPERATION_TIME_LIMIT":
        log("Limite atingido, esperando %s segundos..." % RETRY_DELAY)
        time.sleep(RETRY_DELAY)
        return bitrix_call(method, params)
    return r.status_code, data

def extract_emails(company):
    emails = []
    email_field = company.get("EMAIL")
    if isinstance(email_field, list):
        for item in email_field:
            if isinstance(item, dict) and item.get("VALUE"):
                emails.append(item["VALUE"].strip().lower())
    return emails

def extract_phones(company):
    phones = []
    phone_field = company.get("PHONE")
    if isinstance(phone_field, list):
        for item in phone_field:
            if isinstance(item, dict) and item.get("VALUE"):
                phones.append(item["VALUE"].strip())
    return phones

def contact_exists_with_email(email):
    _, resp = bitrix_call("crm.contact.list", {"filter": {"EMAIL": email}, "select": ["ID"]})
    items = (resp.get("result") or []) if isinstance(resp, dict) else []
    return len(items) > 0

def company_has_any_contact(company_id):
    _, resp = bitrix_call("crm.company.contact.items.get", {"id": company_id})
    items = (resp.get("result") or []) if isinstance(resp, dict) else []
    return len(items) > 0

def create_contact_for_company(company_id, company_title, name, email, phone):
    contact_params = {
        "fields": {
            "NAME": name,
            "LAST_NAME": company_title[:50],
            "COMPANY_ID": company_id,
            "EMAIL": [{"VALUE": email, "VALUE_TYPE": "WORK"}],
            "PHONE": [{"VALUE": phone, "VALUE_TYPE": "WORK"}] if phone else [],
        }
    }
    return bitrix_call("crm.contact.add", contact_params)

def load_checkpoint():
    if os.path.exists(CHECKPOINT_PATH):
        try:
            with open(CHECKPOINT_PATH, "r", encoding="utf-8") as f:
                cp = json.load(f)
            return int(cp.get("start", 0) or 0)
        except Exception:
            return 0
    return 0

def save_checkpoint(start):
    with open(CHECKPOINT_PATH, "w", encoding="utf-8") as f:
        json.dump({"start": int(start)}, f)

def main():
    if not BITRIX_WEBHOOK_BASE or BITRIX_WEBHOOK_BASE == "/":
        raise RuntimeError("BITRIX_WEBHOOK_BASE ausente no .env")

    log("Iniciando reparo em lote: Empresa -> Contato (até %s empresas)" % MAX_PROCESS)
    start = load_checkpoint()
    reparados = 0
    erros = 0
    verificados = 0

    log("Checkpoint: start=%s" % start)

    while verificados < MAX_PROCESS:
        params = {
            "start": start,
            "filter": {"HAS_EMAIL": "Y"},
            "select": ["ID", "TITLE", "EMAIL", "PHONE"],
            "order": {"ID": "ASC"},
        }
        _, resp = bitrix_call("crm.company.list", params)

        if isinstance(resp, dict) and resp.get("error"):
            log("Erro: %s" % resp.get("error"))
            break

        companies = (resp.get("result") or []) if isinstance(resp, dict) else []
        if not companies:
            log("Fim do processamento.")
            break

        for comp in companies:
            verificados += 1
            comp_id = comp.get("ID")
            comp_title = (comp.get("TITLE") or "Empresa sem nome").strip()

            if not comp_id:
                continue

            if company_has_any_contact(comp_id):
                pass
            else:
                emails = extract_emails(comp)
                phones = extract_phones(comp)

                if emails:
                    for i, email in enumerate(emails):
                        if contact_exists_with_email(email):
                            continue

                        phone = phones[i] if i < len(phones) else ""
                        log("Criando contato: %s (%s) para empresa %s" % ("Comprador", email, 
comp_id))
                        _, new_contact = create_contact_for_company(comp_id, comp_title, 
"Comprador", email, phone)

                        if isinstance(new_contact, dict) and new_contact.get("result"):
                            reparados += 1
                        else:
                            erros += 1
                            log("Falha: %s" % new_contact)

                        time.sleep(SLEEP_BETWEEN_COMPANIES)

            if verificados % 50 == 0:
                log("Progresso: verificados=%s | criados=%s | erros=%s | start=%s" %
                    (verificados, reparados, erros, start))

            if verificados >= MAX_PROCESS:
                log("Limite de lote atingido (%s). Parando..." % MAX_PROCESS)
                break

        nxt = resp.get("next", None) if isinstance(resp, dict) else None
        if nxt is None:
            log("Fim da paginação.")
            break

        start = int(nxt)
        save_checkpoint(start)

    log("Lote concluído! verificados=%s | criados=%s | erros=%s" % (verificados, reparados, 
erros))

if __name__ == "__main__":
    main()
