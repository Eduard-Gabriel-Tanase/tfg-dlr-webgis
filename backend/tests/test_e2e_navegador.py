"""
Pruebas end-to-end sobre el frontend real, con el backend en ejecución.

Abren index.html en un navegador Chromium controlado por Playwright,
suben un archivo por el botón "Importar" y comprueban lo que se
muestra en pantalla. Requieren que el backend esté levantado con
uvicorn, ya que el navegador hace peticiones HTTP reales a la API.
"""

import os
import shutil
import pytest
from playwright.sync_api import Page, expect

FRONTEND_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "index.html")
)
FRONTEND_URL = "file:///" + FRONTEND_PATH.replace(os.sep, "/")

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures_csv")
SCREENSHOTS_DIR = os.path.join(os.path.dirname(__file__), "screenshots")


@pytest.fixture(scope="session", autouse=True)
def limpiar_screenshots_antes_de_correr():
    shutil.rmtree(SCREENSHOTS_DIR, ignore_errors=True)
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


def subir_archivo_por_ui(page: Page, nombre_archivo: str):
    ruta_csv = os.path.join(FIXTURES_DIR, nombre_archivo)
    with page.expect_file_chooser() as fc_info:
        page.locator("button:has-text('Importar')").first.click()
    file_chooser = fc_info.value
    file_chooser.set_files(ruta_csv)
    page.wait_for_timeout(1200)


def capturar(page: Page, nombre_caso: str):
    ruta = os.path.join(SCREENSHOTS_DIR, f"{nombre_caso}.png")
    page.screenshot(path=ruta, full_page=True)


class TestImportacionEnNavegadorReal:

    def test_A02_importacion_correcta_dibuja_la_linea(self, page: Page):
        page.goto(FRONTEND_URL)
        subir_archivo_por_ui(page, "A02_separador_puntoycoma_wgs84.csv")
        capturar(page, "A02_importacion_correcta")
        boton_exportar = page.locator("#btn-export-csv")
        expect(boton_exportar).to_be_enabled()

    def test_A06_columnas_no_reconocidas_muestra_alerta_roja(self, page: Page):
        page.goto(FRONTEND_URL)
        subir_archivo_por_ui(page, "A06_columnas_no_reconocidas.csv")
        capturar(page, "A06_alerta_columnas_no_reconocidas")
        alerta = page.locator("#map-alert")
        expect(alerta).to_be_visible()
        expect(page.locator("#map-alert-text")).to_contain_text("columnas")

    def test_A17_duplicados_muestra_aviso_amarillo(self, page: Page):
        page.goto(FRONTEND_URL)
        subir_archivo_por_ui(page, "A17_apoyos_duplicados.csv")
        capturar(page, "A17_aviso_duplicados")
        aviso = page.locator("#aviso-import-datos")
        expect(aviso).to_be_visible()
        expect(page.locator("#aviso-import-contenido")).to_contain_text("duplicado")

    def test_A18_vano_atipico_muestra_aviso_amarillo(self, page: Page):
        page.goto(FRONTEND_URL)
        subir_archivo_por_ui(page, "A18_vano_atipico.csv")
        capturar(page, "A18_aviso_vano_atipico")
        aviso = page.locator("#aviso-import-datos")
        expect(aviso).to_be_visible()
        expect(page.locator("#aviso-import-contenido")).to_contain_text("atípico")

    def test_A20_mas_de_500_apoyos_es_rechazado_en_ui(self, page: Page):
        page.goto(FRONTEND_URL)
        subir_archivo_por_ui(page, "A20_mas_de_500_apoyos.csv")
        capturar(page, "A20_rechazo_limite_500")
        alerta = page.locator("#map-alert")
        expect(alerta).to_be_visible()
        expect(page.locator("#map-alert-text")).to_contain_text("500")

    def test_A01_extension_no_soportada_es_rechazada_en_ui(self, page: Page):
        page.goto(FRONTEND_URL)
        subir_archivo_por_ui(page, "A01_extension_no_soportada.txt")
        capturar(page, "A01_extension_no_soportada")
        alerta = page.locator("#map-alert")
        expect(alerta).to_be_visible()

    def test_A04_con_bom_de_excel_se_importa_bien(self, page: Page):
        page.goto(FRONTEND_URL)
        subir_archivo_por_ui(page, "A04_con_bom_de_excel.csv")
        capturar(page, "A04_bom_excel_importado")
        boton_exportar = page.locator("#btn-export-csv")
        expect(boton_exportar).to_be_enabled()

    def test_A16_todas_las_filas_invalidas_es_rechazado(self, page: Page):
        page.goto(FRONTEND_URL)
        subir_archivo_por_ui(page, "A16_todas_las_filas_invalidas.csv")
        capturar(page, "A16_todas_invalidas")
        alerta = page.locator("#map-alert")
        expect(alerta).to_be_visible()
