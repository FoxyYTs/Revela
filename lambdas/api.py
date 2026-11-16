"""API de Revela detrás de API Gateway HTTP con autorizador JWT de Cognito.

POST   /subidas       → URL prefirmada para subir una foto directo a S3
GET    /fotos         → galería por fecha (?siguiente=cursor), búsqueda (?q=perro), exacta y por significado, o
                        fotos de una persona (?persona=id)
GET    /fotos/{id}    → detalle con la URL del original
DELETE /fotos/{id}    → borra la foto, la miniatura y sus índices
GET    /etiquetas     → etiquetas del usuario con cuántas fotos tiene cada una
GET    /lugares       → fotos con ubicación, para el mapa
GET    /mapa          → API key de Amazon Location (restringida a la web) para pintar el mapa
GET    /personas      → si el reconocimiento está activo y las personas con su cara de portada
PUT    /personas      → activa el reconocimiento de caras (consentimiento): crea la colección del usuario
DELETE /personas      → lo desactiva y borra todos los datos biométricos (la colección entera)
PUT    /personas/{id} → nombre (vacío lo quita) y/o foto de portada de una persona
POST   /personas/{id}/unir → {"persona": otra}: la otra persona pasa a ser esta (Rekognition la confundió con dos)
POST   /fotos/{id}/caras → vuelve a buscar caras en una foto con un filtro de calidad más permisivo
GET    /albumes       → álbumes del usuario;  POST /albumes {nombre} → crea uno
GET    /albumes/{id}  → el álbum con sus fotos;  PUT {nombre?, agregar?, quitar?};  DELETE lo borra (y su enlace)
POST   /albumes/{id}/enlace {dias} → enlace para verlo sin cuenta;  DELETE lo revoca
GET    /compartidos/{token} (pública) → el álbum compartido con URLs de CloudFront firmadas por una hora
POST   /asistente     → lo atiende otra Lambda (asistente.py): agente de Strands que combina estas búsquedas
"""
import base64
import json
import logging
import os
import re
import secrets
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key

import caras as rostros

log = logging.getLogger()
log.setLevel(logging.INFO)

TABLA = os.environ.get("TABLA", "jyj-revela-fotos")
BUCKET = os.environ.get("BUCKET", "jyj-revela-fotos")
TIPOS = {"image/jpeg": "jpg", "image/png": "png"}  # los formatos que acepta Rekognition
MAX_BYTES = 15 * 1024 * 1024  # límite de Rekognition para imágenes en S3
VIGENCIA_URL = 3600
POR_PAGINA = 60
EXPIRA_PENDIENTE = 24 * 3600  # una subida que nunca llega se borra sola por TTL
# Tope de gasto por persona: cada subida cuesta 3 llamadas a Rekognition + Nova. Cuenta las URLs pedidas, no las fotos
# que llegan, para que pedir URLs en bucle también se frene
SUBIDAS_DIA = int(os.environ.get("SUBIDAS_DIA", "50"))
BUSQUEDAS_CARAS_DIA = 30  # cada "Buscar caras" cuesta hasta 31 llamadas a Rekognition (10 caras × 3 + IndexFaces)
COLOMBIA = timezone(timedelta(hours=-5))
MAPA_KEY = os.environ.get("MAPA_KEY", "jyj-revela-mapa")
MAX_LUGARES = 500
# Una foto que sigue en PROCESANDO después de esto se abandonó (la subida a S3 falló) o el análisis se trabó
ESPERA_ANALISIS = timedelta(minutes=10)
VECTORES = os.environ.get("VECTORES", "jyj-revela-vectores")
EMBEDDINGS = "amazon.nova-2-multimodal-embeddings-v1:0"
POR_SIGNIFICADO = 30
MAX_NOMBRE = 60
MAX_ALBUM = 200
DIAS_ENLACE = (1, 7, 30)
VIGENCIA_CDN = 3600  # si se revoca el enlace, lo ya abierto deja de cargar a la hora
CDN = os.environ.get("CDN", "")
FIRMA_ID = os.environ.get("FIRMA_ID", "")
LLAVE = os.environ.get("LLAVE", "/jyj-revela/cloudfront-llave")
# shortcut: umbral medido con una sola foto (relacionadas 0.38-0.70, ajenas 0.74-0.87); recalibrar con más fotos
DISTANCIA_MAX = float(os.environ.get("DISTANCIA_MAX", "0.72"))

dynamo = boto3.resource("dynamodb")
tabla = dynamo.Table(TABLA)
s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
bedrock = boto3.client("bedrock-runtime")
rekognition = rostros.rekognition
_clave_mapa = None
_vectores = None
_firmador = None


def vectores():
    # Perezoso: si el boto3 del runtime no trae s3vectors, solo se pierde la búsqueda por significado
    global _vectores
    if _vectores is None:
        _vectores = boto3.client("s3vectors")
    return _vectores


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


def firmar_cdn(key, vence):
    """URL de CloudFront firmada con la llave del key group: el bucket de fotos solo se abre así."""
    global _firmador
    if _firmador is None:
        import rsa  # capa "firmas"
        from botocore.signers import CloudFrontSigner
        pem = boto3.client("ssm").get_parameter(Name=LLAVE, WithDecryption=True)["Parameter"]["Value"]
        llave = rsa.PrivateKey.load_pkcs1(pem.encode())
        _firmador = CloudFrontSigner(FIRMA_ID, lambda mensaje: rsa.sign(mensaje, llave, "SHA-1"))
    return _firmador.generate_presigned_url(f"https://{CDN}/{key}", date_less_than=vence)


def firmar(key):
    return s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=VIGENCIA_URL)


def estado_visible(item):
    """El estado que ve el usuario; None si es una subida que nunca llegó (se oculta hasta que el TTL la borre)."""
    if item["estado"] != "PROCESANDO":
        return item["estado"]
    subida = datetime.fromisoformat(item["fecha_subida"]).replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - subida < ESPERA_ANALISIS:
        return "PROCESANDO"
    return "ERROR" if item.get("analisis") else None


def resumen(item):
    foto = {"id": item["SK"][2:], "nombre": item.get("nombre"), "estado": estado_visible(item),
            "fecha": item["fecha_orden"].split("#")[0], "etiquetas": item.get("etiquetas", [])[:5],
            "descripcion": item.get("descripcion"), "ancho": item.get("ancho"), "alto": item.get("alto")}  # la grilla respeta la forma
    # Las fotos en cuarentena nunca salen con URL
    if item["estado"] == "LISTA":
        foto["miniatura"] = firmar(item["miniatura"])
    return foto


def leer_ubicacion(valor):
    """{"lat", "lon"} del navegador; se usa solo si la foto no trae GPS (Android e iOS lo borran al compartir)."""
    if valor is None:
        return None
    try:
        lat, lon = float(valor["lat"]), float(valor["lon"])
    except (KeyError, TypeError, ValueError):
        raise ErrorCliente(400, "Ubicación inválida") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ErrorCliente(400, "Ubicación inválida")
    return {"lat": Decimal(str(round(lat, 6))), "lon": Decimal(str(round(lon, 6)))}


def descontar_cupo(usuario, contador="subidas", tope=None, mensaje=None):
    # Contador atómico por día: la condición impide pasar el tope aunque lleguen peticiones simultáneas
    hoy = datetime.now(COLOMBIA)
    try:
        tabla.update_item(
            Key={"PK": f"U#{usuario}", "SK": f"CUPO#{hoy:%Y-%m-%d}"},
            UpdateExpression=f"ADD {contador} :uno SET expira = if_not_exists(expira, :expira)",
            ConditionExpression=f"attribute_not_exists({contador}) OR {contador} < :tope",
            ExpressionAttributeValues={":uno": 1, ":tope": tope or SUBIDAS_DIA, ":expira": int(hoy.timestamp()) + 2 * 86400})
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        raise ErrorCliente(429, mensaje or f"Llegaste al límite de {SUBIDAS_DIA} fotos por día; mañana puedes subir más") from None


def crear_subida(usuario, cuerpo):
    tipo = cuerpo.get("tipo")
    if tipo not in TIPOS:
        raise ErrorCliente(400, "Solo se aceptan fotos JPG o PNG")
    descontar_cupo(usuario)
    foto = uuid.uuid4().hex
    key = f"originales/{usuario}/{foto}.{TIPOS[tipo]}"
    ahora = datetime.now(timezone.utc)

    item = {
        "PK": f"U#{usuario}", "SK": f"F#{foto}", "estado": "PROCESANDO",
        "nombre": str(cuerpo.get("nombre", ""))[:200], "original": key,
        "fecha_subida": ahora.strftime("%Y-%m-%dT%H:%M:%S"), "fecha_orden": f"{ahora:%Y-%m-%dT%H:%M:%S}#{foto}",
        "expira": int(ahora.timestamp()) + EXPIRA_PENDIENTE,
    }
    ubicacion = leer_ubicacion(cuerpo.get("ubicacion"))
    if ubicacion:
        item["gps_subida"] = ubicacion
    tabla.put_item(Item=item)
    # S3 rechaza el POST si el tipo, la key o el tamaño no coinciden con lo firmado
    post = s3.generate_presigned_post(
        BUCKET, key, Fields={"Content-Type": tipo},
        Conditions=[{"Content-Type": tipo}, ["content-length-range", 1, MAX_BYTES]], ExpiresIn=120)
    return 201, {"foto": foto, "url": post["url"], "campos": post["fields"]}


def listar(usuario, params):
    pk = f"U#{usuario}"
    if params.get("persona"):
        if not re.fullmatch(r"[0-9a-f]{32}", params["persona"]):
            raise ErrorCliente(400, "Persona inválida")
        ids = [i["foto"] for i in consultar(pk, f"P#{params['persona']}#", ScanIndexForward=False)]
        return 200, {"fotos": [resumen(i) for i in leer_fotos(pk, ids[:100]).values()], "siguiente": None}
    texto = params.get("q", "")[:200]
    q = normalizar(texto)
    if q:
        return 200, {"fotos": buscar(usuario, q, texto), "siguiente": None}

    consulta = {"IndexName": "por-fecha", "KeyConditionExpression": Key("PK").eq(pk),
                "ScanIndexForward": False, "Limit": POR_PAGINA}
    if params.get("siguiente"):
        try:
            consulta["ExclusiveStartKey"] = json.loads(base64.urlsafe_b64decode(params["siguiente"]))
        except ValueError:
            raise ErrorCliente(400, "Cursor inválido") from None
        if consulta["ExclusiveStartKey"].get("PK") != pk:
            raise ErrorCliente(400, "Cursor inválido") from None
    r = tabla.query(**consulta)
    siguiente = r.get("LastEvaluatedKey")
    return 200, {"fotos": [f for f in map(resumen, r["Items"]) if f["estado"]],
                 "siguiente": base64.urlsafe_b64encode(json.dumps(siguiente).encode()).decode() if siguiente else None}


def por_significado(usuario, texto):
    """Ids de las fotos cuyo vector queda cerca del texto, de la más parecida a la menos."""
    cuerpo = {"taskType": "SINGLE_EMBEDDING", "singleEmbeddingParams": {
        "embeddingPurpose": "IMAGE_RETRIEVAL", "embeddingDimension": 1024, "text": {"truncationMode": "END", "value": texto}}}
    vector = json.loads(bedrock.invoke_model(modelId=EMBEDDINGS, body=json.dumps(cuerpo))["body"].read())["embeddings"][0]["embedding"]
    r = vectores().query_vectors(vectorBucketName=VECTORES, indexName="fotos", queryVector={"float32": vector},
                                 topK=POR_SIGNIFICADO, filter={"usuario": usuario}, returnDistance=True)
    return [v["key"] for v in r["vectors"] if v["distance"] <= DISTANCIA_MAX]


def buscar(usuario, q, texto):
    # shortcut: máximo 100 resultados exactos sin paginar; paginar si una búsqueda llega a ese tope
    pk, exactas = f"U#{usuario}", []
    prefijos = [f"{p}{q}#" for p in ("E#", "T#", "L#")]
    prefijos += [f"P#{i['SK'][8:]}#" for i in consultar(pk, "PERSONA#") if normalizar(i.get("nombre", "")) == q]  # por nombre
    for prefijo in prefijos:
        r = tabla.query(KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with(prefijo), ScanIndexForward=False, Limit=100)
        exactas += [i["foto"] for i in r["Items"] if i["foto"] not in exactas]
    exactas = exactas[:100]
    try:
        parecidas = [i for i in por_significado(usuario, texto) if i not in exactas]
    except Exception as e:  # noqa: BLE001 — si Bedrock o S3 Vectors fallan quedan las coincidencias exactas
        log.warning("Búsqueda por significado falló: %s", type(e).__name__)
        parecidas = []
    items = leer_fotos(pk, (exactas + parecidas)[:100])  # tope de BatchGetItem
    # Primero las exactas por fecha, luego las parecidas por cercanía; un vector viejo de una foto en cuarentena no sale
    fotos = sorted((resumen(items[i]) for i in exactas if i in items), key=lambda f: f["fecha"], reverse=True)
    return fotos + [{**resumen(items[i]), "parecida": True} for i in parecidas if i in items and items[i]["estado"] == "LISTA"]


def leer_fotos(pk, ids):
    """{id: item} en el mismo orden de ids; las que ya no existen no aparecen."""
    items = {}
    for inicio in range(0, len(ids), 100):  # tope de BatchGetItem
        pedido = {TABLA: {"Keys": [{"PK": pk, "SK": f"F#{i}"} for i in dict.fromkeys(ids[inicio:inicio + 100])]}}
        while pedido:  # DynamoDB puede devolver parte de las llaves sin leer si hay mucha carga
            r = dynamo.batch_get_item(RequestItems=pedido)
            items.update((i["SK"][2:], i) for i in r["Responses"][TABLA])
            pedido = r.get("UnprocessedKeys")
    return {i: items[i] for i in ids if i in items}


def consultar(pk, prefijo, **extra):
    """Todos los items de la partición del usuario cuyo SK empieza por prefijo, paginando."""
    consulta = {"KeyConditionExpression": Key("PK").eq(pk) & Key("SK").begins_with(prefijo), **extra}
    while True:
        r = tabla.query(**consulta)
        yield from r["Items"]
        if "LastEvaluatedKey" not in r:
            return
        consulta["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def obtener(usuario, foto):
    item = tabla.get_item(Key={"PK": f"U#{usuario}", "SK": f"F#{foto}"}).get("Item")
    if not item:
        raise ErrorCliente(404, "La foto no existe")
    return item


def detalle(usuario, foto):
    item = obtener(usuario, foto)
    datos = resumen(item)
    datos.update({k: item.get(k) for k in ("etiquetas", "texto", "descripcion", "camara", "gps", "gps_fuente", "lugar", "ancho", "alto",
                                       "fecha_toma", "moderacion", "personas")})
    if item["estado"] == "LISTA":
        datos["original"] = firmar(item.get("revisada", item["original"]))  # las de antes de fotos/ no tienen copia
    return 200, datos


def borrar(usuario, foto):
    item = obtener(usuario, foto)
    pk = f"U#{usuario}"
    vectores().delete_vectors(vectorBucketName=VECTORES, indexName="fotos", keys=[foto])  # primero: si falla, no queda nada a medias
    if item.get("caras"):
        rostros.borrar(usuario, item["caras"])
    s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": k} for k in (item["original"], item.get("revisada"), item.get("miniatura"), item.get("publica")) if k]})
    with tabla.batch_writer() as lote:
        for sk in item.get("claves", []):
            lote.delete_item(Key={"PK": pk, "SK": sk})
    tabla.delete_item(Key={"PK": pk, "SK": item["SK"]})
    return 204, None


def etiquetas(usuario):
    conteo = {}
    for i in consultar(f"U#{usuario}", "E#", ProjectionExpression="foto, etiqueta"):
        conteo.setdefault(i["etiqueta"], set()).add(i["foto"])
    lista = sorted(({"etiqueta": e, "fotos": len(f)} for e, f in conteo.items()), key=lambda x: (-x["fotos"], x["etiqueta"]))
    return 200, {"etiquetas": lista}


def lugares(usuario):
    # shortcut: recorre la galería entera hasta MAX_LUGARES fotos; un índice disperso por GPS si crece mucho
    fotos, consulta = [], {"IndexName": "por-fecha", "KeyConditionExpression": Key("PK").eq(f"U#{usuario}"),
                           "ScanIndexForward": False}
    while len(fotos) < MAX_LUGARES:
        r = tabla.query(**consulta)
        fotos += [{**resumen(i), "lat": i["gps"]["lat"], "lon": i["gps"]["lon"], "lugar": (i.get("lugar") or {}).get("nombre"),
                   "aproximada": i.get("gps_fuente") == "subida"}
                  for i in r["Items"] if i["estado"] == "LISTA" and i.get("gps")]
        if "LastEvaluatedKey" not in r:
            break
        consulta["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    return 200, {"fotos": fotos[:MAX_LUGARES]}


def personas(usuario):
    pk = f"U#{usuario}"
    activo = bool(tabla.get_item(Key={"PK": pk, "SK": "PERFIL"}).get("Item", {}).get("personas"))
    datos = {i["SK"][8:]: i for i in consultar(pk, "PERSONA#")}
    grupos = {}
    # De la más reciente a la más vieja: la portada es la que eligió el usuario o, si no, la última foto
    for i in consultar(pk, "P#", ScanIndexForward=False, ProjectionExpression="foto, persona, caja"):
        g = grupos.setdefault(i["persona"], {"id": i["persona"], "nombre": datos.get(i["persona"], {}).get("nombre"),
                                             "fotos": set(), "portada": None})
        if g["portada"] is None or i["foto"] == datos.get(i["persona"], {}).get("portada"):
            g["portada"] = {"foto": i["foto"], "miniatura": f"miniaturas/{usuario}/{i['foto']}.jpg", "caja": i["caja"]}
        g["fotos"].add(i["foto"])
    for g in grupos.values():
        g["portada"]["miniatura"] = firmar(g["portada"]["miniatura"])
    lista = sorted(({**g, "fotos": len(g["fotos"])} for g in grupos.values()), key=lambda g: (-g["fotos"], g["nombre"] or "~"))
    return 200, {"activo": activo, "personas": lista}


def activar_personas(usuario):
    try:
        rekognition.create_collection(CollectionId=rostros.CARAS + usuario)
    except rekognition.exceptions.ResourceAlreadyExistsException:
        pass
    tabla.put_item(Item={"PK": f"U#{usuario}", "SK": "PERFIL", "personas": True,
                         "consentimiento": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")})
    return personas(usuario)


def desactivar_personas(usuario):
    # Primero la colección (las huellas de las caras); lo demás son ids y cajas que sin ella no identifican a nadie
    pk = f"U#{usuario}"
    tabla.delete_item(Key={"PK": pk, "SK": "PERFIL"})
    try:
        rekognition.delete_collection(CollectionId=rostros.CARAS + usuario)
    except rekognition.exceptions.ResourceNotFoundException:
        pass
    with tabla.batch_writer() as lote:
        for prefijo in ("P#", "PERSONA#"):
            for i in consultar(pk, prefijo, ProjectionExpression="SK"):
                lote.delete_item(Key={"PK": pk, "SK": i["SK"]})
    # shortcut: recorre toda la galería; un índice disperso por "caras" si las galerías crecen mucho
    for i in consultar(pk, "F#", FilterExpression="attribute_exists(caras)", ProjectionExpression="SK, claves"):
        tabla.update_item(Key={"PK": pk, "SK": i["SK"]}, UpdateExpression="SET claves = :cl REMOVE personas, caras",
                          ExpressionAttributeValues={":cl": [c for c in i.get("claves", []) if not c.startswith("P#")]})
    return personas(usuario)


def editar_persona(usuario, persona, cuerpo):
    pk = f"U#{usuario}"
    fotos = {i["foto"] for i in consultar(pk, f"P#{persona}#", ProjectionExpression="foto")}
    if not fotos:
        raise ErrorCliente(404, "La persona no existe")
    poner, quitar, valores = [], [], {}
    if "nombre" in cuerpo:
        nombre = " ".join(str(cuerpo["nombre"] or "").split())
        if len(nombre) > MAX_NOMBRE:
            raise ErrorCliente(400, f"El nombre puede tener hasta {MAX_NOMBRE} caracteres")
        if nombre:
            poner.append("nombre = :n")
            valores[":n"] = nombre
        else:
            quitar.append("nombre")
    if "portada" in cuerpo:
        if cuerpo["portada"] not in fotos:
            raise ErrorCliente(400, "Esa foto no es de esta persona")
        poner.append("portada = :p")
        valores[":p"] = cuerpo["portada"]
    if not poner and not quitar:
        raise ErrorCliente(400, "No hay nada que cambiar")
    expresion = " ".join(([f"SET {', '.join(poner)}"] if poner else []) + ([f"REMOVE {', '.join(quitar)}"] if quitar else []))
    r = tabla.update_item(Key={"PK": pk, "SK": f"PERSONA#{persona}"}, UpdateExpression=expresion, ReturnValues="ALL_NEW",
                          **({"ExpressionAttributeValues": valores} if valores else {}))
    return 200, {"id": persona, "nombre": r["Attributes"].get("nombre"), "portada": r["Attributes"].get("portada")}


def unir_personas(usuario, persona, cuerpo):
    pk, otra = f"U#{usuario}", cuerpo.get("persona")
    if not isinstance(otra, str) or not re.fullmatch(r"[0-9a-f]{32}", otra) or otra == persona:
        raise ErrorCliente(400, "Elige otra persona")
    indices = list(consultar(pk, f"P#{otra}#"))
    if not indices or not tabla.query(KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with(f"P#{persona}#"), Limit=1)["Items"]:
        raise ErrorCliente(404, "La persona no existe")

    # Rekognition: las caras de la otra pasan a esta, para que las fotos nuevas de cualquiera de las dos caigan aquí.
    # Umbral 0: el usuario ya decidió que son la misma, aunque Rekognition no las vea parecidas
    coleccion = rostros.CARAS + usuario
    caras = [c["FaceId"] for pagina in rekognition.get_paginator("list_faces").paginate(CollectionId=coleccion, UserId=otra)
             for c in pagina["Faces"]]
    for i in range(0, len(caras), 100):
        rekognition.disassociate_faces(CollectionId=coleccion, UserId=otra, FaceIds=caras[i:i + 100])
        rekognition.associate_faces(CollectionId=coleccion, UserId=persona, FaceIds=caras[i:i + 100], UserMatchThreshold=0)
    rekognition.delete_user(CollectionId=coleccion, UserId=otra)

    # DynamoDB: el índice P# y las fotos cambian de persona; el nombre de la otra sirve si esta no tiene
    with tabla.batch_writer(overwrite_by_pkeys=["PK", "SK"]) as lote:
        for i in indices:
            nueva = f"P#{persona}#" + i["SK"].split("#", 2)[2]
            lote.put_item(Item={**i, "SK": nueva, "persona": persona})
            lote.delete_item(Key={"PK": pk, "SK": i["SK"]})
    for foto in {i["foto"] for i in indices}:
        item = tabla.get_item(Key={"PK": pk, "SK": f"F#{foto}"}).get("Item")
        if item:
            tabla.update_item(Key={"PK": pk, "SK": item["SK"]}, UpdateExpression="SET personas = :pe, claves = :cl", ExpressionAttributeValues={
                ":pe": list(dict.fromkeys(persona if p == otra else p for p in item.get("personas", []))),
                ":cl": list(dict.fromkeys(c.replace(f"P#{otra}#", f"P#{persona}#", 1) for c in item.get("claves", [])))})
    datos_otra = tabla.get_item(Key={"PK": pk, "SK": f"PERSONA#{otra}"}).get("Item", {})
    if datos_otra.get("nombre"):
        tabla.update_item(Key={"PK": pk, "SK": f"PERSONA#{persona}"}, UpdateExpression="SET nombre = if_not_exists(nombre, :n)",
                          ExpressionAttributeValues={":n": datos_otra["nombre"]})
    tabla.delete_item(Key={"PK": pk, "SK": f"PERSONA#{otra}"})
    return personas(usuario)


def buscar_caras(usuario, foto):
    """Otra pasada sobre una foto (subida antes de activar Personas, o con caras pequeñas o borrosas que el filtro
    automático descartó): reemplaza sus caras y su índice por persona."""
    pk = f"U#{usuario}"
    item = obtener(usuario, foto)
    if item["estado"] != "LISTA":
        raise ErrorCliente(409, "Solo se buscan caras en fotos ya analizadas")
    if not tabla.get_item(Key={"PK": pk, "SK": "PERFIL"}).get("Item", {}).get("personas"):
        raise ErrorCliente(409, "Primero activa Personas")
    descontar_cupo(usuario, "busquedas_caras", BUSQUEDAS_CARAS_DIA,
                   f"Llegaste al límite de {BUSQUEDAS_CARAS_DIA} búsquedas de caras por día; mañana puedes buscar más")
    # publicas/ sigue en Standard; fotos/ pasa a Glacier IR al mes (cobra por leerlo). Las fotos viejas no tienen publicas/
    imagen = item.get("publica") or item.get("revisada", item["original"])
    personas, caras = rostros.reconocer(usuario, foto, BUCKET, imagen, calidad="LOW")
    if item.get("caras"):
        rostros.borrar(usuario, item["caras"])
    claves = {f"P#{p}#{item['fecha_orden']}": {"persona": p, "caja": caja} for p, caja in personas}
    otras = [c for c in item.get("claves", []) if not c.startswith("P#")]
    with tabla.batch_writer(overwrite_by_pkeys=["PK", "SK"]) as lote:
        for sk in set(item.get("claves", [])) - set(otras) - set(claves):
            lote.delete_item(Key={"PK": pk, "SK": sk})
        for sk, datos in claves.items():
            lote.put_item(Item={"PK": pk, "SK": sk, "foto": foto, **datos})
    ids = list(dict.fromkeys(p for p, _ in personas))
    tabla.update_item(Key={"PK": pk, "SK": item["SK"]}, UpdateExpression="SET personas = :pe, caras = :cr, claves = :cl",
                      ExpressionAttributeValues={":pe": ids, ":cr": caras, ":cl": otras + list(claves)})
    return 200, {"personas": ids}


def obtener_album(usuario, album):
    item = tabla.get_item(Key={"PK": f"U#{usuario}", "SK": f"ALBUM#{album}"}).get("Item")
    if not item:
        raise ErrorCliente(404, "El álbum no existe")
    return item


def nombre_album(cuerpo):
    nombre = " ".join(str(cuerpo.get("nombre") or "").split())
    if not nombre or len(nombre) > MAX_NOMBRE:
        raise ErrorCliente(400, f"El álbum necesita un nombre de hasta {MAX_NOMBRE} caracteres")
    return nombre


def enlace(item):
    """{url, expira} si el álbum está compartido y el enlace sigue vigente."""
    if item.get("enlace") and int(item["enlace_expira"]) > datetime.now(timezone.utc).timestamp():
        return {"url": f"https://{CDN}/album.html#{item['enlace']}",
                "expira": datetime.fromtimestamp(int(item["enlace_expira"]), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    return None


def albumes(usuario):
    pk = f"U#{usuario}"
    items = sorted(consultar(pk, "ALBUM#"), key=lambda i: i["creado"], reverse=True)
    portadas = leer_fotos(pk, [i["fotos"][0] for i in items if i.get("fotos")])
    lista = []
    for i in items:
        portada = portadas.get((i.get("fotos") or [None])[0])
        lista.append({"id": i["SK"][6:], "nombre": i["nombre"], "fotos": len(i.get("fotos", [])), "enlace": enlace(i),
                      "portada": firmar(portada["miniatura"]) if portada and portada["estado"] == "LISTA" else None})
    return 200, {"albumes": lista}


def crear_album(usuario, cuerpo):
    album = uuid.uuid4().hex
    nombre = nombre_album(cuerpo)
    tabla.put_item(Item={"PK": f"U#{usuario}", "SK": f"ALBUM#{album}", "nombre": nombre, "fotos": [],
                         "creado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")})
    return 201, {"id": album, "nombre": nombre, "fotos": 0, "enlace": None, "portada": None}


def ver_album(usuario, album):
    item = obtener_album(usuario, album)
    fotos = [f for f in map(resumen, leer_fotos(f"U#{usuario}", item.get("fotos", [])).values()) if f["estado"]]
    return 200, {"id": album, "nombre": item["nombre"], "enlace": enlace(item), "fotos": fotos}


def ids_validos(valor):
    if not isinstance(valor, list) or len(valor) > MAX_ALBUM or not all(isinstance(i, str) and re.fullmatch(r"[0-9a-f]{32}", i) for i in valor):
        raise ErrorCliente(400, "Lista de fotos inválida")
    return valor


def editar_album(usuario, album, cuerpo):
    item = obtener_album(usuario, album)
    nombre = nombre_album(cuerpo) if "nombre" in cuerpo else item["nombre"]
    fotos = list(item.get("fotos", []))
    if "agregar" in cuerpo:
        # Solo fotos propias y ya analizadas: lo que entra a un álbum puede terminar compartido
        propias = leer_fotos(f"U#{usuario}", ids_validos(cuerpo["agregar"]))
        fotos += [i for i, f in propias.items() if f["estado"] == "LISTA" and i not in fotos]
        if len(fotos) > MAX_ALBUM:
            raise ErrorCliente(400, f"Un álbum puede tener hasta {MAX_ALBUM} fotos")
    if "quitar" in cuerpo:
        quitar = set(ids_validos(cuerpo["quitar"]))
        fotos = [i for i in fotos if i not in quitar]
    tabla.update_item(Key={"PK": item["PK"], "SK": item["SK"]}, UpdateExpression="SET nombre = :n, fotos = :f",
                      ExpressionAttributeValues={":n": nombre, ":f": fotos})
    return 200, {"id": album, "nombre": nombre, "fotos": len(fotos)}


def revocar(item):
    if item.get("enlace"):
        tabla.delete_item(Key={"PK": f"C#{item['enlace']}", "SK": "C"})


def borrar_album(usuario, album):
    item = obtener_album(usuario, album)
    revocar(item)
    tabla.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    return 204, None


def compartir(usuario, album, cuerpo):
    if cuerpo.get("dias") not in DIAS_ENLACE:
        raise ErrorCliente(400, "El enlace puede durar 1, 7 o 30 días")
    item = obtener_album(usuario, album)
    revocar(item)  # un enlace nuevo invalida el anterior
    token = secrets.token_urlsafe(24)
    expira = int(datetime.now(timezone.utc).timestamp()) + cuerpo["dias"] * 86400
    tabla.put_item(Item={"PK": f"C#{token}", "SK": "C", "usuario": usuario, "album": album, "expira": expira})
    tabla.update_item(Key={"PK": item["PK"], "SK": item["SK"]}, UpdateExpression="SET enlace = :t, enlace_expira = :e",
                      ExpressionAttributeValues={":t": token, ":e": expira})
    return 200, enlace({"enlace": token, "enlace_expira": expira})


def dejar_de_compartir(usuario, album):
    item = obtener_album(usuario, album)
    revocar(item)
    tabla.update_item(Key={"PK": item["PK"], "SK": item["SK"]}, UpdateExpression="REMOVE enlace, enlace_expira")
    return 204, None


def ver_compartido(token):
    """Ruta pública: solo lo necesario para mirar el álbum (sin GPS, etiquetas ni personas). Nunca se firma el archivo
    subido (fotos/ conserva el EXIF con el GPS): la copia limpia de publicas/ o, en fotos viejas sin ella, la miniatura."""
    ahora = datetime.now(timezone.utc)
    c = tabla.get_item(Key={"PK": f"C#{token}", "SK": "C"}).get("Item") if re.fullmatch(r"[A-Za-z0-9_-]{32}", token) else None
    item = c and tabla.get_item(Key={"PK": f"U#{c['usuario']}", "SK": f"ALBUM#{c['album']}"}).get("Item")
    # El TTL de DynamoDB borra tarde: la fecha se revisa aquí
    if not item or item.get("enlace") != token or int(c["expira"]) <= ahora.timestamp():
        raise ErrorCliente(404, "Este enlace no existe o ya venció")
    vence = min(ahora + timedelta(seconds=VIGENCIA_CDN), datetime.fromtimestamp(int(c["expira"]), timezone.utc))
    fotos = [{"miniatura": firmar_cdn(f["miniatura"], vence), "original": firmar_cdn(f.get("publica") or f["miniatura"], vence),
              "descripcion": f.get("descripcion"), "fecha": f["fecha_orden"].split("#")[0]}
             for f in leer_fotos(f"U#{c['usuario']}", item.get("fotos", [])).values() if f["estado"] == "LISTA"]
    return 200, {"nombre": item["nombre"], "fotos": fotos}


def mapa():
    # La key es pública por diseño (va al navegador) pero solo sirve para mosaicos y desde el dominio de la web
    global _clave_mapa
    if _clave_mapa is None:
        _clave_mapa = boto3.client("location").describe_key(KeyName=MAPA_KEY)["Key"]
    return 200, {"apiKey": _clave_mapa, "region": tabla.meta.client.meta.region_name}


def lambda_handler(event, _context):
    ruta = event["routeKey"]
    if ruta == "GET /compartidos/{token}":  # la única ruta sin JWT
        try:
            return responder(*ver_compartido(event["pathParameters"]["token"]))
        except ErrorCliente as e:
            return responder(e.status, {"error": str(e)})
    usuario = event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    params = event.get("queryStringParameters") or {}
    foto = (event.get("pathParameters") or {}).get("id", "")  # id de la foto o de la persona, según la ruta
    try:
        if foto and not re.fullmatch(r"[0-9a-f]{32}", foto):
            raise ErrorCliente(404, "No existe")
        cuerpo = {}
        if event.get("body"):
            cuerpo = base64.b64decode(event["body"]) if event.get("isBase64Encoded") else event["body"]
            try:
                cuerpo = json.loads(cuerpo)
            except ValueError:
                raise ErrorCliente(400, "JSON inválido") from None
            if not isinstance(cuerpo, dict):
                raise ErrorCliente(400, "JSON inválido")
        if ruta == "POST /subidas":
            return responder(*crear_subida(usuario, cuerpo))
        if ruta == "GET /fotos":
            return responder(*listar(usuario, params))
        if ruta == "GET /fotos/{id}":
            return responder(*detalle(usuario, foto))
        if ruta == "DELETE /fotos/{id}":
            return responder(*borrar(usuario, foto))
        if ruta == "GET /etiquetas":
            return responder(*etiquetas(usuario))
        if ruta == "GET /lugares":
            return responder(*lugares(usuario))
        if ruta == "GET /mapa":
            return responder(*mapa())
        if ruta == "GET /personas":
            return responder(*personas(usuario))
        if ruta == "PUT /personas":
            return responder(*activar_personas(usuario))
        if ruta == "DELETE /personas":
            return responder(*desactivar_personas(usuario))
        if ruta == "PUT /personas/{id}":
            return responder(*editar_persona(usuario, foto, cuerpo))
        if ruta == "POST /personas/{id}/unir":
            return responder(*unir_personas(usuario, foto, cuerpo))
        if ruta == "GET /albumes":
            return responder(*albumes(usuario))
        if ruta == "POST /albumes":
            return responder(*crear_album(usuario, cuerpo))
        if ruta == "GET /albumes/{id}":
            return responder(*ver_album(usuario, foto))
        if ruta == "PUT /albumes/{id}":
            return responder(*editar_album(usuario, foto, cuerpo))
        if ruta == "DELETE /albumes/{id}":
            return responder(*borrar_album(usuario, foto))
        if ruta == "POST /albumes/{id}/enlace":
            return responder(*compartir(usuario, foto, cuerpo))
        if ruta == "DELETE /albumes/{id}/enlace":
            return responder(*dejar_de_compartir(usuario, foto))
        if ruta == "POST /fotos/{id}/caras":
            return responder(*buscar_caras(usuario, foto))
        raise ErrorCliente(404, "Ruta no encontrada")
    except ErrorCliente as e:
        return responder(e.status, {"error": str(e)})
