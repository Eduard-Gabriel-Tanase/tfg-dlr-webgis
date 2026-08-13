from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import csv
import io
from tempfile import NamedTemporaryFile
import os
import requests
import pandas as pd
from pyproj import Transformer
import time
from datetime import datetime
from math import radians, degrees, atan2, sin, cos

# --- PYPACITY: librería de cálculo de ampacidad (IEEE 738 / CIGRE 601) ---
from pypacity.cable import cable as cable_mod
from pypacity.case import case as case_mod
from pypacity.ieee738 import ieee738 as ieee738_mod

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
    conductor: Optional[str] = "Opción A"


class LineaDLR(BaseModel):
    puntos: list[dict]           # [{lat, lng, nombre}, ...]
    conductor: str = "DRAKE"     # ID tal como aparece en cable_db.csv (pypacity)
    temp_max_override: Optional[float] = None  # límite térmico manual, si se quiere sobreescribir


@app.get("/ping")
async def ping():
    return {"ping": "pong"}


@app.post("/upload-csv")
async def upload_file(file: UploadFile = File(...)):
    filename = file.filename.lower()
    if not (filename.endswith('.csv') or filename.endswith('.zip') or filename.endswith('.xlsx') or filename.endswith('.xls')):
        raise HTTPException(status_code=400, detail="El archivo debe ser CSV, Excel (.xlsx) o Shapefile (.zip)")
    try:
        lista_puntos = []
        if filename.endswith('.xlsx') or filename.endswith('.xls') or filename.endswith('.csv'):
            contents = await file.read()
            if filename.endswith('.csv'):
                decoded_contents = contents.decode("utf-8")
                df = pd.read_csv(io.StringIO(decoded_contents))
            else:
                df = pd.read_excel(io.BytesIO(contents))

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
                    raise HTTPException(status_code=400, detail="El archivo debe contener columnas X/Y o Lat/Lon")

            transformer = Transformer.from_crs("EPSG:25830", "EPSG:4326", always_xy=True) if is_utm else None

            for index, row in df.iterrows():
                try:
                    if is_utm:
                        x_str = str(row['X']).replace(',', '.')
                        y_str = str(row['Y']).replace(',', '.')
                        lon, lat = transformer.transform(float(x_str), float(y_str))
                    else:
                        lat = float(str(row['LATITUD']).replace(',', '.'))
                        lon = float(str(row['LONGITUD']).replace(',', '.'))

                    nombre_poste = str(row.get('STRUCTURE COMMENT', f"Apoyo {index+1}"))
                    lista_puntos.append({"lat": lat, "lng": lon, "nombre": nombre_poste})
                except Exception as row_e:
                    continue

        return {"mensaje": f"Archivo leído correctamente", "puntos": lista_puntos}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error procesando el archivo: {str(e)}")


# --- CACHÉ Y EXTRACTOR DE DATOS ATMOSFÉRICOS (ALTA DENSIDAD) ---
CACHE_METEO = {"timestamp": 0, "data": None}


@app.get("/api/capas_atmosfericas")
def get_capas_atmosfericas():
    global CACHE_METEO
    if time.time() - CACHE_METEO["timestamp"] < 3600 and CACHE_METEO["data"] is not None:
        return CACHE_METEO["data"]

    data_result = {"temp": [], "viento": [], "rad": []}

    try:
        lats, lons = [], []

        # Malla de altísima densidad (20x20 = 400 puntos) para que la sensibilidad
        # térmica en el mapa sea perfecta y refleje el terreno detallado
        for i in range(20):
            for j in range(20):
                lats.append(str(round(35.5 + i*(9.0/19.0), 4)))
                lons.append(str(round(-9.5 + j*(14.0/19.0), 4)))

        url_om = "https://api.open-meteo.com/v1/forecast"

        # Peticiones en lotes para no saturar Open-Meteo
        for batch_idx in range(0, len(lats), 90):
            batch_lats = lats[batch_idx:batch_idx+90]
            batch_lons = lons[batch_idx:batch_idx+90]

            params = {
                "latitude": ",".join(batch_lats),
                "longitude": ",".join(batch_lons),
                "current": "temperature_2m,wind_speed_10m,shortwave_radiation",
                "timezone": "Europe/Madrid"
            }
            res_om = requests.get(url_om, params=params, timeout=15)
            if res_om.status_code == 200:
                dlist = res_om.json()
                if isinstance(dlist, list):
                    for d in dlist:
                        curr = d.get("current", {})
                        temp = curr.get("temperature_2m", 0)
                        viento = round(curr.get("wind_speed_10m", 0) / 3.6, 2)
                        rad = curr.get("shortwave_radiation", 0)

                        data_result["temp"].append({"lat": d["latitude"], "lng": d["longitude"], "val": temp})
                        data_result["viento"].append({"lat": d["latitude"], "lng": d["longitude"], "val": viento})
                        data_result["rad"].append({"lat": d["latitude"], "lng": d["longitude"], "val": rad})

            time.sleep(0.3)

    except Exception as e:
        print("Error Open-Meteo (Capas Globales):", e)
        return {"error": "Fallo al conectar con el servidor meteorológico global."}

    CACHE_METEO["timestamp"] = time.time()
    CACHE_METEO["data"] = data_result
    return data_result


@app.post("/linea")
async def recibir_linea(linea: Linea):
    try:
        if not linea.puntos:
            return {"error": "No se han enviado puntos."}

        latitudes = []
        longitudes = []
        nombres = []
        for p in linea.puntos:
            latitudes.append(str(round(p['lat'], 4)))
            longitudes.append(str(round(p['lng'], 4)))
            nombres.append(p.get('nombre', 'Desconocido'))

        if len(latitudes) > 90:
            latitudes = latitudes[:90]
            longitudes = longitudes[:90]
            nombres = nombres[:90]

        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": ",".join(latitudes),
            "longitude": ",".join(longitudes),
            "current": "temperature_2m,wind_speed_10m,wind_direction_10m,shortwave_radiation",
            "timezone": "Europe/Madrid"
        }

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        datos_meteo = response.json()

        if not isinstance(datos_meteo, list):
            datos_meteo = [datos_meteo]

        resultados_nodos = []
        for i, data in enumerate(datos_meteo):
            current = data.get("current", {})
            temp = current.get("temperature_2m", 25.0)
            vel_viento_ms = round(current.get("wind_speed_10m", 0.0) / 3.6, 2)

            nodo_nombre = nombres[i] if i < len(nombres) else f"Apoyo {i+1}"

            resultados_nodos.append({
                "id_apoyo": i + 1,
                "nombre": nodo_nombre,
                "meteo": {
                    "temperatura_C": temp,
                    "viento_vel_ms": vel_viento_ms,
                    "viento_dir_grados": current.get("wind_direction_10m", 0),
                    "radiacion_Wm2": current.get("shortwave_radiation", 0.0)
                }
            })

        temps = [n["meteo"]["temperatura_C"] for n in resultados_nodos]
        vientos = [n["meteo"]["viento_vel_ms"] for n in resultados_nodos]

        return {
            "mensaje": "Cálculo meteorológico completado",
            "estadisticas_linea": {
                "conductor_usado": linea.conductor,
                "temp_max": max(temps),
                "temp_min": min(temps),
                "temp_media": round(sum(temps) / len(temps), 2),
                "viento_min": min(vientos),
                "vano_critico_id": temps.index(max(temps)) + 1
            },
            "nodos": resultados_nodos
        }

    except Exception as e:
        return {"error": str(e)}


# =========================================================
# HELPER: azimut (rumbo) entre dos puntos lat/lng, en grados
# desde el norte (necesario para Case1.Z1_DEG en pypacity)
# =========================================================
def calcular_azimut(lat1, lon1, lat2, lon2):
    lat1_r, lat2_r = radians(lat1), radians(lat2)
    dlon_r = radians(lon2 - lon1)
    x = sin(dlon_r) * cos(lat2_r)
    y = cos(lat1_r) * sin(lat2_r) - sin(lat1_r) * cos(lat2_r) * cos(dlon_r)
    brng = (degrees(atan2(x, y)) + 360) % 360
    return brng


# =========================================================
# NUEVO ENDPOINT: catálogo de conductores disponibles en pypacity
# (para rellenar el <select> del index.html en vez de las
# opciones de prueba "Opción A/B/C")
# =========================================================
@app.get("/catalogo-conductores")
def get_catalogo_conductores():
    try:
        c = cable_mod.Cable()
        db, error = c.load_cable_db()
        if error:
            raise HTTPException(status_code=500, detail="La base de datos de conductores está vacía.")
        cols = [col for col in ["ID", "D", "TCDRMAX", "RLO", "RHI"] if col in db.columns]
        return db[cols].to_dict(orient="records")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error leyendo catálogo: {str(e)}")


# =========================================================
# NUEVO ENDPOINT: cálculo DLR real con pypacity (IEEE 738)
# =========================================================
@app.post("/calcular-dlr")
async def calcular_dlr(linea: LineaDLR):
    try:
        if not linea.puntos or len(linea.puntos) < 2:
            return {"error": "Se necesitan al menos 2 puntos para definir un tramo."}

        latitudes = [str(round(p['lat'], 4)) for p in linea.puntos]
        longitudes = [str(round(p['lng'], 4)) for p in linea.puntos]
        nombres = [p.get('nombre', 'Desconocido') for p in linea.puntos]

        if len(latitudes) > 90:
            latitudes, longitudes, nombres = latitudes[:90], longitudes[:90], nombres[:90]

        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": ",".join(latitudes),
            "longitude": ",".join(longitudes),
            "current": "temperature_2m,wind_speed_10m,wind_direction_10m,shortwave_radiation",
            "timezone": "Europe/Madrid"
        }
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        datos_meteo = response.json()
        if not isinstance(datos_meteo, list):
            datos_meteo = [datos_meteo]

        # Cable único para toda la línea (mismo conductor en todos los vanos)
        cable_obj = cable_mod.Cable()
        cable_obj.set_cable(NSELECT=2, conductor=linea.conductor)
        temp_max = linea.temp_max_override if linea.temp_max_override else cable_obj.TCDRMAX

        dia_del_anio = datetime.now().timetuple().tm_yday
        resultados_nodos = []

        for i, data in enumerate(datos_meteo):
            current = data.get("current", {})
            temp = current.get("temperature_2m", 25.0)
            vel_viento_ms = round(current.get("wind_speed_10m", 0.0) / 3.6, 2)
            dir_viento = current.get("wind_direction_10m", 0)
            radiacion = current.get("shortwave_radiation", 0.0)
            lat = float(latitudes[i])
            lon = float(longitudes[i])

            if i < len(linea.puntos) - 1:
                z1 = calcular_azimut(lat, lon, linea.puntos[i+1]['lat'], linea.puntos[i+1]['lng'])
            else:
                z1 = calcular_azimut(linea.puntos[i-1]['lat'], linea.puntos[i-1]['lng'], lat, lon)

            case_obj = case_mod.Case()
            case_obj.NSELECT = 2                      # Ampacidad en régimen estacionario
            case_obj.TCDRPRELOAD = temp_max            # Temperatura máxima admisible del conductor
            case_obj.TAMB = temp
            case_obj.VWIND = max(vel_viento_ms, 0.1)   # evitar división por 0 en el modelo
            case_obj.DWIND_DEG = dir_viento
            case_obj.Z1_DEG = z1
            case_obj.CDR_ELEV = 0.0                    # TODO: sustituir por elevación real (DEM) si se dispone
            case_obj.CDR_LAT_DEG = lat
            case_obj.SUN_TIME = 99                     # usar SolarRadiation medida directamente
            case_obj.NDAY = dia_del_anio
            case_obj.A3 = 0                             # atmósfera despejada
            case_obj.SolarRadiation = radiacion

            solver = ieee738_mod.IEEE738()
            solver.set_cable(cable_obj)
            solver.set_case(case_obj)
            # OJO: el método real instalado (pypacity 0.1.3) es "ieee_738", con guion bajo,
            # NO "ieee738". Verificado con dir(solver) en la consola del usuario.
            solver.ieee_738()

            resultados_nodos.append({
                "id_apoyo": i + 1,
                "nombre": nombres[i] if i < len(nombres) else f"Apoyo {i+1}",
                "meteo": {
                    "temperatura_C": temp,
                    "viento_vel_ms": vel_viento_ms,
                    "viento_dir_grados": dir_viento,
                    "radiacion_Wm2": radiacion
                },
                "dlr": {
                    "ampacidad_A": round(case_obj.TR, 1),
                    "temp_max_conductor_C": temp_max,
                    "azimut_vano_deg": round(z1, 1),
                    "solar_Wm": round(case_obj.QS, 1),
                    "radiacion_perdida_Wm": round(case_obj.QR, 1),
                    "conveccion_perdida_Wm": round(case_obj.QC, 1)
                }
            })

        ampacidades = [n["dlr"]["ampacidad_A"] for n in resultados_nodos]
        vano_critico = ampacidades.index(min(ampacidades)) + 1

        return {
            "mensaje": "Cálculo DLR completado con pypacity (IEEE 738)",
            "estadisticas_linea": {
                "conductor_usado": cable_obj.ID,
                "ampacidad_min_A": min(ampacidades),
                "ampacidad_max_A": max(ampacidades),
                "ampacidad_media_A": round(sum(ampacidades) / len(ampacidades), 1),
                "vano_critico_id": vano_critico
            },
            "nodos": resultados_nodos
        }

    except ValueError as ve:
        # Se lanza cuando el conductor pedido no existe en cable_db.csv
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        return {"error": str(e)}
