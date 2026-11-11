import os
import json
import secrets
import sqlite3
from pathlib import Path
from functools import wraps
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from math import radians, sin, cos, asin, sqrt
from flask import Flask, render_template, request, redirect, url_for, session, g, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash

ROOT = Path(__file__).resolve().parent
app = Flask(__name__)
(ROOT / 'instance').mkdir(exist_ok=True)
key_file = ROOT / 'instance' / 'secret.key'
if not key_file.exists():
    try:
        with key_file.open('x') as f:
            f.write(secrets.token_hex(32))
        key_file.chmod(0o600)
    except FileExistsError:
        pass
app.config.update(SECRET_KEY=os.environ.get('SECRET_KEY') or key_file.read_text(),
                  DATABASE=str(ROOT / 'instance' / 'rede.db'), SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE='Lax', MAX_CONTENT_LENGTH=1024 * 1024)
# Configure as unidades autorizadas no arquivo senacs.json. Endereços e coordenadas precisam ser confirmados.
SENACS_FILE = ROOT / 'senacs.json'
if not SENACS_FILE.exists():
    SENACS_FILE.write_text(json.dumps([{'nome':'Senac RN - Alecrim','endereco':'Endereço a confirmar com a unidade','latitude':None,'longitude':None}, {'nome':'Senac RN - Zona Norte','endereco':'Endereço a confirmar com a unidade','latitude':None,'longitude':None}, {'nome':'Senac RN - Centro','endereco':'Endereço a confirmar com a unidade','latitude':None,'longitude':None}], ensure_ascii=False,indent=2), encoding='utf8')
UNIDADES = json.loads(SENACS_FILE.read_text(encoding='utf8'))
if not UNIDADES:
    UNIDADES = [{'nome':'Senac RN - Alecrim','estado':'RN','endereco':'Endereço pendente de confirmação','latitude':None,'longitude':None}, {'nome':'Senac RN - Zona Norte','estado':'RN','endereco':'Endereço pendente de confirmação','latitude':None,'longitude':None}, {'nome':'Senac RN - Centro','estado':'RN','endereco':'Endereço pendente de confirmação','latitude':None,'longitude':None}]
    SENACS_FILE.write_text(json.dumps(UNIDADES,ensure_ascii=False,indent=2),encoding='utf8')
for unidade in UNIDADES:
    unidade.setdefault('estado', 'RN' if ' RN ' in unidade['nome'] else '')
ESTADOS = ['AC','AL','AP','AM','BA','CE','DF','ES','GO','MA','MT','MS','MG','PA','PB','PR','PE','PI','RJ','RN','RS','RO','RR','SC','SP','SE','TO']
PONTOS = [x['nome'] for x in UNIDADES]
ALIMENTOS = ['Arroz', 'Feijão', 'Óleo', 'Açúcar', 'Macarrão', 'Café', 'Leite', 'Farinha', 'Sal', 'Biscoito', 'Milho', 'Ervilha', 'Molho', 'Aveia', 'Fubá', 'Sardinha', 'Atum', 'Achocolatado', 'Macarrão instantâneo', 'Canjica']
HIGIENE = ['Sabonete', 'Pasta de dente', 'Papel higiênico', 'Shampoo', 'Detergente']
ROUPAS = ['Camisa', 'Calça', 'Sapato']
PRODUTOS = ALIMENTOS + HIGIENE
TIPOS = {'Infantil': (20, ['Leite', 'Achocolatado', 'Biscoito']), 'Emergencial': (18, ['Arroz', 'Feijão', 'Óleo']), 'Nutricional': (15, ['Arroz', 'Feijão', 'Aveia', 'Leite'])}
ICONES = {'Arroz':'🍚','Feijão':'🫘','Óleo':'🫙','Açúcar':'🥣','Macarrão':'🍝','Café':'☕','Leite':'🥛','Farinha':'🌾','Sal':'🧂','Biscoito':'🍪','Milho':'🌽','Ervilha':'🫛','Molho':'🥫','Aveia':'🌾','Fubá':'🌽','Sardinha':'🐟','Atum':'🐟','Achocolatado':'🍫','Macarrão instantâneo':'🍜','Canjica':'🥣','Sabonete':'🧼','Pasta de dente':'🪥','Papel higiênico':'🧻','Shampoo':'🧴','Detergente':'🫧','Camisa':'👕','Calça':'👖','Sapato':'👟'}

def db():
    if 'db' not in g:
        g.db = sqlite3.connect(app.config['DATABASE'], timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys=ON')
    return g.db

@app.teardown_appcontext
def close_db(error):
    connection = g.pop('db', None)
    if connection is not None:
        connection.close()

def init_db():
    c = db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS usuarios(id INTEGER PRIMARY KEY, nome TEXT NOT NULL, cpf TEXT UNIQUE NOT NULL, telefone TEXT NOT NULL, senha TEXT NOT NULL, moradores INTEGER NOT NULL, criancas INTEGER NOT NULL, renda INTEGER NOT NULL, perfil TEXT NOT NULL, higiene INTEGER NOT NULL, codigo TEXT UNIQUE NOT NULL, ponto TEXT NOT NULL, admin INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS estoque(ponto TEXT NOT NULL, produto TEXT NOT NULL, quantidade INTEGER NOT NULL CHECK(quantidade>=0), PRIMARY KEY(ponto,produto));
    CREATE TABLE IF NOT EXISTS movimentos(id INTEGER PRIMARY KEY, usuario INTEGER NOT NULL REFERENCES usuarios(id), tipo TEXT NOT NULL, ponto TEXT NOT NULL, itens TEXT NOT NULL DEFAULT '{}', valor INTEGER NOT NULL DEFAULT 0, codigo TEXT UNIQUE NOT NULL, criado TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE UNIQUE INDEX IF NOT EXISTS cesta_unica ON movimentos(usuario) WHERE tipo='Cesta';
    ''')
    c.executescript('''
    CREATE TABLE IF NOT EXISTS entregas(movimento_id INTEGER PRIMARY KEY REFERENCES movimentos(id), prazo TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Pendente', confirmado_em TEXT, confirmado_por INTEGER REFERENCES usuarios(id));
    CREATE TABLE IF NOT EXISTS administradores_pontos(usuario_id INTEGER PRIMARY KEY REFERENCES usuarios(id), ponto TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS pix_cobrancas(movimento_id INTEGER PRIMARY KEY REFERENCES movimentos(id), expira_em TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Aguardando pagamento');
    ''')
    c.execute('''CREATE TABLE IF NOT EXISTS unidades_admin (
        ponto TEXT PRIMARY KEY, cep TEXT UNIQUE NOT NULL,
        usuario_id INTEGER UNIQUE NOT NULL REFERENCES usuarios(id),
        senha_hash TEXT NOT NULL, trocar_senha INTEGER NOT NULL DEFAULT 1
    )''')
    colunas = {r['name'] for r in c.execute('PRAGMA table_info(usuarios)')}
    for coluna in ('cep','logradouro','numero','complemento','bairro','cidade','estado'):
        if coluna not in colunas:
            c.execute(f"ALTER TABLE usuarios ADD COLUMN {coluna} TEXT NOT NULL DEFAULT ''")
    quantidades = [(20,15,10),(10,8,5),(12,10,7),(18,12,9),(14,11,6)]
    for i, ponto in enumerate(PONTOS):
        for produto in PRODUTOS + ROUPAS:
            quantidade = 0
            c.execute('INSERT OR IGNORE INTO estoque VALUES(?,?,?)', (ponto,produto,quantidade))
    # Acessos iniciais fixos por CEP; aplicados uma única vez também em bancos antigos.
    acessos = {
        '59030350': 'Alecrim@2026',
        '59110185': 'ZonaNorte@2026',
        '59025030': 'Centro@2026',
    }
    marco = ROOT / 'instance' / '.admins_fixos_v1'
    for unidade in UNIDADES:
        cep = ''.join(ch for ch in str(unidade.get('cep', '')) if ch.isdigit())
        if cep not in acessos:
            continue
        ponto = unidade['nome']
        atual = c.execute('SELECT usuario_id, trocar_senha FROM unidades_admin WHERE ponto=? OR cep=?', (ponto, cep)).fetchone()
        if atual and marco.exists():
            continue
        if atual:
            usuario_id = atual['usuario_id']
            c.execute('UPDATE usuarios SET admin=1 WHERE id=?', (usuario_id,))
            c.execute('UPDATE unidades_admin SET ponto=?, cep=?, senha_hash=?, trocar_senha=1 WHERE usuario_id=?',
                      (ponto, cep, generate_password_hash(acessos[cep]), usuario_id))
        else:
            # Identificador interno reservado, não corresponde a CPF de pessoa real.
            cpf_interno = 'ADMIN' + cep
            usuario = c.execute('SELECT id FROM usuarios WHERE cpf=?', (cpf_interno,)).fetchone()
            if usuario:
                usuario_id = usuario['id']
            else:
                cur = c.execute("""INSERT INTO usuarios
                    (nome,cpf,telefone,senha,moradores,criancas,renda,perfil,higiene,codigo,ponto,admin)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,1)""",
                    ('Administrador ' + ponto, cpf_interno, '', generate_password_hash(secrets.token_urlsafe(24)),
                     0,0,0,'Administrador',0,'ADM-' + cep,ponto))
                usuario_id = cur.lastrowid
            c.execute('INSERT INTO unidades_admin(ponto,cep,usuario_id,senha_hash,trocar_senha) VALUES(?,?,?,?,1)',
                      (ponto,cep,usuario_id,generate_password_hash(acessos[cep])))
        c.execute('INSERT OR REPLACE INTO administradores_pontos(usuario_id,ponto) VALUES(?,?)', (usuario_id,ponto))
    c.commit()
    if not marco.exists():
        marco.write_text('Credenciais iniciais configuradas. Alterações futuras de senha são preservadas.\n', encoding='utf-8')

def codigo(prefix):
    return prefix + '-' + secrets.token_hex(5).upper()

def dinheiro(value):
    try:
        n = Decimal(str(value).replace(',', '.'))
        if not n.is_finite() or n < 0 or n > 100000000 or n.as_tuple().exponent < -2:
            raise ValueError
        return int(n * 100)
    except (InvalidOperation, ValueError):
        raise ValueError('Informe um valor válido, com até duas casas decimais.')

@app.template_filter('brl')
def brl(value):
    return ('R$ ' + f'{value / 100:,.2f}').replace(',', 'X').replace('.', ',').replace('X', '.')

@app.before_request
def prepare():
    g.user = db().execute('SELECT * FROM usuarios WHERE id=?', (session.get('uid'),)).fetchone()
    if 'csrf' not in session:
        session['csrf'] = secrets.token_hex(32)
    if request.method == 'POST' and not secrets.compare_digest(session['csrf'], request.form.get('csrf','')):
        abort(400, 'Sua sessão expirou. Recarregue a página e tente novamente.')

def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not g.user:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapped

@app.context_processor
def context():
    return dict(user=g.user, pontos=PONTOS, unidades=UNIDADES, estados=ESTADOS, icones=ICONES, tipos=TIPOS)

@app.route('/')
def home():
    return redirect(url_for('painel' if g.user else 'login'))

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        cpf = ''.join(x for x in request.form.get('cpf','') if x.isdigit())
        u = db().execute('SELECT * FROM usuarios WHERE cpf=?', (cpf,)).fetchone()
        if u and check_password_hash(u['senha'], request.form.get('senha','')):
            session.clear()
            session['uid'] = u['id']
            return redirect(url_for('admin_painel' if u['admin'] else 'painel'))
        flash('CPF ou senha incorretos.', 'error')
    return render_template('auth.html', title='Bem-vindo à Rede Solidariza', cadastro=False)

def cep_limpo(valor):
    return ''.join(c for c in valor if c.isdigit())

@app.route('/login-admin', methods=['GET', 'POST'])
def login_admin():
    if request.method == 'POST':
        cep = cep_limpo(request.form.get('cep', ''))
        senha = request.form.get('senha', '')
        registro = db().execute('SELECT * FROM unidades_admin WHERE cep=?', (cep,)).fetchone()
        if registro and check_password_hash(registro['senha_hash'], senha):
            session.clear()
            session['uid'] = registro['usuario_id']
            session['csrf'] = secrets.token_hex(32)
            if registro['trocar_senha']:
                return redirect(url_for('trocar_senha_admin'))
            return redirect(url_for('admin_painel'))
        flash('CEP ou senha incorretos.', 'error')
    return render_template('login_admin.html', title='Acesso administrativo')

@app.route('/admin/trocar-senha', methods=['GET','POST'])
@login_required
def trocar_senha_admin():
    registro = db().execute('SELECT * FROM unidades_admin WHERE usuario_id=?',(g.user['id'],)).fetchone()
    if not registro: abort(403)
    if request.method == 'POST':
        atual = request.form.get('atual','')
        nova = request.form.get('nova','')
        if not check_password_hash(registro['senha_hash'],atual):
            flash('Senha atual incorreta.', 'error')
        elif len(nova) < 12 or nova == registro['cep']:
            flash('Crie uma senha diferente do CEP com pelo menos 12 caracteres.', 'error')
        else:
            with db():
                db().execute('UPDATE unidades_admin SET senha_hash=?,trocar_senha=0 WHERE usuario_id=?',
                             (generate_password_hash(nova),g.user['id']))
            flash('Senha atualizada.', 'success')
            return redirect(url_for('admin_painel'))
    return render_template('trocar_senha_admin.html',title='Alterar senha administrativa')

@app.route('/configurar-primeiro-admin', methods=['GET', 'POST'])
def configurar_primeiro_admin():
    return redirect(url_for('login_admin'))

@app.route('/admin/vincular', methods=['GET','POST'])
@login_required
def vincular_admin():
    if not g.user['admin']: abort(403)
    # O primeiro administrador é o gestor das vinculações, e somente ele pode criar novos acessos.
    primeiro = db().execute('SELECT usuario_id FROM unidades_admin ORDER BY rowid LIMIT 1').fetchone()
    if not primeiro or primeiro['usuario_id'] != g.user['id']: abort(403)
    if request.method == 'POST':
        cpf = cep_limpo(request.form.get('cpf',''))
        cep = cep_limpo(request.form.get('cep',''))
        ponto = request.form.get('ponto','')
        usuario = db().execute('SELECT id FROM usuarios WHERE cpf=?',(cpf,)).fetchone()
        if not usuario or ponto not in PONTOS or len(cep) != 8:
            flash('Verifique o usuário cadastrado, o CEP e a unidade.', 'error')
        else:
            try:
                with db():
                    db().execute('UPDATE usuarios SET admin=1 WHERE id=?',(usuario['id'],))
                    db().execute('INSERT INTO administradores_pontos(usuario_id,ponto) VALUES(?,?)',(usuario['id'],ponto))
                    db().execute('INSERT INTO unidades_admin(ponto,cep,usuario_id,senha_hash) VALUES(?,?,?,?)',
                                 (ponto,cep,usuario['id'],generate_password_hash(cep)))
                flash('Administrador vinculado. Senha inicial igual ao CEP; alteração obrigatória.', 'success')
            except sqlite3.IntegrityError:
                flash('Conta, unidade ou CEP já vinculados.', 'error')
    vinculados = db().execute('SELECT ponto,cep FROM unidades_admin ORDER BY ponto').fetchall()
    return render_template('vincular_admin.html',title='Vincular administradores',vinculados=vinculados)

@app.route('/admin/unidades', methods=['GET', 'POST'])
@login_required
def administrar_unidades():
    if not g.user['admin']:
        abort(403)
    primeiro = db().execute('SELECT usuario_id FROM unidades_admin ORDER BY rowid LIMIT 1').fetchone()
    gestor = bool(primeiro and primeiro['usuario_id'] == g.user['id'])
    if request.method == 'POST' and not gestor: abort(403)
    if request.method == 'POST':
        nome = request.form.get('nome','').strip()
        estado = request.form.get('estado','').strip().upper()
        endereco = request.form.get('endereco','').strip()
        cep = cep_limpo(request.form.get('cep',''))
        if not (3 <= len(nome) <= 100 and estado in ESTADOS and len(endereco) >= 8 and len(cep) == 8):
            flash('Preencha nome, UF, endereço e CEP real da unidade.', 'error')
        elif nome in PONTOS or any(u.get('cep') == cep for u in UNIDADES):
            flash('Unidade ou CEP já cadastrado.', 'error')
        else:
            unidade = {'nome':nome,'estado':estado,'endereco':endereco,'cep':cep,'latitude':None,'longitude':None}
            UNIDADES.append(unidade)
            PONTOS.append(nome)
            try:
                SENACS_FILE.write_text(json.dumps(UNIDADES, ensure_ascii=False, indent=2),encoding='utf8')
                with db():
                    for produto in PRODUTOS + ROUPAS:
                        db().execute('INSERT OR IGNORE INTO estoque(ponto,produto,quantidade) VALUES(?,?,0)',(nome,produto))
                flash('Unidade cadastrada. Vincule o responsável em Gerenciar administradores.', 'success')
            except Exception:
                UNIDADES.remove(unidade)
                PONTOS.remove(nome)
                flash('Não foi possível salvar a unidade.', 'error')
        return redirect(url_for('administrar_unidades'))
    return render_template('administrar_unidades.html', title='Unidades Senac', gestor=gestor)

@app.route('/cadastro', methods=['GET','POST'])
def cadastro():
    if request.method == 'POST':
        try:
            nome = request.form.get('nome','').strip()
            cpf = ''.join(x for x in request.form.get('cpf','') if x.isdigit())
            telefone = ''.join(x for x in request.form.get('telefone','') if x.isdigit())
            senha = request.form.get('senha','')
            moradores = int(request.form.get('moradores','0'))
            criancas = int(request.form.get('criancas','0'))
            renda = dinheiro(request.form.get('renda',''))
            ponto = request.form.get('ponto')
            cep = ''.join(x for x in request.form.get('cep','') if x.isdigit())
            logradouro = request.form.get('logradouro','').strip()
            numero = request.form.get('numero','').strip()
            complemento = request.form.get('complemento','').strip()
            bairro = request.form.get('bairro','').strip()
            cidade = request.form.get('cidade','').strip()
            estado = request.form.get('estado','').strip().upper()
            if not 2 <= len(nome) <= 100 or len(cpf) != 11 or len(telefone) not in (10,11):
                raise ValueError('Confira nome, CPF (11 dígitos) e telefone com DDD.')
            if not 8 <= len(senha) <= 128:
                raise ValueError('A senha deve ter de 8 a 128 caracteres.')
            if not 1 <= moradores <= 100 or not 0 <= criancas <= moradores:
                raise ValueError('Confira a quantidade de moradores e crianças.')
            if ponto not in PONTOS or request.form.get('consentimento') != 'on':
                raise ValueError('Escolha um ponto e confirme o consentimento.')
            if len(cep) != 8 or not all([logradouro,numero,bairro,cidade]) or len(estado) != 2:
                raise ValueError('Preencha o endereço completo, incluindo CEP e UF.')
            if any(len(x)>120 for x in (logradouro,numero,complemento,bairro,cidade)):
                raise ValueError('Endereço muito longo.')
            perfil = 'Doador' if renda >= 80000 * moradores else 'Beneficiário'
            with db():
                cur = db().execute('INSERT INTO usuarios(nome,cpf,telefone,senha,moradores,criancas,renda,perfil,higiene,codigo,ponto,cep,logradouro,numero,complemento,bairro,cidade,estado) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (nome,cpf,telefone,generate_password_hash(senha),moradores,criancas,renda,perfil,int(renda <= 30000 * moradores),codigo('DOA' if perfil=='Doador' else 'REC'),ponto,cep,logradouro,numero,complemento,bairro,cidade,estado))
            session.clear()
            session['uid'] = cur.lastrowid
            return redirect(url_for('resultado'))
        except (ValueError, sqlite3.IntegrityError) as e:
            flash(str(e) if isinstance(e,ValueError) else 'Não foi possível cadastrar. Confira os dados ou tente entrar.', 'error')
    return render_template('auth.html', title='Vamos começar?', cadastro=True)

@app.route('/resultado')
@login_required
def resultado():
    return render_template('resultado.html', title='Resultado do cadastro')

@app.route('/painel')
@login_required
def painel():
    registros = db().execute('SELECT * FROM movimentos WHERE usuario=? ORDER BY id DESC', (g.user['id'],)).fetchall()
    cesta = next((r for r in registros if r['tipo']=='Cesta'), None)
    return render_template('painel.html', title='Meu painel', registros=registros, cesta=cesta,
                           itens=json.loads(cesta['itens']) if cesta else {}, total=sum(r['valor'] for r in registros))

def estoque_atual(ponto):
    return {r['produto']:r['quantidade'] for r in db().execute('SELECT * FROM estoque WHERE ponto=?',(ponto,))}

def registrar(tipo, ponto, itens, direction, valor=0):
    c = db()
    try:
        c.execute('BEGIN IMMEDIATE')
        for produto, qtd in itens.items():
            cur = c.execute('UPDATE estoque SET quantidade=quantidade+? WHERE ponto=? AND produto=? AND quantidade+?>=0', (direction*qtd,ponto,produto,direction*qtd))
            if cur.rowcount != 1:
                raise ValueError(f'Estoque insuficiente de {produto}. Nenhum item foi registrado.')
        cur = c.execute('INSERT INTO movimentos(usuario,tipo,ponto,itens,valor,codigo) VALUES(?,?,?,?,?,?)', (g.user['id'],tipo,ponto,json.dumps(itens,ensure_ascii=False),valor,codigo('RS')))
        c.commit()
        return cur.lastrowid
    except Exception:
        c.rollback()
        raise

@app.route('/cesta', methods=['GET','POST'])
@login_required
def cesta():
    if g.user['perfil'] != 'Beneficiário':
        abort(403)
    if db().execute("SELECT id FROM movimentos WHERE usuario=? AND tipo='Cesta'",(g.user['id'],)).fetchone():
        flash('Sua cesta já está registrada. Consulte o código no painel.', 'info')
        return redirect(url_for('painel'))
    tipo = request.values.get('tipo','Emergencial')
    if tipo not in TIPOS:
        abort(400)
    limite, obrigatorios = TIPOS[tipo]
    limite += 5 if g.user['criancas'] else 0
    extras = HIGIENE if g.user['higiene'] else []
    if request.method == 'POST':
        try:
            itens = {}
            for i, p in enumerate(PRODUTOS):
                n = int(request.form.get(f'q_{i}','0'))
                if n < 0 or n > limite:
                    raise ValueError('Quantidade fora do limite.')
                if n:
                    itens[p] = n
            for p in obrigatorios:
                if itens.get(p,0) < 1:
                    raise ValueError('Mantenha ao menos uma unidade de cada item obrigatório.')
            if sum(itens.values()) > limite:
                raise ValueError('O limite da cesta foi excedido.')
            for p in extras:
                itens[p] = itens.get(p,0) + 1
            mid = registrar('Cesta',g.user['ponto'],itens,-1)
            return redirect(url_for('comprovante',mid=mid))
        except (ValueError,sqlite3.IntegrityError) as e:
            flash(str(e) if isinstance(e,ValueError) else 'Esta cesta já foi registrada.', 'error')
    return render_template('cesta.html',title='Monte sua cesta',produtos=PRODUTOS, tipo=tipo,limite=limite,obrigatorios=obrigatorios,extras=extras,estoque=estoque_atual(g.user['ponto']))

@app.route('/movimentar/<modo>', methods=['GET','POST'])
@login_required
def movimentar(modo):
    opcoes = {'alimentos':('Doação de alimentos',PRODUTOS,1),'roupas':('Doação de roupas',ROUPAS,1),'pedido-roupas':('Pedido de roupas',ROUPAS,-1),'dinheiro':('Registro de valor',[],0)}
    if modo not in opcoes:
        abort(404)
    title, produtos, direction = opcoes[modo]
    if (direction >= 0 and g.user['perfil'] != 'Doador') or (direction < 0 and g.user['perfil'] != 'Beneficiário'):
        abort(403)
    ponto = request.form.get('ponto',g.user['ponto']) if direction >= 0 else g.user['ponto']
    if request.method == 'POST':
        try:
            if ponto not in PONTOS:
                raise ValueError('Escolha um ponto válido.')
            if direction >= 0 and not check_password_hash(g.user['senha'],request.form.get('senha','')):
                raise ValueError('Senha incorreta. Confirme sua senha para registrar.')
            itens = {}
            for i,p in enumerate(produtos):
                n = int(request.form.get(f'q_{i}','0'))
                if not 0 <= n <= 10000:
                    raise ValueError('Use quantidades de 0 a 10.000.')
                if n:
                    itens[p] = n
            valor = dinheiro(request.form.get('valor','0')) if modo=='dinheiro' else 0
            if not itens and valor <= 0:
                raise ValueError('Adicione itens ou informe um valor positivo.')
            if modo in ('alimentos','roupas'):
                c=db()
                try:
                    c.execute('BEGIN IMMEDIATE')
                    cur=c.execute('INSERT INTO movimentos(usuario,tipo,ponto,itens,valor,codigo) VALUES(?,?,?,?,?,?)',(g.user['id'],title,ponto,json.dumps(itens,ensure_ascii=False),0,codigo('RS')))
                    mid=cur.lastrowid
                    prazo=(datetime.now(timezone.utc)+timedelta(days=7)).isoformat()
                    c.execute('INSERT INTO entregas(movimento_id,prazo) VALUES(?,?)',(mid,prazo))
                    c.commit()
                except Exception:
                    c.rollback(); raise
            elif modo=='dinheiro':
                chave=os.environ.get('PIX_CHAVE','').strip()
                if not chave:
                    raise ValueError('Pix ainda não configurado. Defina PIX_CHAVE e PIX_RECEBEDOR no servidor.')
                recebedor=os.environ.get('PIX_RECEBEDOR','').strip()
                cidade=os.environ.get('PIX_CIDADE','NATAL').strip()
                if not recebedor:
                    raise ValueError('Configure PIX_RECEBEDOR no servidor.')
                payload=gerar_pix(chave,recebedor,cidade,valor)
                c=db()
                with c:
                    cur=c.execute('INSERT INTO movimentos(usuario,tipo,ponto,itens,valor,codigo) VALUES(?,?,?,?,?,?)',(g.user['id'],title,ponto,'{}',valor,codigo('RS')))
                    mid=cur.lastrowid
                    c.execute('INSERT INTO pix_cobrancas(movimento_id,expira_em,payload) VALUES(?,?,?)',(mid,(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),payload))
            else:
                mid = registrar(title,ponto,itens,direction,valor)
            return redirect(url_for('comprovante',mid=mid))
        except ValueError as e:
            flash(str(e),'error')
    return render_template('movimentar.html',title=title,modo=modo,produtos=produtos,direction=direction,estoque=estoque_atual(g.user['ponto']))

@app.route('/comprovante/<int:mid>')
@login_required
def comprovante(mid):
    r = db().execute('SELECT * FROM movimentos WHERE id=? AND usuario=?',(mid,g.user['id'])).fetchone()
    if not r:
        abort(404)
    entrega=db().execute('SELECT * FROM entregas WHERE movimento_id=?',(mid,)).fetchone()
    pix=db().execute('SELECT * FROM pix_cobrancas WHERE movimento_id=?',(mid,)).fetchone()
    if pix and datetime.fromisoformat(pix['expira_em']) <= datetime.now(timezone.utc) and pix['status']=='Aguardando pagamento':
        with db(): db().execute("UPDATE pix_cobrancas SET status='Expirado' WHERE movimento_id=?",(mid,))
        pix=db().execute('SELECT * FROM pix_cobrancas WHERE movimento_id=?',(mid,)).fetchone()
    qr=None
    if pix and pix['status']=='Aguardando pagamento':
        import qrcode, io, base64
        out=io.BytesIO(); qrcode.make(pix['payload']).save(out,format='PNG'); qr=base64.b64encode(out.getvalue()).decode()
    return render_template('comprovante.html',title='Registro concluído',r=r,itens=json.loads(r['itens']),entrega=entrega,pix=pix,qr=qr)

@app.route('/pontos',methods=['GET','POST'])
@login_required
def pontos():
    if request.method == 'POST':
        p = request.form.get('ponto')
        if p not in PONTOS:
            abort(400)
        with db():
            db().execute('UPDATE usuarios SET ponto=? WHERE id=?',(p,g.user['id']))
        flash('Ponto atualizado para os próximos registros. Registros anteriores mantêm seu destino.','success')
        return redirect(url_for('pontos'))
    return render_template('pontos.html',title='Pontos de coleta', estado_selecionado=request.args.get('estado',''))

@app.route('/estoque')
@login_required
def estoque():
    p = request.args.get('ponto',g.user['ponto'])
    if p not in PONTOS:
        abort(400)
    return render_template('estoque.html',title='Estoque por ponto',ponto=p,estoque=estoque_atual(p))

@app.route('/cadastros')
@login_required
def cadastros():
    if not g.user['admin']:
        abort(403)
    vinculo=db().execute('SELECT ponto FROM administradores_pontos WHERE usuario_id=?',(g.user['id'],)).fetchone()
    if not vinculo: abort(403)
    pessoas = db().execute('SELECT nome,perfil,moradores,criancas,ponto,codigo FROM usuarios WHERE ponto=? ORDER BY nome',(vinculo['ponto'],)).fetchall()
    valores = db().execute('SELECT u.nome, SUM(m.valor) AS total FROM movimentos m JOIN usuarios u ON u.id=m.usuario WHERE m.valor>0 AND m.ponto=? GROUP BY u.id ORDER BY total DESC',(vinculo['ponto'],)).fetchall()
    return render_template('cadastros.html',title='Cadastros',pessoas=pessoas,valores=valores,total=sum(r['total'] for r in valores))

@app.cli.command('tornar-admin')
def tornar_admin():
    import click
    cpf = click.prompt('CPF da conta que será administradora')
    with db():
        ponto=click.prompt('Nome exato da unidade Senac')
        if ponto not in PONTOS:
            click.echo('Unidade inválida. Consulte senacs.json.'); return
        cur = db().execute('UPDATE usuarios SET admin=1 WHERE cpf=?',(cpf,))
        usuario=db().execute('SELECT id FROM usuarios WHERE cpf=?',(cpf,)).fetchone()
        if usuario: db().execute('INSERT OR REPLACE INTO administradores_pontos VALUES(?,?)',(usuario['id'],ponto))
    click.echo('Conta atualizada.' if cur.rowcount else 'Conta não encontrada.')

def campo_pix(tag, value):
    value=str(value)
    return f'{tag}{len(value.encode("utf8")):02d}{value}'

def gerar_pix(chave, recebedor, cidade, centavos):
    # BR Code estático (EMV); a validade de 5 minutos é controlada pelo sistema,
    # NÃO pelo banco recebedor. Sem PSP, não há expiração bancária nem confirmação automática.
    conta=campo_pix('00','br.gov.bcb.pix')+campo_pix('01',chave)
    base=campo_pix('00','01')+campo_pix('26',conta)+campo_pix('52','0000')+campo_pix('53','986')
    base+=campo_pix('54',f'{centavos/100:.2f}')+campo_pix('58','BR')
    base+=campo_pix('59',recebedor[:25])+campo_pix('60',cidade[:15])
    base+=campo_pix('62',campo_pix('05','***'))+'6304'
    crc=0xFFFF
    for b in base.encode('utf8'):
        crc^=b<<8
        for _ in range(8): crc=((crc<<1)^0x1021) & 0xFFFF if crc & 0x8000 else (crc<<1)&0xFFFF
    return base+f'{crc:04X}'

@app.route('/admin')
@login_required
def admin_painel():
    if not g.user['admin']: abort(403)
    vinculo=db().execute('SELECT ponto FROM administradores_pontos WHERE usuario_id=?',(g.user['id'],)).fetchone()
    if not vinculo: abort(403)
    ponto=vinculo['ponto']
    registro=db().execute('SELECT trocar_senha FROM unidades_admin WHERE usuario_id=?',(g.user['id'],)).fetchone()
    if registro and registro['trocar_senha']: return redirect(url_for('trocar_senha_admin'))
    resumo=db().execute("""SELECT COUNT(*) AS total,
        SUM(CASE WHEN e.status='Pendente' THEN 1 ELSE 0 END) AS pendentes,
        SUM(CASE WHEN e.status='Entregue' THEN 1 ELSE 0 END) AS entregues
        FROM entregas e JOIN movimentos m ON m.id=e.movimento_id WHERE m.ponto=?""",(ponto,)).fetchone()
    recentes=db().execute("""SELECT m.id,m.codigo,m.tipo,u.nome,e.prazo,e.status
        FROM entregas e JOIN movimentos m ON m.id=e.movimento_id
        JOIN usuarios u ON u.id=m.usuario WHERE m.ponto=?
        ORDER BY CASE WHEN e.status='Pendente' THEN 0 ELSE 1 END,m.id DESC LIMIT 12""",(ponto,)).fetchall()
    return render_template('admin_painel.html',title='Painel do administrador',ponto=ponto,resumo=resumo,recentes=recentes)

@app.route('/admin/doacoes')
@login_required
def admin_doacoes():
    if not g.user['admin']:
        abort(403)
    vinculo = db().execute('SELECT ponto FROM administradores_pontos WHERE usuario_id=?', (g.user['id'],)).fetchone()
    if not vinculo:
        abort(403)
    categoria = request.args.get('tipo', 'todas')
    filtros = {
        'todas': None,
        'pix': 'Registro de valor',
        'roupas': 'Doação de roupas',
        'alimentos': 'Doação de alimentos',
    }
    if categoria not in filtros:
        abort(404)
    sql = """SELECT m.id, m.codigo, m.tipo, m.itens, m.valor, m.criado,
                    u.nome, e.status AS entrega_status, e.prazo,
                    p.status AS pix_status, p.expira_em
             FROM movimentos m
             JOIN usuarios u ON u.id=m.usuario
             LEFT JOIN entregas e ON e.movimento_id=m.id
             LEFT JOIN pix_cobrancas p ON p.movimento_id=m.id
             WHERE m.ponto=? AND m.tipo IN ('Registro de valor','Doação de roupas','Doação de alimentos')"""
    parametros = [vinculo['ponto']]
    if filtros[categoria]:
        sql += ' AND m.tipo=?'
        parametros.append(filtros[categoria])
    sql += ' ORDER BY m.id DESC'
    registros = db().execute(sql, parametros).fetchall()
    return render_template('admin_doacoes.html', title='Doações da unidade',
                           ponto=vinculo['ponto'], registros=registros, categoria=categoria)

@app.route('/admin/entregas')
@login_required
def admin_entregas():
    if not g.user['admin']: abort(403)
    vinculo=db().execute('SELECT ponto FROM administradores_pontos WHERE usuario_id=?',(g.user['id'],)).fetchone()
    if not vinculo: abort(403)
    registros=db().execute("""SELECT m.id,m.codigo,m.tipo,m.itens,m.criado,e.prazo,e.status,u.nome
        FROM entregas e JOIN movimentos m ON m.id=e.movimento_id
        JOIN usuarios u ON u.id=m.usuario WHERE m.ponto=? ORDER BY e.status DESC,m.id DESC""",(vinculo['ponto'],)).fetchall()
    return render_template('admin_entregas.html',title='Entregas da unidade',registros=registros,ponto=vinculo['ponto'])

@app.route('/admin/entregas/<int:mid>/confirmar',methods=['POST'])
@login_required
def confirmar_entrega(mid):
    if not g.user['admin']: abort(403)
    vinculo=db().execute('SELECT ponto FROM administradores_pontos WHERE usuario_id=?',(g.user['id'],)).fetchone()
    if not vinculo: abort(403)
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute("""SELECT m.ponto,m.itens FROM movimentos m JOIN entregas e ON e.movimento_id=m.id
            WHERE m.id=? AND e.status='Pendente' """,(mid,)).fetchone()
        if not r or r['ponto']!=vinculo['ponto']: abort(404)
        for produto,qtd in json.loads(r['itens']).items():
            c.execute('UPDATE estoque SET quantidade=quantidade+? WHERE ponto=? AND produto=?',(qtd,r['ponto'],produto))
        c.execute("UPDATE entregas SET status='Entregue',confirmado_em=?,confirmado_por=? WHERE movimento_id=?",(datetime.now(timezone.utc).isoformat(),g.user['id'],mid))
        c.commit()
    except Exception:
        c.rollback(); raise
    flash('Entrega confirmada e estoque atualizado.','success')
    return redirect(url_for('admin_entregas'))

@app.route('/admin/estoque', methods=['GET','POST'])
@login_required
def admin_estoque():
    if not g.user['admin']: abort(403)
    vinculo=db().execute('SELECT ponto FROM administradores_pontos WHERE usuario_id=?',(g.user['id'],)).fetchone()
    if not vinculo or vinculo['ponto'] not in PONTOS: abort(403)
    ponto=vinculo['ponto']
    if request.method == 'POST':
        produto=request.form.get('produto','')
        try:
            quantidade=int(request.form.get('quantidade',''))
            if produto not in PRODUTOS + ROUPAS or not -10000 <= quantidade <= 10000 or quantidade == 0:
                raise ValueError('Produto ou quantidade inválida.')
            with db():
                cur=db().execute('UPDATE estoque SET quantidade=quantidade+? WHERE ponto=? AND produto=? AND quantidade+?>=0', (quantidade,ponto,produto,quantidade))
                if cur.rowcount != 1: raise ValueError('Estoque insuficiente para este ajuste.')
            flash('Estoque ajustado.', 'success')
        except ValueError as e:
            flash(str(e), 'error')
        return redirect(url_for('admin_estoque'))
    return render_template('admin_estoque.html',title='Controle de estoque',ponto=ponto,estoque=estoque_atual(ponto))

@app.route('/api/pontos-proximos')
@login_required
def pontos_proximos():
    try:
        lat=float(request.args['lat']); lon=float(request.args['lon'])
        if not -90<=lat<=90 or not -180<=lon<=180: abort(400)
    except (KeyError,ValueError): abort(400)
    def distancia(x):
        a=radians(lat); b=radians(float(x['latitude'])); dl=radians(float(x['longitude'])-lon)
        h=sin((b-a)/2)**2+cos(a)*cos(b)*sin(dl/2)**2
        return 12742*asin(min(1,sqrt(h)))
    disponiveis=[dict(x,distancia_km=round(distancia(x),2)) for x in UNIDADES if x.get('latitude') is not None and x.get('longitude') is not None]
    return {'pontos':sorted(disponiveis,key=lambda x:x['distancia_km'])}

@app.route('/sair',methods=['POST'])
@login_required
def sair():
    session.clear()
    return redirect(url_for('login'))

@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def error(e):
    return render_template('erro.html',title='Não foi possível continuar',error=e),e.code

with app.app_context():
    init_db()

if __name__ == '__main__':
    app.run(host='127.0.0.1',port=5000,debug=False)
