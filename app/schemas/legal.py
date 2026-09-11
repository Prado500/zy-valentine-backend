from pydantic import BaseModel


class TermsResponse(BaseModel):
    """Texto vigente. El checksum es el que se guardará con el consentimiento."""

    version: str
    checksum: str
    content: str
