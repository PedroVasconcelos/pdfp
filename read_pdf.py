import PyPDF2
import os

def read_pdf():
    pdf_path = os.path.join('uploads', 'SVO - TÉCNICO ÁREA FIM S.VINC-SETEMBRO.2024-PROC. E-41010.0000025700.2024-ANALÍTICO.pdf')
    
    with open(pdf_path, 'rb') as file:
        reader = PyPDF2.PdfReader(file)
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n=== Página ===\n"
            
        print("=== Conteúdo do PDF ===")
        print(text)
        
if __name__ == "__main__":
    read_pdf() 