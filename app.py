# -*- coding: utf-8 -*-
import os
import json
import base64
import hashlib
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from werkzeug.security import generate_password_hash, check_password_hash
import holidays

# ============ INICIALIZAÇÃO DO FLASK ============
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")  # use variável de ambiente em produção

# ============ CONEXÃO COM GOOGLE SHEETS ============
def obter_credenciais():
    """
    Obtém as credenciais a partir da variável de ambiente GOOGLE_CREDENTIALS (Base64).
    Se falhar, tenta interpretar como JSON puro.
    """
    creds_b64 = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_b64:
        raise Exception("GOOGLE_CREDENTIALS não definida no ambiente.")

    try:
        # Tenta decodificar como Base64
        creds_json = json.loads(base64.b64decode(creds_b64).decode("utf-8"))
        return creds_json
    except Exception:
        # Fallback: tentar interpretar como JSON puro
        try:
            return json.loads(creds_b64)
        except Exception as e:
            raise Exception(f"GOOGLE_CREDENTIALS inválida. Deve ser Base64 ou JSON: {e}")

def conectar_planilha():
    creds_dict = obter_credenciais()
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    sheet_id = os.environ.get("SHEET_ID")
    if not sheet_id:
        raise Exception("SHEET_ID não definida no ambiente.")
    return client.open_by_key(sheet_id)

# Tenta conectar na inicialização (mas não quebra se falhar, para evitar erro de import)
try:
    sheet = conectar_planilha()
    print("✅ Conectado ao Google Sheets.")
except Exception as e:
    print(f"⚠️ Erro ao conectar ao Google Sheets: {e}")
    sheet = None

# ============ FUNÇÕES AUXILIARES ============

def get_worksheet(name):
    """Retorna uma aba da planilha, levantando exceção se não existir."""
    if sheet is None:
        raise Exception("Planilha não conectada.")
    return sheet.worksheet(name)

def get_feriados_do_ano(ano):
    """Retorna lista de feriados nacionais do Brasil para o ano (formato DD/MM/AAAA)."""
    feriados = holidays.Brazil(years=ano)
    return [data.strftime("%d/%m/%Y") for data in feriados.keys()]

def ler_config():
    """Lê a aba Config e retorna {'ano': int, 'feriados': list}."""
    ws = get_worksheet("Config")
    records = ws.get_all_records()
    if not records:
        return {"ano": 2027, "feriados": []}
    linha = records[0]
    ano = int(linha.get("ano", 2027))
    feriados = json.loads(linha.get("feriados", "[]"))
    return {"ano": ano, "feriados": feriados}

def atualizar_config(ano, feriados):
    """Atualiza a aba Config com ano e lista de feriados."""
    ws = get_worksheet("Config")
    ws.clear()
    ws.update(range_name="A1", values=[["ano", "feriados"]])
    ws.append_row([ano, json.dumps(feriados)])

def calcular_dias_uteis(data_inicio_str, data_fim_str, feriados):
    """Conta dias úteis entre duas datas (inclusive), excluindo fins de semana e feriados."""
    inicio = datetime.strptime(data_inicio_str, "%d/%m/%Y")
    fim = datetime.strptime(data_fim_str, "%d/%m/%Y")
    dias = 0
    atual = inicio
    while atual <= fim:
        if atual.weekday() < 5 and atual.strftime("%d/%m/%Y") not in feriados:
            dias += 1
        atual += timedelta(days=1)
    return dias

def proximo_dia_util(data_inicio_str, quantidade, feriados):
    """Retorna a data final (DD/MM/AAAA) após adicionar N dias úteis."""
    data = datetime.strptime(data_inicio_str, "%d/%m/%Y")
    dias_adicionados = 0
    while dias_adicionados < quantidade:
        data += timedelta(days=1)
        if data.weekday() < 5 and data.strftime("%d/%m/%Y") not in feriados:
            dias_adicionados += 1
    return data.strftime("%d/%m/%Y")

def equipe_plantao_para_data(data_str):
    """
    Retorna o ID da equipe (1 a 4) que está de plantão em uma determinada data.
    Regra: Dia 1 -> Equipe2, Dia 2 -> Equipe3, Dia 3 -> Equipe4, Dia 4 -> Equipe1, repete a cada 4 dias.
    """
    dt = datetime.strptime(data_str, "%d/%m/%Y")
    dia_ano = dt.timetuple().tm_yday
    ordem = [2, 3, 4, 1]
    return ordem[(dia_ano - 1) % 4]

def verificar_prioridade(usuario_id):
    """
    Verifica se o usuário pode fazer uma nova reserva com base no nível.
    Retorna (bool, mensagem).
    """
    try:
        ws_usuarios = get_worksheet("Usuarios")
        usuarios = ws_usuarios.get_all_records()
        usuario = next((u for u in usuarios if str(u.get("id")) == str(usuario_id)), None)
        if not usuario:
            return False, "Usuário não encontrado."

        equipe_id = usuario["equipe_id"]
        nivel = usuario["nivel"]

        # Busca usuários da mesma equipe com nível inferior
        inferiores = [u for u in usuarios if u.get("equipe_id") == equipe_id and u.get("nivel", 999) < nivel]

        # Se não há inferiores, sempre pode
        if not inferiores:
            return True, ""

        # Soma dias de férias de cada inferior
        ws_ferias = get_worksheet("Ferias")
        ferias = ws_ferias.get_all_records()
        for inf in inferiores:
            total_dias = sum([int(r.get("dias_uteis", 0)) for r in ferias if str(r.get("usuario_id")) == str(inf.get("id"))])
            if total_dias < 25:
                return False, f"Usuário {inf.get('nome')} (nível {inf.get('nivel')}) ainda não completou 25 dias (tem {total_dias})."

        return True, ""
    except Exception as e:
        return False, f"Erro ao verificar prioridade: {e}"

def verificar_conflito_plantao(equipe_id, data_inicio_str, data_fim_str, reserva_id=None):
    """
    Verifica se a reserva proposta deixaria a equipe desfalcada em algum plantão.
    Retorna (bool, mensagem).
    """
    try:
        # Busca todos os membros da equipe
        ws_usuarios = get_worksheet("Usuarios")
        usuarios = ws_usuarios.get_all_records()
        membros = [u for u in usuarios if u.get("equipe_id") == equipe_id]
        total_membros = len(membros)

        # Busca todas as reservas (exceto a atual, se for edição)
        ws_ferias = get_worksheet("Ferias")
        todas_reservas = ws_ferias.get_all_records()
        if reserva_id:
            todas_reservas = [r for r in todas_reservas if str(r.get("id")) != str(reserva_id)]

        # Mapeia cada dia do período
        inicio = datetime.strptime(data_inicio_str, "%d/%m/%Y")
        fim = datetime.strptime(data_fim_str, "%d/%m/%Y")
        atual = inicio

        while atual <= fim:
            data_str = atual.strftime("%d/%m/%Y")
            if equipe_plantao_para_data(data_str) == equipe_id:
                # Conta quantos membros estarão de férias neste dia
                ferias_no_dia = 0
                for r in todas_reservas:
                    r_inicio = datetime.strptime(r["data_inicio"], "%d/%m/%Y")
                    r_fim = datetime.strptime(r["data_fim"], "%d/%m/%Y")
                    if r_inicio <= atual <= r_fim:
                        user_reserva = next((u for u in usuarios if str(u.get("id")) == str(r.get("usuario_id"))), None)
                        if user_reserva and user_reserva.get("equipe_id") == equipe_id:
                            ferias_no_dia += 1
                # Se todos estiverem de férias, há conflito
                if ferias_no_dia >= total_membros:
                    return False, f"No dia {data_str} (plantão da Equipe {equipe_id}), todos os membros estão de férias."

            atual += timedelta(days=1)

        return True, ""
    except Exception as e:
        return False, f"Erro ao verificar conflito: {e}"

# ============ ROTAS DE AUTENTICAÇÃO ============

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        login = request.form.get('login')
        senha = request.form.get('senha')
        if not login or not senha:
            return render_template('login.html', erro="Preencha login e senha.")

        try:
            # Verifica se é admin (aba Equipes)
            ws_equipes = get_worksheet("Equipes")
            equipes = ws_equipes.get_all_records()
            admin = next((eq for eq in equipes if eq.get("login_admin") == login), None)
            if admin and check_password_hash(admin.get("senha_admin"), senha):
                session['user_id'] = f"admin_{admin['id']}"
                session['is_admin'] = True
                session['equipe_id'] = admin['id']
                session['nome'] = admin['nome']
                session['login'] = login
                return redirect(url_for('admin_panel'))

            # Verifica se é usuário comum (aba Usuarios)
            ws_usuarios = get_worksheet("Usuarios")
            usuarios = ws_usuarios.get_all_records()
            user = next((u for u in usuarios if u.get("login") == login), None)
            if user and check_password_hash(user.get("senha_hash"), senha):
                session['user_id'] = user['id']
                session['is_admin'] = False
                session['equipe_id'] = user['equipe_id']
                session['nome'] = user['nome']
                session['login'] = login
                session['nivel'] = user['nivel']
                return redirect(url_for('index'))

            return render_template('login.html', erro="Login ou senha inválidos.")
        except Exception as e:
            return render_template('login.html', erro=f"Erro ao autenticar: {e}")

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ============ DECORADOR DE AUTENTICAÇÃO ============

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session or not session.get('is_admin'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# ============ ROTAS PRINCIPAIS ============

@app.route('/')
@login_required
def index():
    return render_template('index.html', usuario=session.get('nome'), is_admin=session.get('is_admin'))

@app.route('/admin')
@admin_required
def admin_panel():
    return render_template('admin.html', equipe=session.get('equipe_id'))

# ============ API: TESTE DE CONEXÃO ============

@app.route('/test-sheet')
def test_sheet():
    try:
        ws = get_worksheet("Equipes")
        registros = ws.get_all_records()
        return jsonify({
            "status": "ok",
            "rows": len(registros),
            "data": registros[:3]
        })
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500

# ============ API: FERIADOS ============

@app.route('/api/feriados')
def api_feriados():
    config = ler_config()
    return jsonify(config['feriados'])

# ============ API: CALENDÁRIO ============

@app.route('/api/calendario')
def api_calendario():
    """
    Retorna dados do mês: dias, cores das equipes de plantão, reservas da equipe do usuário.
    Parâmetros: ano, mes (opcionais, padrão atual)
    """
    try:
        ano = int(request.args.get('ano', datetime.now().year))
        mes = int(request.args.get('mes', datetime.now().month))

        config = ler_config()
        feriados = config['feriados']

        # Se não estiver logado, retorna apenas os plantões
        equipe_id = session.get('equipe_id')
        if not equipe_id:
            return jsonify({"error": "Não autenticado"}), 401

        # Gera dias do mês
        primeiro_dia = datetime(ano, mes, 1)
        ultimo_dia = datetime(ano, mes, 1) + timedelta(days=31)
        ultimo_dia = ultimo_dia.replace(day=1) - timedelta(days=1)

        dias = []
        atual = primeiro_dia
        while atual <= ultimo_dia:
            data_str = atual.strftime("%d/%m/%Y")
            plantao = equipe_plantao_para_data(data_str)

            # Verifica se há reservas da equipe para este dia
            reservas_hoje = []
            if sheet:
                ws_ferias = get_worksheet("Ferias")
                ferias = ws_ferias.get_all_records()
                for r in ferias:
                    r_inicio = datetime.strptime(r["data_inicio"], "%d/%m/%Y")
                    r_fim = datetime.strptime(r["data_fim"], "%d/%m/%Y")
                    if r_inicio <= atual <= r_fim:
                        user_id = r.get("usuario_id")
                        ws_usuarios = get_worksheet("Usuarios")
                        usuarios = ws_usuarios.get_all_records()
                        user = next((u for u in usuarios if str(u.get("id")) == str(user_id)), None)
                        if user and user.get("equipe_id") == equipe_id:
                            reservas_hoje.append({
                                "usuario": user.get("nome"),
                                "inicio": r["data_inicio"],
                                "fim": r["data_fim"]
                            })

            dias.append({
                "data": data_str,
                "dia_semana": atual.weekday(),
                "equipe_plantao": plantao,
                "feriado": data_str in feriados,
                "reservas": reservas_hoje
            })
            atual += timedelta(days=1)

        return jsonify({
            "ano": ano,
            "mes": mes,
            "dias": dias,
            "equipe_atual": equipe_id
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============ API: RESERVAS ============

@app.route('/api/reservas', methods=['GET', 'POST', 'DELETE'])
@login_required
def api_reservas():
    usuario_id = session.get('user_id')
    if session.get('is_admin'):
        # Admin pode gerenciar reservas de qualquer um da equipe? Vamos permitir via parâmetro.
        usuario_id = request.args.get('usuario_id', usuario_id)

    if request.method == 'GET':
        try:
            ws_ferias = get_worksheet("Ferias")
            ferias = ws_ferias.get_all_records()
            minhas = [r for r in ferias if str(r.get("usuario_id")) == str(usuario_id)]
            return jsonify(minhas)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif request.method == 'POST':
        data = request.json
        required = ['usuario_id', 'data_inicio', 'dias_uteis']
        if not all(k in data for k in required):
            return jsonify({"error": "Campos obrigatórios: usuario_id, data_inicio, dias_uteis"}), 400

        user_id = data['usuario_id']
        data_inicio = data['data_inicio']  # formato DD/MM/AAAA
        dias_uteis = int(data['dias_uteis'])

        # Valida se o usuário tem prioridade
        pode, msg = verificar_prioridade(user_id)
        if not pode:
            return jsonify({"error": msg}), 403

        # Calcula data_fim com base nos dias úteis
        config = ler_config()
        feriados = config['feriados']
        data_fim = proximo_dia_util(data_inicio, dias_uteis, feriados)

        # Verifica se o total de dias do usuário não ultrapassa 25
        ws_ferias = get_worksheet("Ferias")
        ferias = ws_ferias.get_all_records()
        total_atual = sum([int(r.get("dias_uteis", 0)) for r in ferias if str(r.get("usuario_id")) == str(user_id)])
        if total_atual + dias_uteis > 25:
            return jsonify({"error": f"Usuário já tem {total_atual} dias; só pode adicionar até {25-total_atual}."}), 400

        # Obtém equipe do usuário
        ws_usuarios = get_worksheet("Usuarios")
        usuarios = ws_usuarios.get_all_records()
        user = next((u for u in usuarios if str(u.get("id")) == str(user_id)), None)
        if not user:
            return jsonify({"error": "Usuário não encontrado."}), 404
        equipe_id = user.get("equipe_id")

        # Verifica conflito de plantão
        pode, msg = verificar_conflito_plantao(equipe_id, data_inicio, data_fim)
        if not pode:
            return jsonify({"error": msg}), 409

        # Gera novo ID
        ids = [int(r.get("id", 0)) for r in ferias]
        novo_id = max(ids) + 1 if ids else 1

        # Insere reserva
        ws_ferias.append_row([
            novo_id,
            user_id,
            data_inicio,
            data_fim,
            dias_uteis,
            "aprovado"
        ])
        return jsonify({"success": True, "id": novo_id})

    elif request.method == 'DELETE':
        reserva_id = request.args.get('id')
        if not reserva_id:
            return jsonify({"error": "ID da reserva obrigatório."}), 400

        try:
            ws_ferias = get_worksheet("Ferias")
            ferias = ws_ferias.get_all_records()
            # Procura a reserva
            idx = None
            for i, r in enumerate(ferias, start=2):  # linha 1 é cabeçalho
                if str(r.get("id")) == str(reserva_id):
                    idx = i
                    break
            if idx is None:
                return jsonify({"error": "Reserva não encontrada."}), 404

            # Verifica se o usuário é dono da reserva ou admin
            if not session.get('is_admin') and str(ferias[idx-2].get("usuario_id")) != str(session.get('user_id')):
                return jsonify({"error": "Permissão negada."}), 403

            ws_ferias.delete_rows(idx)
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

# ============ ROTAS ADMINISTRATIVAS ============

@app.route('/api/admin/usuarios', methods=['GET', 'POST'])
@admin_required
def admin_usuarios():
    equipe_id = session.get('equipe_id')
    if request.method == 'GET':
        try:
            ws_usuarios = get_worksheet("Usuarios")
            usuarios = ws_usuarios.get_all_records()
            da_equipe = [u for u in usuarios if u.get("equipe_id") == equipe_id]
            # Oculta hashes
            for u in da_equipe:
                u.pop("senha_hash", None)
            return jsonify(da_equipe)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif request.method == 'POST':
        data = request.json
        required = ['nome', 'nivel', 'login', 'senha']
        if not all(k in data for k in required):
            return jsonify({"error": "Campos obrigatórios: nome, nivel, login, senha"}), 400

        try:
            ws_usuarios = get_worksheet("Usuarios")
            usuarios = ws_usuarios.get_all_records()
            # Gera novo ID
            ids = [int(u.get("id", 0)) for u in usuarios]
            novo_id = max(ids) + 1 if ids else 1

            senha_hash = generate_password_hash(data['senha'])
            ws_usuarios.append_row([
                novo_id,
                data['nome'],
                equipe_id,
                data['nivel'],
                data['login'],
                senha_hash,
                False  # admin = FALSE para usuário comum
            ])
            return jsonify({"success": True, "id": novo_id})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

@app.route('/api/admin/config', methods=['GET', 'POST'])
@admin_required
def admin_config():
    if request.method == 'GET':
        return jsonify(ler_config())

    elif request.method == 'POST':
        data = request.json
        novo_ano = data.get('ano')
        if not novo_ano:
            return jsonify({"error": "Ano obrigatório."}), 400

        try:
            novo_ano = int(novo_ano)
            # Obtém feriados do ano
            feriados = get_feriados_do_ano(novo_ano)
            atualizar_config(novo_ano, feriados)
            return jsonify({"success": True, "ano": novo_ano, "feriados": feriados})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

# ============ PÁGINAS DE ERRO ============

@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(e):
    return render_template('500.html'), 500

# ============ INICIALIZAÇÃO ============
if __name__ == "__main__":
    app.run(debug=True)