"""Último paso del flujo: junta lo que devolvió Rekognition y la miniatura, y lo guarda en DynamoDB.

Entrada (la arma la máquina de estados):
  {"bucket", "key", "etiquetas": [Labels de DetectLabels], "textos": [líneas de DetectText],
   "moderacion": [ModerationLabels], "imagen": salida de procesar.py}

Modelo de la tabla (una sola tabla, PK = "U#<usuario>"):
  SK "F#<foto>"                  → la foto (GSI por-fecha: PK + fecha_orden)
  SK "E#<etiqueta>#<fecha_orden>" → índice por etiqueta de Rekognition (es y en)
  SK "T#<palabra>#<fecha_orden>"  → índice por palabra leída con OCR
"""
import json
import logging
import os
import re
import unicodedata
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

TABLA = os.environ.get("TABLA", "jadg-revela-fotos")
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

tabla = boto3.resource("dynamodb").Table(TABLA)
s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime")


def lista(valor):
    # JSONata devuelve un escalar cuando hay un solo elemento y nada cuando hay cero
    if valor is None:
        return []
    return valor if isinstance(valor, list) else [valor]


def normalizar(texto):
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[#\s]+", " ", sin_tildes).strip().lower()


def describir(bucket, miniatura, nombres, textos):
    """Una sola llamada a Nova Lite: mira la miniatura, escribe el pie de foto y traduce las etiquetas
    (Rekognition solo da inglés y Amazon Translate no está habilitado en la cuenta del curso).
    Si falla, la foto queda sin descripción y con las etiquetas en inglés."""
    try:
        jpeg = s3.get_object(Bucket=bucket, Key=miniatura)["Body"].read()
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

    # Lo que quedó en cuarentena no se le muestra a Nova
    nombres_es, descripcion = (describir(event["bucket"], imagen["miniatura"], nombres_en, textos)
                               if estado == "LISTA" else (list(nombres_en), None))

    # Orden de la galería: fecha de toma si la cámara la guardó, si no la de subida
    fecha_orden = f"{imagen.get('fecha_toma') or actual['fecha_subida']}#{foto}"

    claves = {}
    if estado == "LISTA":
        for es, en in zip(nombres_es, nombres_en):
            for nombre in (es, en):
                claves.setdefault(f"E#{normalizar(nombre)}#{fecha_orden}", es)
        for p in palabras(textos):
            claves.setdefault(f"T#{p}#{fecha_orden}", p)

    # Si la foto se reprocesa (SQS entrega al menos una vez) se reemplazan los índices viejos
    with tabla.batch_writer(overwrite_by_pkeys=["PK", "SK"]) as lote:
        for sk in set(actual.get("claves", [])) - set(claves):
            lote.delete_item(Key={"PK": pk, "SK": sk})
        for sk, nombre in claves.items():
            lote.put_item(Item={"PK": pk, "SK": sk, "foto": foto, "etiqueta": nombre})

    gps = imagen.get("gps")
    tabla.update_item(
        Key={"PK": pk, "SK": f"F#{foto}"},
        UpdateExpression=("SET estado = :e, fecha_orden = :fo, fecha_toma = :ft, etiquetas = :et, etiquetas_en = :en, "
                          "texto = :tx, descripcion = :de, moderacion = :mo, ancho = :an, alto = :al, camara = :ca, gps = :gp, "
                          "miniatura = :mi, revisada = :rv, claves = :cl REMOVE expira"),
        ExpressionAttributeValues={
            ":e": estado, ":fo": fecha_orden, ":ft": imagen.get("fecha_toma"),
            ":et": nombres_es, ":en": nombres_en, ":tx": "\n".join(textos), ":de": descripcion, ":mo": bloqueo,
            ":an": imagen["ancho"], ":al": imagen["alto"], ":ca": imagen.get("camara"),
            ":gp": {k: Decimal(str(v)) for k, v in gps.items()} if gps else None,
            ":mi": imagen["miniatura"], ":rv": imagen["revisada"], ":cl": list(claves),
        },
    )
    log.info("Foto %s → %s (%d etiquetas, %d índices)", foto, estado, len(nombres_es), len(claves))
    return {"foto": foto, "estado": estado}
