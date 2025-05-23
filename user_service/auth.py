# user_service/auth.py
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from typing import Optional, Dict, Any
import httpx
from datetime import datetime, timedelta
from .config import get_settings, Settings

_jwks_cache_user: Optional[Dict[str, Any]] = None # Cache değişken adını özelleştir
_jwks_cache_expiry_user: Optional[datetime] = None
JWKS_CACHE_TTL_SECONDS = 3600

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token_not_issued_here_either")

async def fetch_jwks_for_user_service(settings: Settings) -> Dict[str, Any]:
    global _jwks_cache_user, _jwks_cache_expiry_user # global değişkenleri kullan
    # ... (ticket_service/auth.py'deki fetch_jwks_for_ticket_service ile aynı mantık, sadece print loglarında "UserService" yazabilir) ...
    # Örnek olarak sadece print'i değiştiriyorum, geri kalanı aynı varsayıyorum:
    now = datetime.utcnow()
    if _jwks_cache_user and _jwks_cache_expiry_user and _jwks_cache_expiry_user > now:
        print("USER_SERVICE_AUTH: Using cached JWKS.")
        return _jwks_cache_user
    if not settings.keycloak.jwks_uri:
        # ... (hata yönetimi) ...
        print("ERROR (UserService): JWKS URI is not configured.")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="JWKS URI not configured in UserService")
    try:
        async with httpx.AsyncClient() as client:
            print(f"USER_SERVICE_AUTH: Fetching JWKS from {settings.keycloak.jwks_uri}")
            # ... (geri kalan fetch mantığı ticket_service/auth.py ile aynı) ...
            response = await client.get(settings.keycloak.jwks_uri)
            response.raise_for_status()
            new_jwks = response.json()
            _jwks_cache_user = new_jwks
            _jwks_cache_expiry_user = now + timedelta(seconds=JWKS_CACHE_TTL_SECONDS)
            print("USER_SERVICE_AUTH: Fetched and cached new JWKS.")
            return new_jwks
    except Exception as e:
        print(f"ERROR (UserService): Could not fetch JWKS: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Could not fetch JWKS from {settings.keycloak.jwks_uri}")


class AuthHandlerUserService: # Sınıf adını değiştir
    @staticmethod
    async def decode_token(token: str, settings: Settings) -> Optional[dict]:
        # ... (ticket_service/auth.py'deki AuthHandlerTicketService.decode_token ile aynı mantık) ...
        # Sadece print loglarındaki "TicketService" yerine "UserService" yazın.
        # Örnek olarak birkaç print'i değiştiriyorum:
        print(f"USER_SERVICE_AUTH: Attempting to decode token. Expected audience: '{settings.keycloak.audience}', Expected issuer: '{settings.keycloak.issuer_uri}'")
        # ... (decode mantığının geri kalanı aynı) ...
        # Hata yakalama blokları da aynı kalabilir, sadece loglardaki servis adı değişir.
        if not settings.keycloak.issuer_uri or not settings.keycloak.audience:
            print("ERROR (UserService): Keycloak issuer_uri or audience not configured.")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Auth config error in UserService")

        jwks = await fetch_jwks_for_user_service(settings) # Doğru fetch fonksiyonunu çağır
        if not jwks or not jwks.get("keys"):
             print(f"ERROR (UserService): JWKS not found or no keys in JWKS. JWKS: {jwks}")
             raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not retrieve valid JWKS for token validation in UserService.")
        try:
            unverified_header = jwt.get_unverified_header(token)
            token_kid = unverified_header.get("kid")
            # ... (ticket_service/auth.py'deki gibi RSA key bulma ve jwt.decode)
            if not token_kid:
                raise JWTError("Token header missing 'kid'")
            rsa_key = {}
            for key_val in jwks["keys"]:
                if key_val.get("kid") == token_kid:
                    rsa_key = { "kty": key_val.get("kty"), "kid": key_val.get("kid"), "use": key_val.get("use"), "n": key_val.get("n"), "e": key_val.get("e")}
                    if "alg" in key_val: rsa_key["alg"] = key_val.get("alg")
                    break
            if not rsa_key:
                raise JWTError("UserService: Unable to find appropriate key in JWKS")

            payload = jwt.decode( token, rsa_key, algorithms=["RS256"], issuer=settings.keycloak.issuer_uri, audience=settings.keycloak.audience)
            print(f"USER_SERVICE_AUTH: Token successfully decoded for UserService. Payload 'sub': {payload.get('sub')}")
            return payload
        except JWTError as e:
            print(f"ERROR (UserService): JWT validation error: {type(e).__name__} - {e}")
            return None
        except Exception as e:
            print(f"ERROR (UserService): Unexpected error during token decoding: {type(e).__name__} - {e}")
            return None


async def get_current_user_payload(token: str = Depends(oauth2_scheme), settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    payload = await AuthHandlerUserService.decode_token(token, settings) # Doğru handler'ı çağır
    if payload is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="UserService: Geçersiz kimlik bilgileri veya token", headers={"WWW-Authenticate": "Bearer"})
    return payload