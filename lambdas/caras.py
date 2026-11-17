"""Caras de las fotos de quien activó "Personas": se indexan en la colección de Rekognition del usuario y se agrupan
por persona con Rekognition Users. Lo usan guardar.py (fotos nuevas) y api.py (volver a buscar en una foto)."""
import os
import time
import uuid
from decimal import Decimal

import boto3

CARAS = os.environ.get("CARAS", "jyj-revela-caras-")  # + usuario: una colección por persona, nunca se cruzan caras entre cuentas
UMBRAL_CARA = 90
MAX_CARAS = 10

rekognition = boto3.client("rekognition")


def borrar(usuario, caras):
    try:
        rekognition.delete_faces(CollectionId=CARAS + usuario, FaceIds=caras)
    except rekognition.exceptions.ResourceNotFoundException:
        pass  # desactivó personas: la colección ya se borró con todas sus caras


def reconocer(usuario, foto, bucket, key, calidad="AUTO"):
    """Indexa las caras en la colección del usuario y las agrupa con Rekognition Users: una cara que se parece a una
    persona ya conocida se le asocia; si no, es una persona nueva. Devuelve ([(persona, caja)], [face_id])."""
    coleccion = CARAS + usuario
    try:
        r = rekognition.index_faces(CollectionId=coleccion, Image={"S3Object": {"Bucket": bucket, "Name": key}},
                                    ExternalImageId=foto, MaxFaces=MAX_CARAS, QualityFilter=calidad)
    except rekognition.exceptions.ResourceNotFoundException:
        return [], []  # desactivó personas mientras se analizaba la foto
    personas, caras = [], [c["Face"]["FaceId"] for c in r["FaceRecords"]]
    try:
        for registro in r["FaceRecords"]:
            personas.append(agrupar(coleccion, registro))
    except Exception:
        borrar(usuario, caras)  # sin el item que las referencia quedarían huellas huérfanas
        raise
    return personas, caras


def agrupar(coleccion, registro):
    """Asocia la cara a la persona más parecida o crea una nueva; devuelve (persona, caja)."""
    cara = registro["Face"]["FaceId"]
    # Con muchas fotos a la vez, la cara recién indexada a veces todavía no se puede buscar y Rekognition responde
    # InvalidParameterException (así trata una FaceId que no encuentra); unos segundos después ya aparece
    for espera in (1, 2, 4, 0):
        try:
            parecidas = rekognition.search_users(CollectionId=coleccion, FaceId=cara, UserMatchThreshold=UMBRAL_CARA, MaxUsers=1)["UserMatches"]
            break
        except rekognition.exceptions.InvalidParameterException:
            if not espera:
                raise
            time.sleep(espera)
    persona = parecidas[0]["User"]["UserId"] if parecidas else uuid.uuid4().hex
    if not parecidas:
        rekognition.create_user(CollectionId=coleccion, UserId=persona)
    rekognition.associate_faces(CollectionId=coleccion, UserId=persona, FaceIds=[cara], UserMatchThreshold=UMBRAL_CARA)
    # La caja ya viene corregida por la orientación EXIF, igual que la miniatura
    caja = {k: Decimal(str(round(v, 4))) for k, v in registro["Face"]["BoundingBox"].items()}
    return persona, caja
