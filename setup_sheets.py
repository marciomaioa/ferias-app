import os
import json
import base64
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from werkzeug.security import generate_password_hash

# ============ CONFIGURAÇÃO ============
SHEET_ID = "1C-NAw7tGXx97Lb2RwCNC64eRIdBJSHpq1kYk05_z5R4"
ANO = 2027

EQUIPES = [
    {"id": 1, "nome": "Equipe1", "cor": "#FFB6C1", "login_admin": "equipe1", "senha": "123"},
    {"id": 2, "nome": "Equipe2", "cor": "#90EE90", "login_admin": "equipe2", "senha": "123"},
    {"id": 3, "nome": "Equipe3", "cor": "#ADD8E6", "login_admin": "equipe3", "senha": "123"},
    {"id": 4, "nome": "Equipe4", "cor": "#FFFFE0", "login_admin": "equipe4", "senha": "123"},
]

USUARIOS_EXEMPLO = [
    {"nome": "João", "equipe_id": 1, "nivel": 1, "login": "joao", "senha": "123", "admin": False},
    {"nome": "Maria", "equipe_id": 1, "nivel": 2, "login": "maria", "senha": "123", "admin": False},
    {"nome": "Pedro", "equipe_id": 2, "nivel": 1, "login": "pedro", "senha": "123", "admin": False},
    {"nome": "Ana", "equipe_id": 3, "nivel": 1, "login": "ana", "senha": "123", "admin": False},
]

# ============ CONEXÃO ============
def obter_credenciais():
    creds_b64 = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_b64:
        creds_json = json.loads(base64.b64decode(creds_b64).decode("utf-8"))
    else:
        with open("credentials.json", "r") as f:
            creds_json = json.load(f)
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    return ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)

def popular_planilha():
    creds = obter_credenciais()
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID)

    # ---------- Cria as abas se não existirem ----------
    abas_necessarias = ["Equipes", "Usuarios", "Ferias", "Config"]
    for nome in abas_necessarias:
        try:
            sheet.worksheet(nome)
        except gspread.WorksheetNotFound:
            sheet.add_worksheet(title=nome, rows=100, cols=20)
            print(f"✅ Aba '{nome}' criada.")

    # ---------- Aba Equipes ----------
    ws = sheet.worksheet("Equipes")
    ws.clear()
    ws.update(range_name="A1", values=[["id", "nome", "cor", "login_admin", "senha_admin"]])
    linhas = []
    for eq in EQUIPES:
        senha_hash = generate_password_hash(eq["senha"])
        linhas.append([eq["id"], eq["nome"], eq["cor"], eq["login_admin"], senha_hash])
    ws.append_rows(linhas, value_input_option="USER_ENTERED")
    print("✅ Equipes inseridas.")

    # ---------- Aba Usuarios ----------
    ws = sheet.worksheet("Usuarios")
    ws.clear()
    ws.update(range_name="A1", values=[["id", "nome", "equipe_id", "nivel", "login", "senha_hash", "admin"]])
    linhas = []
    for i, user in enumerate(USUARIOS_EXEMPLO, start=1):
        senha_hash = generate_password_hash(user["senha"])
        admin_str = "TRUE" if user["admin"] else "FALSE"
        linhas.append([i, user["nome"], user["equipe_id"], user["nivel"],
                       user["login"], senha_hash, admin_str])
    ws.append_rows(linhas, value_input_option="USER_ENTERED")
    print("✅ Usuários inseridos.")

    # ---------- Aba Config ----------
    ws = sheet.worksheet("Config")
    ws.clear()
    ws.update(range_name="A1", values=[["ano", "feriados"]])
    ws.append_row([ANO, json.dumps([])])
    print("✅ Config inserida.")

    # ---------- Aba Ferias ----------
    ws = sheet.worksheet("Ferias")
    ws.clear()
    ws.update(range_name="A1", values=[["id", "usuario_id", "data_inicio", "data_fim", "dias_uteis", "status"]])
    print("✅ Aba Ferias pronta.")

    print(f"\n🎉 Planilha {SHEET_ID} populada com sucesso!")

if __name__ == "__main__":
    popular_planilha()