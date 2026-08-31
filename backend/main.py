"""
Backend de la plataforma Web-GIS de Capacidad Dinámica de Líneas (DLR).

API construida con FastAPI que da servicio al frontend (index.html) para:
  1. Servir datos meteorológicos ERA5-Land (capas raster y consulta puntual).
  2. Importar/exportar la geometría de una línea eléctrica desde CSV/Excel.
  3. Gestionar el catálogo de conductores (por defecto o subido por el usuario).
  4. Calcular la ampacidad (intensidad admisible) de cada vano con el modelo
     térmico IEEE 738, a través de la librería pypacity, y compararla con
     un rating estático de referencia.
  5. Generar un informe PDF con el resultado del cálculo.

El archivo se organiza en las siguientes secciones, en este orden:
  - Configuración inicial y modelos de datos de entrada (Pydantic)
  - Carga del dataset meteorológico ERA5-Land
  - Gestión del catálogo de conductores
  - Consulta meteorológica puntual
  - Importación de líneas desde CSV/Excel
  - Capas raster meteorológicas (temperatura, viento, radiación)
  - Motor de cálculo DLR (IEEE 738 vía pypacity)
  - Generación del informe PDF
"""

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import io
import os
import sys
import math
import numpy as np
import pandas as pd
import xarray as xr
from pyproj import Transformer
from datetime import datetime
from math import radians, degrees, atan2, sin, cos, sqrt

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.backends.backend_pdf import PdfPages
from scipy.interpolate import griddata
from scipy.stats import gaussian_kde

from pypacity.cable import cable as cable_mod
from pypacity.case import case as case_mod
from pypacity.ieee738 import ieee738 as ieee738_mod

# ============================================================
# CONFIGURACIÓN Y MODELOS DE ENTRADA
# ============================================================

STANDARD_POLE_HEIGHT_M = 15.0   # Altura de apoyo asumida cuando el CSV no trae columna de altura.
MAX_POINTS_PER_REQUEST = 500    # Límite de apoyos por línea, tanto al importar como al calcular.

# Condiciones fijas que definen el "rating estático" de referencia contra
# el que se compara la ampacidad dinámica calculada.
RATING_ESTATICO_TAMB_C = 40.0
RATING_ESTATICO_VWIND_MS = 0.61
RATING_ESTATICO_SOLAR_WM2 = 1000.0

BASE_DIR = os.path.dirname(__file__)
WEATHER_DATA_PATH = os.path.join(BASE_DIR, "weather_data", "data.nc")
CONDUCTOR_CATALOG_PATH = os.path.join(BASE_DIR, "catalog", "spanish_overhead_conductor_catalog.csv")

# Columnas que debe tener cualquier catálogo de conductores (de fábrica o
# subido por el usuario) para poder alimentar el modelo térmico de pypacity.
REQUIRED_CATALOG_COLUMNS = ["ID", "D", "D1", "d", "TLO", "THI", "TCDRMAX", "RLO", "RHI", "HNH", "HEATOUT", "HEATCORE", "EMISS", "ABSORP", "MALUM", "MSTEEL"]
NUMERIC_CATALOG_COLUMNS = ["D", "D1", "d", "TLO", "THI", "TCDRMAX", "RLO", "RHI", "HNH", "HEATOUT", "HEATCORE", "EMISS", "ABSORP", "MALUM", "MSTEEL"]

# Estilo visual común para todas las figuras del informe PDF.
plt.rcParams.update({
    "font.family": "serif",
    "axes.edgecolor": "#333333",
    "axes.grid": True,
    "grid.color": "#dddddd",
    "grid.linestyle": "--",
    "grid.linewidth": 0.6,
    "axes.titlesize": 11,
    "axes.labelsize": 10,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
})

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

@app.get("/")
def index():
    return FileResponse("frontend/index.html")

app.mount("/frontend", StaticFiles(directory="frontend"), name="frontend")

class Linea(BaseModel):
    """Cuerpo de petición para /linea: puntos de la línea y conductor elegido."""
    puntos: list[dict]
    conductor: Optional[str] = "Opción A"

class LineaDLR(BaseModel):
    """Cuerpo de petición para /calcular-dlr. temp_max_override permite
    forzar una temperatura máxima de conductor distinta a la del catálogo;
    anio/mes filtran qué periodo de ERA5-Land se usa para la meteorología."""
    puntos: list[dict]
    conductor: str
    temp_max_override: Optional[float] = None
    mes: Optional[int] = None
    anio: Optional[int] = None

class InformeRequest(BaseModel):
    """Cuerpo de petición para /informe-pdf: el JSON completo que ya
    devolvió /calcular-dlr, para no tener que repetir el cálculo."""
    resultado: dict

# ============================================================
# DATASET METEOROLÓGICO (ERA5-Land)
# ============================================================
# Se carga una sola vez al arrancar el servidor y se mantiene en memoria
# durante toda la vida del proceso; todas las consultas posteriores
# (puntuales o de capa completa) se resuelven contra este objeto xarray.

if not os.path.isfile(WEATHER_DATA_PATH):
    print(f"FATAL: no se encontró el archivo de datos meteorológicos en '{WEATHER_DATA_PATH}'.")
    sys.exit(1)

DS_METEO = xr.open_dataset(WEATHER_DATA_PATH)
ANIOS_DISPONIBLES = sorted(set(DS_METEO.valid_time.dt.year.values.tolist()))
MESES_DISPONIBLES = list(range(1, 13))

# ============================================================
# CATÁLOGO DE CONDUCTORES
# ============================================================
# Existen dos catálogos en juego: el de fábrica (DEFAULT_CATALOG_STATE,
# leído de disco) y el activo (CATALOGO_ACTIVO, el que realmente usan
# los cálculos). El usuario puede sustituir el activo por uno propio sin
# tocar el archivo en disco, y volver al de fábrica cuando quiera.

CATALOG_STATE = {"df": None, "mtime": None, "ok": False, "categoria": None}

# Mensaje de usuario para cada categoría de error posible al leer un catálogo.
MENSAJES_CORTOS_UI = {
    "archivo_no_encontrado": "No se encuentra el archivo del catálogo.",
    "archivo_vacio": "El archivo del catálogo está vacío.",
    "encoding_invalido": "El archivo del catálogo tiene un formato no compatible.",
    "error_lectura": "No se pudo leer el archivo del catálogo.",
    "sin_filas": "El catálogo no tiene ningún conductor cargado.",
    "separador_incorrecto": "El catálogo tiene un error de formato.",
    "columnas_faltantes": "Al catálogo le faltan columnas obligatorias.",
    "ids_duplicados": "El catálogo tiene conductores con nombres duplicados.",
    "valores_no_numericos": "El catálogo tiene un error de sintaxis.",
}

def _validar_catalogo(df: pd.DataFrame) -> Optional[str]:
    """Revisa la estructura de un catálogo ya leído en un DataFrame:
    que tenga filas, las columnas obligatorias, IDs sin duplicar y
    valores numéricos donde corresponde. Devuelve la categoría de
    error encontrada, o None si el catálogo es válido."""
    if len(df) < 1:
        return "sin_filas"
    if len(df.columns) == 1 and ',' in str(df.columns[0]):
        return "separador_incorrecto"
    missing = [c for c in REQUIRED_CATALOG_COLUMNS if c not in df.columns]
    if missing:
        return "columnas_faltantes"
    duplicados = df['ID'].astype(str).str.upper()
    if duplicados[duplicados.duplicated()].any():
        return "ids_duplicados"
    for col in NUMERIC_CATALOG_COLUMNS:
        if pd.to_numeric(df[col], errors='coerce').isna().any():
            return "valores_no_numericos"
    return None

def _parsear_catalogo_bytes(contenido: bytes):
    """Punto de entrada común para cargar un catálogo, tanto el de
    fábrica como uno subido por el usuario: decodifica, separa por ';'
    y valida la estructura con _validar_catalogo. Devuelve
    (DataFrame, None) si todo está bien, o (None, categoria_error) si no."""
    try:
        texto = contenido.decode("utf-8")
    except UnicodeDecodeError:
        return None, "encoding_invalido"
    try:
        df = pd.read_csv(io.StringIO(texto), sep=';', comment='#')
    except pd.errors.EmptyDataError:
        return None, "archivo_vacio"
    except Exception:
        return None, "error_lectura"
    categoria = _validar_catalogo(df)
    if categoria:
        return None, categoria
    for col in NUMERIC_CATALOG_COLUMNS:
        df[col] = pd.to_numeric(df[col])
    return df, None

# Catálogo de fábrica, cacheado en memoria y recargado solo si el
# archivo en disco cambia (comparando su fecha de modificación) o si
# se fuerza explícitamente la recarga.
DEFAULT_CATALOG_STATE = {"df": None, "mtime": None, "ok": False, "categoria": None, "raw_bytes": None}

def _cargar_catalogo_defecto(forzar: bool = False):
    if not os.path.isfile(CONDUCTOR_CATALOG_PATH):
        DEFAULT_CATALOG_STATE.update({"ok": False, "df": None, "categoria": "archivo_no_encontrado", "raw_bytes": None})
        return
    mtime_actual = os.path.getmtime(CONDUCTOR_CATALOG_PATH)
    if not forzar and DEFAULT_CATALOG_STATE["ok"] and DEFAULT_CATALOG_STATE["mtime"] == mtime_actual:
        return
    with open(CONDUCTOR_CATALOG_PATH, "rb") as f:
        contenido = f.read()
    df, categoria = _parsear_catalogo_bytes(contenido)
    if categoria:
        DEFAULT_CATALOG_STATE.update({"ok": False, "df": None, "categoria": categoria, "mtime": mtime_actual, "raw_bytes": contenido})
        return
    DEFAULT_CATALOG_STATE.update({"ok": True, "df": df, "categoria": None, "mtime": mtime_actual, "raw_bytes": contenido})

_cargar_catalogo_defecto(forzar=True)

# Catálogo que usan realmente los cálculos. Arranca como el de fábrica;
# subir un catálogo personalizado lo sustituye en memoria sin escribir
# nada en disco, y "usar-defecto" lo restaura.
CATALOGO_ACTIVO = {
    "df": DEFAULT_CATALOG_STATE["df"],
    "ok": DEFAULT_CATALOG_STATE["ok"],
    "categoria": DEFAULT_CATALOG_STATE["categoria"],
    "origen": "defecto",
    "nombre_archivo": "spanish_overhead_conductor_catalog.csv",
    "raw_bytes": DEFAULT_CATALOG_STATE["raw_bytes"],
}

# ============================================================
# CONSULTA METEOROLÓGICA PUNTUAL
# ============================================================

def filtrar_dataset(ds, anio: Optional[int] = None, mes: Optional[int] = None):
    """Restringe el dataset (completo o ya recortado a un punto) a un
    año y/o mes concretos. Lanza ValueError si el filtro no deja ningún dato."""
    sub = ds
    if anio is not None:
        sub = sub.sel(valid_time=sub.valid_time.dt.year == anio)
    if mes is not None:
        sub = sub.sel(valid_time=sub.valid_time.dt.month == mes)
    if sub.valid_time.size == 0:
        raise ValueError("No hay datos disponibles para la combinación año/mes solicitada.")
    return sub

def obtener_meteo_punto(lat: float, lon: float, anio: Optional[int] = None, mes: Optional[int] = None):
    """Localiza el punto de la malla ERA5-Land más cercano a (lat, lon)
    y resume su temperatura, viento y radiación (medias y máximos) en
    el periodo filtrado por año/mes. Esta función es la que alimenta
    de meteorología tanto a /linea como al motor de cálculo DLR."""
    punto = DS_METEO.sel(latitude=lat, longitude=lon, method="nearest")
    punto_filtrado = filtrar_dataset(punto, anio=anio, mes=mes)
    t2m = punto_filtrado["t2m"].values - 273.15
    ssrd = punto_filtrado["ssrd"].values / 3600.0
    u10 = punto_filtrado["u10"].values
    v10 = punto_filtrado["v10"].values
    velocidad = np.sqrt(u10 ** 2 + v10 ** 2)
    direccion = (np.degrees(np.arctan2(u10, v10)) + 360) % 360
    idx_v_max = int(np.argmax(velocidad))
    return {
        "lat_real": float(punto.latitude.values),
        "lon_real": float(punto.longitude.values),
        "temperatura_C": round(float(t2m.mean()), 2),
        "viento_vel_ms": round(float(velocidad.mean()), 2),
        "viento_dir_grados": round(float(direccion[idx_v_max]), 1),
        "radiacion_Wm2": round(float(ssrd.mean()), 2),
        "temp_max_C": round(float(t2m.max()), 2),
        "viento_max_ms": round(float(velocidad.max()), 2),
        "radiacion_max_Wm2": round(float(ssrd.max()), 2),
    }

@app.get("/ping")
async def ping():
    return {"ping": "pong"}

@app.get("/api/filtros-disponibles")
def get_filtros_disponibles():
    """Años y meses que tiene realmente el dataset cargado, para
    rellenar los desplegables de filtro temporal del frontend."""
    return {"anios": ANIOS_DISPONIBLES, "meses": MESES_DISPONIBLES}

# ============================================================
# IMPORTACIÓN DE LÍNEAS DESDE CSV/EXCEL
# ============================================================

# Rangos físicos plausibles en España, usados para detectar coordenadas
# expresadas por error en milímetros en vez de metros (ver
# parsear_con_correccion_escala).
LIMITE_UTM_X_M = 1_000_000
LIMITE_UTM_Y_M = 5_000_000
LIMITE_Z_M = 5_000

def parsear_numero_flexible(valor_raw) -> float:
    """Convierte un valor de celda a float admitiendo tanto el formato
    español (coma decimal, punto de miles) como el anglosajón. Lanza
    ValueError si la celda está vacía o no representa un número."""
    v = str(valor_raw).strip()
    if v == "" or v.lower() in ("nan", "none"):
        raise ValueError("valor vacío")
    if ',' in v:
        v = v.replace('.', '').replace(',', '.')
    elif v.count('.') > 1:
        partes = v.split('.')
        v = ''.join(partes[:-1]) + '.' + partes[-1]
    resultado = float(v)
    if not math.isfinite(resultado):
        raise ValueError(f"valor no finito: {valor_raw!r}")
    return resultado

def parsear_con_correccion_escala(valor_raw, limite: float) -> float:
    """Igual que parsear_numero_flexible, pero si el resultado supera
    con mucho el rango físico esperado (parámetro limite), asume que
    el archivo de origen expresó el valor en milímetros y lo corrige
    dividiendo entre 1000. Cubre un caso real observado en archivos de
    topografía con coordenadas UTM/altura enteras en milímetros."""
    resultado = parsear_numero_flexible(valor_raw)
    if abs(resultado) > limite:
        resultado = resultado / 1000.0
    return resultado

def distancia_haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Distancia en línea recta entre dos coordenadas geográficas, en metros."""
    R = 6371000.0
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))

DUPLICADO_DISTANCIA_MAX_M = 1.0
OUTLIER_VANO_MAX_M = 5000.0

def detectar_duplicados_y_outliers(puntos: list[dict]) -> dict:
    """Revisa la línea ya importada y señala, sin descartar ningún
    punto: (a) pares de apoyos a menos de 1 m entre sí, que suelen ser
    una fila repetida por error en el archivo de origen, y (b) vanos
    consecutivos de más de 5 km, que suelen indicar una coordenada mal
    transcrita. Los avisos se muestran al usuario, que decide qué hacer."""
    duplicados = []
    outliers = []
    for i in range(len(puntos)):
        for j in range(i + 1, len(puntos)):
            d = distancia_haversine_m(puntos[i]["lat"], puntos[i]["lng"], puntos[j]["lat"], puntos[j]["lng"])
            if d < DUPLICADO_DISTANCIA_MAX_M:
                duplicados.append({
                    "apoyo_1": {"indice": i + 1, "nombre": puntos[i]["nombre"]},
                    "apoyo_2": {"indice": j + 1, "nombre": puntos[j]["nombre"]},
                    "distancia_m": round(d, 3)
                })
    for i in range(len(puntos) - 1):
        d = distancia_haversine_m(puntos[i]["lat"], puntos[i]["lng"], puntos[i + 1]["lat"], puntos[i + 1]["lng"])
        if d > OUTLIER_VANO_MAX_M:
            outliers.append({
                "apoyo_1": {"indice": i + 1, "nombre": puntos[i]["nombre"]},
                "apoyo_2": {"indice": i + 2, "nombre": puntos[i + 1]["nombre"]},
                "distancia_m": round(d, 1)
            })
    return {"duplicados": duplicados, "outliers": outliers}

def detectar_separador_csv(texto: str) -> str:
    """Decide si el CSV usa ';' o ',' mirando la primera línea, con
    preferencia por ';' (el que usan tanto el catálogo como el propio
    exportador del frontend). Evita depender de un separador fijo, ya
    que distintos orígenes (Excel España, herramientas SIG, exportación
    propia) no siempre coinciden."""
    primera_linea = texto.splitlines()[0] if texto.splitlines() else ""
    if ';' in primera_linea:
        return ';'
    if ',' in primera_linea:
        return ','
    return ';'

@app.post("/upload-csv")
async def upload_file(file: UploadFile = File(...)):
    """Importa una línea desde CSV o Excel. Acepta indistintamente
    coordenadas UTM (columnas X/Y, se reproyectan a WGS84) o geográficas
    (LATITUD/LONGITUD, o variantes con LAT/LON/LNG en el nombre). Las
    filas con coordenadas ilegibles o fuera de rango se descartan de
    forma individual sin abortar la importación completa; solo se
    devuelve error si el archivo entero no aporta ningún punto válido."""
    filename = file.filename.lower()
    if not (filename.endswith('.csv') or filename.endswith('.xlsx') or filename.endswith('.xls')):
        raise HTTPException(status_code=400, detail="El archivo debe ser CSV, XLSX o XLS.")
    try:
        lista_puntos = []
        col_z = None

        contents = await file.read()
        if filename.endswith('.csv'):
            # utf-8-sig retira el BOM que añade Excel al guardar "CSV UTF-8",
            # que si no se limpia se cuela en el nombre de la primera columna.
            decoded_contents = contents.decode("utf-8-sig")
            separador = detectar_separador_csv(decoded_contents)
            df = pd.read_csv(io.StringIO(decoded_contents), sep=separador, comment='#')
        else:
            df = pd.read_excel(io.BytesIO(contents))

        # Mismo límite de apoyos por petición que rige el cálculo DLR,
        # comprobado ya en la importación para avisar cuanto antes.
        if len(df) > MAX_POINTS_PER_REQUEST:
            raise HTTPException(
                status_code=400,
                detail=f"El archivo tiene {len(df)} filas, por encima del límite de "
                       f"{MAX_POINTS_PER_REQUEST} apoyos por importación. Divide la línea en tramos más cortos."
            )

        df.columns = df.columns.str.strip().str.upper()
        is_utm = 'X' in df.columns and 'Y' in df.columns
        is_wgs = 'LATITUD' in df.columns and 'LONGITUD' in df.columns

        if not is_utm and not is_wgs:
            lat_col = next((k for k in df.columns if 'LAT' in k), None)
            lon_col = next((k for k in df.columns if 'LON' in k or 'LNG' in k), None)
            if lat_col and lon_col:
                is_wgs = True
                df = df.rename(columns={lat_col: 'LATITUD', lon_col: 'LONGITUD'})
            else:
                raise HTTPException(status_code=400, detail="El archivo debe contener columnas X/Y (UTM) o Lat/Lon (WGS84).")

        col_z = next((k for k in df.columns if k in ('Z', 'ALTURA', 'ELEVACION', 'ELEVATION', 'COTA', 'HEIGHT')), None)
        transformer = Transformer.from_crs("EPSG:25830", "EPSG:4326", always_xy=True) if is_utm else None

        for index, row in df.iterrows():
            try:
                if is_utm:
                    x_val = parsear_con_correccion_escala(row['X'], LIMITE_UTM_X_M)
                    y_val = parsear_con_correccion_escala(row['Y'], LIMITE_UTM_Y_M)
                    lon, lat = transformer.transform(x_val, y_val)
                else:
                    lat = parsear_numero_flexible(row['LATITUD'])
                    lon = parsear_numero_flexible(row['LONGITUD'])

                if not (math.isfinite(lat) and math.isfinite(lon)):
                    continue
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    continue

                nombre_poste = str(row.get('STRUCTURE COMMENT', row.get('NOMBRE', row.get('NAME', f"Apoyo {index+1}"))))
                if nombre_poste.lower() in ('nan', 'none', ''):
                    nombre_poste = f"Apoyo {index+1}"

                punto = {"lat": lat, "lng": lon, "nombre": nombre_poste}
                if col_z is not None:
                    try:
                        punto["z"] = parsear_con_correccion_escala(row[col_z], LIMITE_Z_M)
                    except Exception:
                        pass  # fila sin altura utilizable: se importa el punto sin ella
                lista_puntos.append(punto)
            except Exception:
                continue  # fila con coordenada ilegible: se descarta solo esa fila

        if not lista_puntos:
            raise HTTPException(status_code=400, detail="No se pudo extraer ninguna coordenada válida del archivo.")

        avisos = detectar_duplicados_y_outliers(lista_puntos)
        return {
            "mensaje": "Archivo leído correctamente",
            "puntos": lista_puntos,
            "tiene_altura_real": col_z is not None,
            "duplicados": avisos["duplicados"],
            "outliers": avisos["outliers"]
        }
    except HTTPException:
        raise
    except Exception:
        # Cualquier fallo no anticipado (CSV corrupto, error de reproyección...)
        # llega al cliente como 500 controlado, nunca como traceback en crudo.
        raise HTTPException(status_code=500, detail="No se pudo procesar el archivo importado.")

# ============================================================
# CAPAS RASTER METEOROLÓGICAS
# ============================================================
# Generan una imagen PNG transparente por variable (temperatura, viento,
# radiación) que el frontend superpone sobre el mapa como overlay de
# Leaflet, georreferenciada implícitamente por /api/raster-bounds.

CACHE_RASTER = {}

CONFIG_VARIABLES = {
    "temp": {"campo": "t2m", "transform": lambda arr: arr - 273.15, "vmin": 0, "vmax": 40,
             "colores": ["#0000ff", "#00ffff", "#00ff00", "#ffff00", "#ff7800", "#ff0000"]},
    "viento": {"campo": None, "transform": None, "vmin": 0, "vmax": 3,
               "colores": ["#1a1a2e", "#0f3460", "#16c79a", "#f9d923", "#f83600", "#ff0057"]},
    "rad": {"campo": "ssrd", "transform": lambda arr: arr / 3600.0, "vmin": 500, "vmax": 3000,
            "colores": ["#3a0ca3", "#7209b7", "#f72585", "#ff9e00", "#ffea00"]}
}

def rellenar_huecos_costeros(matriz: np.ndarray) -> np.ndarray:
    """ERA5-Land no tiene dato de tierra en algunas celdas costeras
    (aparecen como NaN). Se rellenan por interpolación al vecino más
    cercano para que la capa raster no muestre huecos en el litoral."""
    if not np.isnan(matriz).any():
        return matriz
    filas, cols = matriz.shape
    yy, xx = np.mgrid[0:filas, 0:cols]
    mascara_valida = ~np.isnan(matriz)
    if not mascara_valida.any():
        return matriz
    puntos_validos = np.column_stack((yy[mascara_valida], xx[mascara_valida]))
    valores_validos = matriz[mascara_valida]
    matriz_rellena = matriz.copy()
    puntos_nan = np.column_stack((yy[~mascara_valida], xx[~mascara_valida]))
    valores_rellenados = griddata(puntos_validos, valores_validos, puntos_nan, method="nearest")
    matriz_rellena[~mascara_valida] = valores_rellenados
    return matriz_rellena

def calcular_matriz_variable(tipo: str, sub_ds) -> np.ndarray:
    """Media temporal de la variable pedida sobre todo el periodo ya
    filtrado (sub_ds), convertida a la unidad de visualización."""
    cfg = CONFIG_VARIABLES[tipo]
    if tipo == "viento":
        u = sub_ds["u10"].mean(dim="valid_time").values
        v = sub_ds["v10"].mean(dim="valid_time").values
        matriz = np.sqrt(u ** 2 + v ** 2)
    else:
        arr = sub_ds[cfg["campo"]].mean(dim="valid_time").values
        matriz = cfg["transform"](arr)
    return rellenar_huecos_costeros(matriz)

def generar_raster_png(tipo: str, anio: Optional[int], mes: Optional[int]) -> bytes:
    """Renderiza la matriz de la variable como PNG transparente, con
    la paleta de colores definida en CONFIG_VARIABLES."""
    cfg = CONFIG_VARIABLES[tipo]
    sub = filtrar_dataset(DS_METEO, anio=anio, mes=mes)
    matriz = calcular_matriz_variable(tipo, sub)
    cmap = LinearSegmentedColormap.from_list(f"cmap_{tipo}", cfg["colores"])
    fig = plt.figure(figsize=(matriz.shape[1] / 50, matriz.shape[0] / 50), dpi=100)
    ax = plt.axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.imshow(matriz, cmap=cmap, vmin=cfg["vmin"], vmax=cfg["vmax"],
              origin="upper", aspect="auto", interpolation="bilinear")
    buf = io.BytesIO()
    plt.savefig(buf, format="png", transparent=True, bbox_inches=None, pad_inches=0)
    plt.close(fig)
    buf.seek(0)
    return buf.read()

@app.get("/api/raster/{tipo}")
def get_raster(tipo: str, anio: Optional[int] = None, mes: Optional[int] = None):
    """Devuelve el PNG de la variable pedida, cacheado por (tipo, año, mes)
    para no regenerar la misma imagen en cada petición del frontend."""
    if tipo not in CONFIG_VARIABLES:
        raise HTTPException(status_code=400, detail="Tipo de capa no válido. Usa: temp, viento o rad.")
    cache_key = (tipo, anio, mes)
    if cache_key in CACHE_RASTER:
        return Response(content=CACHE_RASTER[cache_key], media_type="image/png")
    try:
        png_bytes = generar_raster_png(tipo, anio, mes)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generando raster: {str(e)}")
    CACHE_RASTER[cache_key] = png_bytes
    return Response(content=png_bytes, media_type="image/png")

@app.get("/api/raster-bounds")
def get_raster_bounds():
    """Límites geográficos de la malla ERA5-Land, para que el frontend
    sepa dónde encajar exactamente el PNG del raster sobre el mapa."""
    lats = DS_METEO.latitude.values
    lons = DS_METEO.longitude.values
    return {"south": float(lats.min()), "north": float(lats.max()),
            "west": float(lons.min()), "east": float(lons.max())}

# Sin ningún await real dentro: declarado como "def" normal para que
# FastAPI lo ejecute en threadpool y no bloquee el resto de la API
# mientras se resuelve.
@app.post("/linea")
def recibir_linea(linea: Linea):
    """Endpoint temprano de solo meteorología (sin cálculo DLR): devuelve
    la temperatura y viento de cada apoyo, útil para una vista previa
    rápida antes de lanzar el cálculo completo."""
    try:
        if not linea.puntos:
            return {"error": "No se han enviado puntos."}
        resultados_nodos = []
        for i, p in enumerate(linea.puntos):
            meteo = obtener_meteo_punto(p['lat'], p['lng'])
            nodo_nombre = p.get('nombre', f"Apoyo {i+1}")
            resultados_nodos.append({
                "id_apoyo": i + 1, "nombre": nodo_nombre,
                "meteo": {
                    "temperatura_C": meteo["temperatura_C"], "viento_vel_ms": meteo["viento_vel_ms"],
                    "viento_dir_grados": meteo["viento_dir_grados"], "radiacion_Wm2": meteo["radiacion_Wm2"]
                }
            })
        temps = [n["meteo"]["temperatura_C"] for n in resultados_nodos]
        vientos = [n["meteo"]["viento_vel_ms"] for n in resultados_nodos]
        return {
            "mensaje": "Cálculo meteorológico completado (fuente: ERA5-Land local)",
            "estadisticas_linea": {
                "conductor_usado": linea.conductor,
                "temp_max": max(temps), "temp_min": min(temps),
                "temp_media": round(sum(temps) / len(temps), 2),
                "viento_min": min(vientos), "vano_critico_id": temps.index(max(temps)) + 1
            },
            "nodos": resultados_nodos
        }
    except Exception as e:
        return {"error": str(e)}

def calcular_azimut(lat1, lon1, lat2, lon2):
    """Rumbo (0-360°, medido desde el norte) del punto 1 hacia el punto 2.
    Necesario para el modelo IEEE 738, que tiene en cuenta la orientación
    del vano frente a la dirección del viento."""
    lat1_r, lat2_r = radians(lat1), radians(lat2)
    dlon_r = radians(lon2 - lon1)
    x = sin(dlon_r) * cos(lat2_r)
    y = cos(lat1_r) * sin(lat2_r) - sin(lat1_r) * cos(lat2_r) * cos(dlon_r)
    return (degrees(atan2(x, y)) + 360) % 360

@app.get("/catalogo-conductores")
def get_catalogo_conductores():
    """Lista de conductores del catálogo activo, con el rango de
    temperatura máxima permitido para cada uno (usado por el slider
    de temperatura del frontend)."""
    if not CATALOGO_ACTIVO["ok"]:
        mensaje_ui = MENSAJES_CORTOS_UI.get(CATALOGO_ACTIVO["categoria"], "El catálogo de conductores no está disponible.")
        return {"catalogo_ok": False, "mensaje": mensaje_ui, "conductores": [], "origen": CATALOGO_ACTIVO["origen"], "nombre_archivo": CATALOGO_ACTIVO["nombre_archivo"]}
    registros = []
    for _, row in CATALOGO_ACTIVO["df"].iterrows():
        d_nom = float(row["D"])
        tmax_nom = float(row["TCDRMAX"])
        registros.append({
            "ID": row["ID"], "D": d_nom, "TCDRMAX": tmax_nom,
            "temp_max_min_C": round(tmax_nom * 0.5, 0),
            "temp_max_max_C": round(tmax_nom * 2.0, 0)
        })
    return {
        "catalogo_ok": True, "mensaje": None, "conductores": registros,
        "origen": CATALOGO_ACTIVO["origen"], "nombre_archivo": CATALOGO_ACTIVO["nombre_archivo"]
    }

@app.get("/catalogo-conductores/descargar")
def descargar_catalogo_activo():
    """Devuelve el catálogo activo tal cual (bytes originales), para
    que el usuario pueda editarlo localmente y volver a subirlo."""
    if not CATALOGO_ACTIVO["raw_bytes"]:
        raise HTTPException(status_code=404, detail="No hay ningún catálogo cargado para descargar.")
    nombre_descarga = "catalogo_" + ("defecto" if CATALOGO_ACTIVO["origen"] == "defecto" else "personalizado") + ".csv"
    return Response(
        content=CATALOGO_ACTIVO["raw_bytes"],
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{nombre_descarga}"'}
    )

@app.post("/catalogo-conductores/subir")
async def subir_catalogo_personalizado(file: UploadFile = File(...)):
    """Sustituye el catálogo activo por uno subido por el usuario, sin
    tocar el archivo de fábrica en disco. Si el archivo no es válido,
    el catálogo activo anterior se mantiene sin cambios."""
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="El catálogo debe ser un archivo .csv.")
    contenido = await file.read()
    df, categoria = _parsear_catalogo_bytes(contenido)
    if categoria:
        mensaje_ui = MENSAJES_CORTOS_UI.get(categoria, "No se pudo cargar el catálogo subido.")
        return {"catalogo_ok": False, "mensaje": mensaje_ui}

    CATALOGO_ACTIVO.update({
        "df": df, "ok": True, "categoria": None,
        "origen": "personalizado", "nombre_archivo": file.filename,
        "raw_bytes": contenido
    })
    return {
        "catalogo_ok": True,
        "mensaje": f"Catálogo personalizado '{file.filename}' cargado correctamente ({len(df)} conductores).",
        "origen": "personalizado", "nombre_archivo": file.filename
    }

@app.post("/catalogo-conductores/usar-defecto")
def volver_a_catalogo_defecto():
    """Descarta el catálogo personalizado en memoria y recupera el de fábrica."""
    _cargar_catalogo_defecto(forzar=True)
    CATALOGO_ACTIVO.update({
        "df": DEFAULT_CATALOG_STATE["df"], "ok": DEFAULT_CATALOG_STATE["ok"],
        "categoria": DEFAULT_CATALOG_STATE["categoria"], "origen": "defecto",
        "nombre_archivo": "spanish_overhead_conductor_catalog.csv",
        "raw_bytes": DEFAULT_CATALOG_STATE["raw_bytes"]
    })
    if not CATALOGO_ACTIVO["ok"]:
        mensaje_ui = MENSAJES_CORTOS_UI.get(CATALOGO_ACTIVO["categoria"], "El catálogo por defecto no está disponible.")
        return {"catalogo_ok": False, "mensaje": mensaje_ui}
    return {"catalogo_ok": True, "mensaje": "Catálogo por defecto restaurado.", "origen": "defecto"}

# ============================================================
# MOTOR DE CÁLCULO DLR (IEEE 738 vía pypacity)
# ============================================================

def construir_cable_desde_catalogo(conductor_id: str) -> cable_mod.Cable:
    """Busca el conductor por ID (sin distinguir mayúsculas) en el
    catálogo activo y construye el objeto Cable que espera pypacity,
    combinando las propiedades del catálogo con constantes físicas
    del material (acero/aluminio) que son iguales para todos los
    conductores de este tipo."""
    if not CATALOGO_ACTIVO["ok"]:
        mensaje_ui = MENSAJES_CORTOS_UI.get(CATALOGO_ACTIVO["categoria"], "El catálogo de conductores no está disponible.")
        raise HTTPException(status_code=503, detail=mensaje_ui)
    df = CATALOGO_ACTIVO["df"]
    fila = df.loc[df['ID'].astype(str).str.upper() == conductor_id.strip().upper()]
    if fila.empty:
        disponibles = ', '.join(df['ID'].astype(str))
        raise HTTPException(status_code=400, detail=f"Conductor '{conductor_id}' no encontrado. Disponibles: {disponibles}")
    data = fila.iloc[0]
    cable_obj = cable_mod.Cable()
    cable_obj.ID = data['ID']
    cable_obj.D = float(data['D'])
    cable_obj.D1 = float(data['D1'])
    cable_obj.d = float(data['d'])
    cable_obj.TLO = float(data['TLO'])
    cable_obj.THI = float(data['THI'])
    cable_obj.TCDRMAX = float(data['TCDRMAX'])
    cable_obj.RLO = float(data['RLO']) / 1000.0
    cable_obj.RHI = float(data['RHI']) / 1000.0
    cable_obj.HNH = int(data['HNH'])
    cable_obj.HEATOUT = float(data['HEATOUT'])
    cable_obj.HEATCORE = float(data['HEATCORE'])
    cable_obj.EMISS = float(data['EMISS'])
    cable_obj.ABSORP = float(data['ABSORP'])
    cable_obj.CSteel20 = 481.0
    cable_obj.CAlum20 = 897.0
    cable_obj.BetaSteel20 = 1.00e-4
    cable_obj.BetaAlum20 = 3.80e-4
    cable_obj.mSteel = float(data['MSTEEL'])
    cable_obj.mAlum = float(data['MALUM'])
    cable_obj.lambda_ertc = 0.7
    cable_obj.Stranded = 1
    cable_obj.TCDRPRELOAD = 101.1
    cable_obj.HEATCAP = cable_obj.HEATOUT + cable_obj.HEATCORE
    return cable_obj

def calcular_ampacidad_segmento(cable_obj, temp_max, lat1, lon1, elev1, meteo1, lat2, lon2, elev2, meteo2, dia_del_anio):
    """Ampacidad de un vano concreto: promedia la meteorología de sus
    dos apoyos, monta el caso de pypacity (orientación del vano,
    elevación, latitud, día del año) y ejecuta el solver IEEE 738.
    Devuelve la ampacidad junto con los términos de balance térmico
    (radiación solar absorbida, pérdidas por radiación y convección)."""
    z1 = calcular_azimut(lat1, lon1, lat2, lon2)
    lat_media = (lat1 + lat2) / 2
    elev_media = (elev1 + elev2) / 2
    temp_media = (meteo1["temperatura_C"] + meteo2["temperatura_C"]) / 2
    viento_medio = (meteo1["viento_vel_ms"] + meteo2["viento_vel_ms"]) / 2
    dir_viento_media = (meteo1["viento_dir_grados"] + meteo2["viento_dir_grados"]) / 2
    radiacion_media = (meteo1["radiacion_Wm2"] + meteo2["radiacion_Wm2"]) / 2

    case_obj = case_mod.Case()
    case_obj.NSELECT = 2
    case_obj.TCDRPRELOAD = temp_max
    case_obj.TAMB = temp_media
    case_obj.VWIND = max(viento_medio, 0.1)
    case_obj.DWIND_DEG = dir_viento_media
    case_obj.Z1_DEG = z1
    case_obj.CDR_ELEV = elev_media
    case_obj.CDR_LAT_DEG = lat_media
    case_obj.SUN_TIME = 99
    case_obj.NDAY = dia_del_anio
    case_obj.A3 = 0
    case_obj.SolarRadiation = radiacion_media

    solver = ieee738_mod.IEEE738()
    solver.set_cable(cable_obj)
    solver.set_case(case_obj)
    solver.ieee_738()

    return {
        "ampacidad_A": round(case_obj.TR, 1),
        "temp_max_conductor_C": temp_max,
        "azimut_vano_deg": round(z1, 1),
        "elevacion_media_m": round(elev_media, 1),
        "solar_Wm": round(case_obj.QS, 1),
        "radiacion_perdida_Wm": round(case_obj.QR, 1),
        "conveccion_perdida_Wm": round(case_obj.QC, 1),
        "meteo_segmento": {
            "temperatura_C": round(temp_media, 2),
            "viento_vel_ms": round(viento_medio, 2),
            "viento_dir_grados": round(dir_viento_media, 1),
            "radiacion_Wm2": round(radiacion_media, 2)
        }
    }

def calcular_ampacidad_estatica_segmento(cable_obj, temp_max, lat1, lon1, elev1, lat2, lon2, elev2, dia_del_anio):
    """Repite el mismo cálculo de ampacidad para el vano, pero con
    condiciones meteorológicas fijas (RATING_ESTATICO_*) en vez de las
    reales de ERA5-Land. Sirve como referencia comparativa: cuánta
    capacidad extra permite el DLR frente al criterio estático clásico."""
    z1 = calcular_azimut(lat1, lon1, lat2, lon2)
    viento_dir_perpendicular = (z1 + 90) % 360
    meteo_fijo = {
        "temperatura_C": RATING_ESTATICO_TAMB_C,
        "viento_vel_ms": RATING_ESTATICO_VWIND_MS,
        "viento_dir_grados": viento_dir_perpendicular,
        "radiacion_Wm2": RATING_ESTATICO_SOLAR_WM2,
    }
    return calcular_ampacidad_segmento(
        cable_obj, temp_max, lat1, lon1, elev1, meteo_fijo, lat2, lon2, elev2, meteo_fijo, dia_del_anio
    )

# Endpoint más costoso de la API (hasta dos llamadas al solver por vano,
# más una consulta a ERA5-Land por apoyo): "def" normal, sin await real
# dentro, para que FastAPI lo ejecute en threadpool y no bloquee el resto
# del servicio mientras se resuelve.
@app.post("/calcular-dlr")
def calcular_dlr(linea: LineaDLR):
    """Calcula la ampacidad DLR de cada vano de la línea, identifica el
    vano crítico (el de menor ampacidad) y compara los resultados con
    el rating estático de referencia. Devuelve tanto el detalle por
    vano como estadísticas agregadas (percentiles, peores condiciones
    meteorológicas de la línea) que alimentan el informe del frontend."""
    try:
        if not linea.puntos or len(linea.puntos) < 2:
            return {"error": "Se necesitan al menos 2 puntos para definir un tramo."}
        if len(linea.puntos) > MAX_POINTS_PER_REQUEST:
            raise HTTPException(
                status_code=400,
                detail=f"La línea tiene {len(linea.puntos)} apoyos, por encima del límite de "
                       f"{MAX_POINTS_PER_REQUEST} por petición. Divide la línea en tramos más cortos."
            )

        puntos = linea.puntos
        nombres = [p.get('nombre', 'Desconocido') for p in puntos]
        elevaciones = [float(p['z']) if p.get('z') is not None else STANDARD_POLE_HEIGHT_M for p in puntos]

        cable_obj = construir_cable_desde_catalogo(linea.conductor)
        temp_max = linea.temp_max_override if linea.temp_max_override else cable_obj.TCDRMAX

        dia_del_anio = datetime.now().timetuple().tm_yday

        # Una consulta meteorológica por apoyo; los vanos usan la media
        # de sus dos extremos (ver calcular_ampacidad_segmento).
        meteo_por_nodo = []
        for p in puntos:
            m = obtener_meteo_punto(float(p['lat']), float(p['lng']), anio=linea.anio, mes=linea.mes)
            meteo_por_nodo.append(m)

        segmentos = []
        ampacidades_estaticas_seg = []
        for i in range(len(puntos) - 1):
            lat1, lon1 = float(puntos[i]['lat']), float(puntos[i]['lng'])
            lat2, lon2 = float(puntos[i+1]['lat']), float(puntos[i+1]['lng'])
            resultado_seg = calcular_ampacidad_segmento(
                cable_obj, temp_max,
                lat1, lon1, elevaciones[i], meteo_por_nodo[i],
                lat2, lon2, elevaciones[i+1], meteo_por_nodo[i+1],
                dia_del_anio
            )
            segmentos.append({
                "id_segmento": i + 1,
                "nodo_origen": nombres[i] if i < len(nombres) else f"Apoyo {i+1}",
                "nodo_destino": nombres[i+1] if (i+1) < len(nombres) else f"Apoyo {i+2}",
                "dlr": resultado_seg
            })
            resultado_estatico = calcular_ampacidad_estatica_segmento(
                cable_obj, temp_max,
                lat1, lon1, elevaciones[i],
                lat2, lon2, elevaciones[i+1],
                dia_del_anio
            )
            ampacidades_estaticas_seg.append(resultado_estatico["ampacidad_A"])

        resultados_nodos = []
        for i, p in enumerate(puntos):
            m = meteo_por_nodo[i]
            resultados_nodos.append({
                "id_apoyo": i + 1,
                "nombre": nombres[i] if i < len(nombres) else f"Apoyo {i+1}",
                "altura_m": elevaciones[i],
                "meteo": {
                    "temperatura_C": m["temperatura_C"], "viento_vel_ms": m["viento_vel_ms"],
                    "viento_dir_grados": m["viento_dir_grados"], "radiacion_Wm2": m["radiacion_Wm2"]
                }
            })

        # El vano crítico es el de menor ampacidad: ese es el que
        # realmente limita cuánta corriente puede transportar la línea.
        ampacidades_seg = [s["dlr"]["ampacidad_A"] for s in segmentos]
        idx_min = ampacidades_seg.index(min(ampacidades_seg))
        segmento_critico = segmentos[idx_min]

        ampacidad_media_A = sum(ampacidades_seg) / len(ampacidades_seg)
        ampacidad_estatica_media_A = sum(ampacidades_estaticas_seg) / len(ampacidades_estaticas_seg)
        ampacidad_estatica_critico_A = ampacidades_estaticas_seg[idx_min]

        ganancia_pct_media = ((ampacidad_media_A - ampacidad_estatica_media_A) / ampacidad_estatica_media_A) * 100
        ganancia_pct_vano_critico = ((ampacidades_seg[idx_min] - ampacidad_estatica_critico_A) / ampacidad_estatica_critico_A) * 100

        # Peor temperatura/viento/radiación de toda la línea, vano a vano:
        # no tiene por qué coincidir con el vano crítico, porque la
        # ampacidad depende del efecto combinado de las tres variables.
        temps_seg = [s["dlr"]["meteo_segmento"]["temperatura_C"] for s in segmentos]
        vientos_seg = [s["dlr"]["meteo_segmento"]["viento_vel_ms"] for s in segmentos]
        radiaciones_seg = [s["dlr"]["meteo_segmento"]["radiacion_Wm2"] for s in segmentos]
        idx_peor_temp = temps_seg.index(max(temps_seg))
        idx_peor_viento = vientos_seg.index(min(vientos_seg))
        idx_peor_radiacion = radiaciones_seg.index(max(radiaciones_seg))

        return {
            "mensaje": "Cálculo DLR completado con pypacity (IEEE 738) — por segmento, meteorología ERA5-Land",
            "estadisticas_linea": {
                "conductor_usado": cable_obj.ID,
                "diametro_mm": cable_obj.D,
                "ampacidad_min_A": min(ampacidades_seg),
                "ampacidad_max_A": max(ampacidades_seg),
                "ampacidad_media_A": round(ampacidad_media_A, 1),
                "segmento_critico_id": segmento_critico["id_segmento"],
                "nodos_criticos_ids": [idx_min + 1, idx_min + 2],
                "vano_critico_id": idx_min + 1,
                "rating_estatico_min_A": round(min(ampacidades_estaticas_seg), 1),
                "rating_estatico_max_A": round(max(ampacidades_estaticas_seg), 1),
                "rating_estatico_media_A": round(ampacidad_estatica_media_A, 1),
                "rating_estatico_vano_critico_A": round(ampacidad_estatica_critico_A, 1),
                "ganancia_pct_media": round(ganancia_pct_media, 1),
                "ganancia_pct_vano_critico": round(ganancia_pct_vano_critico, 1),
                "percentil_10_A": round(float(np.percentile(ampacidades_seg, 10)), 1),
                "percentil_50_A": round(float(np.percentile(ampacidades_seg, 50)), 1),
                "percentil_90_A": round(float(np.percentile(ampacidades_seg, 90)), 1),
                "desviacion_std_A": round(float(np.std(ampacidades_seg)), 1),
                "rango_A": round(max(ampacidades_seg) - min(ampacidades_seg), 1),
                "peor_temperatura_C": temps_seg[idx_peor_temp],
                "peor_temperatura_vano_id": idx_peor_temp + 1,
                "peor_viento_ms": vientos_seg[idx_peor_viento],
                "peor_viento_vano_id": idx_peor_viento + 1,
                "peor_radiacion_Wm2": radiaciones_seg[idx_peor_radiacion],
                "peor_radiacion_vano_id": idx_peor_radiacion + 1
            },
            "nodos": resultados_nodos,
            "segmentos": segmentos
        }

    except HTTPException:
        raise
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        return {"error": str(e)}

# ============================================================
# INFORME PDF
# ============================================================

def generar_informe_pdf(data: dict) -> bytes:
    """Construye un PDF de 3 páginas a partir del JSON que ya devolvió
    /calcular-dlr, sin recalcular nada: (1) resumen y comparación con
    el rating estático, (2) distribución de ampacidad por vano
    mediante estimación de densidad kernel, (3) condiciones del vano
    crítico y peores condiciones meteorológicas de la línea."""
    est = data["estadisticas_linea"]
    segmentos = data["segmentos"]
    seg_critico = segmentos[est["segmento_critico_id"] - 1]
    ampacidades = [s["dlr"]["ampacidad_A"] for s in segmentos]
    titulo_conductor = f"Informe de Estimación DLR — Conductor: {est['conductor_usado']}  (Ø {est['diametro_mm']} mm)"

    def titulo_pagina(subtitulo):
        return titulo_conductor + "\n" + subtitulo

    buf = io.BytesIO()
    with PdfPages(buf) as pdf_pages:

        # Página 1: resumen numérico y comparación de barras estático vs. DLR.
        fig1, (ax_txt, ax_bar) = plt.subplots(
            2, 1, figsize=(11.7, 8.3), gridspec_kw={"height_ratios": [0.8, 1.6]}, constrained_layout=True
        )
        fig1.suptitle(titulo_pagina("Resumen y comparación con rating estático"), fontsize=13, fontweight="bold")
        ax_txt.axis("off")
        resumen = (
            f"Ampacidad mínima: {est['ampacidad_min_A']:.0f} A     "
            f"media: {est['ampacidad_media_A']:.0f} A     "
            f"máxima: {est['ampacidad_max_A']:.0f} A"
        )
        ax_txt.text(0.0, 0.65, resumen, fontsize=13, transform=ax_txt.transAxes)
        ax_txt.text(0.0, 0.25, f"Vano más crítico: {seg_critico['nodo_origen']} → {seg_critico['nodo_destino']}",
                    fontsize=13, color="#a52626", transform=ax_txt.transAxes)

        etiquetas = ["Rating estático", "DLR calculado"]
        valores = [est["rating_estatico_media_A"], est["ampacidad_media_A"]]
        colores = ["#7f7f7f", "#1f77b4"]
        ax_bar.barh(etiquetas, valores, color=colores, height=0.45)
        for i, v in enumerate(valores):
            ax_bar.text(v + max(valores) * 0.02, i, f"{v:.0f} A", va="center", fontsize=11)
        ax_bar.set_xlim(0, max(valores) * 1.25)
        ax_bar.set_xlabel("Ampacidad media de la línea (A)")
        ax_bar.set_title(f"Ganancia: {est['ganancia_pct_media']:+.1f} % respecto al rating estático (40°C, viento 0.61 m/s)")
        pdf_pages.savefig(fig1)
        plt.close(fig1)

        # Página 2: distribución de ampacidad por vano (KDE) con percentiles.
        fig2, (ax_kde, ax_caption) = plt.subplots(
            2, 1, figsize=(11.7, 8.3), gridspec_kw={"height_ratios": [5, 0.9]}, constrained_layout=True
        )
        fig2.suptitle(titulo_pagina("Distribución de ampacidad por vano"), fontsize=13, fontweight="bold")
        if len(set(ampacidades)) > 1:
            kde = gaussian_kde(ampacidades)
            margen = (max(ampacidades) - min(ampacidades)) * 0.4
            xs = np.linspace(min(ampacidades) - margen, max(ampacidades) + margen, 200)
            ys = kde(xs)
        else:
            xs = np.array([ampacidades[0] - 10, ampacidades[0], ampacidades[0] + 10])
            ys = np.array([0.0, 1.0, 0.0])
        ax_kde.plot(xs, ys, color="#1f77b4", linewidth=1.5)
        ax_kde.fill_between(xs, ys, color="#1f77b4", alpha=0.15)
        for p, color, estilo, etiqueta in [
            (est["percentil_10_A"], "#7f7f7f", "--", "P10"),
            (est["percentil_50_A"], "#333333", ":", "P50 (mediana)"),
            (est["percentil_90_A"], "#7f7f7f", "--", "P90"),
        ]:
            ax_kde.axvline(p, color=color, linestyle=estilo, linewidth=1.2, label=f"{etiqueta} = {p:.0f} A")
        ax_kde.set_xlabel("Ampacidad (A)")
        ax_kde.set_ylabel("Densidad de probabilidad")
        ax_kde.legend(fontsize=9, loc="upper right")

        ax_caption.axis("off")
        ax_caption.text(
            0.5, 0.5,
            "El eje vertical no es una magnitud física: indica en qué zona de ampacidad se concentran más vanos.\n"
            "Cuanto más alta la curva en un punto, más vanos de la línea tienen una ampacidad cercana a ese valor.",
            fontsize=9, color="#555555", ha="center", va="center", transform=ax_caption.transAxes
        )
        pdf_pages.savefig(fig2)
        plt.close(fig2)

        # Página 3: condiciones del vano crítico y peores valores de la línea.
        fig3, ax3 = plt.subplots(figsize=(11.7, 8.3), constrained_layout=True)
        fig3.suptitle(titulo_pagina("Detalles interesantes"), fontsize=13, fontweight="bold")
        ax3.axis("off")

        meteo_c = seg_critico["dlr"]["meteo_segmento"]
        bloque_critico = (
            "Condiciones del vano más crítico\n\n"
            f"Vano: {seg_critico['nodo_origen']} → {seg_critico['nodo_destino']}\n"
            f"Temperatura ambiente: {meteo_c['temperatura_C']:.1f} °C\n"
            f"Viento: {meteo_c['viento_vel_ms']:.2f} m/s, dirección {meteo_c['viento_dir_grados']:.0f}°\n"
            f"Radiación solar: {meteo_c['radiacion_Wm2']:.0f} W/m²"
        )
        bloque_peores = (
            "Peores condiciones meteorológicas de toda la línea\n\n"
            f"Temperatura más alta: {est['peor_temperatura_C']:.1f} °C, en el vano nº {est['peor_temperatura_vano_id']}\n"
            f"Viento más flojo: {est['peor_viento_ms']:.2f} m/s, en el vano nº {est['peor_viento_vano_id']}\n"
            f"Radiación más alta: {est['peor_radiacion_Wm2']:.0f} W/m², en el vano nº {est['peor_radiacion_vano_id']}"
        )
        ax3.text(0.0, 0.95, bloque_critico, fontsize=12.5, va="top", transform=ax3.transAxes,
                 linespacing=1.9)
        ax3.text(0.0, 0.48, bloque_peores, fontsize=12.5, va="top", transform=ax3.transAxes,
                 linespacing=1.9)

        vanos_dispares = len({est["vano_critico_id"], est["peor_temperatura_vano_id"], est["peor_viento_vano_id"], est["peor_radiacion_vano_id"]}) > 1
        if vanos_dispares:
            ax3.text(0.0, 0.12,
                     "El vano más crítico no coincide necesariamente con el de peor condición individual: la\n"
                     "ampacidad depende del efecto combinado de las tres variables, no solo de la más\n"
                     "desfavorable por separado.",
                     fontsize=10, color="#666666", style="italic", va="top", transform=ax3.transAxes, linespacing=1.6)

        pdf_pages.savefig(fig3)
        plt.close(fig3)

    buf.seek(0)
    return buf.read()

# Generar el PDF con matplotlib es trabajo de CPU sin await real: "def"
# normal para que corra en threadpool.
@app.post("/informe-pdf")
def informe_pdf_endpoint(payload: InformeRequest):
    try:
        pdf_bytes = generar_informe_pdf(payload.resultado)
        return Response(content=pdf_bytes, media_type="application/pdf")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo generar el informe: {str(e)}")