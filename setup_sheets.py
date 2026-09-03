import os
import json
import base64
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from werkzeug.security import generate_password_hash

# ============ CONFIGURAÇÃO ============
# Defina o nome da nova planilha
NOME_PLANILHA = "GestaoFerias2027"

# Defina o ano (para a aba Config)
ANO = 2027

# Defina as equipes iniciais
EQUIPES = [
    {"id": 1, "nome": "Equipe1", "cor": "#FFB6C1", "login_admin": "equipe1", "senha": "123"},
    {"id": 2, "nome": "Equipe2", "cor": "#90EE90", "login_admin": "equipe2", "senha": "123"},
    {"id": 3, "nome": "Equipe3", "cor": "#ADD8E6", "login_admin": "equipe3", "senha": "123"},
    {"id": 4, "nome": "Equipe4", "cor": "#FFFFE0", "login_admin": "equipe4", "senha": "123"},
]

# (Opcional) Usuários de exemplo para cada equipe
USUARIOS_EXEMPLO = [
    {"nome": "João", "equipe_id": 1, "nivel": 1, "login": "joao", "senha": "123", "admin": False},
    {"nome": "Maria", "equipe_id": 1, "nivel": 2, "login": "maria", "senha": "123", "admin": False},
    {"nome": "Pedro", "equipe_id": 2, "nivel": 1, "login": "pedro", "senha": "123", "admin": False},
    {"nome": "Ana", "equipe_id": 3, "nivel": 1, "login": "ana", "senha": "123", "admin": False},
]

# ============ CONEXÃO COM GOOGLE SHEETS ============
def obter_credenciais():
    """Obtém as credenciais do ambiente (Base64) ou de um arquivo local."""
    creds_b64 = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_b64:
        # Decodifica de Base64
        creds_json = json.loads(base64.b64decode(creds_b64).decode("utf-8"))
    else:
        # Tenta carregar de um arquivo local (para testes)
        try:
            with open("credentials.json", "r") as f:
                creds_json = json.load(f)
        except FileNotFoundError:
            raise Exception("Credenciais não encontradas. Defina GOOGLE_CREDENTIALS ou coloque credentials.json no diretório.")
    
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    return ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)

def criar_planilha():
    """Cria uma nova planilha com as abas e dados iniciais."""
    creds = obter_credenciais()
    client = gspread.authorize(creds)

    # Cria uma nova planilha
    print(f"📝 Criando planilha: {NOME_PLANILHA}")
    sheet = client.create(NOME_PLANILHA)  # Cria na raiz do Drive
    print(f"✅ Planilha criada! ID: {sheet.id}")
    print(f"🔗 URL: https://docs.google.com/spreadsheets/d/{sheet.id}")

    # Compartilha com a conta de serviço (opcional, mas já tem acesso)
    # sheet.share(creds.service_account_email, perm_type='user', role='writer')

    # ============ CRIAÇÃO DAS ABAS ============
    abas = {
        "Equipes": ["id", "nome", "cor", "login_admin", "senha_admin"],
        "Usuarios": ["id", "nome", "equipe_id", "nivel", "login", "senha_hash", "admin"],
        "Ferias": ["id", "usuario_id", "data_inicio", "data_fim", "dias_uteis", "status"],
        "Config": ["ano", "feriados"]
    }

    for nome, cabecalho in abas.items():
        try:
            # Se a aba já existir, usa-a; senão, cria
            ws = sheet.worksheet(nome)
        except gspread.WorksheetNotFound:
            ws = sheet.add_worksheet(title=nome, rows=100, cols=20)
        # Limpa e insere cabeçalho
        ws.clear()
        ws.update(range_name="A1", values=[cabecalho])
        print(f"  ✅ Aba '{nome}' criada com cabeçalho.")

    # ============ POPULAR ABA 'Equipes' ============
    ws_equipes = sheet.worksheet("Equipes")
    linhas = []
    for eq in EQUIPES:
        senha_hash = generate_password_hash(eq["senha"])
        linhas.append([eq["id"], eq["nome"], eq["cor"], eq["login_admin"], senha_hash])
    if linhas:
        ws_equipes.append_rows(linhas, value_input_option="USER_ENTERED")
        print(f"  ✅ Inseridas {len(linhas)} equipes.")

    # ============ POPULAR ABA 'Usuarios' (exemplo) ============
    ws_usuarios = sheet.worksheet("Usuarios")
    linhas_usuarios = []
    # Gerar IDs sequenciais a partir de 1
    for i, user in enumerate(USUARIOS_EXEMPLO, start=1):
        senha_hash = generate_password_hash(user["senha"])
        admin_str = "TRUE" if user["admin"] else "FALSE"
        linhas_usuarios.append([
            i, user["nome"], user["equipe_id"], user["nivel"],
            user["login"], senha_hash, admin_str
        ])
    if linhas_usuarios:
        ws_usuarios.append_rows(linhas_usuarios, value_input_option="USER_ENTERED")
        print(f"  ✅ Inseridos {len(linhas_usuarios)} usuários de exemplo.")

    # ============ POPULAR ABA 'Config' ============
    ws_config = sheet.worksheet("Config")
    # Inicialmente, feriados vazios (o sistema preencherá depois)
    ws_config.append_row([ANO, json.dumps([])])
    print(f"  ✅ Configuração definida para o ano {ANO}.")

    print("\n🎉 Planilha criada com sucesso!")
    print(f"📌 Lembre-se de compartilhar a planilha com o e-mail da conta de serviço:")
    print(f"   {creds.service_account_email}")
    print("   (Dê permissão de Editor)")

    return sheet.id

if __name__ == "__main__":
    criar_planilha()