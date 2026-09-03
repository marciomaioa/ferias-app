import os
import json
import hashlib
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import holidays

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Configuração Google Sheets
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
CREDS_JSON = json.loads(os.environ.get("GOOGLE_CREDENTIALS"))
creds = ServiceAccountCredentials.from_json_keyfile_dict(CREDS_JSON, SCOPE)
client = gspread.authorize(creds)
SHEET_ID = os.environ.get("SHEET_ID")
sheet = client.open_by_key(SHEET_ID)

# Abas
aba_equipes = sheet.worksheet("Equipes")
aba_usuarios = sheet.worksheet("Usuarios")
aba_ferias = sheet.worksheet("Ferias")
aba_config = sheet.worksheet("Config")

# ---------- FUNÇÕES AUXILIARES ----------
def get_feriados(ano):
    """Retorna lista de feriados nacionais do Brasil para o ano."""
    br_holidays = holidays.Brazil(years=ano)
    return [data.strftime("%d/%m/%Y") for data in br_holidays.keys()]

def carregar_config():
    """Lê o ano atual e os feriados da aba Config."""
    dados = aba_config.get_all_records()
    if dados:
        return dados[0]  # assume uma única linha
    return {"ano": 2027, "feriados": json.dumps(get_feriados(2027))}

def salvar_config(ano, feriados_json):
    """Atualiza a linha de config."""
    # implementar update

def calcular_dias_uteis(data_inicio, data_fim, feriados):
    """Conta dias úteis entre duas datas (inclusive)."""
    inicio = datetime.strptime(data_inicio, "%d/%m/%Y")
    fim = datetime.strptime(data_fim, "%d/%m/%Y")
    dias = 0
    atual = inicio
    while atual <= fim:
        if atual.weekday() < 5 and atual.strftime("%d/%m/%Y") not in feriados:
            dias += 1
        atual += timedelta(days=1)
    return dias

def proximo_dia_util(data_inicio_str, quantidade, feriados):
    """Retorna a data final após adicionar N dias úteis (excluindo fins de semana e feriados)."""
    data = datetime.strptime(data_inicio_str, "%d/%m/%Y")
    dias_adicionados = 0
    while dias_adicionados < quantidade:
        data += timedelta(days=1)
        if data.weekday() < 5 and data.strftime("%d/%m/%Y") not in feriados:
            dias_adicionados += 1
    return data.strftime("%d/%m/%Y")

def verificar_prioridade(usuario_id):
    """Verifica se o usuário tem permissão para fazer nova reserva baseado nos níveis inferiores."""
    usuarios = aba_usuarios.get_all_records()
    user = next((u for u in usuarios if u["id"] == usuario_id), None)
    if not user:
        return False
    equipe_id = user["equipe_id"]
    nivel = user["nivel"]
    # Busca todos os usuários da mesma equipe com nível < nivel
    inferiores = [u for u in usuarios if u["equipe_id"] == equipe_id and u["nivel"] < nivel]
    for inf in inferiores:
        # Calcula total de dias já reservados por esse inferior
        reservas = aba_ferias.get_all_records()
        total = sum([r["dias_uteis"] for r in reservas if r["usuario_id"] == inf["id"]])
        if total < 25:
            return False
    return True

def verificar_conflito_plantao(equipe_id, data_inicio, data_fim, reserva_id=None):
    """
    Verifica se a reserva proposta deixa a equipe desfalcada em algum plantão.
    Retorna True se NÃO houver conflito (reserva permitida).
    """
    # Mapeia dia do ano (1-365) para equipe de plantão (1 a 4)
    def equipe_plantao(data_str):
        dt = datetime.strptime(data_str, "%d/%m/%Y")
        dia_ano = dt.timetuple().tm_yday
        # Sequência: dia 1 = Eq2, 2 = Eq3, 3 = Eq4, 4 = Eq1, 5 = Eq2, ...
        ordem = [2, 3, 4, 1]
        return ordem[(dia_ano - 1) % 4]

    # Busca todos os membros da equipe
    usuarios = aba_usuarios.get_all_records()
    membros = [u for u in usuarios if u["equipe_id"] == equipe_id]
    total_membros = len(membros)

    # Para cada dia do período, verifica se é plantão da equipe
    inicio = datetime.strptime(data_inicio, "%d/%m/%Y")
    fim = datetime.strptime(data_fim, "%d/%m/%Y")
    atual = inicio
    while atual <= fim:
        data_str = atual.strftime("%d/%m/%Y")
        if equipe_plantao(data_str) == equipe_id:
            # Conta quantos membros estarão de férias neste dia
            # (incluindo a nova reserva e todas as outras)
            ferias_no_dia = 0
            todas_reservas = aba_ferias.get_all_records()
            for r in todas_reservas:
                if reserva_id and r["id"] == reserva_id:
                    continue  # ignora a reserva atual para simulação
                r_inicio = datetime.strptime(r["data_inicio"], "%d/%m/%Y")
                r_fim = datetime.strptime(r["data_fim"], "%d/%m/%Y")
                if r_inicio <= atual <= r_fim:
                    # Verifica se o usuário da reserva é da mesma equipe
                    user_reserva = next((u for u in usuarios if u["id"] == r["usuario_id"]), None)
                    if user_reserva and user_reserva["equipe_id"] == equipe_id:
                        ferias_no_dia += 1
            # Se todos os membros estiverem de férias nesse plantão, há conflito
            if ferias_no_dia >= total_membros:
                return False
        atual += timedelta(days=1)
    return True

# ---------- ROTAS ----------
@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('index.html', user=session.get('user'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        login = request.form['login']
        senha = request.form['senha']
        # Busca nas abas Equipes (admin) e Usuarios (comum)
        # Verifica hash
        # Se ok, salva na sessão
        pass
    return render_template('login.html')

@app.route('/admin')
def admin():
    if 'user_id' not in session or not session.get('is_admin'):
        return redirect(url_for('login'))
    return render_template('admin.html', equipe=session.get('equipe'))

@app.route('/api/calendario')
def api_calendario():
    ano = int(request.args.get('ano', 2027))
    mes = int(request.args.get('mes', 1))
    # Retorna os dias do mês com a cor da equipe de plantão e as reservas
    # ...
    return jsonify(dias)

@app.route('/api/reservas', methods=['GET', 'POST', 'DELETE'])
def api_reservas():
    if 'user_id' not in session:
        return jsonify({"error": "Não autenticado"}), 401
    if request.method == 'GET':
        # Lista reservas do usuário logado
        pass
    elif request.method == 'POST':
        data = request.json
        usuario_id = session['user_id']
        # Validar prioridade e conflito de plantão
        if not verificar_prioridade(usuario_id):
            return jsonify({"error": "Prioridade não respeitada"}), 400
        # Calcular dias úteis
        # Validar soma total <= 25
        # Verificar conflito
        if not verificar_conflito_plantao(equipe_id, data['inicio'], data['fim']):
            return jsonify({"error": "Conflito de plantão"}), 400
        # Salvar reserva
        return jsonify({"success": True})
    elif request.method == 'DELETE':
        # Cancelar reserva (verificar se é do usuário)
        pass

@app.route('/api/feriados')
def api_feriados():
    config = carregar_config()
    return jsonify(json.loads(config['feriados']))

if __name__ == '__main__':
    app.run(debug=True)