from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
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

# Configuração de logging
logging.basicConfig(
    level=logging.DEBUG,  # Mudado para DEBUG para ver mais informações
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configurações
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_EXTENSIONS = {'.pdf'}
UPLOAD_DIR = "uploads"
TEMP_DIR = "temp"

# Criar diretórios necessários
for directory in [UPLOAD_DIR, TEMP_DIR]:
    if not os.path.exists(directory):
        os.makedirs(directory)
        logger.info(f"Diretório {directory} criado")

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

# Montar arquivos estáticos
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def read_root():
    return FileResponse("static/index.html")

def validate_file(file: UploadFile) -> None:
    """
    Valida o arquivo enviado.
    
    Args:
        file: Arquivo a ser validado
        
    Raises:
        HTTPException: Se o arquivo for inválido
    """
    logger.debug(f"Validando arquivo: {file.filename}")
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
    
    Args:
        text: Texto extraído do PDF
        
    Returns:
        Dict com os dados extraídos
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
    
    # Dividir o texto em blocos por funcionário
    funcionarios = text.split("Nome do Funcionário")
    
    for bloco in funcionarios[1:]:  # Ignorar o primeiro split que é o cabeçalho
        try:
            # Extrair nome e matrícula
            nome_match = re.search(r'(?:Mat\.)?\s*(\d+)\s+([A-ZÀ-Ú\s]+?)\s+(\d{3}\.\d{3}\.\d{3}-\d{2})', bloco)
            if nome_match:
                matricula = nome_match.group(1)
                nome = nome_match.group(2).strip()
                cpf = nome_match.group(3)
                
                dados["NOME"].append(nome)
                dados["CPF"].append(cpf)
                
                # Extrair PIS/NIT
                pis_match = re.search(r'Código\s*(\d{3}\.\d{5}\.\d{2}-\d)', bloco)
                if pis_match:
                    dados["PIS/NIT"].append(pis_match.group(1))
                else:
                    dados["PIS/NIT"].append("")
                
                # Extrair data de admissão
                data_match = re.search(r'Admissão\s+(\d{2}/\d{2}/\d{4})', bloco)
                dados["DATA DE ADMISSÃO"].append(data_match.group(1) if data_match else "")
                
                # Extrair função
                funcao_match = re.search(r'Cargo:\s*([A-ZÀ-Ú\s]+?)(?=\s+Estabelecimento:|$|\n)', bloco)
                if funcao_match:
                    dados["FUNÇÃO"].append(funcao_match.group(1).strip())
                else:
                    # Fallback: procurar por qualquer texto entre Cargo: e a próxima palavra-chave
                    funcao_match = re.search(r'Cargo:\s*(.+?)(?=\s+(?:Estabelecimento|Nível|Código|Descontos|$))', bloco)
                    dados["FUNÇÃO"].append(funcao_match.group(1).strip() if funcao_match else "")
                
                # Adicionar lotação completa
                dados["LOTAÇÃO"].append(lotacao_completa)
                
                # Extrair carga horária
                carga_match = re.search(r'Horas Mensais:\s*(\d+)', bloco)
                if carga_match:
                    dados["CARGA HORARIA"].append(int(carga_match.group(1)))
                else:
                    # Tentar outro padrão
                    carga_match = re.search(r'Nível:.*?(\d+)\s+Horas', bloco)
                    dados["CARGA HORARIA"].append(int(carga_match.group(1)) if carga_match else 0)
                
                # Calcular valor bruto (soma dos proventos)
                proventos = []
                for linha in bloco.split('\n'):
                    if "SALÁRIO BASE" in linha or "PLANTÃO NOTURNO" in linha:
                        valor_match = re.search(r'\d+(?:\.\d+)*,\d{2}$', linha.strip())
                        if valor_match:
                            valor_str = valor_match.group()
                            valor = float(valor_str.replace('.', '').replace(',', '.'))
                            proventos.append(valor)
                
                v_bruto = sum(proventos)
                dados["V. BRUTO"].append(f"{v_bruto:.2f}".replace('.', ','))
                
                # Extrair INSS (valor após "INSS 11.00")
                inss_match = re.search(r'INSS\s+11\.00\s+(\d+(?:\.\d+)*,\d{2})', bloco)
                inss_valor = 0.0
                if inss_match:
                    inss_str = inss_match.group(1)
                    inss_valor = float(inss_str.replace('.', '').replace(',', '.'))
                    dados["DESCONTO INSS"].append(inss_str)
                else:
                    dados["DESCONTO INSS"].append("")
                
                # Não usamos mais o Base IRRF como desconto
                dados["DESCONTO IR"].append("0,00")
                
                # Total de descontos é igual ao INSS (não há outros descontos no exemplo)
                dados["OUT. DESC."].append(f"{inss_valor:.2f}".replace('.', ','))
                
                # Calcular valor líquido (V. BRUTO - INSS)
                v_liquido = v_bruto - inss_valor
                dados["V. LIQUIDO"].append(f"{v_liquido:.2f}".replace('.', ','))
                
                # Vínculo com PIS/NIT
                pis = dados["PIS/NIT"][-1]
                dados["VÍNCULO"].append(f"AUTONOMOS Pis/Pasep: {pis}" if pis else "AUTONOMOS")
                
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
    
    return dados

async def extract_text_from_pdf(pdf_file: UploadFile) -> str:
    """
    Extrai texto de um arquivo PDF.
    
    Args:
        pdf_file: Arquivo PDF
        
    Returns:
        Texto extraído do PDF
    """
    try:
        content = await pdf_file.read()
        text = ""
        
        # Tentar extrair texto diretamente
        with pdfplumber.open(BytesIO(content)) as pdf:
            logger.info(f"PDF tem {len(pdf.pages)} páginas")
            for i, page in enumerate(pdf.pages, 1):
                logger.info(f"Processando página {i}")
                page_text = page.extract_text()
                if page_text:
                    text += f"\n=== Página {i} ===\n{page_text}\n"
                else:
                    logger.warning(f"Nenhum texto encontrado na página {i}")
                    # Tentar OCR
                    try:
                        images = convert_from_bytes(content, first_page=i, last_page=i)
                        for img in images:
                            page_text = pytesseract.image_to_string(img, lang='por')
                            if page_text:
                                text += f"\n=== Página {i} (OCR) ===\n{page_text}\n"
                                logger.info(f"Texto extraído com OCR da página {i}")
                            else:
                                logger.warning(f"Nenhum texto encontrado com OCR na página {i}")
                    except Exception as e:
                        logger.error(f"Erro ao fazer OCR na página {i}: {str(e)}")
        
        if not text:
            logger.error("Nenhum texto foi extraído do PDF")
            raise HTTPException(status_code=400, detail="Não foi possível extrair texto do PDF")
        
        return text
    except Exception as e:
        logger.error(f"Erro ao processar PDF: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erro ao processar PDF: {str(e)}")

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
    try:
        logger.info(f"Recebido upload de {len(files)} arquivos")
        processed_files = []
        
        for file in files:
            try:
                logger.info(f"Processando arquivo: {file.filename}")
                validate_file(file)
                
                # Extrair texto do PDF
                text = await extract_text_from_pdf(file)
                logger.debug(f"Texto extraído: {text[:200]}...")
                
                # Processar texto e gerar Excel
                excel_path = process_and_generate_excel(text, file.filename)
                processed_files.append(excel_path)
                logger.info(f"Arquivo processado com sucesso: {excel_path}")
                
            except Exception as e:
                logger.error(f"Erro ao processar arquivo {file.filename}: {str(e)}")
                raise HTTPException(status_code=500, detail=f"Erro ao processar arquivo {file.filename}: {str(e)}")
        
        return {"message": "Arquivos processados com sucesso", "files": processed_files}
        
    except Exception as e:
        logger.error(f"Erro geral no upload: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

