# TFG DLR Web-GIS

Plataforma web con componente GIS para el trazado y análisis de líneas eléctricas aéreas, con estimación de su capacidad dinámica (Dynamic Line Rating, DLR) a partir de datos meteorológicos ERA5-Land y el modelo térmico IEEE 738.

## Instalación con Docker

### Requisitos previos

- Docker y Docker Compose instalados en el sistema.

### Puesta en marcha

Desde la raíz del repositorio, construir y levantar el contenedor:

```bash
docker compose up --build
```

La aplicación se sirve íntegramente (backend y frontend) desde un único contenedor a través de FastAPI, en el puerto 8000. Una vez esté en marcha, acceder desde el navegador en:

**http://localhost:8000**

Para detener la aplicación:

```bash
docker compose down
```

---

## Guía de usuario

### Vista general

La interfaz se divide en dos zonas: un panel lateral con todas las herramientas y opciones, y una zona principal con dos pestañas, **Mapa** y **Resultados**. La pestaña de Resultados permanece deshabilitada hasta que se realiza un cálculo.

El mapa muestra por defecto la Península y permite navegar con el ratón (arrastrar para desplazar, rueda para hacer zoom).

### Definir la geometría de la línea

Hay dos formas de definir el trazado de una línea: dibujándolo directamente sobre el mapa o importándolo desde un archivo.

**Trazado manual**

Al pulsar "Trazado Manual" se activa el modo de dibujo y aparece una barra de atajos en la parte inferior del mapa:

- Tecla **1**: añade un vértice en la posición actual del cursor.
- Tecla **2**: deshace el último vértice añadido.
- Tecla **3**: finaliza el trazado (requiere al menos dos apoyos).
- Tecla **0**: cancela el dibujo en curso.

Si ya existe una línea en el mapa y se pulsa "Trazado Manual" de nuevo, la aplicación pregunta si se quiere editar la línea existente o borrarla para empezar una nueva.

**Edición de vértices**

El botón "Editar Vértices" permite arrastrar cualquier punto de una línea ya trazada para reposicionarlo, añadir apoyos intermedios o eliminarlos, usando los mismos atajos de teclado (añadir, quitar, guardar o descartar los cambios).

**Eliminar traza**

Borra por completo la línea actual del mapa.

**Importar línea**

El botón "Importar" acepta archivos CSV, XLSX o XLS. El archivo puede venir en dos formatos de coordenadas:

- Columnas **X, Y** en UTM (huso 30N).
- Columnas **LATITUD, LONGITUD** (o variantes como LAT/LON) en WGS84.

Opcionalmente puede incluir una columna de altura o cota (Z, ALTURA, ELEVACION, etc.) y un nombre de apoyo (STRUCTURE COMMENT). Si el archivo trae la marca BOM característica de los CSV exportados por Excel, se procesa sin problema.

Tras importar, si el sistema detecta apoyos a menos de un metro de distancia entre sí (posibles duplicados) o vanos superiores a 5 km (posibles valores atípicos), se muestra un aviso detallado con los apoyos implicados, sin llegar a bloquear la importación.

**Exportar línea**

El botón "Exportar" descarga la traza actual como un CSV con coordenadas UTM, distancia acumulada (station) y ángulo por vano, listo para reutilizarse o compartirse.

### Capas meteorológicas

En el selector de capas del mapa (esquina superior derecha) se puede superponer una de tres variables climáticas, generadas a partir de datos ERA5-Land:

- **Temperatura ambiente**
- **Velocidad del viento**
- **Radiación solar global**

Cada capa se acompaña de una leyenda de color con su escala de valores. El panel lateral incluye un filtro temporal por año y mes para consultar la media histórica de esa combinación concreta; si no se selecciona ninguno, se muestra la media de todo el periodo disponible.

### Parámetros de línea y catálogo de conductores

En el panel lateral, el desplegable de conductor carga el catálogo activo y muestra su diámetro nominal.

Junto al diámetro aparece el control de **temperatura máxima admisible** del conductor: un slider acompañado de un campo numérico editable a mano, ambos sincronizados entre sí. Al seleccionar un conductor, el slider se ajusta automáticamente a su rango de temperaturas de diseño (definido en el propio catálogo) y toma como valor por defecto la temperatura máxima nominal de ese conductor. Este valor puede modificarse antes de calcular, para estudiar el DLR bajo un límite térmico distinto al nominal, y es el que finalmente se envía al backend como temperatura máxima del conductor (`tempmax_override`) en el cálculo.

El catálogo por defecto es un listado de conductores tipo utilizado habitualmente en líneas aéreas españolas, pero puede sustituirse por uno propio:

- **Descargar**: obtiene el catálogo que está activo en ese momento en formato CSV.
- **Subir**: permite cargar un catálogo propio en CSV, siempre que respete las columnas requeridas por el sistema.
- **Volver al catálogo por defecto**: descarta el catálogo personalizado y restaura el original.

Si el catálogo activo tiene algún problema (columnas faltantes, valores no numéricos, identificadores duplicados, etc.), la interfaz muestra un aviso explicando el motivo y bloquea el cálculo hasta que se resuelva.

### Cálculo de la capacidad dinámica (DLR)

Con una línea definida y un conductor seleccionado, el botón "Calcular DLR" envía la petición al backend, que evalúa cada vano de forma independiente aplicando el modelo térmico IEEE 738 con las condiciones meteorológicas locales de sus dos apoyos y la temperatura máxima admisible fijada en el slider. El cálculo tarda unos segundos mientras se consultan los datos climáticos.

### Panel de resultados

Al finalizar el cálculo se habilita la pestaña "Resultados", con un informe estructurado en varios bloques:

- **Resumen**: vano más crítico de la línea, y ampacidad mínima, media y máxima obtenidas.
- **Comparación con rating estático**: barras comparativas entre el DLR calculado y un rating estático de referencia (40°C, viento de 0,61 m/s), junto con el porcentaje de ganancia o pérdida de capacidad.
- **Distribución de ampacidad**: una curva de densidad (KDE) que muestra cómo se reparten los valores de ampacidad entre los distintos vanos de la línea, con los percentiles P10, P50 y P90 marcados.
- **Detalles interesantes**: condiciones meteorológicas exactas del vano más crítico, con un botón para localizarlo directamente en el mapa, además de las peores condiciones individuales de temperatura, viento y radiación registradas en toda la línea.
- **Detalle por vano**: listado completo de todos los tramos con su ampacidad y condiciones meteorológicas particulares, destacando el vano crítico.

**Exportar informe a PDF**

El icono de PDF en la cabecera del panel de resultados genera un informe descargable de varias páginas con el resumen, la comparación con el rating estático, la curva de distribución y el detalle de las condiciones más críticas de la línea.