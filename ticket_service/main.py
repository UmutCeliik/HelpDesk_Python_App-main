# ticket_service/main.py
from fastapi import FastAPI, HTTPException, status, Depends, Response
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any, Annotated # Annotated eklendi
import uuid
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
import httpx

# Ortak ve yerel modülleri import et
from database_pkg.database import get_db
from database_pkg.schemas import Role # database_pkg.schemas'dan Role enum'ı
from . import models # ticket_service Pydantic modelleri
from . import crud
from .auth import get_current_user_payload # Güncellenmiş auth dependency
from .config import get_settings, Settings # config.py'den settings

app = FastAPI(title="Ticket Service API - Keycloak Integrated")

USER_SERVICE_URL = "http://localhost:8001"
# CORS Ayarları
origins = [
    "http://localhost:5173", # Vue frontend
    "http://localhost:8080",
    "http://localhost:3000",
    "http://localhost",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def read_root(settings: Settings = Depends(get_settings)): # Ayarları kontrol için ekledim
    # Başlangıçta config.py'deki print'ler çalışacak, bu endpoint sadece bir hoşgeldin mesajı verir.
    # print(f"TicketService Root - Configured Audience: {settings.keycloak.audience}") # Test için
    return {"message": "Ticket Service API'ye hoş geldiniz! Keycloak ile güvende."}


@app.post("/tickets/", response_model=models.Ticket, status_code=status.HTTP_201_CREATED)
async def create_ticket(
    ticket_in: models.TicketCreate,
    current_user_payload: Annotated[Dict[str, Any], Depends(get_current_user_payload)],
    db: Session = Depends(get_db),
):
    keycloak_id_str = current_user_payload.get("sub")
    if not keycloak_id_str:
        # ... (hata yönetimi)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token ID (sub) yok")

    # 1. Adım: Kullanıcıyı user_service üzerinden senkronize et/varlığını doğrula
    try:
        keycloak_roles = current_user_payload.get("roles", [])
        if not keycloak_roles and current_user_payload.get("realm_access"):
            keycloak_roles = current_user_payload.get("realm_access", {}).get("roles", [])

        user_sync_payload = {
            "id": keycloak_id_str,
            "email": current_user_payload.get("email"),
            "full_name": current_user_payload.get("name") or \
                         f"{current_user_payload.get('given_name', '')} {current_user_payload.get('family_name', '')}".strip() or \
                         current_user_payload.get("preferred_username"),
            "roles": keycloak_roles,
            "is_active": current_user_payload.get("email_verified", True) # veya Keycloak'taki 'enabled'
        }
        print(f"TICKET_SERVICE_MAIN: Attempting to sync user {keycloak_id_str} with user_service...")
        async with httpx.AsyncClient() as client:
            # user_service'deki endpoint'e POST yap
            response = await client.post(f"{USER_SERVICE_URL}/users/sync-from-keycloak", json=user_sync_payload)
            response.raise_for_status() # Hata varsa exception fırlat
            synced_user = response.json()
            print(f"TICKET_SERVICE_MAIN: User {synced_user.get('id')} synced/retrieved from user_service.")
            # creator_id_uuid = uuid.UUID(synced_user.get("id")) # user_service'den dönen ID'yi kullan
            creator_id_uuid = uuid.UUID(keycloak_id_str) # Keycloak ID'sini doğrudan kullanalım

    except httpx.HTTPStatusError as e:
        print(f"ERROR (TicketService-Create): Failed to sync user with user_service. Status: {e.response.status_code}, Response: {e.response.text}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Kullanıcı servisi ile senkronizasyon hatası: {e.response.status_code}")
    except Exception as e:
        print(f"ERROR (TicketService-Create): Unexpected error during user sync: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Kullanıcı senkronizasyonu sırasında beklenmedik hata.")

    # 2. Adım: Bileti oluştur
    print(f"TICKET_SERVICE_MAIN: create_ticket request from user_sub: {keycloak_id_str} (UUID: {creator_id_uuid})")
    try:
        created_db_ticket = crud.create_ticket(
            db=db, ticket=ticket_in, creator_id=creator_id_uuid
        )
        return created_db_ticket
    # ... (mevcut hata yönetimi bloklarınız) ...
    except IntegrityError as e:
        db.rollback()
        print(f"ERROR (TicketService-Create): IntegrityError while creating ticket (after user sync attempt): {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bilet oluşturulurken veritabanı bütünlük hatası.")
    except Exception as e:
        db.rollback()
        print(f"ERROR (TicketService-Create): Unexpected error while creating ticket (after user sync attempt): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Bilet oluşturulurken beklenmedik bir sunucu hatası oluştu.")


@app.get("/tickets/", response_model=List[models.Ticket])
async def read_tickets(
    current_user_payload: Annotated[Dict[str, Any], Depends(get_current_user_payload)],
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 100,
):
    user_sub = current_user_payload.get('sub')
    # Keycloak'tan gelen rolleri almanın birkaç yolu olabilir, token yapınıza göre ayarlayın
    user_roles = current_user_payload.get("roles", []) # Eğer 'roles' claim'i varsa
    if not user_roles and current_user_payload.get("realm_access"): # realm_access altındaysa
        user_roles = current_user_payload.get("realm_access", {}).get("roles", [])
    
    print(f"TICKET_SERVICE_MAIN: /tickets/ request from user_sub: {user_sub}, roles: {user_roles}")

    # Örnek Yetkilendirme: Sadece 'agent' veya 'helpdesk-admin' tüm biletleri görebilir,
    # 'employee' sadece kendi biletlerini görebilir (bu endpoint'te implemente edilmedi, ayrı bir endpoint olabilir).
    # Bu endpoint şimdilik tüm biletleri listeliyor, yetkilendirme ekleyebilirsiniz.
    # if Role.AGENT.value not in user_roles and "helpdesk-admin" not in user_roles: # Rol isimlerini kontrol edin
    #     # TODO: Çalışanların sadece kendi biletlerini görmesi için filtreleme ekle
    #     print(f"WARNING (TicketService-ReadMany): User {user_sub} is not an agent/admin, returning all tickets for now. Implement filtering.")
    #     # raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bu işlem için yetkiniz yok")
    
    db_tickets = crud.get_tickets(db=db, skip=skip, limit=limit)
    return db_tickets


@app.get("/tickets/{ticket_id}", response_model=models.Ticket)
async def read_ticket(
    ticket_id: uuid.UUID,
    current_user_payload: Annotated[Dict[str, Any], Depends(get_current_user_payload)],
    db: Session = Depends(get_db),
):
    user_sub_str = current_user_payload.get("sub")
    user_roles = current_user_payload.get("roles", [])
    if not user_roles and current_user_payload.get("realm_access"):
        user_roles = current_user_payload.get("realm_access", {}).get("roles", [])
    
    print(f"TICKET_SERVICE_MAIN: /tickets/{ticket_id} GET request from user_sub: {user_sub_str}, roles: {user_roles}")

    db_ticket = crud.get_ticket(db=db, ticket_id=ticket_id)
    if db_ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bilet bulunamadı")

    # Yetkilendirme: Bileti oluşturan kişi veya bir 'agent'/'admin' görebilir
    is_owner = str(db_ticket.creator_id) == user_sub_str
    is_agent_or_admin = Role.AGENT.value in user_roles or "helpdesk-admin" in user_roles # Rol adınızı kontrol edin

    if not is_owner and not is_agent_or_admin:
        print(f"ERROR (TicketService-ReadOne): User {user_sub_str} not authorized to view ticket {ticket_id}. Owner: {db_ticket.creator_id}, User Roles: {user_roles}")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bu bileti görüntüleme yetkiniz yok")
    
    return db_ticket


@app.patch("/tickets/{ticket_id}", response_model=models.Ticket)
async def update_ticket_status( # Fonksiyon adını daha spesifik hale getirdim, sadece status güncelliyorsa
    ticket_id: uuid.UUID,
    ticket_update: models.TicketUpdate, # Sadece status değil, başlık ve açıklama da güncellenebilir.
    current_user_payload: Annotated[Dict[str, Any], Depends(get_current_user_payload)],
    db: Session = Depends(get_db)
):
    user_sub_str = current_user_payload.get("sub")
    user_roles = current_user_payload.get("roles", [])
    if not user_roles and current_user_payload.get("realm_access"):
        user_roles = current_user_payload.get("realm_access", {}).get("roles", [])

    print(f"TICKET_SERVICE_MAIN: /tickets/{ticket_id} PATCH request from user_sub: {user_sub_str}, roles: {user_roles}, update_data: {ticket_update.model_dump_json(exclude_unset=True)}")

    # Yetkilendirme: Sadece 'agent' veya 'admin' bilet durumunu/içeriğini güncelleyebilir
    is_agent_or_admin = Role.AGENT.value in user_roles or "helpdesk-admin" in user_roles

    if not is_agent_or_admin:
        print(f"ERROR (TicketService-Update): User {user_sub_str} not authorized to update ticket {ticket_id}. User Roles: {user_roles}")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bilet güncelleme yetkiniz yok")

    updated_db_ticket = crud.update_ticket(db=db, ticket_id=ticket_id, ticket_update=ticket_update)
    if updated_db_ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Güncellenecek bilet bulunamadı")
    return updated_db_ticket


@app.delete("/tickets/{ticket_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ticket_endpoint( # Fonksiyon adını değiştirdim, delete_ticket crud fonksiyonuyla karışmasın
    ticket_id: uuid.UUID,
    current_user_payload: Annotated[Dict[str, Any], Depends(get_current_user_payload)],
    db: Session = Depends(get_db)
):
    user_sub_str = current_user_payload.get("sub")
    user_roles = current_user_payload.get("roles", [])
    if not user_roles and current_user_payload.get("realm_access"):
        user_roles = current_user_payload.get("realm_access", {}).get("roles", [])

    print(f"TICKET_SERVICE_MAIN: /tickets/{ticket_id} DELETE request from user_sub: {user_sub_str}, roles: {user_roles}")
    
    # Yetkilendirme: Sadece 'agent' veya 'admin' bilet silebilir (veya sadece admin)
    # Bu kuralı projenize göre ayarlayın.
    is_admin = "helpdesk-admin" in user_roles # Örneğin sadece admin silebilir

    if not is_admin: # Veya is_agent_or_admin
        print(f"ERROR (TicketService-Delete): User {user_sub_str} not authorized to delete ticket {ticket_id}. User Roles: {user_roles}")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bilet silme yetkiniz yok")

    deleted_db_ticket = crud.delete_ticket(db=db, ticket_id=ticket_id)
    if deleted_db_ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Silinecek bilet bulunamadı")
    return Response(status_code=status.HTTP_204_NO_CONTENT)