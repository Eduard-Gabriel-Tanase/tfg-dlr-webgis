from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import csv
import io

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Linea(BaseModel):
    puntos: list[dict]

@app.get("/ping")
async def ping():
    return {"ping": "pong"}

@app.post("/linea")
async def recibir_linea(linea: Linea):
    print(f"He recibido una línea manual con {len(linea.puntos)} apoyos.")
    return {"mensaje": "Línea guardada en el backend para cálculo DLR.", "datos": linea.puntos}

# --- NUEVO ENDPOINT PARA LEER CSV ---
@app.post("/upload-csv")
async def upload_csv_file(file: UploadFile = File(...)):
    if not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="El archivo debe ser un .csv")

    try:
        # Leemos el archivo enviado desde el frontend
        contents = await file.read()
        decoded_contents = contents.decode("utf-8")
        csv_reader = csv.DictReader(io.StringIO(decoded_contents))
        
        lista_puntos = []
        # Leemos línea por línea el CSV
        for row in csv_reader:
            # Aseguramos que la latitud y longitud existen y las convertimos a números
            if 'latitud' in row and 'longitud' in row:
                lista_puntos.append({
                    "lat": float(row['latitud']),
                    "lng": float(row['longitud'])
                })
        
        if not lista_puntos:
            raise HTTPException(status_code=400, detail="El CSV no tiene columnas 'latitud' y 'longitud'")

        print(f"CSV procesado. Se han extraído {len(lista_puntos)} puntos.")
        # Le devolvemos los puntos limpios al Frontend
        return {"mensaje": "CSV leído correctamente", "puntos": lista_puntos}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error procesando CSV: {str(e)}")
