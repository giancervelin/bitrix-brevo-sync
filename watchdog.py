import subprocess
import time
import os

def run_repair():
    while True:
        print("--- Cão de Guarda: Iniciando o reparo ---")
        # Roda o script e espera ele terminar ou ser morto
        process = subprocess.Popen(["python3", "-u", "repair_bitrix.py"])
        process.wait()
        
        print("--- Cão de Guarda: O script foi morto ou parou. Reiniciando em 30 segundos... ---")
        time.sleep(30)

if __name__ == "__main__":
    run_repair()
