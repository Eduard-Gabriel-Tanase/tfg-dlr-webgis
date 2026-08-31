"""
Pruebas de integración sobre la carga de CSV (línea y catálogo).

Cada test sube un archivo real desde fixtures_csv/ o fixtures_catalogo/
al endpoint correspondiente y comprueba la respuesta del backend. El
identificador de cada test (A01, A02... B01, B02...) corresponde a un
caso de Taxonomia_Casos_Error_CSV.md.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

DIR_LINEA = os.path.join(os.path.dirname(__file__), "fixtures_csv")
DIR_CATALOGO = os.path.join(os.path.dirname(__file__), "fixtures_catalogo")


def subir_linea(nombre_archivo):
    ruta = os.path.join(DIR_LINEA, nombre_archivo)
    with open(ruta, "rb") as f:
        contenido = f.read()
    return client.post("/upload-csv", files={"file": (nombre_archivo, contenido, "text/csv")})


def subir_catalogo(nombre_archivo):
    ruta = os.path.join(DIR_CATALOGO, nombre_archivo)
    with open(ruta, "rb") as f:
        contenido = f.read()
    return client.post("/catalogo-conductores/subir", files={"file": (nombre_archivo, contenido, "text/csv")})


class TestImportacionLinea:

    def test_A01_extension_no_soportada(self):
        r = subir_linea("A01_extension_no_soportada.txt")
        assert r.status_code == 400

    def test_A02_separador_puntoycoma_wgs84(self):
        r = subir_linea("A02_separador_puntoycoma_wgs84.csv")
        assert r.status_code == 200
        assert len(r.json()["puntos"]) == 2

    def test_A03_separador_coma_utm(self):
        r = subir_linea("A03_separador_coma_utm.csv")
        assert r.status_code == 200
        assert len(r.json()["puntos"]) == 2

    def test_A04_con_bom_de_excel(self):
        r = subir_linea("A04_con_bom_de_excel.csv")
        assert r.status_code == 200
        assert len(r.json()["puntos"]) == 2

    def test_A05_encoding_invalido(self):
        r = subir_linea("A05_encoding_invalido.csv")
        assert r.status_code == 500

    def test_A06_columnas_no_reconocidas(self):
        r = subir_linea("A06_columnas_no_reconocidas.csv")
        assert r.status_code == 400
        assert "columnas" in r.json()["detail"].lower()

    def test_A07_columnas_nombre_alternativo(self):
        r = subir_linea("A07_columnas_nombre_alternativo.csv")
        assert r.status_code == 200
        assert len(r.json()["puntos"]) == 2

    def test_A08_sin_columna_de_altura(self):
        r = subir_linea("A02_separador_puntoycoma_wgs84.csv")
        assert r.status_code == 200
        assert r.json()["tiene_altura_real"] is False

    def test_A09_altura_no_parseable_en_una_fila(self):
        r = subir_linea("A09_altura_no_parseable_en_una_fila.csv")
        assert r.status_code == 200
        puntos = r.json()["puntos"]
        assert len(puntos) == 2
        assert "z" not in puntos[1]

    def test_A10_coma_decimal_espanola(self):
        r = subir_linea("A10_coma_decimal_espanola.csv")
        assert r.status_code == 200
        assert len(r.json()["puntos"]) == 2

    def test_A11_miles_y_coma_decimal(self):
        r = subir_linea("A11_miles_y_coma_decimal.csv")
        assert r.status_code == 200
        assert len(r.json()["puntos"]) == 2

    def test_A12_coordenada_vacia_en_una_fila(self):
        r = subir_linea("A12_coordenada_vacia_en_una_fila.csv")
        assert r.status_code == 200
        assert len(r.json()["puntos"]) == 2

    def test_A13_texto_no_numerico_en_una_fila(self):
        r = subir_linea("A13_texto_no_numerico_en_una_fila.csv")
        assert r.status_code == 200
        assert len(r.json()["puntos"]) == 1

    def test_A14_coordenadas_utm_en_milimetros(self):
        r = subir_linea("A14_coordenadas_utm_en_milimetros.csv")
        assert r.status_code == 200
        assert len(r.json()["puntos"]) == 2

    def test_A15_coordenada_fuera_de_rango(self):
        r = subir_linea("A15_coordenada_fuera_de_rango.csv")
        assert r.status_code == 200
        assert len(r.json()["puntos"]) == 1

    def test_A16_todas_las_filas_invalidas(self):
        r = subir_linea("A16_todas_las_filas_invalidas.csv")
        assert r.status_code == 400
        assert "ninguna coordenada" in r.json()["detail"].lower()

    def test_A16b_archivo_sin_filas_de_datos(self):
        r = subir_linea("A16b_archivo_sin_filas_de_datos.csv")
        assert r.status_code == 400

    def test_A17_apoyos_duplicados(self):
        r = subir_linea("A17_apoyos_duplicados.csv")
        assert r.status_code == 200
        assert len(r.json()["duplicados"]) >= 1

    def test_A18_vano_atipico(self):
        r = subir_linea("A18_vano_atipico.csv")
        assert r.status_code == 200
        assert len(r.json()["outliers"]) >= 1

    def test_A19_sin_columna_de_nombre(self):
        r = subir_linea("A19_sin_columna_de_nombre.csv")
        assert r.status_code == 200
        assert r.json()["puntos"][0]["nombre"].startswith("Apoyo")

    def test_A20_mas_de_500_apoyos_es_rechazado(self):
        r = subir_linea("A20_mas_de_500_apoyos.csv")
        assert r.status_code == 400
        assert "500" in r.json()["detail"]

    def test_A21_csv_mal_formado_da_error_controlado(self):
        r = subir_linea("A21_csv_mal_formado.csv")
        assert r.status_code in (400, 500)


class TestCatalogoConductores:

    def test_B00_catalogo_valido_es_aceptado(self):
        r = subir_catalogo("B00_catalogo_valido.csv")
        assert r.json()["catalogo_ok"] is True

    def test_B01_extension_no_csv(self):
        r = subir_catalogo("B01_extension_no_csv.txt")
        assert r.status_code == 400

    def test_B02_encoding_invalido(self):
        r = subir_catalogo("B02_encoding_invalido.csv")
        assert r.json()["catalogo_ok"] is False

    def test_B03_archivo_vacio(self):
        r = subir_catalogo("B03_archivo_vacio.csv")
        assert r.json()["catalogo_ok"] is False

    def test_B04_error_lectura_generico(self):
        r = subir_catalogo("B04_error_lectura_generico.csv")
        assert r.json()["catalogo_ok"] is False

    def test_B05_sin_filas(self):
        r = subir_catalogo("B05_sin_filas.csv")
        assert r.json()["catalogo_ok"] is False

    def test_B06_separador_incorrecto(self):
        r = subir_catalogo("B06_separador_incorrecto.csv")
        assert r.json()["catalogo_ok"] is False

    def test_B07_columnas_faltantes(self):
        r = subir_catalogo("B07_columnas_faltantes.csv")
        assert r.json()["catalogo_ok"] is False

    def test_B08_ids_duplicados(self):
        r = subir_catalogo("B08_ids_duplicados.csv")
        assert r.json()["catalogo_ok"] is False

    def test_B09_valores_no_numericos(self):
        r = subir_catalogo("B09_valores_no_numericos.csv")
        assert r.json()["catalogo_ok"] is False


def test_zzz_restaurar_catalogo_por_defecto():
    r = client.post("/catalogo-conductores/usar-defecto")
    assert r.status_code == 200