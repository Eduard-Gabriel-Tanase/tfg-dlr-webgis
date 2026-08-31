# Misma version de Python que el entorno de desarrollo local, para
# evitar incompatibilidades de wheels entre versiones al instalar
# dependencias con version fijada.
FROM python:3.14-slim

WORKDIR /app

# Librerias de sistema necesarias para compilar/ejecutar netCDF4,
# pyproj y matplotlib. build-essential se retira despues de instalar
# los paquetes de Python para no dejar peso innecesario en la imagen.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libnetcdf-dev \
    libproj-dev \
    proj-data \
    proj-bin \
    libgeos-dev \
    && rm -rf /var/lib/apt/lists/*

# Se copian primero solo las dependencias para aprovechar la cache de
# Docker: si requirements.txt no cambia, no se reinstala nada en builds futuros.
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential

# Codigo de la aplicacion, dataset meteorologico y catalogo de conductores.
COPY backend/main.py .
COPY backend/weather_data ./weather_data
COPY backend/catalog ./catalog

# Frontend estatico, servido por la propia app FastAPI (ver main.py).
COPY frontend ./frontend

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
