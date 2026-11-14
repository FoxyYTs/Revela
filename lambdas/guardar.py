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

MODELO = os.environ.get("MODELO", "us.amazon.nova-micro-v1:0")
INSTRUCCION = ("Traduces etiquetas de fotos del inglés al español de Colombia. Recibes un arreglo JSON y respondes SOLO "
               "otro arreglo JSON con la traducción de cada etiqueta, en el mismo orden y con la misma cantidad. "
               "Usa sustantivos cortos con espacios normales, sin explicaciones. "
               'Ejemplo: ["Sunglasses", "Mobile Phone", "Coat"] → ["Gafas de sol", "Teléfono móvil", "Abrigo"]')

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


def traducir(nombres):
    """Etiquetas de Rekognition (solo inglés) al español con Nova Micro; si falla, quedan en inglés.
    (Amazon Translate no está habilitado en la cuenta del curso: SubscriptionRequiredException)."""
    if not nombres:
        return []
    try:
        r = bedrock.converse(modelId=MODELO, system=[{"text": INSTRUCCION}],
                             messages=[{"role": "user", "content": [{"text": json.dumps(nombres)}]}],
                             inferenceConfig={"maxTokens": 400, "temperature": 0})
        texto = r["output"]["message"]["content"][0]["text"].strip().removeprefix("```json").strip("`\n ")
        # Nova a veces mezcla mayúsculas ("GAfas", "ACCESORIOS"): solo la inicial en mayúscula
        traducidos = [str(t).strip().capitalize() for t in json.loads(texto)]
        if len(traducidos) == len(nombres) and all(traducidos):
            return traducidos
        log.warning("Traducción desalineada: %r → %r", nombres, traducidos)
    except (ClientError, ValueError, KeyError, TypeError):
        log.exception("La traducción con Nova falló; se guardan las etiquetas en inglés")
    return list(nombres)


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
    nombres_es = traducir(nombres_en)
    textos = [str(t) for t in lista(event.get("textos"))]
    bloqueo = en_cuarentena(lista(event.get("moderacion")))
    estado = "CUARENTENA" if bloqueo else "LISTA"

    actual = tabla.get_item(Key={"PK": pk, "SK": f"F#{foto}"}).get("Item")
    if not actual:
        # El usuario la borró mientras se analizaba: no se resucita y se limpia la miniatura
        log.info("Foto %s ya no existe; se descarta", foto)
        s3.delete_objects(Bucket=event["bucket"], Delete={"Objects": [{"Key": imagen["miniatura"]}, {"Key": imagen["revisada"]}]})
        return {"foto": foto, "estado": "BORRADA"}

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
                          "texto = :tx, moderacion = :mo, ancho = :an, alto = :al, camara = :ca, gps = :gp, "
                          "miniatura = :mi, revisada = :rv, claves = :cl REMOVE expira"),
        ExpressionAttributeValues={
            ":e": estado, ":fo": fecha_orden, ":ft": imagen.get("fecha_toma"),
            ":et": nombres_es, ":en": nombres_en, ":tx": "\n".join(textos), ":mo": bloqueo,
            ":an": imagen["ancho"], ":al": imagen["alto"], ":ca": imagen.get("camara"),
            ":gp": {k: Decimal(str(v)) for k, v in gps.items()} if gps else None,
            ":mi": imagen["miniatura"], ":rv": imagen["revisada"], ":cl": list(claves),
        },
    )
    log.info("Foto %s → %s (%d etiquetas, %d índices)", foto, estado, len(nombres_es), len(claves))
    return {"foto": foto, "estado": estado}
