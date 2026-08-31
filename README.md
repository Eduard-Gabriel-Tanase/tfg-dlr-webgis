# TFG — Plataforma Web-GIS para Estimación de Capacidad Dinámica de Líneas Aéreas (DLR)

Grado en Ingeniería Informática — Facultad de Ciencias — Universidad de Cantabria

Plataforma Web-GIS que permite definir la traza de una línea eléctrica aérea (mediante dibujo manual o importación de CSV/Excel), superponer capas meteorológicas (ERA5-Land) y estimar su capacidad dinámica (Dynamic Line Rating) mediante el modelo térmico IEEE 738, a través de la librería `pypacity`.

---

## Índice

- [Guía de instalación](#guía-de-instalación)
- [Guía de uso](#guía-de-uso)
- [Despliegue con Docker](#despliegue-con-docker)
- [Estructura del repositorio](#estructura-del-repositorio)
- [Pruebas](#pruebas)

---

## Guía de instalación

Hay dos formas de tener el proyecto funcionando: **entorno virtual manual** (recomendado para desarrollo/depuración) o **Docker** (recomendado para una puesta en marcha rápida sin instalar dependencias de Python en el sistema).

### Opción A — Entorno virtual (desarrollo)

**Requisitos:** Python 3.14 y `pip`.

1. Clona el repositorio y sitúate en la carpeta `backend/`.
2. Crea y activa el entorno virtual:

   ```bat
   python -m venv venv
   venv\Scripts\activate.bat
   ```

3. Instala las dependencias:

   ```bat
   pip install -r requirements.txt
   ```

4. Comprueba que existen los siguientes archivos de datos (no se generan automáticamente, deben estar en el repositorio o añadirse manualmente):
   - `backend/weather_data/data.nc` — dataset meteorológico ERA5-Land.
   - `backend/catalog/spanish_overhead_conductor_catalog.csv` — catálogo de conductores por defecto.

### Opción B — Docker

Ver la sección [Despliegue con Docker](#despliegue-con-docker) más abajo.

---

## Guía de uso

### Inicialización del BACKEND

Con el entorno virtual activado:

```bat
venv\Scripts\activate.bat
uvicorn main:app --reload
```

El backend queda escuchando en `http://127.0.0.1:8000`.

### Inicialización del FRONTEND

El frontend (`frontend/index.html`) es un archivo estático sin build. Basta con abrirlo directamente en el navegador, o servirlo con un servidor HTTP simple:

```bat
cd frontend
python -m http.server 5500
```

### Acceso a la aplicación

Con ambos servicios activos:

**http://localhost:5500**

El frontend está configurado para consultar la API en `http://127.0.0.1:8000` (variable `API_BASE` en `index.html`). El backend permite peticiones de cualquier origen (CORS abierto), por lo que el frontend puede abrirse igualmente como archivo local (`file://`) sin necesidad del servidor de la Opción B, siempre que el backend esté activo en el puerto 8000.

### Flujo básico de uso

1. **Definir la línea**: mediante trazado manual sobre el mapa (botón "Trazado Manual", atajos de teclado `1`/`2`/`3`/`0`) o importando un archivo CSV/Excel con columnas UTM (`X`, `Y`) o geográficas (`LATITUD`, `LONGITUD`).
2. **Seleccionar el conductor**: desde el catálogo por defecto, o subiendo un catálogo propio (sección "Catálogo de conductores").
3. **Ajustar filtros**: año/mes de la meteorología ERA5-Land a utilizar, y temperatura máxima admisible del conductor si se desea sobrescribir la del catálogo.
4. **Calcular DLR**: botón "Calcular DLR". El resultado se muestra en la pestaña "Resultados", con comparación frente al rating estático de referencia, distribución de ampacidad por vano y detalle del vano crítico.
5. **Exportar**: la línea puede exportarse de nuevo a CSV, y el informe de resultados puede descargarse como PDF.

---

## Despliegue con Docker

El backend y el frontend estático se distribuyen en un único contenedor. Se recomienda para probar la aplicación en una máquina distinta a la de desarrollo sin instalar Python ni sus dependencias directamente en el sistema.

### Requisitos

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) instalado y en ejecución (en Windows requiere WSL2 y virtualización activada en la BIOS).

### Puesta en marcha

Desde la raíz del repositorio:

```bash
docker compose up --build
```

La primera ejecución tarda varios minutos, ya que descarga la imagen base de Python e instala todas las dependencias. Las siguientes ejecuciones son mucho más rápidas gracias a la caché de capas de Docker.

Cuando el registro muestre `Uvicorn running on http://0.0.0.0:8000`, la aplicación está lista.

### Acceso

Backend (API): **http://localhost:8000**

Frontend: se abre directamente el archivo `frontend/index.html` en el navegador (ver nota de CORS en la sección anterior), con el contenedor Docker sirviendo la API en el puerto 8000.

### Detener el contenedor

```bash
docker compose down
```

### Archivos relacionados con Docker

| Archivo | Ubicación | Función |
|---|---|---|
| `Dockerfile` | `backend/Dockerfile` | Receta de construcción de la imagen: instala dependencias de sistema y de Python, y copia el código, el dataset meteorológico y el catálogo de conductores. |
| `docker-compose.yml` | raíz del repositorio | Define el servicio `dlr-webgis`, construye la imagen a partir del `Dockerfile` y publica el puerto 8000. |
| `requirements.txt` | `backend/requirements.txt` | Dependencias de Python necesarias para ejecutar la aplicación (no incluye dependencias de desarrollo/pruebas, como Playwright o pytest). |

---

## Estructura del repositorio

```
tfg-dlr-webgis/
├── docker-compose.yml
├── README.md
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                  # API FastAPI: importación de líneas, catálogo de conductores,
│   │                             # capas meteorológicas ERA5-Land y motor de cálculo DLR (IEEE 738).
│   ├── weather_data/
│   │   └── data.nc              # Dataset meteorológico ERA5-Land.
│   ├── catalog/
│   │   └── spanish_overhead_conductor_catalog.csv
│   └── tests/
│       ├── test_carga_csv.py         # Pruebas de integración (backend en memoria, FastAPI TestClient).
│       ├── test_e2e_navegador.py     # Pruebas end-to-end (navegador real, Playwright).
│       ├── fixtures_csv/             # Archivos CSV de prueba para la importación de línea.
│       └── fixtures_catalogo/        # Archivos CSV de prueba para el catálogo de conductores.
├── frontend/
│   └── index.html                # Interfaz Web-GIS (Leaflet + Chart.js), sin build ni dependencias.
└── datasets/
    └── ...                       # Líneas de ejemplo para pruebas manuales.
```

---

## Pruebas

El proyecto cuenta con dos niveles de pruebas automatizadas sobre el módulo de carga de datos (importación de línea y catálogo de conductores), documentadas con su taxonomía de casos en `Anexo_Pruebas_TFG` (memoria).

### Pruebas de integración (backend en memoria)

No requieren tener el servidor arrancado; ejecutan el backend real en memoria mediante `TestClient` de FastAPI.

```bat
venv\Scripts\activate.bat
pip install pytest httpx
pytest backend\tests\test_carga_csv.py -v
```

### Pruebas end-to-end (navegador real)

Requieren el backend arrancado con `uvicorn` en una terminal aparte, y automatizan la interacción real con `frontend/index.html` mediante un navegador Chromium controlado por Playwright.

```bat
pip install pytest-playwright
playwright install chromium
pytest backend\tests\test_e2e_navegador.py -v
```

Estas dependencias de pruebas (`pytest`, `pytest-playwright`) son exclusivas del entorno de desarrollo y no forman parte de `backend/requirements.txt` ni de la imagen Docker de producción.