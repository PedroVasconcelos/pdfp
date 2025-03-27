from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware
import pdfplumber
import pandas as pd
import pytesseract
from pdf2image import convert_from_bytes
from io import BytesIO
import os
import re
import logging
from typing import List, Dict, Any
from datetime import datetime
import tempfile
import shutil
import subprocess
import magic

# Configuração de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Verificar se o Tesseract está instalado
try:
    subprocess.run(['tesseract', '--version'], capture_output=True, text=True)
    logger.info("Tesseract está instalado e funcionando")
except FileNotFoundError:
    logger.error("Tesseract não está instalado!")
    # Não vamos falhar aqui, apenas logar o erro

# Configurações
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_EXTENSIONS = {'.pdf'}
UPLOAD_DIR = "uploads"
TEMP_DIR = "temp"

# Criar diretórios necessários
for directory in [UPLOAD_DIR, TEMP_DIR]:
    try:
        if not os.path.exists(directory):
            os.makedirs(directory)
            logger.info(f"Diretório {directory} criado")
    except Exception as e:
        logger.error(f"Erro ao criar diretório {directory}: {str(e)}")
        # Não vamos falhar aqui, apenas logar o erro

app = FastAPI(
    title="PDF Processor API",
    description="API para processamento de PDFs e extração de dados",
    version="1.0.0"
)

# Configuração do CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Adicionar middleware de segurança
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["*"]
)

# Montar arquivos estáticos
try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
    logger.info("Diretório static montado com sucesso")
except Exception as e:
    logger.error(f"Erro ao montar diretório static: {str(e)}")
    # Não vamos falhar aqui, apenas logar o erro

# Montar a pasta uploads para servir os arquivos
try:
    app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
    logger.info("Diretório uploads montado com sucesso")
except Exception as e:
    logger.error(f"Erro ao montar diretório uploads: {str(e)}")
    # Não vamos falhar aqui, apenas logar o erro

@app.get("/")
async def read_root():
    try:
        return FileResponse("static/index.html")
    except Exception as e:
        logger.error(f"Erro ao servir index.html: {str(e)}")
        raise HTTPException(status_code=500, detail="Erro ao carregar página inicial")

def validate_file(file: UploadFile) -> None:
    """
    Valida o arquivo enviado.
    """
    logger.debug(f"Validando arquivo: {file.filename}")
    
    # Verificar extensão
    if not file.filename.lower().endswith(tuple(ALLOWED_EXTENSIONS)):
        raise HTTPException(
            status_code=400,
            detail=f"Tipo de arquivo não permitido. Tipos permitidos: {', '.join(ALLOWED_EXTENSIONS)}"
        )
    
    # Verificar tamanho do arquivo
    file.file.seek(0, 2)  # Ir para o final do arquivo
    size = file.file.tell()  # Obter posição atual (tamanho)
    file.file.seek(0)  # Voltar para o início
    
    if size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"Arquivo muito grande. Tamanho máximo permitido: {MAX_FILE_SIZE/1024/1024}MB"
        )
    
    logger.debug(f"Arquivo validado com sucesso. Tamanho: {size/1024/1024:.2f}MB")

def extract_data_from_text(text: str) -> Dict[str, Any]:
    """
    Extrai dados relevantes do texto usando expressões regulares.
    """
    # Log do texto recebido para debug
    logger.info("Texto recebido do PDF:")
    logger.info(text[:500] + "..." if len(text) > 500 else text)
    
    # Inicializar dicionário com listas vazias
    dados = {
        "NOME": [],
        "CPF": [],
        "PIS/NIT": [],
        "FUNÇÃO": [],
        "DATA DE ADMISSÃO": [],
        "LOTAÇÃO": [],
        "CARGA HORARIA": [],
        "V. BRUTO": [],
        "DESCONTO INSS": [],
        "DESCONTO IR": [],
        "OUT. DESC.": [],
        "V. LIQUIDO": [],
        "VÍNCULO": []
    }
    
    # Extrair a lotação do cabeçalho do PDF
    lotacao_match = re.search(r'Centro de Custo:\s*\d+\s*-\s*([^-\n]+)', text)
    lotacao_especifica = lotacao_match.group(1).strip() if lotacao_match else ""
    lotacao_completa = f"{lotacao_especifica} - UNCISAL" if lotacao_especifica else "UNCISAL"
    logger.debug(f"Lotação encontrada: {lotacao_completa}")
    
    # Dividir o texto em blocos por funcionário
    funcionarios = text.split("Mat.")
    logger.info(f"Encontrados {len(funcionarios)-1} funcionários no texto")
    
    for bloco in funcionarios[1:]:  # Ignorar o primeiro split que é o cabeçalho
        try:
            # Extrair nome e matrícula
            nome_match = re.search(r'(\d+)\s+([A-ZÀ-Ú\s]+?)\s+(\d{3}\.\d{3}\.\d{3}-\d{2})', bloco)
            if nome_match:
                matricula = nome_match.group(1)
                nome = nome_match.group(2).strip()
                cpf = nome_match.group(3)
                
                logger.debug(f"Processando funcionário: {nome} (Mat: {matricula}, CPF: {cpf})")
                
                dados["NOME"].append(nome)
                dados["CPF"].append(cpf)
                
                # Extrair PIS/NIT
                pis_match = re.search(r'Pis/Pasep:\s*(\d{3}\.\d{5}\.\d{2}-\d)', bloco)
                if pis_match:
                    pis = pis_match.group(1)
                    dados["PIS/NIT"].append(pis)
                    logger.debug(f"PIS/NIT encontrado: {pis}")
                else:
                    dados["PIS/NIT"].append("")
                    logger.debug("PIS/NIT não encontrado")
                
                # Extrair data de admissão
                data_match = re.search(r'Admissão\s+(\d{2}/\d{2}/\d{4})', bloco)
                if data_match:
                    data = data_match.group(1)
                    dados["DATA DE ADMISSÃO"].append(data)
                    logger.debug(f"Data de admissão encontrada: {data}")
                else:
                    dados["DATA DE ADMISSÃO"].append("")
                    logger.debug("Data de admissão não encontrada")
                
                # Extrair função
                funcao_match = re.search(r'Cargo:\s*([A-ZÀ-Ú\s]+?)(?=\s+Estabelecimento:|$|\n)', bloco)
                if funcao_match:
                    funcao = funcao_match.group(1).strip()
                    dados["FUNÇÃO"].append(funcao)
                    logger.debug(f"Função encontrada: {funcao}")
                else:
                    dados["FUNÇÃO"].append("")
                    logger.debug("Função não encontrada")
                
                # Adicionar lotação completa
                dados["LOTAÇÃO"].append(lotacao_completa)
                
                # Extrair carga horária
                carga_match = re.search(r'Horas Mensais:\s*(\d+)', bloco)
                if carga_match:
                    carga = int(carga_match.group(1))
                    dados["CARGA HORARIA"].append(carga)
                    logger.debug(f"Carga horária encontrada: {carga}")
                else:
                    dados["CARGA HORARIA"].append(0)
                    logger.debug("Carga horária não encontrada")
                
                # Calcular valor bruto (soma dos proventos)
                proventos = []
                for linha in bloco.split('\n'):
                    if "SALÁRIO BASE" in linha or "PLANTÃO NOTURNO" in linha:
                        valor_match = re.search(r'\d+(?:\.\d+)*,\d{2}$', linha.strip())
                        if valor_match:
                            valor_str = valor_match.group()
                            valor = float(valor_str.replace('.', '').replace(',', '.'))
                            proventos.append(valor)
                            logger.debug(f"Provento encontrado: {valor_str}")
                
                v_bruto = sum(proventos)
                dados["V. BRUTO"].append(f"{v_bruto:.2f}".replace('.', ','))
                logger.debug(f"Valor bruto calculado: {v_bruto}")
                
                # Extrair INSS (valor após "INSS 11.00")
                inss_match = re.search(r'INSS\s+11\.00\s+(\d+(?:\.\d+)*,\d{2})', bloco)
                inss_valor = 0.0
                if inss_match:
                    inss_str = inss_match.group(1)
                    inss_valor = float(inss_str.replace('.', '').replace(',', '.'))
                    dados["DESCONTO INSS"].append(inss_str)
                    logger.debug(f"INSS encontrado: {inss_str}")
                else:
                    dados["DESCONTO INSS"].append("")
                    logger.debug("INSS não encontrado")
                
                # Não usamos mais o Base IRRF como desconto
                dados["DESCONTO IR"].append("0,00")
                
                # Total de descontos é igual ao INSS (não há outros descontos no exemplo)
                dados["OUT. DESC."].append(f"{inss_valor:.2f}".replace('.', ','))
                
                # Calcular valor líquido (V. BRUTO - INSS)
                v_liquido = v_bruto - inss_valor
                dados["V. LIQUIDO"].append(f"{v_liquido:.2f}".replace('.', ','))
                logger.debug(f"Valor líquido calculado: {v_liquido}")
                
                # Vínculo com PIS/NIT
                pis = dados["PIS/NIT"][-1]
                vinculo = f"AUTONOMOS Pis/Pasep: {pis}" if pis else "AUTONOMOS"
                dados["VÍNCULO"].append(vinculo)
                logger.debug(f"Vínculo definido: {vinculo}")
                
            else:
                logger.warning("Não foi possível encontrar nome, matrícula ou CPF no bloco")
                logger.debug(f"Conteúdo do bloco: {bloco[:200]}...")
                
        except Exception as e:
            logger.error(f"Erro ao processar funcionário: {str(e)}")
            continue
    
    # Verificar se encontrou algum dado
    if not dados["CPF"]:
        logger.warning("Nenhum funcionário encontrado no texto")
        return {campo: [""] for campo in dados.keys()}
    
    # Garantir que todas as listas tenham o mesmo tamanho
    max_len = max(len(v) for v in dados.values())
    for campo in dados:
        while len(dados[campo]) < max_len:
            dados[campo].append("")
    
    logger.info(f"Total de funcionários processados: {len(dados['CPF'])}")
    return dados

async def extract_text_from_pdf(pdf_file: UploadFile) -> str:
    """
    Extrai texto de um arquivo PDF.
    """
    try:
        # Ler o conteúdo do arquivo
        content = await pdf_file.read()
        text = ""
        
        # Log do início do arquivo para debug
        logger.info(f"Primeiros bytes do arquivo: {content[:20]}")
        
        # Verificar se o conteúdo é um PDF válido
        if not content.startswith(b'%PDF'):
            logger.error(f"Arquivo não é um PDF válido. Primeiros bytes: {content[:20]}")
            # Tentar detectar o tipo do arquivo
            try:
                file_type = magic.from_buffer(content[:2048])
                logger.error(f"Tipo do arquivo detectado: {file_type}")
            except Exception as e:
                logger.error(f"Erro ao detectar tipo do arquivo: {str(e)}")
            
            raise HTTPException(
                status_code=400,
                detail="O arquivo não é um PDF válido. Verifique se o arquivo está corrompido ou se é realmente um PDF."
            )
        
        # Criar um arquivo temporário para o PDF
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as temp_file:
            temp_file.write(content)
            temp_path = temp_file.name
        
        try:
            # Tentar extrair texto diretamente
            with pdfplumber.open(temp_path) as pdf:
                logger.info(f"PDF tem {len(pdf.pages)} páginas")
                for i, page in enumerate(pdf.pages, 1):
                    try:
                        # Tentar extrair texto com diferentes métodos
                        page_text = page.extract_text()
                        if not page_text:
                            # Tentar extrair tabelas
                            tables = page.extract_tables()
                            if tables:
                                for table in tables:
                                    page_text += "\n".join(["\t".join(row) for row in table]) + "\n"
                        
                        if page_text:
                            text += f"\n=== Página {i} ===\n{page_text}\n"
                            logger.info(f"Texto extraído da página {i}")
                        else:
                            logger.warning(f"Nenhum texto encontrado na página {i}")
                            # Tentar OCR apenas se o Tesseract estiver instalado
                            try:
                                images = convert_from_bytes(content, first_page=i, last_page=i)
                                for img in images:
                                    page_text = pytesseract.image_to_string(img, lang='por')
                                    if page_text:
                                        text += f"\n=== Página {i} (OCR) ===\n{page_text}\n"
                                        logger.info(f"Texto extraído com OCR da página {i}")
                            except Exception as e:
                                logger.error(f"Erro no OCR da página {i}: {str(e)}")
                    except Exception as page_error:
                        logger.error(f"Erro ao processar página {i}: {str(page_error)}")
                        continue
            
            if not text:
                logger.error("Nenhum texto foi extraído do PDF")
                raise HTTPException(
                    status_code=400,
                    detail="Não foi possível extrair texto do PDF. Verifique se o arquivo contém texto legível."
                )
            
            # Log do texto extraído para debug
            logger.info(f"Texto extraído: {text[:200]}...")
            return text
            
        finally:
            # Limpar o arquivo temporário
            try:
                os.unlink(temp_path)
            except Exception as e:
                logger.error(f"Erro ao remover arquivo temporário: {str(e)}")
                
    except Exception as e:
        logger.error(f"Erro ao processar PDF: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Erro ao processar PDF: {str(e)}"
        )

def process_and_generate_excel(text: str, filename: str) -> str:
    """
    Processa o texto e gera um arquivo Excel.
    
    Args:
        text: Texto extraído do PDF
        filename: Nome do arquivo original
        
    Returns:
        Caminho do arquivo Excel gerado
    """
    try:
        # Extrair dados do texto
        dados = extract_data_from_text(text)
        
        # Criar DataFrame
        df = pd.DataFrame(dados)
        
        # Organizar colunas em uma ordem específica
        colunas = [
            "NOME", "CPF", "PIS/NIT", "FUNÇÃO", "DATA DE ADMISSÃO",
            "LOTAÇÃO", "CARGA HORARIA", "V. BRUTO", "DESCONTO INSS",
            "DESCONTO IR", "OUT. DESC.", "V. LIQUIDO", "VÍNCULO"
        ]
        df = df[colunas]
        
        # Gerar nome único para o arquivo
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        excel_filename = f"processed_{os.path.splitext(filename)[0]}_{timestamp}.xlsx"
        excel_path = os.path.join(UPLOAD_DIR, excel_filename)
        
        # Salvar Excel
        df.to_excel(excel_path, index=False)
        logger.info(f"Arquivo Excel gerado com sucesso: {excel_path}")
        
        return excel_path
    except Exception as e:
        logger.error(f"Erro ao gerar Excel: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Erro ao gerar Excel: {str(e)}"
        )

@app.post("/upload/")
async def upload_files(files: List[UploadFile] = File(...)):
    """
    Endpoint para upload de arquivos PDF.
    """
    if not files:
        return JSONResponse(
            status_code=400,
            content={"message": "Nenhum arquivo enviado"}
        )
    
    processed_files = []
    for file in files:
        try:
            logger.info(f"Iniciando processamento do arquivo: {file.filename}")
            
            # Validar o arquivo
            validate_file(file)
            
            # Criar diretório de uploads se não existir
            os.makedirs("uploads", exist_ok=True)
            
            # Salvar o arquivo
            file_path = os.path.join("uploads", file.filename)
            with open(file_path, "wb") as buffer:
                content = await file.read()
                buffer.write(content)
            
            logger.info(f"Arquivo salvo: {file_path}")
            
            # Processar o PDF
            logger.info("Iniciando extração de texto do PDF")
            text = await extract_text_from_pdf(file)
            
            if not text:
                raise HTTPException(
                    status_code=400,
                    detail="Não foi possível extrair texto do PDF"
                )
            
            logger.info("Iniciando extração de dados do texto")
            dados = extract_data_from_text(text)
            
            if not dados["CPF"]:
                raise HTTPException(
                    status_code=400,
                    detail="Não foi possível extrair dados do PDF. Verifique se o arquivo contém as informações necessárias."
                )
            
            # Gerar Excel
            excel_filename = f"{os.path.splitext(file.filename)[0]}_processado.xlsx"
            excel_path = os.path.join("uploads", excel_filename)
            
            logger.info("Gerando arquivo Excel")
            df = pd.DataFrame(dados)
            df.to_excel(excel_path, index=False)
            
            processed_files.append({
                "original_name": file.filename,
                "excel_name": excel_filename,
                "status": "success"
            })
            
            logger.info(f"Arquivo Excel gerado: {excel_path}")
            
        except Exception as e:
            logger.error(f"Erro ao processar arquivo {file.filename}: {str(e)}")
            processed_files.append({
                "original_name": file.filename,
                "error": str(e),
                "status": "error"
            })
    
    if not any(f["status"] == "success" for f in processed_files):
        return JSONResponse(
            status_code=400,
            content={
                "message": "Nenhum arquivo processado com sucesso",
                "details": processed_files
            }
        )
    
    return JSONResponse(
        content={
            "message": "Arquivos processados com sucesso",
            "files": processed_files
        }
    )

@app.get("/download/{filename}")
async def download_file(filename: str):
    """
    Endpoint para download de arquivos processados.
    """
    file_path = os.path.join("uploads", filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    return FileResponse(file_path, filename=filename)

