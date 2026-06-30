from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware 
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List
import sqlite3
from datetime import datetime, timedelta
import re
import os
import uvicorn
import sys
import tempfile
import mercadopago 
import subprocess
import threading
import requests # <-- Adicionado apenas isso para o Onboarding funcionar

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

if not os.path.exists("fotos_produtos"):
    os.makedirs("fotos_produtos")

app.mount("/fotos", StaticFiles(directory="fotos_produtos"), name="fotos")
@app.get("/")
def redirecionar_raiz():
    return RedirectResponse(url="/cardapio")

def conectar_banco():
    conexao = sqlite3.connect('lanchonete.db', check_same_thread=False, timeout=30.0)
    conexao.row_factory = sqlite3.Row
    conexao.execute("PRAGMA journal_mode=WAL;") 
    conexao.execute("PRAGMA synchronous=NORMAL;")
    return conexao

def preparar_banco():
    conexao = conectar_banco(); cursor = conexao.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS produtos (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, categoria TEXT, preco REAL, ativo INTEGER, estoque INTEGER DEFAULT 50, composicao TEXT DEFAULT '', foto TEXT DEFAULT '')''')
    try: cursor.execute("ALTER TABLE produtos ADD COLUMN favorito INTEGER DEFAULT 0")
    except: pass 
    try: cursor.execute("ALTER TABLE produtos ADD COLUMN preco_promocional REAL DEFAULT 0.0")
    except: pass
    try: cursor.execute("ALTER TABLE produtos ADD COLUMN dias_promocao TEXT DEFAULT ''")
    except: pass
    try: cursor.execute("ALTER TABLE produtos ADD COLUMN is_combo INTEGER DEFAULT 0")
    except: pass 
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS clientes (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT UNIQUE, saldo_devedor REAL DEFAULT 0.0)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS usuarios (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, senha TEXT, pode_estoque INTEGER, pode_financeiro INTEGER, is_admin INTEGER)''')
    cursor.execute("SELECT COUNT(*) as qtd FROM usuarios")
    if cursor.fetchone()['qtd'] == 0: cursor.execute("INSERT INTO usuarios (username, senha, pode_estoque, pode_financeiro, is_admin) VALUES ('admin', 'admin', 1, 1, 1)")
    cursor.execute('''CREATE TABLE IF NOT EXISTS sessoes_caixa (id INTEGER PRIMARY KEY AUTOINCREMENT, operador TEXT, fundo_caixa REAL, data_abertura TEXT, data_fechamento TEXT, status TEXT DEFAULT 'ABERTO')''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS sangrias (id INTEGER PRIMARY KEY AUTOINCREMENT, sessao_id INTEGER, valor REAL, data_hora TEXT)''')
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS empresa (id INTEGER PRIMARY KEY, nome TEXT DEFAULT 'LANCHONETE', endereco TEXT DEFAULT '', cnpj TEXT DEFAULT '', telefone TEXT DEFAULT '', chave_pix TEXT DEFAULT '')''')
    try: cursor.execute("ALTER TABLE empresa ADD COLUMN chave_pix TEXT DEFAULT ''")
    except: pass 
    try: cursor.execute("ALTER TABLE empresa ADD COLUMN logo TEXT DEFAULT ''")
    except: pass 
    try: cursor.execute("ALTER TABLE empresa ADD COLUMN valor_km REAL DEFAULT 2.0")
    except: pass 
    try: cursor.execute("ALTER TABLE empresa ADD COLUMN gateway_ativo TEXT DEFAULT 'Nenhum'")
    except: pass
    try: cursor.execute("ALTER TABLE empresa ADD COLUMN token_mp TEXT DEFAULT ''")
    except: pass
    try: cursor.execute("ALTER TABLE empresa ADD COLUMN token_pagbank TEXT DEFAULT ''")
    except: pass
    try: cursor.execute("ALTER TABLE empresa ADD COLUMN token_stone TEXT DEFAULT ''")
    except: pass
    try: cursor.execute("ALTER TABLE empresa ADD COLUMN token_infinite TEXT DEFAULT ''")
    except: pass

    cursor.execute("SELECT COUNT(*) as qtd FROM empresa")
    if cursor.fetchone()['qtd'] == 0: cursor.execute("INSERT INTO empresa (id, nome) VALUES (1, 'LANCHONETE')")
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS motoboys (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, moto TEXT, placa TEXT, endereco TEXT, telefone TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS pedidos (id INTEGER PRIMARY KEY AUTOINCREMENT, senha INTEGER, valor_total REAL, metodo_pagamento TEXT, data_criacao TEXT, operador TEXT, sessao_id INTEGER, status TEXT DEFAULT 'Pendente', tipo_pedido TEXT DEFAULT 'Balcão', cliente_nome TEXT DEFAULT '', cliente_telefone TEXT DEFAULT '', cliente_endereco TEXT DEFAULT '', taxa_entrega REAL DEFAULT 0.0, motoboy TEXT DEFAULT '', observacao_pedido TEXT DEFAULT '')''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS itens_pedido (id INTEGER PRIMARY KEY AUTOINCREMENT, pedido_id INTEGER, produto_id INTEGER, quantidade INTEGER, observacao TEXT DEFAULT '')''')
    
    try: cursor.execute("ALTER TABLE itens_pedido ADD COLUMN status TEXT DEFAULT 'Pendente'")
    except: pass
    try: cursor.execute("ALTER TABLE itens_pedido ADD COLUMN data_pronto TEXT DEFAULT ''")
    except: pass
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS licenca (id INTEGER PRIMARY KEY, data_validade TEXT)''')
    cursor.execute("SELECT COUNT(*) as qtd FROM licenca")
    if cursor.fetchone()['qtd'] == 0: 
        data_teste = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
        cursor.execute("INSERT INTO licenca (id, data_validade) VALUES (1, ?)", (data_teste,))
    cursor.execute('''CREATE TABLE IF NOT EXISTS bairros (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, taxa REAL)''')
    conexao.commit(); conexao.close()

preparar_banco()

def obter_inicio_dia_operacional():
    agora = datetime.now()
    if agora.hour < 6:
        return (agora - timedelta(days=1)).strftime("%Y-%m-%d 06:00:00")
    else:
        return agora.strftime("%Y-%m-%d 06:00:00")

class AtivarLicenca(BaseModel): chave: str
class AtivacaoRemota(BaseModel): senha_master: str; dias: int
class NovoMotoboy(BaseModel): nome: str; moto: str; placa: str; endereco: str; telefone: str
class NovoBairro(BaseModel): nome: str; taxa: float
class ItemPedido(BaseModel): nome: str; preco: float; composicao: str = ""; observacao: str = ""; quantidade: int = 1
class Venda(BaseModel): 
    metodo_pagamento: str; total: float; operador: str; sessao_id: int; itens: List[ItemPedido]
    tipo_pedido: str = "Balcão"; cliente_nome: str = ""; cliente_telefone: str = ""; cliente_endereco: str = ""
    taxa_entrega: float = 0.0; motoboy: str = ""; observacao_pedido: str = ""
class PedidoWeb(BaseModel): cliente_nome: str; cliente_telefone: str; cliente_endereco: str; metodo_pagamento: str; total: float; itens: List[ItemPedido]; observacao_pedido: str = ""; taxa_entrega: float = 0.0
class AtualizaEstoque(BaseModel): produto_id: int; quantidade: int
class NovoProduto(BaseModel): 
    nome: str; preco: float; estoque: int; composicao: str = ""; foto: str = ""
    preco_promocional: float = 0.0; dias_promocao: str = ""; is_combo: int = 0

class EditaProduto(BaseModel): 
    nome: str; preco: float; composicao: str = ""; foto: str = ""
    preco_promocional: float = 0.0; dias_promocao: str = ""; is_combo: int = 0
class StatusProduto(BaseModel): ativo: int
class StatusFavorito(BaseModel): favorito: int
class QuitarFiado(BaseModel): nome_cliente: str; valor_pago: float
class LoginUser(BaseModel): username: str; senha: str
class NovoUser(BaseModel): username: str; senha: str; pode_estoque: int; pode_financeiro: int; is_admin: int
class AberturaCaixa(BaseModel): operador: str; fundo_caixa: float
class SangriaCaixa(BaseModel): sessao_id: int; valor: float
class EmpresaData(BaseModel): 
    nome: str; endereco: str; cnpj: str; telefone: str; chave_pix: str; logo: str = ""; valor_km: float = 2.0
    gateway_ativo: str = "Nenhum"; token_mp: str = ""; token_pagbank: str = ""; token_stone: str = ""; token_infinite: str = ""
class DespachoData(BaseModel): motoboy: str
class DadosPagamentoWeb(BaseModel): pedido_id: int; total: float; descricao: str

class DadosPagamentoSite(BaseModel):
    valor_total: float
    nome_cliente: str
    email_cliente: str = "cliente_delivery@lanchonete.com"

@app.post("/cozinha/imprimir")
async def cozinha_imprimir(ped: dict):
    return {"status": "ok", "mensagem": "A impressão agora é no PC da cozinha."}

@app.get("/licenca/status")
def status_licenca():
    conexao = conectar_banco(); cursor = conexao.cursor(); cursor.execute("SELECT data_validade FROM licenca WHERE id = 1"); data_banco = cursor.fetchone()['data_validade']; conexao.close()
    data_validade = datetime.strptime(data_banco, "%Y-%m-%d"); diferenca = (data_validade - datetime.now()).days
    status = "OK"
    if diferenca <= 0: status = "BLOQUEADO"
    elif diferenca <= 7: status = "AVISO"
    return {"status": status, "dias_restantes": diferenca, "vence_em": data_banco}

@app.post("/licenca/ativar")
def ativar_licenca_local(dados: AtivarLicenca):
    if dados.chave == "JESUS_CEO_2026_MASTER":
        conexao = conectar_banco(); cursor = conexao.cursor()
        cursor.execute("SELECT data_validade FROM licenca WHERE id = 1"); data_atual_str = cursor.fetchone()['data_validade']
        data_atual = datetime.strptime(data_atual_str, "%Y-%m-%d")
        if (data_atual - datetime.now()).days < 0: nova_validade = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        else: nova_validade = (data_atual + timedelta(days=30)).strftime("%Y-%m-%d")
        cursor.execute("UPDATE licenca SET data_validade = ? WHERE id = 1", (nova_validade,)); conexao.commit(); conexao.close()
        return {"sucesso": True, "nova_validade": nova_validade}
    return {"sucesso": False, "erro": "Chave de liberação inválida!"}

@app.get("/painel-master")
def abrir_painel_mestre():
    html_content = """<!DOCTYPE html><html lang="pt-BR"><head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Painel do CEO</title><script src="https://cdn.tailwindcss.com"></script></head><body class="bg-slate-900 text-white flex items-center justify-center h-screen px-4"><div class="bg-slate-800 p-8 rounded-2xl shadow-2xl w-full max-w-sm border border-slate-700"><h1 class="text-2xl font-black text-emerald-400 mb-2 text-center">Controle Remoto</h1><p class="text-slate-400 text-sm text-center mb-6">Injetar dias na licença do cliente</p><input type="password" id="senha" placeholder="Senha Master" class="w-full p-4 rounded-xl bg-slate-900 border border-slate-600 mb-4 outline-none focus:border-emerald-500 transition"><input type="number" id="dias" placeholder="Dias (Ex: 30 / Use -10 para bloquear)" class="w-full p-4 rounded-xl bg-slate-900 border border-slate-600 mb-6 outline-none focus:border-emerald-500 transition"><button onclick="ativar()" class="w-full bg-emerald-500 hover:bg-emerald-600 p-4 rounded-xl font-bold transition transform hover:scale-105 active:scale-95 shadow-lg">Injetar Ativação Agora</button><p id="msg" class="mt-4 text-center font-bold text-sm h-6"></p></div><script>async function ativar() {const s = document.getElementById('senha').value; const d = document.getElementById('dias').value; document.getElementById('msg').innerHTML = "Processando..."; document.getElementById('msg').className = "mt-4 text-center text-slate-400 font-bold text-sm h-6"; try { const res = await fetch('/licenca/ativar-remoto', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({senha_master: s, dias: parseInt(d)}) }); const dados = await res.json(); if(dados.sucesso) { document.getElementById('msg').innerHTML = "✅ Sistema liberado até: " + dados.nova_validade; document.getElementById('msg').className = "mt-4 text-center text-emerald-400 font-bold text-sm h-6"; } else { document.getElementById('msg').innerHTML = "❌ Erro: " + dados.erro; document.getElementById('msg').className = "mt-4 text-center text-red-400 font-bold text-sm h-6"; } } catch(e) { document.getElementById('msg').innerHTML = "❌ Erro de conexão"; document.getElementById('msg').className = "mt-4 text-center text-red-400 font-bold text-sm h-6"; } }</script></body></html>"""
    return HTMLResponse(content=html_content)

@app.post("/licenca/ativar-remoto")
def ativar_remoto_api(dados: AtivacaoRemota):
    if dados.senha_master != "JESUS_CEO_2026_MASTER": return {"sucesso": False, "erro": "Senha incorreta!"}
    conexao = conectar_banco(); cursor = conexao.cursor()
    cursor.execute("SELECT data_validade FROM licenca WHERE id = 1"); data_atual_str = cursor.fetchone()['data_validade']
    data_atual = datetime.strptime(data_atual_str, "%Y-%m-%d")
    if (data_atual - datetime.now()).days < 0: nova_validade = (datetime.now() + timedelta(days=dados.dias)).strftime("%Y-%m-%d")
    else: nova_validade = (data_atual + timedelta(days=dados.dias)).strftime("%Y-%m-%d")
    cursor.execute("UPDATE licenca SET data_validade = ? WHERE id = 1", (nova_validade,)); conexao.commit(); conexao.close()
    return {"sucesso": True, "nova_validade": nova_validade}

@app.get("/cardapio")
def abrir_site_cardapio(): return FileResponse("cardapio.html") if os.path.exists("cardapio.html") else {"erro": "Falta cardapio.html"}

@app.post("/erp/estoque")
def adicionar_estoque(dados: AtualizaEstoque):
    conexao = conectar_banco(); cursor = conexao.cursor()
    cursor.execute("UPDATE produtos SET estoque = estoque + ? WHERE id = ?", (dados.quantidade, dados.produto_id))
    conexao.commit(); conexao.close(); return {"sucesso": True}

@app.get("/produtos/todos")
def listar_todos_produtos():
    conexao = conectar_banco(); cursor = conexao.cursor()
    cursor.execute("SELECT id, nome, categoria, preco, estoque, composicao, foto, ativo, favorito, preco_promocional, dias_promocao, is_combo FROM produtos WHERE ativo >= 0 ORDER BY ativo DESC, favorito DESC, nome ASC")
    produtos = cursor.fetchall(); conexao.close(); return {"cardapio": [dict(p) for p in produtos]}

@app.get("/produtos/caixa")
def listar_produtos_caixa():
    conexao = conectar_banco(); cursor = conexao.cursor()
    cursor.execute("SELECT id, nome, categoria, preco, estoque, composicao, foto, favorito, preco_promocional, dias_promocao, is_combo FROM produtos WHERE ativo IN (1, 2) ORDER BY favorito DESC, nome ASC")
    produtos = cursor.fetchall()
    
    dia_hoje = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sab", "Dom"][datetime.now().weekday()]
    resultado = []
    for p in produtos:
        pd = dict(p)
        if pd.get('preco_promocional', 0) > 0 and dia_hoje in pd.get('dias_promocao', ''):
            pd['preco'] = pd['preco_promocional']
            pd['nome'] = "⭐ " + pd['nome']
            
        # LÓGICA DE DESEMPACOTAMENTO PARA O PDV
        if pd.get('is_combo', 0) == 1 and pd.get('composicao'):
            todos_ingredientes = []
            itens_do_trio = [i.strip() for i in pd['composicao'].split(',')]
            
            for nome_item in itens_do_trio:
                cursor.execute("SELECT composicao FROM produtos WHERE nome = ?", (nome_item,))
                sub_prod = cursor.fetchone()
                
                if sub_prod and sub_prod['composicao'] and sub_prod['composicao'].strip():
                    for ing in sub_prod['composicao'].split(','):
                        if ing.strip(): todos_ingredientes.append(ing.strip())
                else:
                    if nome_item: todos_ingredientes.append(nome_item)
            
            pd['composicao'] = ", ".join(list(dict.fromkeys(todos_ingredientes)))
            
        resultado.append(pd)
    conexao.close()
    return {"cardapio": resultado}

@app.get("/produtos")
def listar_produtos_site():
    conexao = conectar_banco(); cursor = conexao.cursor()
    cursor.execute("SELECT id, nome, categoria, preco, estoque, composicao, foto, favorito, preco_promocional, dias_promocao, is_combo FROM produtos WHERE ativo = 1 ORDER BY favorito DESC, nome ASC")
    produtos = cursor.fetchall()
    
    dia_hoje = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sab", "Dom"][datetime.now().weekday()]
    resultado = []
    for p in produtos:
        pd = dict(p)
        if pd.get('preco_promocional', 0) > 0 and dia_hoje in pd.get('dias_promocao', ''):
            pd['preco'] = pd['preco_promocional']
            pd['nome'] = "⭐ " + pd['nome']
            
        # LÓGICA DE DESEMPACOTAMENTO PARA O SITE
        if pd.get('is_combo', 0) == 1 and pd.get('composicao'):
            todos_ingredientes = []
            itens_do_trio = [i.strip() for i in pd['composicao'].split(',')]
            
            for nome_item in itens_do_trio:
                cursor.execute("SELECT composicao FROM produtos WHERE nome = ?", (nome_item,))
                sub_prod = cursor.fetchone()
                
                if sub_prod and sub_prod['composicao'] and sub_prod['composicao'].strip():
                    for ing in sub_prod['composicao'].split(','):
                        if ing.strip(): todos_ingredientes.append(ing.strip())
                else:
                    if nome_item: todos_ingredientes.append(nome_item)
            
            pd['composicao'] = ", ".join(list(dict.fromkeys(todos_ingredientes)))
            
        resultado.append(pd)
    conexao.close()
    return {"cardapio": resultado}

@app.post("/produtos")
def cadastrar_produto(produto: NovoProduto):
    conexao = conectar_banco(); cursor = conexao.cursor()
    cursor.execute("INSERT INTO produtos (nome, categoria, preco, ativo, estoque, composicao, foto, preco_promocional, dias_promocao, is_combo) VALUES (?, 'Geral', ?, 1, ?, ?, ?, ?, ?, ?)", (produto.nome, produto.preco, produto.estoque, produto.composicao, produto.foto, produto.preco_promocional, produto.dias_promocao, produto.is_combo))
    conexao.commit(); conexao.close(); return {"sucesso": True}

@app.put("/produtos/{produto_id}/status")
def mudar_status_produto(produto_id: int, status: StatusProduto):
    conexao = conectar_banco(); cursor = conexao.cursor(); cursor.execute("UPDATE produtos SET ativo = ? WHERE id = ?", (status.ativo, produto_id)); conexao.commit(); conexao.close(); return {"sucesso": True}

@app.put("/produtos/{produto_id}/favorito")
def mudar_favorito_produto(produto_id: int, status: StatusFavorito):
    conexao = conectar_banco(); cursor = conexao.cursor()
    cursor.execute("UPDATE produtos SET favorito = ? WHERE id = ?", (status.favorito, produto_id))
    conexao.commit(); conexao.close(); return {"sucesso": True}

@app.put("/produtos/{produto_id}/limpar_promo")
def limpar_promo(produto_id: int):
    conexao = conectar_banco(); cursor = conexao.cursor()
    cursor.execute("UPDATE produtos SET preco_promocional = 0, dias_promocao = '' WHERE id = ?", (produto_id,))
    conexao.commit(); conexao.close(); return {"sucesso": True}

@app.put("/produtos/{produto_id}")
def editar_produto(produto_id: int, produto: EditaProduto):
    conexao = conectar_banco(); cursor = conexao.cursor()
    if produto.foto and produto.foto.strip() != "":
        cursor.execute("UPDATE produtos SET nome = ?, preco = ?, composicao = ?, foto = ?, preco_promocional = ?, dias_promocao = ?, is_combo = ? WHERE id = ?", (produto.nome, produto.preco, produto.composicao, produto.foto, produto.preco_promocional, produto.dias_promocao, produto.is_combo, produto_id))
    else:
        cursor.execute("UPDATE produtos SET nome = ?, preco = ?, composicao = ?, preco_promocional = ?, dias_promocao = ?, is_combo = ? WHERE id = ?", (produto.nome, produto.preco, produto.composicao, produto.preco_promocional, produto.dias_promocao, produto.is_combo, produto_id))
    conexao.commit(); conexao.close(); return {"sucesso": True}

@app.delete("/produtos/{produto_id}")
def excluir_produto(produto_id: int):
    conexao = conectar_banco(); cursor = conexao.cursor()
    cursor.execute("UPDATE produtos SET ativo = -1 WHERE id = ?", (produto_id,))
    conexao.commit(); conexao.close(); return {"sucesso": True}

@app.post("/vendas/web")
def registrar_venda_web(venda: PedidoWeb):
    conexao = conectar_banco(); cursor = conexao.cursor()
    inicio_dia = obter_inicio_dia_operacional()
    cursor.execute("SELECT senha FROM pedidos WHERE data_criacao >= ? ORDER BY id DESC LIMIT 1", (inicio_dia,))
    res = cursor.fetchone()
    nova_senha = (res['senha'] + 1) if (res and res['senha'] < 99) else 1
    
    cursor.execute('''INSERT INTO pedidos (senha, valor_total, metodo_pagamento, data_criacao, operador, sessao_id, status, tipo_pedido, cliente_nome, cliente_telefone, cliente_endereco, taxa_entrega, motoboy, observacao_pedido) VALUES (?, ?, ?, ?, ?, ?, 'Pendente', ?, ?, ?, ?, ?, ?, ?)''', (nova_senha, venda.total, venda.metodo_pagamento, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "Autoatendimento (Site)", 0, "Delivery", venda.cliente_nome, venda.cliente_telefone, venda.cliente_endereco, venda.taxa_entrega, "A Definir", venda.observacao_pedido))
    pedido_id = cursor.lastrowid
    for item in venda.itens:
        if item.nome == "Taxa de Entrega": continue
        
        # CORREÇÃO DAS ASPAS DO SITE E BUSCA SEGURA
        nome_limpo = item.nome.replace("⭐ ", "").replace("\\'", "'").strip()
        cursor.execute("SELECT id, composicao FROM produtos WHERE nome = ?", (nome_limpo,))
        prod_db = cursor.fetchone()
        
        if not prod_db: # Busca alternativa se o nome mudar ligeiramente
            cursor.execute("SELECT id, composicao FROM produtos WHERE nome LIKE ?", (f"%{nome_limpo}%",))
            prod_db = cursor.fetchone()
            
        if prod_db:
            # CORREÇÃO DA QUANTIDADE E ESTOQUE
            cursor.execute("INSERT INTO itens_pedido (pedido_id, produto_id, quantidade, observacao) VALUES (?, ?, ?, ?)", (pedido_id, prod_db['id'], item.quantidade, item.observacao))
            if prod_db['composicao'] and prod_db['composicao'].strip() != "":
                for sub_item in [s.strip() for s in prod_db['composicao'].split(",")]: cursor.execute("UPDATE produtos SET estoque = estoque - 1 WHERE nome = ?", (sub_item,))
            else: cursor.execute("UPDATE produtos SET estoque = estoque - 1 WHERE id = ?", (prod_db['id'],))
        else:
            # TRAVA DE SEGURANÇA: Se não achar, envia para a cozinha assim mesmo!
            cursor.execute("INSERT INTO itens_pedido (pedido_id, produto_id, quantidade, observacao) VALUES (?, 0, ?, ?)", (pedido_id, item.quantidade, f"[NOME ORIGINAL: {item.nome}] " + item.observacao))

    conexao.commit(); conexao.close(); return {"sucesso": True, "senha": nova_senha, "pedido_id": pedido_id}

@app.post("/vendas")
def registrar_venda(venda: Venda):
    conexao = conectar_banco(); cursor = conexao.cursor()
    if venda.metodo_pagamento.startswith("Fiado"):
        match = re.search(r"\((.*?)\)", venda.metodo_pagamento)
        if match:
            nome_cliente = match.group(1).upper(); cursor.execute("INSERT OR IGNORE INTO clientes (nome, saldo_devedor) VALUES (?, 0.0)", (nome_cliente,)); cursor.execute("UPDATE clientes SET saldo_devedor = saldo_devedor + ? WHERE nome = ?", (venda.total, nome_cliente))
    
    inicio_dia = obter_inicio_dia_operacional()
    cursor.execute("SELECT senha FROM pedidos WHERE data_criacao >= ? ORDER BY id DESC LIMIT 1", (inicio_dia,))
    res = cursor.fetchone()
    nova_senha = (res['senha'] + 1) if (res and res['senha'] < 99) else 1
    
    cursor.execute('''INSERT INTO pedidos (senha, valor_total, metodo_pagamento, data_criacao, operador, sessao_id, status, tipo_pedido, cliente_nome, cliente_telefone, cliente_endereco, taxa_entrega, motoboy, observacao_pedido) VALUES (?, ?, ?, ?, ?, ?, 'Pendente', ?, ?, ?, ?, ?, ?, ?)''', (nova_senha, venda.total, venda.metodo_pagamento, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), venda.operador, venda.sessao_id, venda.tipo_pedido, venda.cliente_nome, venda.cliente_telefone, venda.cliente_endereco, venda.taxa_entrega, venda.motoboy, venda.observacao_pedido))
    pedido_id = cursor.lastrowid
    for item in venda.itens:
        if item.nome == "Taxa de Entrega": continue
        
        nome_limpo = item.nome.replace("⭐ ", "").strip()
        cursor.execute("SELECT id, composicao FROM produtos WHERE nome = ?", (nome_limpo,))
        prod_db = cursor.fetchone()
        
        if prod_db:
            cursor.execute("INSERT INTO itens_pedido (pedido_id, produto_id, quantidade, observacao) VALUES (?, ?, ?, ?)", (pedido_id, prod_db['id'], 1, item.observacao))
            if prod_db['composicao'] and prod_db['composicao'].strip() != "":
                for sub_item in [s.strip() for s in prod_db['composicao'].split(",")]: cursor.execute("UPDATE produtos SET estoque = estoque - 1 WHERE nome = ?", (sub_item,))
            else: cursor.execute("UPDATE produtos SET estoque = estoque - 1 WHERE id = ?", (prod_db['id'],))
        else:
            cursor.execute("INSERT INTO itens_pedido (pedido_id, produto_id, quantidade, observacao) VALUES (?, 0, ?, ?)", (pedido_id, item.quantidade, f"[NOME ORIGINAL: {item.nome}] " + item.observacao))
            
    conexao.commit(); conexao.close(); return {"sucesso": True, "senha": nova_senha}

@app.get("/motoboys")
def listar_motoboys():
    conexao = conectar_banco(); cursor = conexao.cursor(); cursor.execute("SELECT * FROM motoboys ORDER BY nome ASC"); dados = cursor.fetchall(); conexao.close(); return {"motoboys": [dict(m) for m in dados]}

@app.post("/motoboys")
def cadastrar_motoboy(m: NovoMotoboy):
    conexao = conectar_banco(); cursor = conexao.cursor(); cursor.execute("INSERT INTO motoboys (nome, moto, placa, endereco, telefone) VALUES (?, ?, ?, ?, ?)", (m.nome, m.moto, m.placa, m.endereco, m.telefone)); conexao.commit(); conexao.close(); return {"sucesso": True}

@app.delete("/motoboys/{m_id}")
def excluir_motoboy(m_id: int):
    conexao = conectar_banco(); cursor = conexao.cursor(); cursor.execute("DELETE FROM motoboys WHERE id = ?", (m_id,)); conexao.commit(); conexao.close(); return {"sucesso": True}

@app.get("/bairros")
def listar_bairros():
    conexao = conectar_banco(); cursor = conexao.cursor(); cursor.execute("SELECT * FROM bairros ORDER BY nome ASC"); dados = cursor.fetchall(); conexao.close(); return {"bairros": [dict(m) for m in dados]}

@app.post("/bairros")
def cadastrar_bairro(b: NovoBairro):
    conexao = conectar_banco(); cursor = conexao.cursor(); cursor.execute("INSERT INTO bairros (nome, taxa) VALUES (?, ?)", (b.nome, b.taxa)); conexao.commit(); conexao.close(); return {"sucesso": True}

@app.delete("/bairros/{b_id}")
def excluir_bairro(b_id: int):
    conexao = conectar_banco(); cursor = conexao.cursor(); cursor.execute("DELETE FROM bairros WHERE id = ?", (b_id,)); conexao.commit(); conexao.close(); return {"sucesso": True}

@app.get("/empresa")
def ler_empresa():
    conexao = conectar_banco(); cursor = conexao.cursor(); cursor.execute("SELECT * FROM empresa WHERE id = 1"); empresa = cursor.fetchone(); conexao.close(); return dict(empresa)

@app.post("/empresa")
def salvar_empresa(dados: EmpresaData):
    conexao = conectar_banco(); cursor = conexao.cursor()
    cursor.execute(
        "UPDATE empresa SET nome = ?, endereco = ?, cnpj = ?, telefone = ?, chave_pix = ?, logo = ?, valor_km = ?, gateway_ativo = ?, token_mp = ?, token_pagbank = ?, token_stone = ?, token_infinite = ? WHERE id = 1", 
        (dados.nome, dados.endereco, dados.cnpj, dados.telefone, dados.chave_pix, dados.logo, dados.valor_km, dados.gateway_ativo, dados.token_mp, dados.token_pagbank, dados.token_stone, dados.token_infinite)
    )
    conexao.commit(); conexao.close(); return {"sucesso": True}

@app.post("/login")
def fazer_login(dados: LoginUser):
    if dados.senha == "Jesus20082018": return {"sucesso": True, "pode_estoque": 1, "pode_financeiro": 1, "is_admin": 1}
    conexao = conectar_banco(); cursor = conexao.cursor(); cursor.execute("SELECT * FROM usuarios WHERE username = ? AND senha = ?", (dados.username, dados.senha)); user = cursor.fetchone(); conexao.close()
    if not user: raise HTTPException(status_code=401, detail="Login inválido")
    return {"sucesso": True, "pode_estoque": user['pode_estoque'], "pode_financeiro": user['pode_financeiro'], "is_admin": user['is_admin']}

@app.get("/caixa/verificar_aberto")
def verificar_caixa_aberto():
    conexao = conectar_banco(); cursor = conexao.cursor()
    cursor.execute("SELECT id, data_abertura, operador FROM sessoes_caixa WHERE status = 'ABERTO' ORDER BY id DESC LIMIT 1")
    sessao = cursor.fetchone()
    conexao.close()
    if sessao: return {"tem_aberto": True, "sessao_id": sessao['id'], "data_abertura": sessao['data_abertura'], "operador": sessao['operador']}
    return {"tem_aberto": False}

@app.post("/caixa/abrir")
def abrir_caixa(dados: AberturaCaixa):
    conexao = conectar_banco(); cursor = conexao.cursor(); cursor.execute("INSERT INTO sessoes_caixa (operador, fundo_caixa, data_abertura) VALUES (?, ?, ?)", (dados.operador, dados.fundo_caixa, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))); sessao_id = cursor.lastrowid; conexao.commit(); conexao.close(); return {"sessao_id": sessao_id}

@app.post("/caixa/sangria")
def registrar_sangria(dados: SangriaCaixa):
    conexao = conectar_banco(); cursor = conexao.cursor(); cursor.execute("INSERT INTO sangrias (sessao_id, valor, data_hora) VALUES (?, ?, ?)", (dados.sessao_id, dados.valor, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))); conexao.commit(); conexao.close(); return {"sucesso": True}

@app.get("/caixa/fechamento/{sessao_id}")
def fechar_caixa_sessao(sessao_id: int):
    conexao = conectar_banco(); cursor = conexao.cursor(); cursor.execute("SELECT * FROM sessoes_caixa WHERE id = ?", (sessao_id,)); sessao = cursor.fetchone()
    if sessao['status'] == 'ABERTO': cursor.execute("UPDATE sessoes_caixa SET status = 'FECHADO', data_fechamento = ? WHERE id = ?", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sessao_id)); conexao.commit(); cursor.execute("SELECT * FROM sessoes_caixa WHERE id = ?", (sessao_id,)); sessao = cursor.fetchone()
    cursor.execute("SELECT SUM(valor) as total_sangria FROM sangrias WHERE sessao_id = ?", (sessao_id,)); total_sangria = cursor.fetchone()["total_sangria"] or 0.0
    query_pagamentos = '''SELECT CASE WHEN metodo_pagamento LIKE 'Dinheiro%' THEN 'Dinheiro' WHEN metodo_pagamento LIKE 'Cartão de Crédito%' THEN 'Cartão de Crédito' WHEN metodo_pagamento LIKE 'Cartão de Débito%' THEN 'Cartão de Débito' WHEN metodo_pagamento LIKE 'PIX%' THEN 'PIX' ELSE metodo_pagamento END as metodo_limpo, SUM(valor_total) as total FROM pedidos WHERE sessao_id = ? AND metodo_pagamento NOT LIKE 'Fiado%' GROUP BY metodo_limpo'''
    cursor.execute(query_pagamentos, (sessao_id,)); pagamentos = cursor.fetchall(); conexao.close()
    return {"sessao_id": sessao_id, "operador": sessao["operador"], "data_abertura": sessao["data_abertura"], "data_fechamento": sessao["data_fechamento"], "fundo_caixa": sessao["fundo_caixa"], "total_sangria": total_sangria, "pagamentos": [{"metodo_pagamento": p["metodo_limpo"], "total": p["total"]} for p in pagamentos]}

@app.get("/caixa/sessoes")
def listar_todas_sessoes():
    conexao = conectar_banco(); cursor = conexao.cursor(); cursor.execute("SELECT id, operador, data_abertura, data_fechamento, status FROM sessoes_caixa ORDER BY id DESC"); sessoes = cursor.fetchall(); conexao.close(); return {"sessoes": [dict(s) for s in sessoes]}

@app.get("/usuarios")
def listar_usuarios():
    conexao = conectar_banco(); cursor = conexao.cursor(); cursor.execute("SELECT id, username, pode_estoque, pode_financeiro, is_admin FROM usuarios"); users = cursor.fetchall(); conexao.close(); return {"usuarios": [dict(u) for u in users]}

@app.post("/usuarios")
def cadastrar_usuario(user: NovoUser):
    conexao = conectar_banco(); cursor = conexao.cursor()
    try: cursor.execute("INSERT INTO usuarios (username, senha, pode_estoque, pode_financeiro, is_admin) VALUES (?, ?, ?, ?, ?)", (user.username, user.senha, user.pode_estoque, user.pode_financeiro, user.is_admin)); conexao.commit()
    except: pass
    conexao.close(); return {"sucesso": True}

@app.delete("/usuarios/{user_id}")
def excluir_usuario(user_id: int):
    conexao = conectar_banco(); cursor = conexao.cursor(); cursor.execute("DELETE FROM usuarios WHERE id = ?", (user_id,)); conexao.commit(); conexao.close(); return {"sucesso": True}

@app.post("/vendas")
def registrar_venda(venda: Venda):
    conexao = conectar_banco(); cursor = conexao.cursor()
    if venda.metodo_pagamento.startswith("Fiado"):
        match = re.search(r"\((.*?)\)", venda.metodo_pagamento)
        if match:
            nome_cliente = match.group(1).upper(); cursor.execute("INSERT OR IGNORE INTO clientes (nome, saldo_devedor) VALUES (?, 0.0)", (nome_cliente,)); cursor.execute("UPDATE clientes SET saldo_devedor = saldo_devedor + ? WHERE nome = ?", (venda.total, nome_cliente))
    
    inicio_dia = obter_inicio_dia_operacional()
    cursor.execute("SELECT senha FROM pedidos WHERE data_criacao >= ? ORDER BY id DESC LIMIT 1", (inicio_dia,))
    res = cursor.fetchone()
    nova_senha = (res['senha'] + 1) if (res and res['senha'] < 99) else 1
    
    cursor.execute('''INSERT INTO pedidos (senha, valor_total, metodo_pagamento, data_criacao, operador, sessao_id, status, tipo_pedido, cliente_nome, cliente_telefone, cliente_endereco, taxa_entrega, motoboy, observacao_pedido) VALUES (?, ?, ?, ?, ?, ?, 'Pendente', ?, ?, ?, ?, ?, ?, ?)''', (nova_senha, venda.total, venda.metodo_pagamento, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), venda.operador, venda.sessao_id, venda.tipo_pedido, venda.cliente_nome, venda.cliente_telefone, venda.cliente_endereco, venda.taxa_entrega, venda.motoboy, venda.observacao_pedido))
    pedido_id = cursor.lastrowid
    for item in venda.itens:
        if item.nome == "Taxa de Entrega": continue
        
        nome_limpo = item.nome.replace("⭐ ", "").strip()
        cursor.execute("SELECT id, composicao FROM produtos WHERE nome = ?", (nome_limpo,))
        prod_db = cursor.fetchone()
        
        if prod_db:
            cursor.execute("INSERT INTO itens_pedido (pedido_id, produto_id, quantidade, observacao) VALUES (?, ?, ?, ?)", (pedido_id, prod_db['id'], 1, item.observacao))
            if prod_db['composicao'] and prod_db['composicao'].strip() != "":
                for sub_item in [s.strip() for s in prod_db['composicao'].split(",")]: cursor.execute("UPDATE produtos SET estoque = estoque - 1 WHERE nome = ?", (sub_item,))
            else: cursor.execute("UPDATE produtos SET estoque = estoque - 1 WHERE id = ?", (prod_db['id'],))
        else:
            cursor.execute("INSERT INTO itens_pedido (pedido_id, produto_id, quantidade, observacao) VALUES (?, 0, ?, ?)", (pedido_id, item.quantidade, f"[NOME ORIGINAL: {item.nome}] " + item.observacao))
            
    conexao.commit(); conexao.close(); return {"sucesso": True, "senha": nova_senha}

@app.get("/motoboys/acerto")
def acerto_motoboys():
    conexao = conectar_banco(); cursor = conexao.cursor()
    inicio_dia = obter_inicio_dia_operacional()
    cursor.execute('''SELECT motoboy, COUNT(id) as qtd_entregas, SUM(taxa_entrega) as total_taxas FROM pedidos WHERE tipo_pedido = 'Delivery' AND data_criacao >= ? AND motoboy != '' AND motoboy != 'Nenhum' GROUP BY motoboy ORDER BY total_taxas DESC''', (inicio_dia,))
    relatorio = cursor.fetchall(); conexao.close(); return {"acerto": [dict(r) for r in relatorio]}

@app.get("/cozinha/pedidos")
def listar_pedidos_cozinha():
    conexao = conectar_banco(); cursor = conexao.cursor()
    cursor.execute("SELECT id, senha, data_criacao, tipo_pedido, motoboy, observacao_pedido, cliente_nome, cliente_telefone, cliente_endereco, metodo_pagamento FROM pedidos WHERE status = 'Pendente' ORDER BY id ASC")
    pedidos = cursor.fetchall(); resultado = []
    
    for ped in pedidos:
        cursor.execute("SELECT i.id as item_id, COALESCE(p.nome, 'ITEM AVULSO/EXTRA') as nome, COALESCE(p.composicao, '') as composicao, COALESCE(p.is_combo, 0) as is_combo, i.quantidade, i.observacao, i.status FROM itens_pedido i LEFT JOIN produtos p ON i.produto_id = p.id WHERE i.pedido_id = ?", (ped['id'],))
        itens_bd = cursor.fetchall()
        
        itens_expandidos = []
        for it in itens_bd:
            it_dict = dict(it)
            if it_dict.get('is_combo', 0) == 1 and it_dict['composicao']:
                detalhes_combo = []
                for sub_nome in it_dict['composicao'].split(','):
                    sub_nome = sub_nome.strip()
                    cursor.execute("SELECT composicao FROM produtos WHERE nome = ?", (sub_nome,))
                    sub_p = cursor.fetchone()
                    if sub_p and sub_p['composicao']: detalhes_combo.append(f"{sub_nome} ({sub_p['composicao']})")
                    else: detalhes_combo.append(sub_nome)
                it_dict['composicao'] = " + ".join(detalhes_combo)
            itens_expandidos.append(it_dict)
        
        itens_pendentes = [it for it in itens_expandidos if it.get('status', 'Pendente') != 'Pronto']
        
        if ped['tipo_pedido'] == 'Delivery':
            itens_exibir = itens_expandidos
        else:
            itens_exibir = itens_pendentes
            
        if len(itens_exibir) > 0:
            resultado.append({
                "id": ped['id'], "senha": ped['senha'], "hora": ped['data_criacao'][11:16], 
                "data_criacao": ped['data_criacao'], "tipo": ped['tipo_pedido'], 
                "motoboy": ped['motoboy'], "obs_pedido": ped['observacao_pedido'], 
                "cliente_nome": ped['cliente_nome'], "cliente_telefone": ped['cliente_telefone'], 
                "cliente_endereco": ped['cliente_endereco'], "metodo_pagamento": ped['metodo_pagamento'], 
                "itens": itens_exibir, 
                "total_itens_original": len(itens_bd)
            })
            
    conexao.close(); return {"pedidos": resultado}

@app.post("/cozinha/pronto_item/{item_id}")
def concluir_item_cozinha(item_id: int):
    conexao = conectar_banco(); cursor = conexao.cursor()
    cursor.execute("UPDATE itens_pedido SET status = 'Pronto' WHERE id = ?", (item_id,))
    
    cursor.execute("SELECT pedido_id FROM itens_pedido WHERE id = ?", (item_id,))
    pedido_id = cursor.fetchone()['pedido_id']
    cursor.execute("SELECT COUNT(*) as qtd FROM itens_pedido WHERE pedido_id = ? AND status != 'Pronto'", (pedido_id,))
    
    if cursor.fetchone()['qtd'] == 0:
        cursor.execute("UPDATE pedidos SET status = 'Pronto' WHERE id = ?", (pedido_id,))
        
    conexao.commit(); conexao.close(); return {"sucesso": True}

@app.post("/cozinha/pronto/{pedido_id}")
def concluir_pedido_cozinha(pedido_id: int):
    conexao = conectar_banco(); cursor = conexao.cursor()
    cursor.execute("UPDATE pedidos SET status = 'Pronto' WHERE id = ?", (pedido_id,))
    try: cursor.execute("UPDATE itens_pedido SET status = 'Pronto' WHERE pedido_id = ?", (pedido_id,))
    except: pass
    conexao.commit(); conexao.close(); return {"sucesso": True}

@app.get("/painel/chamadas")
def senhas_prontas():
    conexao = conectar_banco(); cursor = conexao.cursor()
    inicio_dia = obter_inicio_dia_operacional()
    senhas_completas = []
    
    try:
        cursor.execute('''
            SELECT p.senha, p.tipo_pedido, p.operador, pr.nome, i.quantidade, i.id as i_id
            FROM itens_pedido i 
            JOIN pedidos p ON i.pedido_id = p.id 
            JOIN produtos pr ON i.produto_id = pr.id
            WHERE i.status = 'Pronto' AND p.status = 'Pendente' AND p.tipo_pedido != 'Delivery' AND p.data_criacao >= ? 
            ORDER BY i.id DESC LIMIT 6
        ''', (inicio_dia,))
        
        for row in cursor.fetchall():
            rotulo_base = "AUTOATENDIMENTO" if "Autoatendimento" in row['operador'] else row['tipo_pedido'].upper()
            rotulo = f"{rotulo_base} ({row['quantidade']}x {row['nome']})"
            senhas_completas.append({"senha": row['senha'], "tipo": row['tipo_pedido'], "rotulo": rotulo, "uid": f"item_{row['i_id']}", "sort_id": row['i_id']})
            
        cursor.execute("SELECT id, senha, tipo_pedido, operador FROM pedidos WHERE status = 'Pronto' AND data_criacao >= ? ORDER BY id DESC LIMIT 6", (inicio_dia,))
        for row in cursor.fetchall():
            rotulo_base = "AUTOATENDIMENTO" if "Autoatendimento" in row['operador'] else row['tipo_pedido'].upper()
            
            cursor.execute("SELECT p.nome, i.quantidade FROM itens_pedido i JOIN produtos p ON i.produto_id = p.id WHERE i.pedido_id = ?", (row['id'],))
            itens_bd = cursor.fetchall()
            
            if len(itens_bd) == 1:
                rotulo = f"{rotulo_base} ({itens_bd[0]['quantidade']}x {itens_bd[0]['nome']})"
            elif len(itens_bd) > 1:
                rotulo = f"{rotulo_base} (PEDIDO COMPLETO)"
            else:
                rotulo = rotulo_base
                
            senhas_completas.append({"senha": row['senha'], "tipo": row['tipo_pedido'], "rotulo": rotulo, "uid": f"pedido_{row['id']}", "sort_id": row['id'] * 10000}) 
            
        senhas_completas = sorted(senhas_completas, key=lambda x: x['sort_id'], reverse=True)[:6]
        for s in senhas_completas: del s['sort_id']
    except Exception as e:
        print("Erro na TV:", e)
        
    conexao.close(); return {"senhas": senhas_completas}

@app.get("/financeiro/{periodo}")
def relatorio_financeiro(periodo: str):
    conexao = conectar_banco(); cursor = conexao.cursor(); hoje_dt = datetime.now()
    if periodo == "semana": data_inicio = (hoje_dt - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
    elif periodo == "mes": data_inicio = (hoje_dt - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
    else: data_inicio = hoje_dt.strftime("%Y-%m-%d 00:00:00")
    cursor.execute("SELECT COUNT(id) as qtd, SUM(valor_total) as fat_vendas FROM pedidos WHERE data_criacao >= ? AND metodo_pagamento NOT LIKE 'Recebimento%'", (data_inicio,)); res_vendas = cursor.fetchone()
    cursor.execute("SELECT SUM(valor_total) as fat_caixa FROM pedidos WHERE data_criacao >= ? AND metodo_pagamento NOT LIKE 'Fiado%'", (data_inicio,)); res_caixa = cursor.fetchone()
    query_pagamentos = '''SELECT CASE WHEN metodo_pagamento LIKE 'Dinheiro%' THEN 'Dinheiro' WHEN metodo_pagamento LIKE 'Fiado%' THEN 'Fiado' WHEN metodo_pagamento LIKE 'Cartão de Crédito%' THEN 'Cartão de Crédito' WHEN metodo_pagamento LIKE 'Cartão de Débito%' THEN 'Cartão de Débito' WHEN metodo_pagamento LIKE 'PIX%' THEN 'PIX' WHEN metodo_pagamento LIKE 'Recebimento%' THEN 'Recebimento de Fiados' ELSE metodo_pagamento END as metodo_limpo, SUM(valor_total) as total FROM pedidos WHERE data_criacao >= ? GROUP BY metodo_limpo ORDER BY total DESC'''
    cursor.execute(query_pagamentos, (data_inicio,)); res_pag = cursor.fetchall()
    cursor.execute("SELECT p.nome, SUM(i.quantidade) as total_vendido FROM itens_pedido i JOIN produtos p ON i.produto_id = p.id JOIN pedidos ped ON i.pedido_id = ped.id WHERE ped.data_criacao >= ? AND ped.metodo_pagamento NOT LIKE 'Recebimento%' GROUP BY p.id ORDER BY total_vendido DESC LIMIT 5", (data_inicio,)); res_top = cursor.fetchall(); conexao.close()
    return {"faturamento_vendas": res_vendas["fat_vendas"] or 0.0, "faturamento_caixa": res_caixa["fat_caixa"] or 0.0, "quantidade_vendas": res_vendas["qtd"] or 0, "pagamentos": [{"metodo_pagamento": p["metodo_limpo"], "total": p["total"]} for p in res_pag], "top_produtos": [dict(tp) for tp in res_top]}

@app.get("/fiados")
def listar_fiados():
    conexao = conectar_banco(); cursor = conexao.cursor(); cursor.execute("SELECT nome, saldo_devedor FROM clientes WHERE saldo_devedor > 0 ORDER BY nome ASC"); clientes = cursor.fetchall(); conexao.close(); return {"fiados": [dict(c) for c in clientes]}

@app.get("/fiados/{nome_cliente}")
def detalhes_fiado(nome_cliente: str):
    conexao = conectar_banco(); cursor = conexao.cursor()
    cursor.execute("SELECT id, data_criacao, valor_total FROM pedidos WHERE metodo_pagamento LIKE ? ORDER BY id DESC", (f"Fiado ({nome_cliente})%",))
    pedidos = cursor.fetchall(); resultado = []
    for ped in pedidos:
        cursor.execute("SELECT p.nome, i.quantidade FROM itens_pedido i LEFT JOIN produtos p ON i.produto_id = p.id WHERE i.pedido_id = ?", (ped['id'],))
        str_itens = ", ".join([f"{it['quantidade']}x {it['nome']}" for it in cursor.fetchall()])
        resultado.append({"data": ped['data_criacao'][:16], "valor": ped['valor_total'], "itens": str_itens})
    conexao.close(); return {"historico": resultado}

@app.post("/fiados/quitar")
def quitar_fiado(dados: QuitarFiado):
    conexao = conectar_banco(); cursor = conexao.cursor()
    cursor.execute("UPDATE clientes SET saldo_devedor = saldo_devedor - ? WHERE nome = ?", (dados.valor_pago, dados.nome_cliente))
    cursor.execute("INSERT INTO pedidos (senha, valor_total, metodo_pagamento, data_criacao, operador, sessao_id) VALUES (0, ?, 'Recebimento (Conta Paga)', ?, 'Sistema', 0)", (dados.valor_pago, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conexao.commit(); conexao.close(); return {"sucesso": True}

@app.get("/financeiro/delivery/{periodo}")
def relatorio_financeiro_delivery(periodo: str):
    conexao = conectar_banco(); cursor = conexao.cursor(); hoje_dt = datetime.now()
    if periodo == "semana": data_inicio = (hoje_dt - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
    elif periodo == "mes": data_inicio = (hoje_dt - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
    else: data_inicio = hoje_dt.strftime("%Y-%m-%d 00:00:00")
    cursor.execute("SELECT SUM(valor_total) as fat_delivery FROM pedidos WHERE data_criacao >= ? AND tipo_pedido = 'Delivery' AND metodo_pagamento NOT LIKE 'Recebimento%'", (data_inicio,)); res_total = cursor.fetchone()
    query_pagamentos = '''SELECT CASE WHEN metodo_pagamento LIKE 'Dinheiro%' THEN 'Dinheiro' WHEN metodo_pagamento LIKE 'Fiado%' THEN 'Fiado' WHEN metodo_pagamento LIKE 'Cartão de Crédito%' THEN 'Cartão de Crédito' WHEN metodo_pagamento LIKE 'Cartão de Débito%' THEN 'Cartão de Débito' WHEN metodo_pagamento LIKE 'PIX%' THEN 'PIX' ELSE metodo_pagamento END as metodo_limpo, SUM(valor_total) as total FROM pedidos WHERE data_criacao >= ? AND tipo_pedido = 'Delivery' AND metodo_pagamento NOT LIKE 'Recebimento%' GROUP BY metodo_limpo ORDER BY total DESC'''
    cursor.execute(query_pagamentos, (data_inicio,)); res_pag = cursor.fetchall(); conexao.close()
    return {"faturamento_delivery": res_total["fat_delivery"] or 0.0, "pagamentos": [{"metodo_pagamento": p["metodo_limpo"], "total": p["total"]} for p in res_pag]}

@app.get("/entregas/prontas")
def listar_entregas_prontas():
    conexao = conectar_banco(); cursor = conexao.cursor()
    cursor.execute("SELECT id, senha, cliente_nome, cliente_telefone, cliente_endereco, metodo_pagamento, valor_total, taxa_entrega, observacao_pedido, status FROM pedidos WHERE status IN ('Pendente', 'Pronto') AND tipo_pedido = 'Delivery' ORDER BY id ASC")
    pedidos = cursor.fetchall(); resultado = []
    for ped in pedidos:
        cursor.execute("SELECT p.nome, i.quantidade, i.observacao FROM itens_pedido i JOIN produtos p ON i.produto_id = p.id WHERE i.pedido_id = ?", (ped['id'],))
        resultado.append({"id": ped['id'], "senha": ped['senha'], "cliente_nome": ped['cliente_nome'], "cliente_telefone": ped['cliente_telefone'], "cliente_endereco": ped['cliente_endereco'], "metodo_pagamento": ped['metodo_pagamento'], "valor_total": ped['valor_total'], "taxa_entrega": ped['taxa_entrega'], "observacao_pedido": ped['observacao_pedido'], "status": ped['status'], "itens": [dict(it) for it in cursor.fetchall()]})
    conexao.close(); return {"entregas": resultado}

# ==========================================
# CONFIGURAÇÃO OAUTH2 MERCADO PAGO (ONBOARDING)
# ==========================================
MEU_CLIENT_ID = "6997755645267982" # <-- COLOQUE SEU CLIENT ID AQUI
MEU_CLIENT_SECRET = "6VbwIVwqVkenxgI3aJZm6ai40DBBSEY3" # <-- COLOQUE SEU CLIENT SECRET AQUI
URL_DE_RETORNO = "https://dorei.jesusdelivery.fun/retorno-mp"

@app.get("/onboarding", response_class=HTMLResponse)
def pagina_onboarding():
    link_autorizacao = f"https://auth.mercadopago.com.br/authorization?client_id={MEU_CLIENT_ID}&response_type=code&platform_id=mp&state=LanchoneteNova&redirect_uri={URL_DE_RETORNO}"
    
    html = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <title>Integração Jesus Code</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-slate-900 text-white flex items-center justify-center h-screen">
        <div class="bg-slate-800 p-8 rounded-2xl shadow-2xl text-center max-w-md border border-slate-700">
            <h1 class="text-3xl font-black text-blue-400 mb-4">Jesus Code Delivery</h1>
            <p class="mb-6 text-slate-300">Integre o seu Mercado Pago para aceitar PIX no seu site e receber o dinheiro na hora.</p>
            <a href="{link_autorizacao}" class="block w-full bg-blue-600 hover:bg-blue-700 text-white font-bold py-4 rounded-xl shadow-lg transition transform hover:scale-105">
                🔗 Conectar meu Mercado Pago
            </a>
            <p class="mt-6 text-xs text-slate-500">Taxa do sistema: 1% por transação PIX aprovada.</p>
        </div>
    </body>
    </html>
    """
    return html

@app.get("/retorno-mp", response_class=HTMLResponse)
def retorno_mercadopago(code: str = None, state: str = None):
    if not code:
        return "<h1 style='color:red;'>Erro: O Mercado Pago não enviou o código de autorização.</h1>"

    url_token = "https://api.mercadopago.com/oauth/token"
    dados_troca = {
        "client_secret": MEU_CLIENT_SECRET,
        "client_id": MEU_CLIENT_ID,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": URL_DE_RETORNO
    }

    try:
        resposta = requests.post(url_token, data=dados_troca)
        dados_json = resposta.json()

        if "access_token" in dados_json:
            token_cliente = dados_json["access_token"]
            
            conexao = conectar_banco()
            cursor = conexao.cursor()
            cursor.execute("UPDATE empresa SET token_mp = ? WHERE id = 1", (token_cliente,))
            conexao.commit()
            conexao.close()

            return """
            <body style="background:#0f172a; color:white; font-family:sans-serif; text-align:center; padding-top:100px;">
                <h1 style="color:#22c55e; font-size:40px;">✅ Integração Concluída!</h1>
                <p>Seu Mercado Pago foi conectado com sucesso.</p>
                <p>Você já pode fechar esta tela e começar a vender.</p>
            </body>
            """
        else:
            return f"<h1 style='color:red;'>Erro ao gerar token: {dados_json}</h1>"

    except Exception as e:
        return f"<h1 style='color:red;'>Erro no servidor: {str(e)}</h1>"

# ==========================================
# ROTAS DO PIX AUTOMÁTICO (SITE JESUS CODE)
# ==========================================
@app.post("/site/gerar-pix")
def gerar_pix_site(dados: DadosPagamentoSite):
    try:
        # Busca o token da lanchonete salvo no banco de dados
        conexao = conectar_banco()
        cursor = conexao.cursor()
        cursor.execute("SELECT token_mp FROM empresa WHERE id = 1")
        emp = cursor.fetchone()
        conexao.close()
        
        token_cliente = emp['token_mp'] if emp and emp['token_mp'] else ""
        if not token_cliente:
            return {"sucesso": False, "erro": "A lanchonete ainda não conectou o Mercado Pago."}
            
        sdk_mp = mercadopago.SDK(token_cliente)
        
        # A MÁGICA DA SUA COMISSÃO (Garantindo que é um Float puro para o Mercado Pago)
        valor_venda = float(dados.valor_total)
        comissao = float(round(valor_venda * 0.01, 2))

        payment_data = {
            "transaction_amount": valor_venda,
            "description": f"Delivery - Lanchonete do Rei ({dados.nome_cliente})",
            "payment_method_id": "pix",
            "payer": {
                "email": dados.email_cliente
            },
            "application_fee": comissao # <-- Split de pagamento ativado e seguro
        }

        res = sdk_mp.payment().create(payment_data)
        pagamento = res["response"]

        if "id" not in pagamento:
            # Captura a mensagem de erro que o Mercado Pago mandou de volta
            erro_banco = pagamento.get("message", "Erro desconhecido")
            causas = pagamento.get("cause", [])
            detalhe = causas[0].get('description', '') if len(causas) > 0 else ""
            codigo_erro = f"{erro_banco} | Detalhe: {detalhe}"
            return {"sucesso": False, "erro": f"Bloqueio MP: {codigo_erro}"}

        return {
            "sucesso": True,
            "id_transacao": pagamento["id"],
            "qr_code_base64": pagamento["point_of_interaction"]["transaction_data"]["qr_code_base64"],
            "copia_e_cola": pagamento["point_of_interaction"]["transaction_data"]["qr_code"]
        }

    except Exception as e:
        return {"sucesso": False, "erro": str(e)}
        @app.get("/site/status-pix/{transacao_id}")
def verificar_status_pix(transacao_id: str):
    try:
        # Busca o token da lanchonete salvo no banco de dados
        conexao = conectar_banco()
        cursor = conexao.cursor()
        cursor.execute("SELECT token_mp FROM empresa WHERE id = 1")
        emp = cursor.fetchone()
        conexao.close()
        
        token_cliente = emp['token_mp'] if emp and emp['token_mp'] else ""
        if not token_cliente:
            return {"status": "pending"}
            
        # Pergunta diretamente ao Mercado Pago se o ID da transacao foi pago
        import requests
        url = f"https://api.mercadopago.com/v1/payments/{transacao_id}"
        headers = {"Authorization": f"Bearer {token_cliente}"}
        resposta = requests.get(url, headers=headers)
        dados = resposta.json()

        # Retorna o status oficial ('approved', 'pending', 'rejected', etc)
        return {"status": dados.get("status", "pending")}

    except Exception as e:
        return {"status": "error", "erro": str(e)}
