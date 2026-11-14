"""API de Revela detrás de API Gateway HTTP con autorizador JWT de Cognito.

POST   /subidas       → URL prefirmada para subir una foto directo a S3
GET    /fotos         → galería por fecha (?siguiente=cursor) o búsqueda (?q=perro)
GET    /fotos/{id}    → detalle con la URL del original
DELETE /fotos/{id}    → borra la foto, la miniatura y sus índices
GET    /etiquetas     → etiquetas del usuario con cuántas fotos tiene cada una
"""
import base64
import json
import logging
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
import boto3
from botocore.config import Config
from boto3.dynamodb.conditions import Key

log = logging.getLogger()
log.setLevel(logging.INFO)

TABLA = os.environ.get("TABLA", "jadg-revela-fotos")
BUCKET = os.environ.get("BUCKET", "jadg-revela-fotos")
TIPOS = {"image/jpeg": "jpg", "image/png": "png"}  # los formatos que acepta Rekognition
MAX_BYTES = 15 * 1024 * 1024  # límite de Rekognition para imágenes en S3
VIGENCIA_URL = 3600
POR_PAGINA = 60
EXPIRA_PENDIENTE = 24 * 3600  # una subida que nunca llega se borra sola por TTL

dynamo = boto3.resource("dynamodb")
tabla = dynamo.Table(TABLA)
s3 = boto3.client("s3", config=Config(signature_version="s3v4"))


class ErrorCliente(Exception):
    def __init__(self, status, mensaje):
        super().__init__(mensaje)
        self.status = status


def responder(status, cuerpo=None):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(cuerpo, default=lambda d: int(d) if d % 1 == 0 else float(d)) if cuerpo is not None else ""}


def normalizar(texto):
    # Igual que en guardar.py: las claves del índice se buscan con la misma forma
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[#\s]+", " ", sin_tildes).strip().lower()


def firmar(key):
    return s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=VIGENCIA_URL)


def resumen(item):
    foto = {"id": item["SK"][2:], "nombre": item.get("nombre"), "estado": item["estado"],
            "fecha": item["fecha_orden"].split("#")[0], "etiquetas": item.get("etiquetas", [])[:5]}
    # Las fotos en cuarentena nunca salen con URL
    if item["estado"] == "LISTA":
        foto["miniatura"] = firmar(item["miniatura"])
    return foto


def crear_subida(usuario, cuerpo):
    tipo = cuerpo.get("tipo")
    if tipo not in TIPOS:
        raise ErrorCliente(400, "Solo se aceptan fotos JPG o PNG")
    foto = uuid.uuid4().hex
    key = f"originales/{usuario}/{foto}.{TIPOS[tipo]}"
    ahora = datetime.now(timezone.utc)

    tabla.put_item(Item={
        "PK": f"U#{usuario}", "SK": f"F#{foto}", "estado": "PROCESANDO",
        "nombre": str(cuerpo.get("nombre", ""))[:200], "original": key,
        "fecha_subida": ahora.strftime("%Y-%m-%dT%H:%M:%S"), "fecha_orden": f"{ahora:%Y-%m-%dT%H:%M:%S}#{foto}",
        "expira": int(ahora.timestamp()) + EXPIRA_PENDIENTE,
    })
    # S3 rechaza el POST si el tipo, la key o el tamaño no coinciden con lo firmado
    post = s3.generate_presigned_post(
        BUCKET, key, Fields={"Content-Type": tipo},
        Conditions=[{"Content-Type": tipo}, ["content-length-range", 1, MAX_BYTES]], ExpiresIn=300)
    return 201, {"foto": foto, "url": post["url"], "campos": post["fields"]}


def listar(usuario, params):
    pk = f"U#{usuario}"
    q = normalizar(params.get("q", ""))
    if q:
        return 200, {"fotos": buscar(pk, q), "siguiente": None}

    consulta = {"IndexName": "por-fecha", "KeyConditionExpression": Key("PK").eq(pk),
                "ScanIndexForward": False, "Limit": POR_PAGINA}
    if params.get("siguiente"):
        try:
            consulta["ExclusiveStartKey"] = json.loads(base64.urlsafe_b64decode(params["siguiente"]))
        except ValueError:
            raise ErrorCliente(400, "Cursor inválido")
        if consulta["ExclusiveStartKey"].get("PK") != pk:
            raise ErrorCliente(400, "Cursor inválido")
    r = tabla.query(**consulta)
    siguiente = r.get("LastEvaluatedKey")
    return 200, {"fotos": [resumen(i) for i in r["Items"]],
                 "siguiente": base64.urlsafe_b64encode(json.dumps(siguiente).encode()).decode() if siguiente else None}


def buscar(pk, q):
    # shortcut: máximo 100 resultados sin paginar; paginar si una búsqueda llega a ese tope
    ids = []
    for prefijo in ("E#", "T#"):
        r = tabla.query(KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with(f"{prefijo}{q}#"),
                        ScanIndexForward=False, Limit=100)
        ids += [i["foto"] for i in r["Items"] if i["foto"] not in ids]
    ids = ids[:100]
    if not ids:
        return []
    r = dynamo.batch_get_item(
        RequestItems={TABLA: {"Keys": [{"PK": pk, "SK": f"F#{i}"} for i in ids]}})
    fotos = [resumen(i) for i in r["Responses"][TABLA]]
    return sorted(fotos, key=lambda f: f["fecha"], reverse=True)


def obtener(usuario, foto):
    item = tabla.get_item(Key={"PK": f"U#{usuario}", "SK": f"F#{foto}"}).get("Item")
    if not item:
        raise ErrorCliente(404, "La foto no existe")
    return item


def detalle(usuario, foto):
    item = obtener(usuario, foto)
    datos = resumen(item)
    datos.update({k: item.get(k) for k in ("etiquetas", "texto", "camara", "gps", "ancho", "alto", "fecha_toma", "moderacion")})
    if item["estado"] == "LISTA":
        datos["original"] = firmar(item["original"])
    return 200, datos


def borrar(usuario, foto):
    item = obtener(usuario, foto)
    pk = f"U#{usuario}"
    s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": k} for k in (item["original"], item.get("miniatura")) if k]})
    with tabla.batch_writer() as lote:
        for sk in item.get("claves", []):
            lote.delete_item(Key={"PK": pk, "SK": sk})
    tabla.delete_item(Key={"PK": pk, "SK": item["SK"]})
    return 204, None


def etiquetas(usuario):
    conteo, consulta = {}, {"KeyConditionExpression": Key("PK").eq(f"U#{usuario}") & Key("SK").begins_with("E#"),
                            "ProjectionExpression": "foto, etiqueta"}
    while True:
        r = tabla.query(**consulta)
        for i in r["Items"]:
            conteo.setdefault(i["etiqueta"], set()).add(i["foto"])
        if "LastEvaluatedKey" not in r:
            break
        consulta["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    lista = sorted(({"etiqueta": e, "fotos": len(f)} for e, f in conteo.items()), key=lambda x: (-x["fotos"], x["etiqueta"]))
    return 200, {"etiquetas": lista}


def lambda_handler(event, _context):
    usuario = event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    ruta = event["routeKey"]
    params = event.get("queryStringParameters") or {}
    foto = (event.get("pathParameters") or {}).get("id", "")
    try:
        if foto and not re.fullmatch(r"[0-9a-f]{32}", foto):
            raise ErrorCliente(404, "La foto no existe")
        if ruta == "POST /subidas":
            cuerpo = event.get("body") or "{}"
            if event.get("isBase64Encoded"):
                cuerpo = base64.b64decode(cuerpo)
            try:
                cuerpo = json.loads(cuerpo)
            except ValueError:
                raise ErrorCliente(400, "JSON inválido")
            return responder(*crear_subida(usuario, cuerpo))
        if ruta == "GET /fotos":
            return responder(*listar(usuario, params))
        if ruta == "GET /fotos/{id}":
            return responder(*detalle(usuario, foto))
        if ruta == "DELETE /fotos/{id}":
            return responder(*borrar(usuario, foto))
        if ruta == "GET /etiquetas":
            return responder(*etiquetas(usuario))
        raise ErrorCliente(404, "Ruta no encontrada")
    except ErrorCliente as e:
        return responder(e.status, {"error": str(e)})
