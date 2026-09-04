from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import IntegrityError


app = FastAPI(
    title="Ecotur-ASOPRADO API",
    description="API RESTful asíncrona para la gestión de paquetes turísticos",
    version="0.1.1",
)

# ===  GLOBAL PYDANTIC EXCEPTION HANDLER (UX FRIENDLY/ UserCreate) ===
# TO DO
    

# === GLOBAL DATABASE EXCEPTION HANDLER ===
# TO DO

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", tags=["Health Check"])
async def root():
    return {"status": "ok", "message": "Zyvencore Valentine Bakend Api (v0.1.0-dev) está en línea"}