# -*- coding: utf-8 -*-
import os
import json
import base64
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from werkzeug.security import generate_password_hash, check_password_hash
import holidays

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")

# ============ CONEXÃO GOOGLE SHEETS ============
def obter_credenciais():
    creds_b64 = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_b64:
        raise Exception("GOOGLE_CREDENTIALS não definida.")
    try:
        return json.loads(base64.b64decode(creds_b64).decode("utf-8"))
    except:
        return json.loads(creds_b64)

def conectar_planilha():
    creds_dict = obter_credenciais()
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    sheet_id = os.environ.get("SHEET_ID")
    if not sheet_id:
        raise Exception("SHEET_ID não definida.")
    return client.open_by_key(sheet_id)

try:
    sheet = conectar_planilha()
    print("✅ Conectado ao Google Sheets.")
except Exception as e:
    print(f"⚠️ Erro ao conectar: {e}")
    sheet = None

def get_worksheet(name):
    if sheet is None:
        raise Exception("Planilha não conectada.")
    return sheet.worksheet(name)

# ============ FUNÇÕES AUXILIARES ============

def get_feriados_do_ano(ano):
    return [d.strftime("%d/%m/%Y") for d in holidays.Brazil(years=ano).keys()]

def ler_config():
    ws = get_worksheet("Config")
    records = ws.get_all_records()
    if not records:
        return {"ano": 2027, "feriados": []}
    linha = records[0]
    return {"ano": int(linha.get("ano", 2027)), "feriados": json.loads(linha.get("feriados", "[]"))}

def atualizar_config(ano, feriados):
    ws = get_worksheet("Config")
    ws.clear()
    ws.update(range_name="A1", values=[["ano", "feriados"]])
    ws.append_row([ano, json.dumps(feriados)])

def calcular_dias_uteis(data_inicio_str, data_fim_str, feriados):
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
    data = datetime.strptime(data_inicio_str, "%d/%m/%Y")
    dias_adicionados = 0
    while dias_adicionados < quantidade:
        data += timedelta(days=1)
        if data.weekday() < 5 and data.strftime("%d/%m/%Y") not in feriados:
            dias_adicionados += 1
    return data.strftime("%d/%m/%Y")

def equipe_plantao_para_data(data_str):
    dt = datetime.strptime(data_str, "%d/%m/%Y")
    dia_ano = dt.timetuple().tm_yday
    ordem = [2, 3, 4, 1]
    return ordem[(dia_ano - 1) % 4]

def verificar_prioridade(usuario_id):
    try:
        ws_usuarios = get_worksheet("Usuarios")
        usuarios = ws_usuarios.get_all_records()
        usuario = next((u for u in usuarios if str(u.get("id")) == str(usuario_id)), None)
        if not usuario:
            return False, "Usuário não encontrado."
        equipe_id = usuario["equipe_id"]
        nivel = usuario["nivel"]
        inferiores = [u for u in usuarios if u.get("equipe_id") == equipe_id and u.get("nivel", 999) < nivel]
        if not inferiores:
            return True, ""
        ws_ferias = get_worksheet("Ferias")
        ferias = ws_ferias.get_all_records()
        for inf in inferiores:
            total = sum([int(r.get("dias_uteis", 0)) for r in ferias if str(r.get("usuario_id")) == str(inf.get("id"))])
            if total < 25:
                return False, f"Usuário {inf.get('nome')} (nível {inf.get('nivel')}) ainda tem {total} dias; precisa de 25."
        return True, ""
    except Exception as e:
        return False, f"Erro: {e}"

def verificar_conflito_plantao(equipe_id, data_inicio_str, data_fim_str, reserva_id=None):
    try:
        ws_usuarios = get_worksheet("Usuarios")
        usuarios = ws_usuarios.get_all_records()
        membros = [u for u in usuarios if u.get("equipe_id") == equipe_id]
        total_membros = len(membros)
        ws_ferias = get_worksheet("Ferias")
        todas_reservas = ws_ferias.get_all_records()
        if reserva_id:
            todas_reservas = [r for r in todas_reservas if str(r.get("id")) != str(reserva_id)]
        inicio = datetime.strptime(data_inicio_str, "%d/%m/%Y")
        fim = datetime.strptime(data_fim_str, "%d/%m/%Y")
        atual = inicio
        while atual <= fim:
            data_str = atual.strftime("%d/%m/%Y")
            if equipe_plantao_para_data(data_str) == equipe_id:
                ferias_no_dia = 0
                for r in todas_reservas:
                    r_inicio = datetime.strptime(r["data_inicio"], "%d/%m/%Y")
                    r_fim = datetime.strptime(r["data_fim"], "%d/%m/%Y")
                    if r_inicio <= atual <= r_fim:
                        user_reserva = next((u for u in usuarios if str(u.get("id")) == str(r.get("usuario_id"))), None)
                        if user_reserva and user_reserva.get("equipe_id") == equipe_id:
                            ferias_no_dia += 1
                if ferias_no_dia >= total_membros:
                    return False, f"No dia {data_str} (plantão Equipe {equipe_id}) todos os membros estão de férias."
            atual += timedelta(days=1)
        return True, ""
    except Exception as e:
        return False, f"Erro: {e}"

# ============ AUTENTICAÇÃO ============
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        login = request.form.get('login')
        senha = request.form.get('senha')
        if not login or not senha:
            return render_template('login.html', erro="Preencha todos os campos.")
        try:
            # Admin
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
            # Usuário comum
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
            return render_template('login.html', erro=f"Erro: {e}")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session or not session.get('is_admin'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ============ PÁGINAS PRINCIPAIS ============
@app.route('/')
@login_required
def index():
    return render_template('index.html', usuario=session.get('nome'), is_admin=session.get('is_admin'))

@app.route('/admin')
@admin_required
def admin_panel():
    return render_template('admin.html', equipe=session.get('equipe_id'))

# ============ API TESTE ============
@app.route('/test-sheet')
def test_sheet():
    try:
        ws = get_worksheet("Equipes")
        return jsonify({"status": "ok", "rows": len(ws.get_all_records())})
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500

# ============ API FERIADOS ============
@app.route('/api/feriados')
def api_feriados():
    return jsonify(ler_config()['feriados'])

# ============ API CALENDÁRIO ============
@app.route('/api/calendario')
@login_required
def api_calendario():
    try:
        ano = int(request.args.get('ano', datetime.now().year))
        mes = int(request.args.get('mes', datetime.now().month))
        config = ler_config()
        feriados = config['feriados']
        equipe_id = session.get('equipe_id')

        primeiro_dia = datetime(ano, mes, 1)
        ultimo_dia = datetime(ano, mes, 1) + timedelta(days=31)
        ultimo_dia = ultimo_dia.replace(day=1) - timedelta(days=1)

        # Buscar todas as reservas da equipe
        ws_ferias = get_worksheet("Ferias")
        ferias = ws_ferias.get_all_records()
        ws_usuarios = get_worksheet("Usuarios")
        usuarios = ws_usuarios.get_all_records()
        membros_equipe = [u['id'] for u in usuarios if u.get('equipe_id') == equipe_id]

        dias = []
        atual = primeiro_dia
        while atual <= ultimo_dia:
            data_str = atual.strftime("%d/%m/%Y")
            plantao = equipe_plantao_para_data(data_str)

            reservas_hoje = []
            for r in ferias:
                r_inicio = datetime.strptime(r["data_inicio"], "%d/%m/%Y")
                r_fim = datetime.strptime(r["data_fim"], "%d/%m/%Y")
                if r_inicio <= atual <= r_fim and int(r['usuario_id']) in membros_equipe:
                    user = next((u for u in usuarios if u['id'] == r['usuario_id']), None)
                    if user:
                        reservas_hoje.append({
                            "usuario": user['nome'],
                            "inicio": r['data_inicio'],
                            "fim": r['data_fim']
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

# ============ API RESERVAS ============
@app.route('/api/reservas', methods=['GET', 'POST', 'DELETE'])
@login_required
def api_reservas():
    usuario_id = session.get('user_id')
    if session.get('is_admin'):
        usuario_id = request.args.get('usuario_id', usuario_id)

    if request.method == 'GET':
        try:
            ws_ferias = get_worksheet("Ferias")
            ferias = ws_ferias.get_all_records()
            minhas = [r for r in ferias if str(r.get('usuario_id')) == str(usuario_id)]
            return jsonify(minhas)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif request.method == 'POST':
        data = request.json
        required = ['usuario_id', 'data_inicio', 'dias_uteis']
        if not all(k in data for k in required):
            return jsonify({"error": "Campos obrigatórios: usuario_id, data_inicio, dias_uteis"}), 400

        user_id = data['usuario_id']
        data_inicio = data['data_inicio']
        dias_uteis = int(data['dias_uteis'])

        # Prioridade
        pode, msg = verificar_prioridade(user_id)
        if not pode:
            return jsonify({"error": msg}), 403

        config = ler_config()
        feriados = config['feriados']
        data_fim = proximo_dia_util(data_inicio, dias_uteis, feriados)

        # Verificar total de dias do usuário
        ws_ferias = get_worksheet("Ferias")
        ferias = ws_ferias.get_all_records()
        total_atual = sum([int(r.get('dias_uteis', 0)) for r in ferias if str(r.get('usuario_id')) == str(user_id)])
        if total_atual + dias_uteis > 25:
            return jsonify({"error": f"Usuário já tem {total_atual} dias; limite 25."}), 400

        # Obter equipe do usuário
        ws_usuarios = get_worksheet("Usuarios")
        usuarios = ws_usuarios.get_all_records()
        user = next((u for u in usuarios if str(u['id']) == str(user_id)), None)
        if not user:
            return jsonify({"error": "Usuário não encontrado."}), 404
        equipe_id = user['equipe_id']

        # Conflito de plantão
        pode, msg = verificar_conflito_plantao(equipe_id, data_inicio, data_fim)
        if not pode:
            return jsonify({"error": msg}), 409

        # Inserir
        ids = [int(r.get('id', 0)) for r in ferias]
        novo_id = max(ids) + 1 if ids else 1
        ws_ferias.append_row([
            novo_id, user_id, data_inicio, data_fim, dias_uteis, "aprovado"
        ])
        return jsonify({"success": True, "id": novo_id})

    elif request.method == 'DELETE':
        reserva_id = request.args.get('id')
        if not reserva_id:
            return jsonify({"error": "ID obrigatório."}), 400
        try:
            ws_ferias = get_worksheet("Ferias")
            ferias = ws_ferias.get_all_records()
            idx = None
            for i, r in enumerate(ferias, start=2):
                if str(r.get('id')) == str(reserva_id):
                    idx = i
                    break
            if idx is None:
                return jsonify({"error": "Reserva não encontrada."}), 404
            if not session.get('is_admin') and str(ferias[idx-2].get('usuario_id')) != str(session.get('user_id')):
                return jsonify({"error": "Permissão negada."}), 403
            ws_ferias.delete_rows(idx)
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

# ============ API ADMIN ============
@app.route('/api/admin/usuarios', methods=['GET', 'POST', 'PUT'])
@admin_required
def admin_usuarios():
    equipe_id = session.get('equipe_id')
    if request.method == 'GET':
        try:
            ws_usuarios = get_worksheet("Usuarios")
            usuarios = ws_usuarios.get_all_records()
            da_equipe = [u for u in usuarios if u.get('equipe_id') == equipe_id]
            for u in da_equipe:
                u.pop('senha_hash', None)
            return jsonify(da_equipe)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif request.method == 'POST':
        data = request.json
        required = ['nome', 'nivel', 'login', 'senha']
        if not all(k in data for k in required):
            return jsonify({"error": "Campos: nome, nivel, login, senha"}), 400
        try:
            ws_usuarios = get_worksheet("Usuarios")
            usuarios = ws_usuarios.get_all_records()
            ids = [int(u.get('id', 0)) for u in usuarios]
            novo_id = max(ids) + 1 if ids else 1
            senha_hash = generate_password_hash(data['senha'])
            ws_usuarios.append_row([
                novo_id, data['nome'], equipe_id, data['nivel'],
                data['login'], senha_hash, False
            ])
            return jsonify({"success": True, "id": novo_id})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif request.method == 'PUT':
        data = request.json
        user_id = data.get('id')
        if not user_id:
            return jsonify({"error": "ID do usuário obrigatório."}), 400
        try:
            ws_usuarios = get_worksheet("Usuarios")
            usuarios = ws_usuarios.get_all_records()
            idx = None
            for i, u in enumerate(usuarios, start=2):
                if str(u.get('id')) == str(user_id):
                    idx = i
                    break
            if idx is None:
                return jsonify({"error": "Usuário não encontrado."}), 404
            # Atualizar campos permitidos
            campos = ['nome', 'nivel', 'login']
            for campo in campos:
                if campo in data:
                    ws_usuarios.update_cell(idx, usuarios[0].keys().index(campo)+1, data[campo])
            if 'senha' in data and data['senha']:
                nova_hash = generate_password_hash(data['senha'])
                ws_usuarios.update_cell(idx, usuarios[0].keys().index('senha_hash')+1, nova_hash)
            return jsonify({"success": True})
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

if __name__ == '__main__':
    app.run(debug=True)