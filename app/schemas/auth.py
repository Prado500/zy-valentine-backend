import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, ValidationInfo, field_validator

from app.core import dian


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class DocumentInput(Input):
    """Tipo y número de documento, tal como los piden el alta y el panel.

    El tipo es el código oficial DIAN: Pydantic valida el ``IntEnum`` de forma nativa
    y un 99 se rechaza sin escribir un validador. El número se deja en su forma
    canónica (sin espacios, en mayúsculas) con el formato que exige cada tipo: solo
    dígitos salvo pasaporte y documento extranjero. Es un esquema compartido para que
    las dos puertas acepten y rechacen exactamente lo mismo.
    """

    documentType: dian.DocumentType
    documentNumber: str = Field(min_length=dian.MIN_LENGTH, max_length=dian.MAX_LENGTH)

    @field_validator("documentNumber", mode="after")
    @classmethod
    def canonical_number(cls, value: str, info: ValidationInfo) -> str:
        """El formato depende del tipo, que ya está validado cuando se llega aquí."""
        document_type = info.data.get("documentType")
        if document_type is None:
            # El tipo no pasó su propia validación; ese es el error que se reporta.
            return value
        return dian.normalize_number(int(document_type), value)


class Login(Input):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.casefold()


class Register(Login, DocumentInput):
    """Alta como acto legal: cuenta, documento y aceptación de los términos vigentes."""

    password: str = Field(min_length=4, max_length=10)
    name: str = Field(min_length=1, max_length=120)
    # Versión exacta del texto que la persona vio; el servicio la contrasta con la vigente.
    acceptedTermsVersion: str = Field(min_length=1, max_length=32)

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name cannot be blank")
        return value


class GoogleLogin(Input):
    credential: str = Field(min_length=1, max_length=8192)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
    id: uuid.UUID
    email: str
    name: str
    email_verified: bool = Field(serialization_alias="emailVerified")
    created_at: datetime = Field(serialization_alias="createdAt")


class CsrfResponse(BaseModel):
    csrfToken: str


class MessageResponse(BaseModel):
    message: str
