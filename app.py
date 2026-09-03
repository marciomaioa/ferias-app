# -*- coding: utf-8 -*-
import os
import json
import base64
import time
import io
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from werkzeug.security import generate_password_hash, check_password_hash
import holidays
import weasyprint
from weasyprint import HTML, CSS
from weasyprint.text.fonts import FontConfiguration

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")

# =============================================================================
# CONEXÃO GOOGLE SHEETS
# =============================================================================

def obter_credenciais():
    creds_b64 = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_b64:
        raise Exception("GOOGLE_CREDENTIALS não definida.")
    try:
        return json.loads(base64.b64decode(creds_b64).decode("utf-8"))
    except Exception:
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

# =============================================================================
# CACHE
# =============================================================================

cache = {
    "feriados": {"data": None, "timestamp": 0},
    "equipes": {"data": None, "timestamp": 0},
    "usuarios": {"data": None, "timestamp": 0},
    "ferias": {"data": None, "timestamp": 0},
}
CACHE_TTL = 60

def get_cache(key, force_refresh=False):
    now = time.time()
    if force_refresh or cache[key]["data"] is None or (now - cache[key]["timestamp"] > CACHE_TTL):
        try:
            if key == "feriados":
                ws = get_worksheet("Config")
                records = ws.get_all_records()
                feriados = json.loads(records[0].get("feriados", "[]")) if records else []
                cache["feriados"]["data"] = feriados
                cache["feriados"]["timestamp"] = now
            else:
                ws = get_worksheet(key.capitalize())
                dados = ws.get_all_records()
                cache[key]["data"] = dados
                cache[key]["timestamp"] = now
        except Exception as e:
            print(f"Erro ao carregar cache para {key}: {e}")
            if cache[key]["data"] is None:
                raise
    return cache[key]["data"]

def invalidate_cache(key=None):
    if key:
        cache[key]["data"] = None
        cache[key]["timestamp"] = 0
    else:
        for k in cache:
            cache[k]["data"] = None
            cache[k]["timestamp"] = 0

# =============================================================================
# FUNÇÕES AUXILIARES
# =============================================================================

def get_feriados_do_ano(ano):
    return [d.strftime("%d/%m/%Y") for d in holidays.Brazil(years=ano).keys()]

def ler_config():
    try:
        ws = get_worksheet("Config")
        records = ws.get_all_records()
        if not records:
            return {"ano": 2027, "feriados": []}
        linha = records[0]
        return {"ano": int(linha.get("ano", 2027)), "feriados": json.loads(linha.get("feriados", "[]"))}
    except Exception:
        return {"ano": 2027, "feriados": []}

def atualizar_config(ano, feriados):
    ws = get_worksheet("Config")
    ws.clear()
    ws.update(range_name="A1", values=[["ano", "feriados"]])
    ws.append_row([ano, json.dumps(feriados)])
    invalidate_cache("feriados")

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
    dias_contados = 1 if (data.weekday() < 5 and data.strftime("%d/%m/%Y") not in feriados) else 0
    while dias_contados < quantidade:
        data += timedelta(days=1)
        if data.weekday() < 5 and data.strftime("%d/%m/%Y") not in feriados:
            dias_contados += 1
    return data.strftime("%d/%m/%Y")

def equipe_plantao_para_data(data_str):
    dt = datetime.strptime(data_str, "%d/%m/%Y")
    dia_ano = dt.timetuple().tm_yday
    ordem = [2, 3, 4, 1]
    return ordem[(dia_ano - 1) % 4]

def verificar_prioridade(usuario_id):
    try:
        usuarios = get_cache("usuarios")
        usuario = next((u for u in usuarios if str(u.get("id")) == str(usuario_id)), None)
        if not usuario:
            return False, "Usuário não encontrado."
        equipe_id = usuario["equipe_id"]
        nivel = usuario["nivel"]
        inferiores = [u for u in usuarios if u.get("equipe_id") == equipe_id and u.get("nivel", 999) < nivel]
        if not inferiores:
            return True, ""
        ferias = get_cache("ferias")
        for inf in inferiores:
            total = sum([int(r.get("dias_uteis", 0)) for r in ferias if str(r.get("usuario_id")) == str(inf.get("id"))])
            if total < 25:
                return False, f"Usuário {inf.get('nome')} (nível {inf.get('nivel')}) ainda tem {total} dias; precisa de 25."
        return True, ""
    except Exception as e:
        return False, f"Erro: {e}"

def verificar_conflito_plantao(equipe_id, data_inicio_str, data_fim_str, usuario_id, reserva_id=None):
    try:
        usuarios = get_cache("usuarios")
        membros = [u for u in usuarios if u.get("equipe_id") == equipe_id]
        ferias = get_cache("ferias")
        if reserva_id:
            ferias = [r for r in ferias if str(r.get("id")) != str(reserva_id)]

        inicio = datetime.strptime(data_inicio_str, "%d/%m/%Y")
        fim = datetime.strptime(data_fim_str, "%d/%m/%Y")
        atual = inicio
        while atual <= fim:
            data_str = atual.strftime("%d/%m/%Y")
            if equipe_plantao_para_data(data_str) == equipe_id:
                ferias_no_dia = 0
                for r in ferias:
                    r_inicio = datetime.strptime(r["data_inicio"], "%d/%m/%Y")
                    r_fim = datetime.strptime(r["data_fim"], "%d/%m/%Y")
                    if r_inicio <= atual <= r_fim:
                        user_reserva = next((u for u in usuarios if str(u.get("id")) == str(r.get("usuario_id"))), None)
                        if user_reserva and user_reserva.get("equipe_id") == equipe_id:
                            ferias_no_dia += 1
                if atual >= inicio and atual <= fim:
                    ferias_no_dia += 1
                if ferias_no_dia > 1:
                    return False, f"No dia {data_str} (plantão Equipe {equipe_id}) {ferias_no_dia} membros estão de férias. Só é permitido 1."
            atual += timedelta(days=1)
        return True, ""
    except Exception as e:
        return False, f"Erro: {e}"

# =============================================================================
# DECORADORES
# =============================================================================

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

def global_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session or not session.get('global_admin'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# =============================================================================
# ROTAS DE AUTENTICAÇÃO
# =============================================================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        login = request.form.get('login')
        senha = request.form.get('senha')
        if not login or not senha:
            return render_template('login.html', erro="Preencha todos os campos.")
        try:
            # Verifica se é admin de equipe (aba Equipes)
            ws_equipes = get_worksheet("Equipes")
            equipes = ws_equipes.get_all_records()
            admin = next((eq for eq in equipes if eq.get("login_admin") == login), None)
            if admin and check_password_hash(admin.get("senha_admin"), senha):
                session['user_id'] = f"admin_{admin['id']}"
                session['is_admin'] = True
                session['global_admin'] = False
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
                session['is_admin'] = user.get('admin') == 'TRUE' or user.get('admin') == True
                session['global_admin'] = user.get('global_admin') == 'TRUE' or user.get('global_admin') == True
                session['equipe_id'] = user.get('equipe_id', 0)
                session['nome'] = user.get('nome', '')
                session['login'] = login
                session['nivel'] = user.get('nivel', 0)
                # Se for global_admin, redireciona para o painel admin com todas as equipes
                if session['global_admin']:
                    return redirect(url_for('admin_panel'))
                # Se for admin de equipe (mas não global), redireciona para admin
                if session['is_admin']:
                    return redirect(url_for('admin_panel'))
                # Usuário comum
                return redirect(url_for('index'))

            return render_template('login.html', erro="Login ou senha inválidos.")
        except Exception as e:
            return render_template('login.html', erro=f"Erro: {e}")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# =============================================================================
# PÁGINAS PRINCIPAIS
# =============================================================================

@app.route('/')
@login_required
def index():
    # Se for admin, redireciona para o painel
    if session.get('is_admin'):
        return redirect(url_for('admin_panel'))
    return render_template('index.html', usuario=session.get('nome'), is_admin=session.get('is_admin'))

@app.route('/admin')
@admin_required
def admin_panel():
    return render_template('admin.html', 
                           equipe=session.get('equipe_id'), 
                           global_admin=session.get('global_admin', False),
                           nome=session.get('nome'))

# =============================================================================
# ROTA PDF
# =============================================================================

@app.route('/api/admin/relatorio-pdf')
@admin_required
def relatorio_pdf():
    equipe_id = session.get('equipe_id')
    global_admin = session.get('global_admin', False)
    
    # Se for global_admin, pode escolher a equipe via query param, senão usa a própria
    if global_admin:
        equipe_id_param = request.args.get('equipe_id')
        if equipe_id_param:
            equipe_id = int(equipe_id_param)
        # Se não especificou, usa a primeira equipe da lista
        if not equipe_id:
            equipes = get_cache("equipes")
            if equipes:
                equipe_id = equipes[0]['id']

    if not equipe_id:
        return jsonify({"error": "Equipe não especificada."}), 400

    ws_equipes = get_worksheet("Equipes")
    equipes = ws_equipes.get_all_records()
    nome_equipe = next((e['nome'] for e in equipes if e['id'] == equipe_id), f"Equipe {equipe_id}")
    data_geracao = datetime.now().strftime("%d/%m/%Y %H:%M")

    usuarios = get_cache("usuarios")
    ferias = get_cache("ferias")
    membros = [u for u in usuarios if u.get('equipe_id') == equipe_id]

    dados_membros = []
    for membro in membros:
        reservas = [r for r in ferias if str(r.get('usuario_id')) == str(membro['id'])]
        reservas.sort(key=lambda x: datetime.strptime(x['data_inicio'], "%d/%m/%Y"))
        total_dias = sum([int(r.get('dias_uteis', 0)) for r in reservas])
        dados_membros.append({
            "nome": membro['nome'],
            "nivel": membro['nivel'],
            "reservas": reservas,
            "total_dias": total_dias
        })
    dados_membros.sort(key=lambda x: x['nivel'])

    def gerar_bloco_membro(m):
        if not m['reservas']:
            return f"""
            <div class="membro">
                <div class="membro-nome">
                    {m['nome']}
                    <span class="nivel">Nível {m['nivel']}</span>
                </div>
                <div class="sem-reservas">Nenhuma reserva de férias registrada.</div>
            </div>
            """
        tabela = """
            <table>
                <thead>
                    <tr><th>Período</th><th>Dias úteis</th><th>Status</th></tr>
                </thead>
                <tbody>
        """
        for r in m['reservas']:
            tabela += f"""
                <tr>
                    <td>{r['data_inicio']} a {r['data_fim']}</td>
                    <td>{r['dias_uteis']}</td>
                    <td>{r['status']}</td>
                </tr>
            """
        tabela += """
                </tbody>
            </table>
        """
        return f"""
        <div class="membro">
            <div class="membro-nome">
                {m['nome']}
                <span class="nivel">Nível {m['nivel']} – Total: {m['total_dias']}/25 dias</span>
            </div>
            {tabela}
            <div class="total-dias">Total de dias: {m['total_dias']}</div>
        </div>
        """

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            @page {{
                size: A4 portrait;
                margin: 2cm 1.5cm 2cm 1.5cm;
                @bottom-center {{
                    content: "Página " counter(page) " de " counter(pages);
                    font-size: 9pt;
                    color: #666;
                }}
            }}
            body {{ font-family: 'Helvetica', 'Arial', sans-serif; font-size: 11pt; line-height: 1.5; color: #222; }}
            .header {{ text-align: center; border-bottom: 3px solid #003366; padding-bottom: 15px; margin-bottom: 20px; }}
            .header h1 {{ font-size: 22pt; margin: 0; color: #003366; letter-spacing: 2px; }}
            .header h2 {{ font-size: 14pt; margin: 5px 0 0 0; font-weight: normal; color: #555; }}
            .header .sub {{ font-size: 12pt; margin-top: 5px; color: #666; }}
            .info-equipe {{ display: flex; justify-content: space-between; background: #f5f7fa; padding: 10px 15px; border-radius: 5px; margin-bottom: 25px; font-size: 10pt; }}
            .info-equipe span {{ font-weight: bold; }}
            .membro {{ margin-bottom: 25px; page-break-inside: avoid; }}
            .membro-nome {{ font-size: 13pt; font-weight: bold; color: #003366; border-bottom: 1px solid #ccc; padding-bottom: 3px; margin-bottom: 8px; display: flex; justify-content: space-between; }}
            .membro-nome .nivel {{ font-weight: normal; font-size: 10pt; color: #666; }}
            table {{ width: 100%; border-collapse: collapse; font-size: 10pt; }}
            table th {{ background-color: #e9ecef; text-align: left; padding: 6px 8px; border-bottom: 2px solid #003366; }}
            table td {{ padding: 5px 8px; border-bottom: 1px solid #ddd; }}
            table tr:last-child td {{ border-bottom: none; }}
            .total-dias {{ text-align: right; font-weight: bold; margin-top: 5px; font-size: 10pt; }}
            .footer {{ margin-top: 30px; border-top: 1px solid #ccc; padding-top: 15px; font-size: 9pt; color: #666; text-align: center; }}
            .assinatura {{ margin-top: 30px; display: flex; justify-content: space-between; font-size: 10pt; }}
            .assinatura .campo {{ text-align: center; }}
            .assinatura .campo .label {{ font-size: 9pt; color: #555; }}
            .sem-reservas {{ color: #999; font-style: italic; padding: 5px 0; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>POLÍCIA PENAL DE MINAS GERAIS</h1>
            <h2>Relatório de Férias – {nome_equipe}</h2>
            <div class="sub">Gerado em: {data_geracao}</div>
        </div>
        <div class="info-equipe">
            <div><span>Equipe:</span> {nome_equipe}</div>
            <div><span>Total de membros:</span> {len(membros)}</div>
            <div><span>Período de referência:</span> {datetime.now().year}</div>
        </div>
        {''.join([gerar_bloco_membro(m) for m in dados_membros])}
        <div class="assinatura">
            <div class="campo"><div>___________________________</div><div class="label">Assinatura do Líder da Equipe</div></div>
            <div class="campo"><div>___________________________</div><div class="label">Assinatura do Diretor</div></div>
        </div>
        <div class="footer">Relatório gerado automaticamente pelo Sistema de Gestão de Férias - {data_geracao}</div>
    </body>
    </html>
    """

    try:
        font_config = FontConfiguration()
        pdf = HTML(string=html_content).write_pdf(
            font_config=font_config,
            stylesheets=[CSS(string='@page { size: A4; margin: 2cm; }')]
        )
        return send_file(
            io.BytesIO(pdf),
            download_name=f'ferias_{nome_equipe}_{datetime.now().strftime("%Y%m%d_%H%M")}.pdf',
            as_attachment=True,
            mimetype='application/pdf'
        )
    except Exception as e:
        return jsonify({"error": f"Erro ao gerar PDF: {str(e)}"}), 500

# =============================================================================
# API ADMIN – GERENCIAR ADMINISTRADORES DE EQUIPE (apenas global_admin)
# =============================================================================

@app.route('/api/admin/equipes', methods=['GET', 'PUT', 'DELETE'])
@global_admin_required
def admin_equipes():
    if request.method == 'GET':
        try:
            ws_equipes = get_worksheet("Equipes")
            equipes = ws_equipes.get_all_records()
            # Remove a senha_hash para não expor
            for eq in equipes:
                eq.pop('senha_admin', None)
            return jsonify(equipes)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif request.method == 'PUT':
        data = request.json
        equipe_id = data.get('id')
        if not equipe_id:
            return jsonify({"error": "ID da equipe obrigatório."}), 400

        try:
            ws_equipes = get_worksheet("Equipes")
            equipes = ws_equipes.get_all_records()
            idx = None
            for i, eq in enumerate(equipes, start=2):
                if str(eq.get('id')) == str(equipe_id):
                    idx = i
                    break
            if idx is None:
                return jsonify({"error": "Equipe não encontrada."}), 404

            header = list(equipes[0].keys())
            # Atualiza login_admin se enviado
            if 'login_admin' in data:
                coluna = header.index('login_admin') + 1
                ws_equipes.update_cell(idx, coluna, data['login_admin'])
            # Atualiza senha_admin se enviada
            if 'senha_admin' in data and data['senha_admin']:
                nova_hash = generate_password_hash(data['senha_admin'])
                coluna = header.index('senha_admin') + 1
                ws_equipes.update_cell(idx, coluna, nova_hash)
            # Atualiza nome e cor se enviados
            if 'nome' in data:
                coluna = header.index('nome') + 1
                ws_equipes.update_cell(idx, coluna, data['nome'])
            if 'cor' in data:
                coluna = header.index('cor') + 1
                ws_equipes.update_cell(idx, coluna, data['cor'])

            invalidate_cache("equipes")
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif request.method == 'DELETE':
        equipe_id = request.args.get('id')
        if not equipe_id:
            return jsonify({"error": "ID da equipe obrigatório."}), 400
        try:
            ws_equipes = get_worksheet("Equipes")
            equipes = ws_equipes.get_all_records()
            idx = None
            for i, eq in enumerate(equipes, start=2):
                if str(eq.get('id')) == str(equipe_id):
                    idx = i
                    break
            if idx is None:
                return jsonify({"error": "Equipe não encontrada."}), 404

            # Remove a linha
            ws_equipes.delete_rows(idx)
            invalidate_cache("equipes")
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

# =============================================================================
# API TESTE, FERIADOS, CALENDÁRIO, RESERVAS, USUÁRIOS
# =============================================================================

@app.route('/test-sheet')
def test_sheet():
    try:
        ws = get_worksheet("Equipes")
        return jsonify({"status": "ok", "rows": len(ws.get_all_records())})
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500

@app.route('/api/feriados')
def api_feriados():
    config = ler_config()
    return jsonify(config['feriados'])

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

        ferias = get_cache("ferias")
        usuarios = get_cache("usuarios")
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

@app.route('/api/reservas', methods=['GET', 'POST', 'DELETE'])
@login_required
def api_reservas():
    usuario_id = session.get('user_id')
    if session.get('is_admin') or session.get('global_admin'):
        usuario_id = request.args.get('usuario_id', usuario_id)

    if request.method == 'GET':
        try:
            ferias = get_cache("ferias")
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

        pode, msg = verificar_prioridade(user_id)
        if not pode:
            return jsonify({"error": msg}), 403

        config = ler_config()
        feriados = config['feriados']
        data_fim = proximo_dia_util(data_inicio, dias_uteis, feriados)

        ferias = get_cache("ferias")
        total_atual = sum([int(r.get('dias_uteis', 0)) for r in ferias if str(r.get('usuario_id')) == str(user_id)])
        if total_atual + dias_uteis > 25:
            return jsonify({"error": f"Usuário já tem {total_atual} dias; limite 25."}), 400

        usuarios = get_cache("usuarios")
        user = next((u for u in usuarios if str(u['id']) == str(user_id)), None)
        if not user:
            return jsonify({"error": "Usuário não encontrado."}), 404
        equipe_id = user['equipe_id']

        pode, msg = verificar_conflito_plantao(equipe_id, data_inicio, data_fim, user_id)
        if not pode:
            return jsonify({"error": msg}), 409

        ws_ferias = get_worksheet("Ferias")
        ids = [int(r.get('id', 0)) for r in ferias]
        novo_id = max(ids) + 1 if ids else 1
        ws_ferias.append_row([
            novo_id, user_id, data_inicio, data_fim, dias_uteis, "aprovado"
        ])
        invalidate_cache("ferias")
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
            if not session.get('is_admin') and not session.get('global_admin'):
                if str(ferias[idx-2].get('usuario_id')) != str(session.get('user_id')):
                    return jsonify({"error": "Permissão negada."}), 403
            ws_ferias.delete_rows(idx)
            invalidate_cache("ferias")
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

# =============================================================================
# API ADMIN – USUÁRIOS (com suporte a global_admin)
# =============================================================================

@app.route('/api/admin/usuarios', methods=['GET', 'POST', 'PUT', 'DELETE'])
@admin_required
def admin_usuarios():
    equipe_id = session.get('equipe_id')
    global_admin = session.get('global_admin', False)

    if request.method == 'GET':
        try:
            usuarios = get_cache("usuarios")
            if global_admin:
                # Retorna todos os usuários
                da_equipe = usuarios
            else:
                # Apenas da equipe do admin
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
            # Se global_admin, pode escolher a equipe; senão, usa a própria
            equipe_destino = data.get('equipe_id') if global_admin else equipe_id
            if not equipe_destino:
                return jsonify({"error": "Equipe não informada."}), 400
            ws_usuarios.append_row([
                novo_id, data['nome'], equipe_destino, data['nivel'],
                data['login'], senha_hash, False, False
            ])
            invalidate_cache("usuarios")
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
            # Se não for global_admin, só pode editar usuários da própria equipe
            if not global_admin:
                usuario_alvo = usuarios[idx-2]
                if usuario_alvo.get('equipe_id') != equipe_id:
                    return jsonify({"error": "Permissão negada."}), 403

            campos = ['nome', 'nivel', 'login', 'equipe_id']
            header = list(usuarios[0].keys())
            for campo in campos:
                if campo in data:
                    coluna = header.index(campo) + 1
                    ws_usuarios.update_cell(idx, coluna, data[campo])
            if 'senha' in data and data['senha']:
                nova_hash = generate_password_hash(data['senha'])
                coluna_hash = header.index('senha_hash') + 1
                ws_usuarios.update_cell(idx, coluna_hash, nova_hash)
            invalidate_cache("usuarios")
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif request.method == 'DELETE':
        user_id = request.args.get('id')
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
            # Verifica permissão
            if not global_admin:
                usuario_alvo = usuarios[idx-2]
                if usuario_alvo.get('equipe_id') != equipe_id:
                    return jsonify({"error": "Permissão negada."}), 403
            # Não pode excluir a si mesmo
            if str(user_id) == str(session.get('user_id')):
                return jsonify({"error": "Não é possível excluir o próprio usuário."}), 400
            # Remove linha
            ws_usuarios.delete_rows(idx)
            invalidate_cache("usuarios")
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

# =============================================================================
# API ADMIN – CONFIGURAÇÃO
# =============================================================================

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
            invalidate_cache("feriados")
            return jsonify({"success": True, "ano": novo_ano, "feriados": feriados})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

# =============================================================================
# PÁGINAS DE ERRO
# =============================================================================

@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(e):
    return render_template('500.html'), 500

# =============================================================================
# EXECUÇÃO
# =============================================================================

if __name__ == '__main__':
    app.run(debug=True)