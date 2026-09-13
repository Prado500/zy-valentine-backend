import secrets

import requests
from cachecontrol import CacheControl
from google.auth.exceptions import GoogleAuthError, TransportError
from google.auth.transport.requests import Request
from google.oauth2 import id_token

from app.core.errors import ApiError


class GoogleVerifier:
    def __init__(self):
        self.session = CacheControl(requests.Session())

    def close(self):
        self.session.close()

    def verify(self, credential: str, client_id: str, nonce: str) -> dict:
        # Bounded transport timeout, library validates signature, aud, iss and exp.
        transport = Request(session=self.session)

        def request_with_timeout(*args, **kwargs):
            kwargs["timeout"] = 5
            return transport(*args, **kwargs)

        try:
            claims = id_token.verify_oauth2_token(credential, request_with_timeout, client_id)
        except (TransportError, requests.RequestException):
            raise ApiError(
                503, "GOOGLE_UNAVAILABLE", "Google no está disponible; reintenta."
            ) from None
        except (ValueError, GoogleAuthError):
            raise ApiError(401, "INVALID_GOOGLE_TOKEN", "No se pudo validar Google.") from None
        if (
            not claims.get("sub")
            or not isinstance(claims["sub"], str)
            or len(claims["sub"]) > 255
            or claims.get("email_verified") is not True
            or not isinstance(claims.get("nonce"), str)
            or not claims["nonce"].isascii()
            or not secrets.compare_digest(claims["nonce"], nonce)
        ):
            raise ApiError(401, "INVALID_GOOGLE_TOKEN", "No se pudo validar Google.")
        return claims
