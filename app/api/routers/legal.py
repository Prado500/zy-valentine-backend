"""Texto legal vigente. Público y cacheable: no hay nada que proteger aquí.

Existe para que el frontend no tenga su propia copia del texto. El consentimiento
guarda el checksum de **este** contenido, así que servirlo desde aquí es lo que hace
que la prueba valga algo.
"""

from fastapi import APIRouter, Response

from app import legal
from app.schemas.legal import TermsResponse

router = APIRouter(prefix="/api/v1/public/legal", tags=["Legal"])


@router.get("/terms", response_model=TermsResponse)
async def terms(response: Response):
    # El middleware pone `no-store` por defecto porque casi todo lleva datos
    # personales. Este texto es público e inmutable dentro de su versión.
    response.headers["Cache-Control"] = "public, max-age=3600"
    return TermsResponse(
        version=legal.TERMS_VERSION,
        checksum=legal.TERMS_CHECKSUM,
        content=legal.TERMS_TEXT,
    )
