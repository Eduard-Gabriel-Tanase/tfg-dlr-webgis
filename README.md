# TFG DLR Web-GIS

## Cómo iniciar el programa

### Inicialización del BACKEND

Activar el entorno virtual y levantar el servidor de la aplicación:

```bat
venv\Scripts\activate.bat
uvicorn main:app --reload
```

### Inicialización del FRONTEND

Iniciar el servidor web para el frontend:

```bat
python -m http.server 5500
```

### Aplicación WEB

Una vez iniciados el backend y el frontend, acceder a la aplicación desde:

**http://localhost:5500**
