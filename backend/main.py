from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

# 1. Configurar CORS para que el frontend pueda hablar con el backend sin que el navegador lo bloquee
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En desarrollo permitimos cualquier origen
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. Definir cómo es la estructura de datos que vamos a recibir
class Linea(BaseModel):
    puntos: list[dict]

# 3. Tus endpoints (Rutas)
@app.get("/ping")
async def ping():
    return {"ping": "pong"}

@app.post("/linea")
async def recibir_linea(linea: Linea):
    # Aquí en el futuro llamaremos al cálculo DLR de tu profesor
    print(f"He recibido una línea con {len(linea.puntos)} apoyos/puntos.")
    return {"mensaje": "Línea recibida correctamente en el servidor", "datos": linea.puntos}
