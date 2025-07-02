from flask import Flask, request, render_template, jsonify
from flask_cors import CORS
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.tools import Toolkit
import requests
import os
from dotenv import load_dotenv
import time
import boto3
from typing import Optional
from pydantic import BaseModel, Field

from agno.memory.v2.db.sqlite import SqliteMemoryDb
from agno.memory.v2.memory import Memory
from agno.storage.sqlite import SqliteStorage

from agno.tools.postgres import PostgresTools
from agno.tools.sql import SQLTools

from rich.pretty import pprint
from urllib.parse import quote
import sqlite3
from datetime import datetime
import json
from openai import OpenAI

import psycopg2
from psycopg2.extras import RealDictCursor

# para atualizar o link do NGROK no twilio
# https://console.twilio.com/us1/develop/sms/try-it-out/whatsapp-learn?frameUrl=%2Fconsole%2Fsms%2Fwhatsapp%2Flearn%3Fx-target-region%3Dus1

load_dotenv()
app = Flask(__name__)
# Habilita CORS para todas as rotas e origens
# Ou se quiser permitir apenas uma origem específica:
# CORS(app, origins=["http://localhost:3000"])
CORS(app)

# UserId for the memories
session_id = "baum"
# Database file for memory and storage
db_file = "tmp/memory.db"

# Initialize memory
memory = Memory(
    # Use any model for creating memories
    model=OpenAIChat(id="gpt-4.1"),
    db=SqliteMemoryDb(table_name="user_memories", db_file=db_file),
)

# Initialize storage
storage = SqliteStorage(table_name="agent_sessions", db_file=db_file)

# postgresql://<username>:<password>@<host>:<port>/<database>
db_url = f"postgresql+psycopg2://{os.getenv("POSTGRES_DB_USER")}:{quote(os.getenv("POSTGRES_DB_PASSWORD"))}@{os.getenv("POSTGRES_DB_HOST")}:{os.getenv("POSTGRES_DB_PORT")}/{os.getenv("POSTGRES_DB_NAME")}"
db_url2 = f"postgresql://{os.getenv("POSTGRES_DB_USER")}:{quote(os.getenv("POSTGRES_DB_PASSWORD"))}@{os.getenv("POSTGRES_DB_HOST")}:{os.getenv("POSTGRES_DB_PORT")}/{os.getenv("POSTGRES_DB_NAME")}"

# Cliente Twilio para enviar mídia
twilio_client = Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))

# Definindo os schemas para os parâmetros das tools
class LerImagemInput(BaseModel):
    filename: str = Field(description="Nome do arquivo da imagem (ex: img_1749692471.jpg)")

class ListarImagensInput(BaseModel):
    pass  # Não precisa de parâmetros

# Inicializa cliente S3 globalmente
def init_s3_client():
    bucket = os.getenv("BUCKET_NAME")
    prefix = os.getenv("PREFIX", "")
    endpoint = os.getenv("S3_ENDPOINT")

    s3 = boto3.client(
        "s3",
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        endpoint_url=endpoint
    )
    base_url = f"https://{bucket}.s3.fr-par.scw.cloud/{bucket}"
    return s3, bucket, prefix, base_url


def audio_to_text(audio_url: str) -> str:
    """ Tool para que recebe uma url de audio e retorna o texto correspondente"""
    print(f">>>>> Tool audio_to_text foi ativado para a url:{audio_url}")
    try:
        # Etapa 1: Baixar o áudio
        #url = "https://exemplo.com/audio.ogg"
        #res = requests.get(audio_url)
        res = requests.get(audio_url, auth=(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")))
        with open("audio.ogg", "wb") as f:
            f.write(res.content)

        # Envie o arquivo para transcrição
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        with open("audio.ogg", "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                file=audio_file,
                model="whisper-1"
            )
        print(transcript.text)
        return transcript.text
    
    except Exception as e:
        print(f"❌ Erro ao converter audio em texto: {str(e)}")
        return f"❌ Erro ao converter audio em texto: {str(e)}"
    

def salvar_imagem_s3(image_url: str) -> str:
    """Tool que recebe um link de uma imagem para recuperar seu conteudo e salvar a imagem no S3"""
    print(f">>>>> Tool salvar_imagem_s3 foi ativado para a url:{image_url}")
    try:
        s3, bucket, prefix, base_url = init_s3_client()
        
        img_data = requests.get(image_url).content
        timestamp = int(time.time())
        filename = f"img_{timestamp}.jpg"
        key = prefix + filename
        
        # Salva com ACL público para que WhatsApp possa acessar
        s3.put_object(
            Bucket=bucket, 
            Key=key, 
            Body=img_data,
            ACL='public-read',  # Importante para acesso público
            ContentType='image/jpeg'  # Define o tipo de conteúdo
        )
        return f"Imagem salva com sucesso. Novo link é: {base_url}/{key}"
    except Exception as e:
        print(f"❌ Erro ao salvar imagem: {str(e)}")
        return f"❌ Erro ao salvar imagem: {str(e)}"


def exibir_imagem_s3(image_url: str, from_number: str, to_number: str) -> str:
    """Tool que Envia imagem para o whatsapp"""
    print(">>>>> Tool exibir_imagem_s3 foi ativado")
    try:
        message = twilio_client.messages.create(
            body="",
            media_url=[image_url],
            from_=from_number,
            to=to_number
        )   
        return "Mensagem enviada com sucesso"
    except Exception as e:
        return f"❌ Erro ao criar a mensagem: {str(e)}"
        

def enviar_imagem_whatsapp(to_number, from_number, image_url, caption=""):
    """Envia imagem via WhatsApp usando Twilio"""
    try:
        message = twilio_client.messages.create(
            body=caption,
            media_url=[image_url],
            from_=from_number,
            to=to_number
        )
        return True
    except Exception as e:
        print(f"❌ Erro ao enviar imagem: {str(e)}")
        return False


agent = Agent(
    model=OpenAIChat(id="gpt-4o"),
    tools=[exibir_imagem_s3, salvar_imagem_s3, audio_to_text, SQLTools(db_url=db_url)],
    #user_id=xxxxxxx,  # ID específico do usuário
    
    # Store memories in a database
    memory=memory,
    # Give the Agent the ability to update memories
    enable_agentic_memory=True,
    # OR - Run the MemoryManager after each response
    enable_user_memories=True,
    # Store the chat history in the database
    storage=storage,
    # Add the chat history to the messages
    add_history_to_messages=True, 
    # Number of history runs
    num_history_runs=5, # Inclui últimas 5 interações no contexto
    markdown=True,
    read_chat_history=True,  # Permite ao agente ler histórico completo quando necessário
    show_tool_calls=True,
    system_message="""
        # CONTEXTO
        Você é um assistente via WhatsApp que armazena descrições de imagens em um banco de dados SQL e solicita ao usuário uma ou mais imagens associadas a essa descrição.
        Ao receber imagens, você armazena a imagem no S3 e você precisa associar o link dessa imagem com a descrição do banco de dados que ja foi gravada.
        
        # TAREFA
        Caso o usuário solicite, você 
        - recupera a descrição e as imagens associadas
        - criam uma descrição
        - Associa uma imagem que o usuário enviar a uma descrição
        - Adiciona uma nova descrição e sua imagem associada quando receber uma imagem que já vem com uma descrição
        
        # INSTRUÇÃO
        
        ## Você tem acesso às seguintes ferramentas:
        - exibir_imagem_s3: Para enviar ao Whatsapp do usuário uma imagem que ele solicitou. Envie para a tool o image_url, to_number e from_number. O "to_number" será o numero "From" da mensagem que você recebeu. O "from_number" será o numero "To" da mensagem que você recebeu.
        - salvar_imagem_s3: SEMPRE que você receber uma mensagem com imagem deverá usar essa ferramenta para salvar a imagem. Envie o link da imagem e a tool salva para você e te retorna o novo link da imagem salva
        - postgres_tools: acesso ao banco de dados SQL para criar novos registros com descrições e com um ou mais links associados a essa descrição
        - audio_to_text: se o usuário mandar um audio, converta em texto enviando a url para a tool. Ela vai retornar o texto. 

        ## Tabelas SQL:
        - 'table_descricao' contem os registros de descrição: campo 'descricao' contem a descrição da imagem e o campo 'from_number' contem o número de telefone (apenas números) de quem enviou a mensagem que é o campo "From" da mensagem
        - 'table_image_link' contem os registros dos links de acesso das imagens: campo 'image_link' contem o link para a imagem e o campo id_descricao faz o relacionamento com a tabela 'table_descriçao'
        
        ## Outras instruções
        - Quando alguem perguntar por uma imagem, não importa quem filtrou, então quando enviar o SQL não coloque o campo 'from_number' na clausula WHERE 
        - Ao fazer um INSERT no banco de dados, utilize "RETURNING id" no final da query para retornar o id do registro criado e evitar erros. 
        - Sempre que atualizar algo na tabela 'table_descricao' também atualizar o campo 'from_number'
        - No banco de dados SQL, Uma descrição de imagem pode ter mais de um link de imagem associada
        - Sempre que inserir uma descrição, iniciar a frase com letra Maiúscula
        
        ## MEMORIA
        - Você tem memória das últimas 5 interações com o usuário através do sistema de memória nativa do agno.
        - Você pode criar, atualizar e buscar memórias sobre preferências do usuário automaticamente.
    """
)

# Pega um numero de telefone e retorna só os digitos
def clean_number(phone_number):
    return ''.join(filter(str.isdigit, phone_number))

@app.route("/whatsapp", methods=['POST'])
def whatsapp():
    
    msg = request.form.get('Body', '')
    from_number = request.form.get('From')
    print(f"\n>>>>> User: {msg}")
    
    #print(dict(request.form))
    dados_json = json.dumps(dict(request.form), ensure_ascii=False)
    print(dados_json)
       
    response = agent.run(str(dados_json), user_id=clean_number(from_number), session_id=session_id)
    print(f"\n>>>>> Agent: {response.content}")
    
    resp = MessagingResponse() 
    resp.message(response.content)
                    
    return str(resp)


# NOVA ROTA: Para limpar memória (opcional)
@app.route("/clear-memory", methods=['POST'])
def clear_memory():
    """Limpa a memória/storage do agente"""
    try:
        # Para agente global
        memory.clear_session("whatsapp_global")
        return "✅ Memória global limpa com sucesso"
    except Exception as e:
        return f"❌ Erro ao limpar memória: {str(e)}"


# NOVA ROTA: Para limpar memória de usuário específico
@app.route("/clear-user-memory/<session>", methods=['POST'])
def clear_user_memory(session):
    """Limpa a memória de um usuário específico"""
    try:
        session_id = session
        memory.clear_session(session_id)
        return f"✅ Memória do usuário {session} limpa com sucesso"
    except Exception as e:
        return f"❌ Erro ao limpar memória: {str(e)}"


# NOVA ROTA: Para ver histórico da memória (opcional - para debug)
@app.route("/memory-status/<user_number>", methods=['GET'])
def memory_status(user_number):
    """Mostra status da memória/storage"""
    try:
        # Lista todas as sessões no storage
        print(">>>> MEMORIA:")
        pprint(memory.get_user_memories(user_id=clean_number(user_number)))
        return memory.get_user_memories(user_id=clean_number(user_number))
    
    except Exception as e:
        return f"❌ Erro ao obter status: {str(e)}"

@app.route("/agent-messages", methods=['GET'])
def agent_messages():
    """Mostra status da memória/storage"""
    try:
        # Lista todas as mensagens do storage
        print(">>>> MEMORIA DE MENSAGENS:")
        
        messages = agent.get_messages_for_session()
        
        response = """
        <style>
            table { border-collapse: collapse; width: 100%; }
            th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
            th { background-color: #f2f2f2; font-weight: bold; }
            tr:nth-child(even) { background-color: #f9f9f9; }
        </style>
        <h2>Mensagens do Agente</h2>
        <table>
            <tr>
                <th>#</th>
                <th>Role</th>
                <th>Content</th>
            </tr>
        """
        for item in messages:
            #response = response + f"<u>{datetime.fromtimestamp(item.created_at)}</u> <strong>role</strong> = {item.role} | <strong>content</strong> = {item.content}  <br><br>"
            response += f"""
            <tr>
                <td>{datetime.fromtimestamp(item.created_at)}</td>
                <td>{item.role}</td>
                <td>{item.content}</td>
            </tr>
            """
        
        return response
    
    except Exception as e:
        return f"❌ Erro ao obter status: {str(e)}"
    
@app.route("/agent-messages-full", methods=['GET'])
def agent_messages_full():
    """Mostra status da memória/storage"""
    try:
        # Lista todas as mensagens do storage
        print(">>>> MEMORIA DE MENSAGENS:")
        
        messages = agent.get_messages_for_session()
        
        response = """
        <style>
            table { border-collapse: collapse; width: 100%; }
            th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
            th { background-color: #f2f2f2; font-weight: bold; }
            tr:nth-child(even) { background-color: #f9f9f9; }
        </style>
        <h2>Mensagens do Agente</h2>
        <table>
            <tr>
                <th>#</th>
                <th>Role</th>
                <th>Content</th>
            </tr>
        """
        for item in messages:
            #response = response + f"<u>{datetime.fromtimestamp(item.created_at)}</u> <strong>role</strong> = {item.role} | <strong>content</strong> = {item.content}  <br><br>"
            response += f"""
            <tr>
                <td>{datetime.fromtimestamp(item.created_at)}</td>
                <td>{item.role}</td>
                <td>{item}</td>
            </tr>
            """
        
        return response
    
    except Exception as e:
        return f"❌ Erro ao obter status: {str(e)}"


@app.route("/report", methods=['GET'])
def show_report():
    return render_template("report.html")

@app.route("/get-report-data", methods=['GET'])
def get_report_data():
    try:
        # Conectar ao PostgreSQL
        conn = psycopg2.connect(db_url2)
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Query SQL para buscar descrições com suas imagens
        query = """
        SELECT 
            d.id,
            d.descricao,
            COALESCE(
                JSON_AGG(
                    il.image_link 
                    ORDER BY il.created_at
                ) FILTER (WHERE il.image_link IS NOT NULL), 
                '[]'::json
            ) as images
        FROM table_descricao d
        LEFT JOIN table_image_link il ON d.id = il.id_descricao
        GROUP BY d.id, d.descricao
        ORDER BY d.created_at DESC;
        """
        
        cursor.execute(query)
        results = cursor.fetchall()
        
        # Processar os resultados
        data = []
        for row in results:
            # Se images vier como string JSON, fazer parse
            images = row['images']
            if isinstance(images, str):
                images = json.loads(images)
            elif images is None:
                images = []
            
            data.append({
                'id': row['id'],
                'descricao': row['descricao'],
                'images': images
            })
        
        # Fechar conexões
        cursor.close()
        conn.close()
        
        # Retornar JSON com headers CORS
        response = jsonify(data)
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
        response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        
        return response
        
    except psycopg2.Error as e:
        print(f"Erro no banco de dados: {e}")
        return jsonify({
            'error': 'Erro ao conectar com o banco de dados',
            'details': str(e)
        }), 500
        
    except Exception as e:
        print(f"Erro geral: {e}")
        return jsonify({
            'error': 'Erro interno do servidor',
            'details': str(e)
        }), 500


@app.route("/memory-content", methods=['GET'])
def memory_content():
    """Mostra o conteudo do banco de dados da memória/storage"""
    db_path = r'C:\Users\mauri\OneDrive\Mauricio Documents\python\vistoria\tmp\memory.db'

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    print('Conteúdo do campo memory da tabela user_memories:')
    cursor.execute('SELECT COUNT(*) FROM user_memories')
    user_memories_data = cursor.fetchall()
    print("oi")
    print(user_memories_data)
    
    print('Conteúdo do campo memory da tabela user_memories:')
    cursor.execute("SELECT created_at,updated_at  FROM agent_sessions")
    user_memories_sessions = cursor.fetchall()
    for created_at, updated_at in user_memories_sessions:
        print(f"Criado: {datetime.fromtimestamp(created_at)} | Updated: {datetime.fromtimestamp(updated_at)}")

    #for row in user_memories_data:
    #    print(row[0])

    # PRAGMA table_info(agent_sessions);
    # (0, 'session_id', 'VARCHAR', 1, None, 1), 
    # (1, 'user_id', 'VARCHAR', 0, None, 0), 
    # (2, 'memory', 'JSON', 0, None, 0), 
    # (3, 'session_data', 'JSON', 0, None, 0), 
    # (4, 'extra_data', 'JSON', 0, None, 0), 
    # (5, 'created_at', 'INTEGER', 0, None, 0), 
    # (6, 'updated_at', 'INTEGER', 0, None, 0), 
    # (7, 'agent_id', 'VARCHAR', 0, None, 0), 
    # (8, 'agent_data', 'JSON', 0, None, 0), 
    # (9, 'team_session_id', 'VARCHAR', 0, None, 0)

    conn.close()
    return user_memories_data

if __name__ == "__main__":
    app.run(port=5000, debug=True)