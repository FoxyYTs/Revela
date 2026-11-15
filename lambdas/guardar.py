"""Último paso del flujo: junta lo que devolvió Rekognition y la miniatura, y lo guarda en DynamoDB.

Entrada (la arma la máquina de estados):
  {"bucket", "key", "etiquetas": [Labels de DetectLabels], "textos": [líneas de DetectText],
   "moderacion": [ModerationLabels], "imagen": salida de procesar.py}

Modelo de la tabla (una sola tabla, PK = "U#<usuario>"):
  SK "F#<foto>"                  → la foto (GSI por-fecha: PK + fecha_orden)
  SK "E#<etiqueta>#<fecha_orden>" → índice por etiqueta de Rekognition (es y en)
  SK "T#<palabra>#<fecha_orden>"  → índice por palabra leída con OCR
  SK "L#<lugar>#<fecha_orden>"    → índice por ciudad, departamento y país (Amazon Location)
  SK "P#<persona>#<fecha_orden>"  → índice por persona, con la caja de la cara (solo si el usuario activó personas)
Aparte, el vector de la miniatura va a S3 Vectors (key = foto, metadato usuario) para buscar por significado.
"""
import base64
import json
import logging
import os
import re
import unicodedata
import uuid
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

TABLA = os.environ.get("TABLA", "jyj-revela-fotos")
UMBRAL_MODERACION = float(os.environ.get("UMBRAL_MODERACION", "80"))
# Categorías de primer nivel de Rekognition que mandan la foto a cuarentena (traje de baño, alcohol, etc. no)
BLOQUEAR = {"Explicit", "Non-Explicit Nudity of Intimate parts and Kissing", "Violence", "Visually Disturbing", "Hate Symbols"}
MAX_PALABRAS = 30

MODELO = os.environ.get("MODELO", "us.amazon.nova-lite-v1:0")
INSTRUCCION = (
    "Describes fotos de una galería personal en español de Colombia. Recibes la foto, sus etiquetas en inglés "
    "(arreglo JSON) y el texto que aparece en ella. Respondes SOLO un objeto JSON con dos campos:\n"
    '"descripcion": una o dos frases naturales sobre lo que se ve (máximo 220 caracteres), sin inventar nombres de '
    "personas ni lugares que no se lean en la foto y sin empezar con \"La imagen muestra\".\n"
    '"etiquetas": la traducción de cada etiqueta, en el mismo orden y con la misma cantidad, como sustantivos cortos '
    "con espacios normales.\n"
    'Ejemplo de etiquetas: ["Sunglasses", "Mobile Phone", "Coat"] → ["Gafas de sol", "Teléfono móvil", "Abrigo"]')
MAX_DESCRIPCION = 300
VECTORES = os.environ.get("VECTORES", "jyj-revela-vectores")
EMBEDDINGS = "amazon.nova-2-multimodal-embeddings-v1:0"  # multilingüe: la búsqueda en español cae cerca de la imagen
CARAS = os.environ.get("CARAS", "jyj-revela-caras-")  # + usuario: una colección por persona, nunca se cruzan caras entre cuentas
UMBRAL_CARA = 90
MAX_CARAS = 10

_lugares = None
_vectores = None


def lugares():
    # Perezoso: si el boto3 del runtime no trae geo-places, solo se pierde el nombre del lugar, no la foto
    global _lugares
    if _lugares is None:
        _lugares = boto3.client("geo-places")
    return _lugares


def vectores():
    global _vectores
    if _vectores is None:
        _vectores = boto3.client("s3vectors")
    return _vectores


tabla = boto3.resource("dynamodb").Table(TABLA)
s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime")
rekognition = boto3.client("rekognition")


def lista(valor):
    # JSONata devuelve un escalar cuando hay un solo elemento y nada cuando hay cero
    if valor is None:
        return []
    return valor if isinstance(valor, list) else [valor]


def normalizar(texto):
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[#\s]+", " ", sin_tildes).strip().lower()


def describir(jpeg, nombres, textos):
    """Una sola llamada a Nova Lite: mira la miniatura, escribe el pie de foto y traduce las etiquetas
    (Rekognition solo da inglés y Amazon Translate no está habilitado en la cuenta del curso).
    Si falla, la foto queda sin descripción y con las etiquetas en inglés."""
    try:
        pedido = f"Etiquetas: {json.dumps(nombres, ensure_ascii=False)}\nTexto en la foto: {' / '.join(textos)[:500] or '(ninguno)'}"
        r = bedrock.converse(modelId=MODELO, system=[{"text": INSTRUCCION}],
                             messages=[{"role": "user", "content": [
                                 {"image": {"format": "jpeg", "source": {"bytes": jpeg}}}, {"text": pedido}]}],
                             inferenceConfig={"maxTokens": 600, "temperature": 0})
        texto = r["output"]["message"]["content"][0]["text"].strip().removeprefix("```json").strip("`\n ")
        datos = json.loads(texto)
        descripcion = str(datos.get("descripcion", "")).strip()[:MAX_DESCRIPCION] or None
        # Nova a veces mezcla mayúsculas ("GAfas", "ACCESORIOS"): solo la inicial en mayúscula
        traducidos = [str(t).strip().capitalize() for t in datos.get("etiquetas", [])]
        if len(traducidos) != len(nombres) or not all(traducidos):
            log.warning("Traducción desalineada: %r → %r", nombres, traducidos)
            traducidos = list(nombres)
        return traducidos, descripcion
    except (ClientError, ValueError, KeyError, TypeError, AttributeError):
        log.exception("Nova falló; se guardan las etiquetas en inglés y sin descripción")
    return list(nombres), None


def vectorizar(foto, usuario, jpeg):
    """Vector de la miniatura en S3 Vectors; si falla, la foto solo se encuentra por etiquetas, texto y lugar."""
    try:
        cuerpo = {"taskType": "SINGLE_EMBEDDING", "singleEmbeddingParams": {
            "embeddingPurpose": "GENERIC_INDEX", "embeddingDimension": 1024,
            "image": {"format": "jpeg", "source": {"bytes": base64.b64encode(jpeg).decode()}}}}
        r = json.loads(bedrock.invoke_model(modelId=EMBEDDINGS, body=json.dumps(cuerpo))["body"].read())
        vectores().put_vectors(vectorBucketName=VECTORES, indexName="fotos", vectors=[
            {"key": foto, "data": {"float32": r["embeddings"][0]["embedding"]}, "metadata": {"usuario": usuario}}])
    except Exception as e:  # noqa: BLE001 — sin vector la foto sigue guardada
        log.warning("No se pudo vectorizar la foto %s: %s", foto, type(e).__name__)


def reconocer(usuario, foto, bucket, key, viejas):
    """Indexa las caras en la colección del usuario y las agrupa con Rekognition Users: una cara que se parece a una
    persona ya conocida se le asocia; si no, es una persona nueva. Devuelve ([(persona, caja)], [face_id])."""
    coleccion = CARAS + usuario
    try:
        if viejas:  # reproceso: no duplicar las caras de la pasada anterior
            rekognition.delete_faces(CollectionId=coleccion, FaceIds=viejas)
        r = rekognition.index_faces(CollectionId=coleccion, Image={"S3Object": {"Bucket": bucket, "Name": key}},
                                    ExternalImageId=foto, MaxFaces=MAX_CARAS, QualityFilter="AUTO")
    except rekognition.exceptions.ResourceNotFoundException:
        return [], []  # desactivó personas mientras se analizaba la foto
    personas, caras = [], []
    for registro in r["FaceRecords"]:
        cara = registro["Face"]["FaceId"]
        parecidas = rekognition.search_users(CollectionId=coleccion, FaceId=cara, UserMatchThreshold=UMBRAL_CARA, MaxUsers=1)["UserMatches"]
        persona = parecidas[0]["User"]["UserId"] if parecidas else uuid.uuid4().hex
        if not parecidas:
            rekognition.create_user(CollectionId=coleccion, UserId=persona)
        rekognition.associate_faces(CollectionId=coleccion, UserId=persona, FaceIds=[cara], UserMatchThreshold=UMBRAL_CARA)
        # La caja ya viene corregida por la orientación EXIF, igual que la miniatura
        caja = {k: Decimal(str(round(v, 4))) for k, v in registro["Face"]["BoundingBox"].items()}
        personas.append((persona, caja))
        caras.append(cara)
    return personas, caras


def geocodificar(gps):
    """Coordenadas → {"nombre": "Rionegro, Antioquia, Colombia", "ciudad", "region", "pais"} o None."""
    try:
        r = lugares().reverse_geocode(QueryPosition=[float(gps["lon"]), float(gps["lat"])], MaxResults=1, Language="es",
                                      IntendedUse="Storage")  # el resultado se guarda en DynamoDB: los términos exigen Storage
        if not r.get("ResultItems"):
            return None
        d = r["ResultItems"][0].get("Address", {})
        # En Colombia el departamento llega en SubRegion y Region viene vacía
        region = (d.get("Region") or {}).get("Name") or (d.get("SubRegion") or {}).get("Name")
        lugar = {"ciudad": d.get("Locality"), "region": region,
                 "pais": (d.get("Country") or {}).get("Name")}
        lugar = {k: v for k, v in lugar.items() if v}
        nombre = ", ".join(lugar[k] for k in ("ciudad", "region", "pais") if k in lugar)
        return {"nombre": nombre, **lugar} if nombre else None
    except Exception as e:  # noqa: BLE001 — cualquier falla de Location deja la foto sin nombre de lugar, nunca sin foto
        log.warning("Amazon Location no pudo nombrar el lugar: %s", type(e).__name__)  # sin coordenadas en los logs
        return None


def en_cuarentena(moderacion):
    return sorted({m["Name"] for m in moderacion
                   if m.get("Confidence", 0) >= UMBRAL_MODERACION
                   and (m.get("Name") in BLOQUEAR or m.get("ParentName") in BLOQUEAR)})


def palabras(textos):
    vistas = []
    for linea in textos:
        for p in re.findall(r"\w{3,}", normalizar(linea)):
            if p not in vistas:
                vistas.append(p)
    return vistas[:MAX_PALABRAS]


def lambda_handler(event, _context):
    _, usuario, archivo = event["key"].split("/")
    foto = archivo.rsplit(".", 1)[0]
    pk = f"U#{usuario}"
    imagen = event["imagen"]

    etiquetas = sorted(lista(event.get("etiquetas")), key=lambda e: -e.get("Confidence", 0))
    nombres_en = [e["Name"] for e in etiquetas]
    textos = [str(t) for t in lista(event.get("textos"))]
    bloqueo = en_cuarentena(lista(event.get("moderacion")))
    estado = "CUARENTENA" if bloqueo else "LISTA"
    actual = tabla.get_item(Key={"PK": pk, "SK": f"F#{foto}"}).get("Item")
    if not actual:
        # El usuario la borró mientras se analizaba: no se resucita y se limpia la miniatura
        log.info("Foto %s ya no existe; se descarta", foto)
        s3.delete_objects(Bucket=event["bucket"], Delete={"Objects": [{"Key": imagen["miniatura"]}, {"Key": imagen["revisada"]}]})
        return {"foto": foto, "estado": "BORRADA"}

    # Lo que quedó en cuarentena no se le muestra a Nova ni se vectoriza
    nombres_es, descripcion = list(nombres_en), None
    personas, caras = [], []
    if estado == "LISTA":
        jpeg = s3.get_object(Bucket=event["bucket"], Key=imagen["miniatura"])["Body"].read()
        nombres_es, descripcion = describir(jpeg, nombres_en, textos)
        vectorizar(foto, usuario, jpeg)
        # Datos biométricos: solo si el usuario dio su consentimiento en la página
        if tabla.get_item(Key={"PK": pk, "SK": "PERFIL"}).get("Item", {}).get("personas"):
            personas, caras = reconocer(usuario, foto, event["bucket"], imagen["revisada"], actual.get("caras", []))

    # GPS de la foto; si el celular lo borró, la ubicación que dio el navegador al subir
    gps = imagen.get("gps")
    fuente = "foto" if gps else None
    if not gps and actual.get("gps_subida"):
        gps, fuente = {k: float(v) for k, v in actual["gps_subida"].items()}, "subida"
    lugar = geocodificar(gps) if gps and estado == "LISTA" else None

    # Orden de la galería: fecha de toma si la cámara la guardó, si no la de subida
    fecha_orden = f"{imagen.get('fecha_toma') or actual['fecha_subida']}#{foto}"

    claves = {}
    if estado == "LISTA":
        for es, en in zip(nombres_es, nombres_en, strict=True):
            for nombre in (es, en):
                claves.setdefault(f"E#{normalizar(nombre)}#{fecha_orden}", {"etiqueta": es})
        for p in palabras(textos):
            claves.setdefault(f"T#{p}#{fecha_orden}", {"etiqueta": p})
        for nombre in {(lugar or {}).get(k) for k in ("ciudad", "region", "pais")} - {None}:
            claves.setdefault(f"L#{normalizar(nombre)}#{fecha_orden}", {"etiqueta": nombre})
        for persona, caja in personas:
            claves.setdefault(f"P#{persona}#{fecha_orden}", {"persona": persona, "caja": caja})

    # Si la foto se reprocesa (SQS entrega al menos una vez) se reemplazan los índices viejos
    with tabla.batch_writer(overwrite_by_pkeys=["PK", "SK"]) as lote:
        for sk in set(actual.get("claves", [])) - set(claves):
            lote.delete_item(Key={"PK": pk, "SK": sk})
        for sk, datos in claves.items():
            lote.put_item(Item={"PK": pk, "SK": sk, "foto": foto, **datos})

    tabla.update_item(
        Key={"PK": pk, "SK": f"F#{foto}"},
        UpdateExpression=("SET estado = :e, fecha_orden = :fo, fecha_toma = :ft, etiquetas = :et, etiquetas_en = :en, "
                          "texto = :tx, descripcion = :de, moderacion = :mo, ancho = :an, alto = :al, camara = :ca, gps = :gp, gps_fuente = :gf, lugar = :lu, "
                          "miniatura = :mi, revisada = :rv, claves = :cl, personas = :pe, caras = :cr REMOVE expira"),
        ExpressionAttributeValues={
            ":e": estado, ":fo": fecha_orden, ":ft": imagen.get("fecha_toma"),
            ":et": nombres_es, ":en": nombres_en, ":tx": "\n".join(textos), ":de": descripcion, ":mo": bloqueo,
            ":an": imagen["ancho"], ":al": imagen["alto"], ":ca": imagen.get("camara"),
            ":gp": {k: Decimal(str(v)) for k, v in gps.items()} if gps else None, ":gf": fuente, ":lu": lugar,
            ":mi": imagen["miniatura"], ":rv": imagen["revisada"], ":cl": list(claves),
            ":pe": list(dict.fromkeys(p for p, _ in personas)), ":cr": caras,
        },
    )
    log.info("Foto %s → %s (%d etiquetas, %d índices)", foto, estado, len(nombres_es), len(claves))
    return {"foto": foto, "estado": estado}
