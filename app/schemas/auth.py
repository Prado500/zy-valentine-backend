import uuid
from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

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
    def canonical_number(cls, value: str | None, info: ValidationInfo) -> str | None:
        """El formato depende del tipo, que ya está validado cuando se llega aquí."""
        document_type = info.data.get("documentType")
        if value is None or document_type is None:
            # Sin número no hay nada que normalizar (el alta lo admite: ver `Register`).
            # Sin tipo, o no pasó su propia validación —ese es el error que se reporta—
            # o falta del todo, y eso lo rechaza la regla de "completo o nada".
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
    """Alta como acto legal: cuenta y aceptación de los términos vigentes.

    El documento es **opcional**. Solo lo necesita quien pide factura electrónica, y
    exigirlo a todo el mundo en el primer paso de la compra era fricción sin
    contrapartida. Cuando llega, valen exactamente las mismas reglas que en el panel
    —se heredan de `DocumentInput`—; lo único que cambia es que puede no venir.

    Lo que no se admite es medio documento: un tipo sin número, o al revés, no sirve
    para facturar ni para comprobar la unicidad entre cuentas, y guardarlo sería
    fingir un dato que no tenemos. Completo o nada.
    """

    password: str = Field(min_length=4, max_length=10)
    name: str = Field(min_length=1, max_length=120)
    # Versión exacta del texto que la persona vio; el servicio la contrasta con la vigente.
    acceptedTermsVersion: str = Field(min_length=1, max_length=32)
    documentType: dian.DocumentType | None = None
    documentNumber: str | None = Field(
        default=None, min_length=dian.MIN_LENGTH, max_length=dian.MAX_LENGTH
    )

    @model_validator(mode="after")
    def whole_document_or_none(self) -> "Register":
        if (self.documentType is None) != (self.documentNumber is None):
            raise ValueError("El documento va completo, tipo y número, o no va")
        return self

    @property
    def has_document(self) -> bool:
        return self.documentType is not None

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
