# add_guardiao.py
import os
import json
import base64
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from werkzeug.security import generate_password_hash

# ============ CONFIGURAÇÃO ============
# Substitua pelo ID da sua planilha
SHEET_ID = "1C-NAw7tGXx97Lb2RwCNC64eRIdBJSHpq1kYk05_z5R4"

# ============ CREDENCIAIS ============
def obter_credenciais():
    creds_b64 = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_b64:
        return json.loads(base64.b64decode(creds_b64).decode("utf-8"))
    else:
        # Se estiver rodando localmente, coloque o arquivo credentials.json na mesma pasta
        with open("credentials.json", "r") as f:
            return json.load(f)

def main():
    creds_dict = obter_credenciais()
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID)

    ws_usuarios = sheet.worksheet("Usuarios")
    usuarios = ws_usuarios.get_all_records()

    # Verifica se já existe
    for u in usuarios:
        if u.get('login') == 'guardiao':
            print("Usuário Guardiao já existe. Nada a fazer.")
            return

    # Gera novo ID
    ids = [int(u.get('id', 0)) for u in usuarios]
    novo_id = max(ids) + 1 if ids else 1

    senha_hash = generate_password_hash("Mac140502*")

    # Insere: id, nome, equipe_id, nivel, login, senha_hash, admin, global_admin
    ws_usuarios.append_row([
        novo_id,
        "Guardiao",
        0,          # equipe_id = 0 (não pertence a nenhuma equipe específica)
        0,          # nivel = 0 (não usado para prioridade)
        "guardiao",
        senha_hash,
        "TRUE",     # admin (tem acesso ao painel)
        "TRUE"      # global_admin (pode ver e editar todas as equipes)
    ])
    print("✅ Usuário Guardiao criado com sucesso!")
    print("   Login: guardiao")
    print("   Senha: Mac140502*")

if __name__ == "__main__":
    main()