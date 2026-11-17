"""Recorre el flujo completo en moto: subida → procesar → guardar → API.
Rekognition no se simula: se pasan respuestas con la forma que entrega Step Functions."""
import io
import json

import botocore.response

import boto3
import pytest
from botocore.stub import Stubber
from PIL import ExifTags, Image, UnidentifiedImageError

import api
import caras as caras_mod
import guardar
import procesar

USUARIO = "1111-aaaa"
PERRO = [{"Name": "Dog", "Confidence": 99.1}, {"Name": "Beach", "Confidence": 91.0}]


def jpeg(ancho=1200, alto=800, gps=True):
    exif = Image.Exif()
    exif[ExifTags.Base.Make] = "Google"
    exif[ExifTags.Base.Model] = "Pixel 8"
    exif[ExifTags.Base.Orientation] = 6  # rotada 90°: la miniatura debe quedar vertical
    exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal] = "2025:12:24 20:15:00"
    if gps:
        exif.get_ifd(ExifTags.IFD.GPSInfo).update({
            ExifTags.GPS.GPSLatitudeRef: "N", ExifTags.GPS.GPSLatitude: (6.0, 15.0, 3.6),
            ExifTags.GPS.GPSLongitudeRef: "W", ExifTags.GPS.GPSLongitude: (75.0, 34.0, 1.2)})
    salida = io.BytesIO()
    Image.new("RGB", (ancho, alto), "orange").save(salida, "JPEG", exif=exif)
    return salida.getvalue()


def nova(etiquetas, descripcion="Un perro corre por la playa.", cerca=False):
    texto = json.dumps({"descripcion": descripcion, "etiquetas": etiquetas}, ensure_ascii=False)
    return f"```json\n{texto}\n```" if cerca else texto


def respuesta_nova(texto):
    return {"output": {"message": {"role": "assistant", "content": [{"text": texto}]}}, "stopReason": "end_turn",
            "usage": {"inputTokens": 80, "outputTokens": 10, "totalTokens": 90}, "metrics": {"latencyMs": 300}}


def respuesta_vector(valor=0.5):
    cuerpo = json.dumps({"embeddings": [{"embeddingType": "IMAGE", "embedding": [valor] * 1024}]}).encode()
    return {"body": botocore.response.StreamingBody(io.BytesIO(cuerpo), len(cuerpo)), "contentType": "application/json"}


def vectores_de(*fotos):
    r = boto3.client("s3vectors").get_vectors(vectorBucketName="revela-test", indexName="fotos", keys=list(fotos), returnMetadata=True)
    return {v["key"]: v["metadata"] for v in r["vectors"]}


@pytest.fixture(autouse=True)
def sin_significado(monkeypatch):
    # Bedrock y QueryVectors no existen en moto: salvo en su test, la búsqueda por significado no encuentra nada
    monkeypatch.setattr(api, "por_significado", lambda usuario, texto: [])


def llamar(ruta, cuerpo=None, params=None, foto=None):
    r = api.lambda_handler({"routeKey": ruta, "body": json.dumps(cuerpo) if cuerpo is not None else None,
                            "queryStringParameters": params, "pathParameters": {"id": foto} if foto else None,
                            "requestContext": {"authorizer": {"jwt": {"claims": {"sub": USUARIO}}}}}, None)
    return r["statusCode"], json.loads(r["body"]) if r["body"] else None


@pytest.fixture
def traductor():
    with Stubber(guardar.bedrock) as s:
        yield s


def subir_y_analizar(traductor, etiquetas=PERRO, textos=None, moderacion=None, traduccion=None, imagen=None, ubicacion=None):
    pedido = {"nombre": "vacaciones.jpg", "tipo": "image/jpeg", **({"ubicacion": ubicacion} if ubicacion else {})}
    status, subida = llamar("POST /subidas", pedido)
    assert status == 201
    key = subida["campos"]["key"]
    boto3.client("s3").put_object(Bucket="revela-test", Key=key, Body=imagen or jpeg())

    traductor.add_response("converse", respuesta_nova(traduccion or nova(["Perro", "Playa"])))
    traductor.add_response("invoke_model", respuesta_vector())
    imagen = procesar.lambda_handler({"bucket": "revela-test", "key": key}, None)
    resultado = guardar.lambda_handler({"bucket": "revela-test", "key": imagen["revisada"], "etiquetas": etiquetas,
                                        "textos": textos, "moderacion": moderacion, "imagen": imagen}, None)
    return subida["foto"], resultado


def test_subida_prefirmada_restringe_tipo_y_tamano(aws):
    status, subida = llamar("POST /subidas", {"nombre": "a.png", "tipo": "image/png"})
    assert status == 201
    assert subida["campos"]["key"] == f"originales/{USUARIO}/{subida['foto']}.png"
    politica = json.loads(__import__("base64").b64decode(subida["campos"]["policy"]))
    assert ["content-length-range", 1, 15 * 1024 * 1024] in politica["conditions"]
    assert {"Content-Type": "image/png"} in politica["conditions"]

    assert llamar("POST /subidas", {"tipo": "image/gif"})[0] == 400
    assert llamar("POST /subidas", {"tipo": "image/webp"})[0] == 400

    # Recién subida aparece como PROCESANDO y sin URL
    _, galeria = llamar("GET /fotos")
    assert galeria["fotos"][0]["estado"] == "PROCESANDO" and "miniatura" not in galeria["fotos"][0]


def test_flujo_completo(aws, traductor):
    foto, resultado = subir_y_analizar(traductor, textos=["FELIZ NAVIDAD", "Café"], traduccion=nova(["PERRO", "pLaya"]))
    assert resultado == {"foto": foto, "estado": "LISTA"}

    status, det = llamar("GET /fotos/{id}", foto=foto)
    assert status == 200
    assert det["etiquetas"] == ["Perro", "Playa"]
    assert det["camara"] == "Google Pixel 8"
    assert det["fecha_toma"] == "2025-12-24T20:15:00" and det["fecha"] == "2025-12-24T20:15:00"
    assert det["gps"] == {"lat": 6.251, "lon": -75.567}
    assert (det["ancho"], det["alto"]) == (800, 1200)  # aplicó la orientación EXIF
    assert det["texto"] == "FELIZ NAVIDAD\nCafé"
    assert det["descripcion"] == "Un perro corre por la playa."
    assert det["original"].startswith("https://") and det["miniatura"].startswith("https://")

    miniatura = boto3.client("s3").get_object(Bucket="revela-test", Key=f"miniaturas/{USUARIO}/{foto}.jpg")["Body"].read()
    img = Image.open(io.BytesIO(miniatura))
    assert img.size == (320, 480) and not img.getexif()  # sin EXIF: no filtra el GPS

    # Se encuentra en español, en inglés, sin tildes y por el texto leído
    for q in ("perro", "DOG", "playa", "navidad", "cafe", "Café"):
        assert [f["id"] for f in llamar("GET /fotos", params={"q": q})[1]["fotos"]] == [foto], q
    assert llamar("GET /fotos", params={"q": "gato"})[1]["fotos"] == []

    assert llamar("GET /etiquetas")[1]["etiquetas"] == [{"etiqueta": "Perro", "fotos": 1}, {"etiqueta": "Playa", "fotos": 1}]
    assert vectores_de(foto) == {foto: {"usuario": USUARIO}}

    assert llamar("DELETE /fotos/{id}", foto=foto) == (204, None)
    assert vectores_de(foto) == {}
    tabla = boto3.resource("dynamodb").Table("revela-test")
    assert [i["SK"][:5] for i in tabla.scan()["Items"]] == ["CUPO#"]  # el cupo del día no se devuelve al borrar
    assert boto3.client("s3").list_objects_v2(Bucket="revela-test")["KeyCount"] == 0
    assert llamar("GET /fotos/{id}", foto=foto)[0] == 404


def test_sobrescribir_originales_no_cambia_lo_mostrado(aws, traductor):
    # Reusar la URL prefirmada después del análisis escribe en originales/, pero la app sirve fotos/
    foto, _ = subir_y_analizar(traductor)
    s3 = boto3.client("s3")
    assert s3.list_objects_v2(Bucket="revela-test", Prefix="originales/")["KeyCount"] == 0
    revisada = s3.get_object(Bucket="revela-test", Key=f"fotos/{USUARIO}/{foto}.jpg")["Body"].read()
    s3.put_object(Bucket="revela-test", Key=f"originales/{USUARIO}/{foto}.jpg", Body=b"otra cosa")

    url = llamar("GET /fotos/{id}", foto=foto)[1]["original"]
    assert f"/fotos/{USUARIO}/{foto}.jpg" in url
    assert s3.get_object(Bucket="revela-test", Key=f"fotos/{USUARIO}/{foto}.jpg")["Body"].read() == revisada

    llamar("DELETE /fotos/{id}", foto=foto)
    assert s3.list_objects_v2(Bucket="revela-test")["KeyCount"] == 0  # borra también lo sobrescrito


def test_tope_de_subidas_por_dia(aws, monkeypatch):
    global USUARIO
    monkeypatch.setattr(api, "SUBIDAS_DIA", 3)
    assert [llamar("POST /subidas", {"tipo": "image/jpeg"})[0] for _ in range(4)] == [201, 201, 201, 429]
    assert "3 fotos por día" in llamar("POST /subidas", {"tipo": "image/jpeg"})[1]["error"]
    assert llamar("POST /subidas", {"tipo": "image/gif"})[0] == 400  # lo inválido no gasta cupo

    # El contador no aparece como foto ni como etiqueta, y cada usuario tiene el suyo
    assert len(llamar("GET /fotos")[1]["fotos"]) == 3
    assert llamar("GET /etiquetas")[1]["etiquetas"] == []
    original, USUARIO = USUARIO, "2222-bbbb"
    try:
        assert llamar("POST /subidas", {"tipo": "image/jpeg"})[0] == 201
    finally:
        USUARIO = original


def test_cuarentena_no_indexa_ni_entrega_urls(aws, traductor):
    moderacion = [{"Name": "Graphic Violence", "ParentName": "Violence", "Confidence": 93.0},
                  {"Name": "Swimwear or Underwear", "ParentName": "", "Confidence": 99.0}]
    foto, resultado = subir_y_analizar(traductor, moderacion=moderacion)
    assert resultado["estado"] == "CUARENTENA"
    det = llamar("GET /fotos/{id}", foto=foto)[1]
    assert det["moderacion"] == ["Graphic Violence"]
    assert det["descripcion"] is None and det["etiquetas"] == ["Dog", "Beach"]  # a Nova no se le muestra
    assert "original" not in det and "miniatura" not in det
    assert llamar("GET /fotos", params={"q": "perro"})[1]["fotos"] == []
    assert vectores_de(foto) == {}


def test_traje_de_bano_o_confianza_baja_no_bloquean(aws, traductor):
    moderacion = [{"Name": "Swimwear or Underwear", "ParentName": "", "Confidence": 99.0},
                  {"Name": "Explicit", "ParentName": "", "Confidence": 55.0}]
    assert subir_y_analizar(traductor, moderacion=moderacion)[1]["estado"] == "LISTA"


def test_nova_recibe_la_miniatura_y_su_respuesta_se_limita(aws, traductor):
    pedidos = []
    guardar.bedrock.meta.events.register("before-parameter-build.bedrock-runtime.Converse",
                                         lambda params, **_: pedidos.append(params))
    foto, _ = subir_y_analizar(traductor, traduccion=nova(["Perro", "Playa"], descripcion="x" * 1000), textos=["HOLA"])
    contenido = pedidos[-1]["messages"][0]["content"]
    assert contenido[0]["image"]["format"] == "jpeg"
    assert Image.open(io.BytesIO(contenido[0]["image"]["source"]["bytes"])).size == (320, 480)  # la miniatura
    assert "Dog" in contenido[1]["text"] and "HOLA" in contenido[1]["text"]
    assert len(llamar("GET /fotos/{id}", foto=foto)[1]["descripcion"]) == guardar.MAX_DESCRIPCION


def test_un_solo_elemento_y_sin_etiquetas(aws, traductor):
    # JSONata entrega un escalar con un solo resultado y nada sin resultados
    foto, _ = subir_y_analizar(traductor, etiquetas={"Name": "Dog", "Confidence": 99.0}, textos="HOLA", traduccion=nova(["Perro"]))
    det = llamar("GET /fotos/{id}", foto=foto)[1]
    assert det["etiquetas"] == ["Perro"] and det["texto"] == "HOLA"

    foto, _ = subir_y_analizar(traductor, etiquetas=None, imagen=jpeg(gps=False))
    assert llamar("GET /fotos/{id}", foto=foto)[1]["gps"] is None


def test_nova_falla_quedan_en_ingles(aws, traductor):
    traductor.add_client_error("converse", "ThrottlingException")
    traductor.add_client_error("invoke_model", "ThrottlingException")
    status, subida = llamar("POST /subidas", {"tipo": "image/jpeg"})
    key = subida["campos"]["key"]
    boto3.client("s3").put_object(Bucket="revela-test", Key=key, Body=jpeg())
    imagen = procesar.lambda_handler({"bucket": "revela-test", "key": key}, None)
    guardar.lambda_handler({"bucket": "revela-test", "key": imagen["revisada"], "etiquetas": PERRO, "imagen": imagen}, None)
    assert llamar("GET /fotos/{id}", foto=subida["foto"])[1]["etiquetas"] == ["Dog", "Beach"]
    assert vectores_de(subida["foto"]) == {}  # sin vector, pero la foto quedó LISTA


def test_reprocesar_reemplaza_indices(aws, traductor):
    status, subida = llamar("POST /subidas", {"tipo": "image/jpeg"})
    key = subida["campos"]["key"]
    boto3.client("s3").put_object(Bucket="revela-test", Key=key, Body=jpeg())
    imagen = procesar.lambda_handler({"bucket": "revela-test", "key": key}, None)
    for etiquetas, traduccion in ((PERRO, nova(["Perro", "Playa"], cerca=True)), ([{"Name": "Cat", "Confidence": 90.0}], nova(["Gato"]))):
        traductor.add_response("converse", respuesta_nova(traduccion))
        traductor.add_response("invoke_model", respuesta_vector())
        guardar.lambda_handler({"bucket": "revela-test", "key": imagen["revisada"], "etiquetas": etiquetas, "imagen": imagen}, None)
    assert llamar("GET /fotos", params={"q": "perro"})[1]["fotos"] == []
    assert len(llamar("GET /fotos", params={"q": "gato"})[1]["fotos"]) == 1


def test_borrada_durante_el_analisis_no_resucita(aws):
    status, subida = llamar("POST /subidas", {"tipo": "image/jpeg"})
    key = subida["campos"]["key"]
    boto3.client("s3").put_object(Bucket="revela-test", Key=key, Body=jpeg())
    imagen = procesar.lambda_handler({"bucket": "revela-test", "key": key}, None)
    llamar("DELETE /fotos/{id}", foto=subida["foto"])
    assert guardar.lambda_handler({"bucket": "revela-test", "key": imagen["revisada"], "imagen": imagen}, None)["estado"] == "BORRADA"
    assert [i["SK"][:5] for i in boto3.resource("dynamodb").Table("revela-test").scan()["Items"]] == ["CUPO#"]
    assert boto3.client("s3").list_objects_v2(Bucket="revela-test")["KeyCount"] == 0


def test_paginacion_y_aislamiento_entre_usuarios(aws, monkeypatch):
    monkeypatch.setattr(api, "POR_PAGINA", 2)
    for _ in range(3):
        llamar("POST /subidas", {"tipo": "image/jpeg"})
    _, p1 = llamar("GET /fotos")
    _, p2 = llamar("GET /fotos", params={"siguiente": p1["siguiente"]})
    assert len(p1["fotos"]) == 2 and len(p2["fotos"]) == 1
    assert not {f["id"] for f in p1["fotos"]} & {f["id"] for f in p2["fotos"]}

    otro = p1["fotos"][0]["id"]
    global USUARIO
    original, USUARIO = USUARIO, "2222-bbbb"
    try:
        assert llamar("GET /fotos")[1]["fotos"] == []
        assert llamar("GET /fotos/{id}", foto=otro)[0] == 404
        assert llamar("DELETE /fotos/{id}", foto=otro)[0] == 404
        assert llamar("GET /fotos", params={"siguiente": p1["siguiente"]})[0] == 400  # cursor de otro usuario
    finally:
        USUARIO = original


@pytest.mark.parametrize("ruta,foto,cuerpo,status", [
    ("GET /fotos/{id}", "../../x", None, 404),
    ("GET /fotos/{id}", "a" * 32, None, 404),
    ("PUT /fotos", None, None, 404),
])
def test_entradas_invalidas(aws, ruta, foto, cuerpo, status):
    assert llamar(ruta, cuerpo, foto=foto)[0] == status
    assert llamar("GET /fotos", params={"siguiente": "no-es-base64!"})[0] == 400


def test_archivo_que_no_es_imagen_falla(aws):
    boto3.client("s3").put_object(Bucket="revela-test", Key=f"originales/{USUARIO}/x.jpg", Body=b"<script>")
    with pytest.raises(UnidentifiedImageError):
        procesar.lambda_handler({"bucket": "revela-test", "key": f"originales/{USUARIO}/x.jpg"}, None)


RIONEGRO = {"PricingBucket": "Advanced", "ResultItems": [{"PlaceId": "x", "PlaceType": "Street", "Title": "Calle 41",
                             "Address": {"Label": "Calle 41, Rionegro, Antioquia, Colombia", "Locality": "Rionegro",
                                         "Region": {}, "SubRegion": {"Code": "AN", "Name": "Antioquia"},  # así responde en Colombia
                                         "Country": {"Code2": "CO", "Code3": "COL", "Name": "Colombia"}}}]}


@pytest.fixture
def location(monkeypatch):
    cliente = boto3.client("geo-places")
    monkeypatch.setattr(guardar, "_lugares", cliente)
    with Stubber(cliente) as s:
        yield s


def test_lugar_por_gps_de_la_foto(aws, traductor, location):
    location.add_response("reverse_geocode", RIONEGRO, {"QueryPosition": [-75.567, 6.251], "MaxResults": 1,
                                                        "Language": "es", "IntendedUse": "Storage"})
    # El GPS de la foto manda sobre la ubicación del navegador
    foto, _ = subir_y_analizar(traductor, ubicacion={"lat": 4.6, "lon": -74.1})
    det = llamar("GET /fotos/{id}", foto=foto)[1]
    assert det["lugar"] == {"nombre": "Rionegro, Antioquia, Colombia", "ciudad": "Rionegro", "region": "Antioquia", "pais": "Colombia"}
    assert det["gps"] == {"lat": 6.251, "lon": -75.567} and det["gps_fuente"] == "foto"

    for q in ("rionegro", "Antioquia", "colombia"):
        assert [f["id"] for f in llamar("GET /fotos", params={"q": q})[1]["fotos"]] == [foto], q
    assert llamar("GET /etiquetas")[1]["etiquetas"] == [{"etiqueta": "Perro", "fotos": 1}, {"etiqueta": "Playa", "fotos": 1}]

    mapa = llamar("GET /lugares")[1]["fotos"]
    assert [(f["id"], f["lat"], f["lon"], f["lugar"], f["aproximada"]) for f in mapa] == [
        (foto, 6.251, -75.567, "Rionegro, Antioquia, Colombia", False)]
    assert mapa[0]["miniatura"].startswith("https://")


def test_ubicacion_del_navegador_si_la_foto_no_trae(aws, traductor, location):
    location.add_response("reverse_geocode", RIONEGRO)
    foto, _ = subir_y_analizar(traductor, imagen=jpeg(gps=False), ubicacion={"lat": 6.1446961, "lon": -75.3716})
    det = llamar("GET /fotos/{id}", foto=foto)[1]
    assert det["gps"] == {"lat": 6.144696, "lon": -75.3716} and det["gps_fuente"] == "subida"
    assert llamar("GET /lugares")[1]["fotos"][0]["aproximada"] is True


def test_sin_ubicacion_o_location_falla(aws, traductor, location):
    foto, _ = subir_y_analizar(traductor, imagen=jpeg(gps=False))
    assert llamar("GET /fotos/{id}", foto=foto)[1]["gps"] is None
    assert llamar("GET /lugares")[1]["fotos"] == []

    location.add_client_error("reverse_geocode", "ThrottlingException")
    foto, resultado = subir_y_analizar(traductor)
    det = llamar("GET /fotos/{id}", foto=foto)[1]
    assert resultado["estado"] == "LISTA" and det["lugar"] is None and det["gps"] == {"lat": 6.251, "lon": -75.567}


@pytest.mark.parametrize("ubicacion", [{"lat": "x", "lon": 1}, {"lat": 91, "lon": 0}, {"lat": 0, "lon": -181}, {"lat": 1}, "6,-75"])
def test_ubicacion_invalida(aws, ubicacion):
    assert llamar("POST /subidas", {"tipo": "image/jpeg", "ubicacion": ubicacion})[0] == 400


def test_subida_abandonada_se_oculta_y_analisis_trabado_es_error(aws, monkeypatch):
    _, abandonada = llamar("POST /subidas", {"tipo": "image/jpeg"})
    _, trabada = llamar("POST /subidas", {"tipo": "image/jpeg"})
    tabla = boto3.resource("dynamodb").Table("revela-test")
    tabla.update_item(Key={"PK": f"U#{USUARIO}", "SK": f"F#{trabada['foto']}"}, UpdateExpression="SET analisis = :a",
                      ExpressionAttributeValues={":a": "2026-01-01T00:00:00Z"})
    assert {f["estado"] for f in llamar("GET /fotos")[1]["fotos"]} == {"PROCESANDO"}

    monkeypatch.setattr(api, "ESPERA_ANALISIS", api.timedelta(seconds=-1))  # como si hubieran pasado 10 minutos
    assert [(f["id"], f["estado"]) for f in llamar("GET /fotos")[1]["fotos"]] == [(trabada["foto"], "ERROR")]
    assert llamar("GET /fotos/{id}", foto=abandonada["foto"])[1]["estado"] is None  # el detalle no se rompe
    assert llamar("DELETE /fotos/{id}", foto=abandonada["foto"])[0] == 204  # la página la borra si S3 falla


def test_busqueda_por_significado(aws, traductor, monkeypatch):
    playa, _ = subir_y_analizar(traductor)
    gato, _ = subir_y_analizar(traductor, etiquetas=[{"Name": "Cat", "Confidence": 90.0}], traduccion=nova(["Gato"]))
    oculta, _ = subir_y_analizar(traductor, moderacion=[{"Name": "Explicit", "ParentName": "", "Confidence": 99.0}])
    pedido = []
    guardar.bedrock.meta.events.register("before-parameter-build.bedrock-runtime.InvokeModel", lambda params, **_: pedido.append(params))
    traductor.add_response("invoke_model", respuesta_vector())
    subir_y_analizar(traductor)  # registra el pedido del vector de la miniatura
    cuerpo = json.loads(pedido[-1]["body"])["singleEmbeddingParams"]
    assert cuerpo["embeddingPurpose"] == "GENERIC_INDEX" and cuerpo["image"]["format"] == "jpeg"

    monkeypatch.undo()  # la búsqueda real, con Bedrock y QueryVectors simulados
    cliente = boto3.client("s3vectors")
    monkeypatch.setattr(api, "_vectores", cliente)
    with Stubber(api.bedrock) as br, Stubber(cliente) as sv:
        br.add_response("invoke_model", respuesta_vector())
        # S3 Vectors devuelve de la más cercana a la más lejana; el vector viejo de la oculta no debe salir
        sv.add_response("query_vectors", {"distanceMetric": "cosine", "vectors": [
            {"key": oculta, "distance": 0.2}, {"key": playa, "distance": 0.3}, {"key": "f" * 32, "distance": 0.4},
            {"key": gato, "distance": 0.5}, {"key": "a" * 32, "distance": 0.9}]},
            {"vectorBucketName": "revela-test", "indexName": "fotos", "queryVector": {"float32": [0.5] * 1024}, "topK": 30,
             "filter": {"usuario": USUARIO}, "returnDistance": True})
        fotos = llamar("GET /fotos", params={"q": "un gato tomando el sol"})[1]["fotos"]
        assert [(f["id"], f.get("parecida", False)) for f in fotos] == [(playa, True), (gato, True)]

        # La coincidencia exacta va primero y no se repite entre las parecidas
        br.add_response("invoke_model", respuesta_vector())
        sv.add_response("query_vectors", {"distanceMetric": "cosine", "vectors": [{"key": playa, "distance": 0.3}, {"key": gato, "distance": 0.5}]})
        fotos = llamar("GET /fotos", params={"q": "Gato"})[1]["fotos"]
        assert [(f["id"], f.get("parecida", False)) for f in fotos] == [(gato, False), (playa, True)]

        br.add_client_error("invoke_model", "ThrottlingException")
        assert [f["id"] for f in llamar("GET /fotos", params={"q": "gato"})[1]["fotos"]] == [gato]  # quedan las exactas


CARA = {"Width": 0.2, "Height": 0.3, "Left": 0.4, "Top": 0.1}


def caras_indexadas(*ids):
    return {"FaceRecords": [{"Face": {"FaceId": i, "BoundingBox": CARA, "ImageId": "x", "Confidence": 99.9}, "FaceDetail": {}} for i in ids],
            "FaceModelVersion": "7.0", "UnindexedFaces": []}


def asociada(cara):
    return {"AssociatedFaces": [{"FaceId": cara}], "UnsuccessfulFaceAssociations": [], "UserStatus": "UPDATING"}


@pytest.fixture
def caras():
    # guardar y la API comparten el cliente de caras.py: un solo Stubber, las respuestas van en el orden de las llamadas
    with Stubber(caras_mod.rekognition) as s:
        yield s, s


def test_personas_solo_con_consentimiento(aws, traductor, caras):
    g, a = caras
    coleccion = f"jyj-revela-caras-{USUARIO}"
    sin_permiso, _ = subir_y_analizar(traductor)  # el Stubber falla si guardar llama a Rekognition
    assert llamar("GET /personas")[1] == {"activo": False, "personas": []}

    a.add_response("create_collection", {"StatusCode": 200}, {"CollectionId": coleccion})
    assert llamar("PUT /personas")[1]["activo"] is True

    # Primera foto: dos caras desconocidas → dos personas nuevas
    g.add_response("index_faces", caras_indexadas("c1", "c2"))
    for cara in ("c1", "c2"):
        g.add_response("search_users", {"UserMatches": []}, {"CollectionId": coleccion, "FaceId": cara, "UserMatchThreshold": 90, "MaxUsers": 1})
        g.add_response("create_user", {})
        g.add_response("associate_faces", asociada(cara))
    grupo, _ = subir_y_analizar(traductor)
    juliana, otro = llamar("GET /fotos/{id}", foto=grupo)[1]["personas"]

    # Segunda foto: la misma cara de la primera persona
    g.add_response("index_faces", caras_indexadas("c3"))
    g.add_response("search_users", {"UserMatches": [{"Similarity": 98.0, "User": {"UserId": juliana, "UserStatus": "ACTIVE"}}]})
    g.add_response("associate_faces", asociada("c3"), {"CollectionId": coleccion, "UserId": juliana, "FaceIds": ["c3"], "UserMatchThreshold": 90})
    sola, _ = subir_y_analizar(traductor)
    g.assert_no_pending_responses()

    lista = llamar("GET /personas")[1]["personas"]
    assert [(p["id"], p["fotos"]) for p in lista] == [(juliana, 2), (otro, 1)]
    assert lista[0]["portada"]["caja"] == CARA
    assert any(f"miniaturas/{USUARIO}/{f}.jpg" in lista[0]["portada"]["miniatura"] for f in (grupo, sola))  # misma fecha de toma
    assert {f["id"] for f in llamar("GET /fotos", params={"persona": juliana})[1]["fotos"]} == {grupo, sola}
    assert llamar("GET /fotos", params={"persona": "../x"})[0] == 400

    # Con nombre se encuentra al buscarlo
    assert llamar("PUT /personas/{id}", {"nombre": "  Juliana   G. "}, foto=juliana)[1] == {"id": juliana, "nombre": "Juliana G.", "portada": None}
    assert {f["id"] for f in llamar("GET /fotos", params={"q": "juliana g."})[1]["fotos"]} == {grupo, sola}
    assert llamar("PUT /personas/{id}", {"nombre": "x" * 61}, foto=juliana)[0] == 400
    assert llamar("PUT /personas/{id}", {"nombre": "Nadie"}, foto="b" * 32)[0] == 404

    # Borrar una foto saca sus caras de la colección
    a.add_response("delete_faces", {"DeletedFaces": ["c3"]}, {"CollectionId": coleccion, "FaceIds": ["c3"]})
    llamar("DELETE /fotos/{id}", foto=sola)
    assert [(p["id"], p["fotos"], p["nombre"]) for p in llamar("GET /personas")[1]["personas"]] == [(juliana, 1, "Juliana G."), (otro, 1, None)]

    # Desactivar borra la colección y todo rastro en la tabla
    a.add_response("delete_collection", {"StatusCode": 200}, {"CollectionId": coleccion})
    assert llamar("DELETE /personas")[1] == {"activo": False, "personas": []}
    a.assert_no_pending_responses()
    tabla = boto3.resource("dynamodb").Table("revela-test")
    assert not [i["SK"] for i in tabla.scan()["Items"] if i["SK"].startswith(("P#", "PERSONA#", "PERFIL"))]
    det = llamar("GET /fotos/{id}", foto=grupo)[1]
    assert det["personas"] is None and llamar("GET /fotos", params={"q": "perro"})[1]["fotos"][0]["id"] in {grupo, sin_permiso}
    assert llamar("DELETE /fotos/{id}", foto=grupo)[0] == 204  # ya sin caras: no llama a Rekognition


def test_caras_no_quedan_huerfanas(aws, traductor, caras):
    g, a = caras
    coleccion = f"jyj-revela-caras-{USUARIO}"
    a.add_response("create_collection", {"StatusCode": 200})
    llamar("PUT /personas")
    # Falla al agrupar: se borran las caras recién indexadas antes de propagar el error
    g.add_response("index_faces", caras_indexadas("c1"))
    g.add_client_error("search_users", "ThrottlingException")
    g.add_response("delete_faces", {"DeletedFaces": ["c1"]}, {"CollectionId": coleccion, "FaceIds": ["c1"]})
    with pytest.raises(caras_mod.rekognition.exceptions.ClientError):
        subir_y_analizar(traductor)

    # Reproceso que ahora cae en cuarentena: las caras de la pasada anterior salen de la colección
    status, subida = llamar("POST /subidas", {"tipo": "image/jpeg"})
    boto3.client("s3").put_object(Bucket="revela-test", Key=subida["campos"]["key"], Body=jpeg())
    imagen = procesar.lambda_handler({"bucket": "revela-test", "key": subida["campos"]["key"]}, None)
    evento = {"bucket": "revela-test", "key": imagen["revisada"], "etiquetas": PERRO, "imagen": imagen}
    traductor.add_response("converse", respuesta_nova(nova(["Perro", "Playa"])))
    traductor.add_response("invoke_model", respuesta_vector())
    g.add_response("index_faces", caras_indexadas("c2"))
    g.add_response("search_users", {"UserMatches": []})
    g.add_response("create_user", {})
    g.add_response("associate_faces", asociada("c2"))
    guardar.lambda_handler(evento, None)
    g.add_response("delete_faces", {"DeletedFaces": ["c2"]}, {"CollectionId": coleccion, "FaceIds": ["c2"]})
    guardar.lambda_handler({**evento, "moderacion": [{"Name": "Explicit", "ParentName": "", "Confidence": 99.0}]}, None)
    g.assert_no_pending_responses()
    assert llamar("GET /fotos/{id}", foto=subida["foto"])[1]["personas"] == []


def test_portada_y_volver_a_buscar_caras(aws, traductor, caras, monkeypatch):
    s, _ = caras
    coleccion = f"jyj-revela-caras-{USUARIO}"
    antes, _ = subir_y_analizar(traductor)  # subida antes de activar: sin caras
    assert llamar("POST /fotos/{id}/caras", foto=antes)[0] == 409  # sin consentimiento no se busca

    s.add_response("create_collection", {"StatusCode": 200})
    llamar("PUT /personas")
    s.add_response("index_faces", caras_indexadas("c1"))
    s.add_response("search_users", {"UserMatches": []})
    s.add_response("create_user", {})
    s.add_response("associate_faces", asociada("c1"))
    fotos = [subir_y_analizar(traductor)[0]]
    persona = llamar("GET /personas")[1]["personas"][0]
    s.add_response("index_faces", caras_indexadas("c2"))
    s.add_response("search_users", {"UserMatches": [{"Similarity": 99.0, "User": {"UserId": persona["id"], "UserStatus": "ACTIVE"}}]})
    s.add_response("associate_faces", asociada("c2"))
    fotos.append(subir_y_analizar(traductor)[0])
    persona = llamar("GET /personas")[1]["personas"][0]

    # Elegir la portada entre sus fotos; una foto ajena no vale
    otra = fotos[0] if persona["portada"]["foto"] == fotos[1] else fotos[1]
    assert llamar("PUT /personas/{id}", {"portada": otra}, foto=persona["id"])[1]["portada"] == otra
    assert llamar("GET /personas")[1]["personas"][0]["portada"]["foto"] == otra
    assert llamar("PUT /personas/{id}", {"portada": antes}, foto=persona["id"])[0] == 400
    assert llamar("PUT /personas/{id}", {}, foto=persona["id"])[0] == 400
    assert llamar("PUT /personas/{id}", {"nombre": ""}, foto=persona["id"])[1] == {"id": persona["id"], "nombre": None, "portada": otra}

    # Volver a buscar en la foto de antes, con el filtro de calidad permisivo: queda en el grupo de la misma persona
    s.add_response("index_faces", caras_indexadas("c3"), {"CollectionId": coleccion, "ExternalImageId": antes, "MaxFaces": 10,
                                                          "QualityFilter": "LOW", "Image": {"S3Object": {"Bucket": "revela-test", "Name": f"publicas/{USUARIO}/{antes}.jpg"}}})
    s.add_response("search_users", {"UserMatches": [{"Similarity": 95.0, "User": {"UserId": persona["id"], "UserStatus": "ACTIVE"}}]})
    s.add_response("associate_faces", asociada("c3"))
    assert llamar("POST /fotos/{id}/caras", foto=antes)[1] == {"personas": [persona["id"]]}
    assert llamar("GET /personas")[1]["personas"][0]["fotos"] == 3

    # Otra vez sobre la misma foto: las caras de la pasada anterior se borran y el índice no se duplica
    s.add_response("index_faces", caras_indexadas())
    s.add_response("delete_faces", {"DeletedFaces": ["c3"]}, {"CollectionId": coleccion, "FaceIds": ["c3"]})
    assert llamar("POST /fotos/{id}/caras", foto=antes)[1] == {"personas": []}
    assert llamar("GET /personas")[1]["personas"][0]["fotos"] == 2
    assert {f["id"] for f in llamar("GET /fotos", params={"q": "perro"})[1]["fotos"]} == {antes, *fotos}  # las otras claves siguen
    s.assert_no_pending_responses()

    # Tope diario propio, aparte del de subidas
    monkeypatch.setattr(api, "BUSQUEDAS_CARAS_DIA", 2)
    status, r = llamar("POST /fotos/{id}/caras", foto=antes)
    assert status == 429 and "búsquedas de caras" in r["error"]
    assert llamar("POST /subidas", {"tipo": "image/jpeg"})[0] == 201


def test_unir_dos_personas(aws, traductor, caras):
    s, _ = caras
    coleccion = f"jyj-revela-caras-{USUARIO}"
    s.add_response("create_collection", {"StatusCode": 200})
    llamar("PUT /personas")
    ids = []
    for cara in ("c1", "c2"):  # la misma persona, pero Rekognition no las ve parecidas (una foto editada)
        s.add_response("index_faces", caras_indexadas(cara))
        s.add_response("search_users", {"UserMatches": []})
        s.add_response("create_user", {})
        s.add_response("associate_faces", asociada(cara))
        ids.append(subir_y_analizar(traductor)[0])
    lista = llamar("GET /personas")[1]["personas"]
    queda = next(p["id"] for p in lista if p["portada"]["foto"] == ids[0])
    sale = next(p["id"] for p in lista if p["id"] != queda)
    llamar("PUT /personas/{id}", {"nombre": "Juliana"}, foto=sale)

    assert llamar("POST /personas/{id}/unir", {"persona": queda}, foto=queda)[0] == 400
    assert llamar("POST /personas/{id}/unir", {"persona": "c" * 32}, foto=queda)[0] == 404

    s.add_response("list_faces", {"Faces": [{"FaceId": "c2"}]}, {"CollectionId": coleccion, "UserId": sale})
    s.add_response("disassociate_faces", {"DisassociatedFaces": [{"FaceId": "c2"}], "UnsuccessfulFaceDisassociations": []},
                   {"CollectionId": coleccion, "UserId": sale, "FaceIds": ["c2"]})
    s.add_response("associate_faces", asociada("c2"), {"CollectionId": coleccion, "UserId": queda, "FaceIds": ["c2"], "UserMatchThreshold": 0})
    s.add_response("delete_user", {}, {"CollectionId": coleccion, "UserId": sale})
    lista = llamar("POST /personas/{id}/unir", {"persona": sale}, foto=queda)[1]["personas"]
    s.assert_no_pending_responses()
    assert [(p["id"], p["fotos"], p["nombre"]) for p in lista] == [(queda, 2, "Juliana")]  # hereda el nombre
    assert llamar("GET /fotos/{id}", foto=ids[1])[1]["personas"] == [queda]
    assert {f["id"] for f in llamar("GET /fotos", params={"persona": queda})[1]["fotos"]} == set(ids)
    assert llamar("GET /fotos", params={"persona": sale})[1]["fotos"] == []

    # Borrar la foto que venía de la otra persona limpia el índice nuevo (las claves de la foto se actualizaron)
    s.add_response("delete_faces", {"DeletedFaces": ["c2"]})
    llamar("DELETE /fotos/{id}", foto=ids[1])
    assert [(p["id"], p["fotos"]) for p in llamar("GET /personas")[1]["personas"]] == [(queda, 1)]


@pytest.fixture
def llave_cdn(monkeypatch):
    import rsa
    publica, privada = rsa.newkeys(1024)  # en AWS es de 2048 (Terraform); aquí basta para comprobar la firma
    boto3.client("ssm").put_parameter(Name=api.LLAVE, Value=privada.save_pkcs1().decode(), Type="SecureString")
    monkeypatch.setattr(api, "CDN", "cdn.test")
    monkeypatch.setattr(api, "FIRMA_ID", "K123")
    monkeypatch.setattr(api, "_firmador", None)
    return publica


def publico(token):
    r = api.lambda_handler({"routeKey": "GET /compartidos/{token}", "pathParameters": {"token": token}, "requestContext": {}}, None)
    return r["statusCode"], json.loads(r["body"])


def test_albumes_y_enlace_compartido(aws, traductor, llave_cdn, monkeypatch):
    global USUARIO
    import rsa
    from urllib.parse import parse_qs, urlsplit
    from botocore.signers import CloudFrontSigner

    playa, _ = subir_y_analizar(traductor)
    oculta, _ = subir_y_analizar(traductor, moderacion=[{"Name": "Explicit", "ParentName": "", "Confidence": 99.0}])
    assert llamar("POST /albumes", {"nombre": "  "})[0] == 400
    status, album = llamar("POST /albumes", {"nombre": "Vacaciones  2026"})
    assert status == 201 and album["nombre"] == "Vacaciones 2026"
    aid = album["id"]

    # Solo entran fotos propias y listas; repetir no duplica
    assert llamar("PUT /albumes/{id}", {"agregar": [playa, oculta, "f" * 32, playa]}, foto=aid)[1]["fotos"] == 1
    assert llamar("PUT /albumes/{id}", {"agregar": ["../x"]}, foto=aid)[0] == 400
    det = llamar("GET /albumes/{id}", foto=aid)[1]
    assert [f["id"] for f in det["fotos"]] == [playa] and det["enlace"] is None
    lista = llamar("GET /albumes")[1]["albumes"]
    assert [(a["id"], a["fotos"]) for a in lista] == [(aid, 1)] and lista[0]["portada"].startswith("https://")

    assert llamar("POST /albumes/{id}/enlace", {"dias": 2}, foto=aid)[0] == 400
    enlace = llamar("POST /albumes/{id}/enlace", {"dias": 7}, foto=aid)[1]
    token = enlace["url"].split("#")[1]
    assert enlace["url"].startswith("https://cdn.test/album.html#") and len(token) == 32

    # Sin cuenta: nombre y fotos con URLs de CloudFront firmadas por una hora; nada de GPS, etiquetas ni personas
    status, compartido = publico(token)
    assert status == 200 and compartido["nombre"] == "Vacaciones 2026"
    foto = compartido["fotos"][0]
    assert set(foto) == {"miniatura", "original", "descripcion", "fecha"}
    url = urlsplit(foto["original"])
    # Nunca el archivo subido (fotos/ conserva el EXIF con el GPS): la copia re-codificada de publicas/, sin metadatos
    assert url.netloc == "cdn.test" and url.path == f"/publicas/{USUARIO}/{playa}.jpg"
    publica = Image.open(io.BytesIO(boto3.client("s3").get_object(Bucket="revela-test", Key=url.path[1:])["Body"].read()))
    assert not publica.getexif() and publica.size == (800, 1200)  # orientada y sin GPS ni cámara
    original = Image.open(io.BytesIO(boto3.client("s3").get_object(Bucket="revela-test", Key=f"fotos/{USUARIO}/{playa}.jpg")["Body"].read()))
    assert original.getexif().get_ifd(ExifTags.IFD.GPSInfo)  # el de la galería privada sí lo tiene
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert q["Key-Pair-Id"] == "K123"
    vence = api.datetime.fromtimestamp(int(q["Expires"]), api.timezone.utc)
    assert 3500 < (vence - api.datetime.now(api.timezone.utc)).total_seconds() <= 3600
    politica = CloudFrontSigner("K123", None).build_policy(f"https://cdn.test{url.path}", vence).encode()
    firma = __import__("base64").b64decode(q["Signature"].replace("-", "+").replace("_", "=").replace("~", "/"))
    assert rsa.verify(politica, firma, llave_cdn) == "SHA-1"

    # Otro usuario no ve ni toca el álbum
    original, USUARIO = USUARIO, "2222-bbbb"
    try:
        assert llamar("GET /albumes/{id}", foto=aid)[0] == 404
        assert llamar("POST /albumes/{id}/enlace", {"dias": 1}, foto=aid)[0] == 404
        assert llamar("GET /albumes")[1]["albumes"] == []
    finally:
        USUARIO = original

    # Un enlace nuevo reemplaza al anterior; revocar y borrar lo dejan en 404; tokens raros también
    nuevo = llamar("POST /albumes/{id}/enlace", {"dias": 1}, foto=aid)[1]["url"].split("#")[1]
    assert publico(token)[0] == 404 and publico(nuevo)[0] == 200
    assert llamar("DELETE /albumes/{id}/enlace", foto=aid)[0] == 204
    assert publico(nuevo)[0] == 404
    assert publico("x")[0] == 404 and publico("../" + "a" * 29)[0] == 404

    # Vencido aunque el TTL de DynamoDB todavía no lo haya borrado
    vencido = llamar("POST /albumes/{id}/enlace", {"dias": 1}, foto=aid)[1]["url"].split("#")[1]
    monkeypatch.setattr(api, "datetime", type("Despues", (api.datetime,), {"now": classmethod(lambda cls, tz=None: api.datetime.fromtimestamp(2e10, tz))}))
    assert publico(vencido)[0] == 404
    monkeypatch.undo()

    # Quitar fotos y borrar el álbum (las fotos se quedan)
    assert llamar("PUT /albumes/{id}", {"quitar": [playa], "nombre": "Playa"}, foto=aid)[1] == {"id": aid, "nombre": "Playa", "fotos": 0}
    vivo = llamar("POST /albumes/{id}/enlace", {"dias": 30}, foto=aid)[1]["url"].split("#")[1]
    assert llamar("DELETE /albumes/{id}", foto=aid)[0] == 204
    assert publico(vivo)[0] == 404 and llamar("GET /fotos/{id}", foto=playa)[0] == 200


def foto_lista(foto, fecha, ciudad=None, persona=None):
    """Una foto ya analizada con sus índices, escrita directo en la tabla (sin pasar por el flujo)."""
    pk, orden = f"U#{USUARIO}", f"{fecha}T10:00:00#{foto}"
    tabla = api.tabla
    tabla.put_item(Item={"PK": pk, "SK": f"F#{foto}", "estado": "LISTA", "fecha_orden": orden, "miniatura": f"miniaturas/{USUARIO}/{foto}.jpg",
                         "original": "x", "descripcion": "Una foto.", **({"lugar": {"nombre": f"{ciudad}, Colombia"}} if ciudad else {})})
    if ciudad:
        tabla.put_item(Item={"PK": pk, "SK": f"L#{api.normalizar(ciudad)}#{orden}", "foto": foto})
    if persona:
        tabla.put_item(Item={"PK": pk, "SK": f"P#{persona}#{orden}", "foto": foto, "persona": persona})


def test_asistente_combina_filtros(aws):
    import asistente
    juliana, otra = "a" * 32, "b" * 32
    api.tabla.put_item(Item={"PK": f"U#{USUARIO}", "SK": f"PERSONA#{juliana}", "nombre": "Juliana"})
    api.tabla.put_item(Item={"PK": f"U#{USUARIO}", "SK": f"PERSONA#{otra}"})  # sin nombre: no sale
    vieja, buena, otra_ciudad, sin_ella = "1" * 32, "2" * 32, "3" * 32, "4" * 32
    foto_lista(vieja, "2024-05-01", "Cartagena", juliana)
    foto_lista(buena, "2025-12-24", "Cartagena", juliana)
    foto_lista(otra_ciudad, "2025-06-01", "Medellín", juliana)
    foto_lista(sin_ella, "2025-03-01", "Cartagena")

    estado = {}
    personas, lugares, buscar_fotos, crear_album = asistente.herramientas(USUARIO, estado)
    assert json.loads(personas()) == ["Juliana"]  # "otra" no tiene nombre todavía
    assert lugares() == "Ninguna foto tiene ubicación."  # lee el gps, que estas fotos no tienen

    r = json.loads(buscar_fotos(personas=["juliana"], lugar="cartagena", desde="2025-01-01", hasta="2025-12-31"))
    assert r["encontradas"] == 1 and r["fotos"][0]["fecha"] == "2025-12-24" and estado["ultimas"] == [buena]
    vacia = json.loads(buscar_fotos(personas=["Juliana"], lugar="Medellín", desde="2024-01-01", hasta="2024-12-31"))
    assert vacia["encontradas"] == 0 and vacia["cada_filtro_por_separado"] == {"persona Juliana": 3, "lugar Medellín": 1}
    api.tabla.put_item(Item={"PK": f"U#{USUARIO}", "SK": f"PERSONA#{otra}", "nombre": "Sara"})
    foto_lista("5" * 32, "2025-07-01", "Cali", otra)
    juntas = json.loads(buscar_fotos(personas=["Juliana", "Sara"]))  # nunca salen juntas: muestra las de cualquiera
    assert juntas["juntas"] == 0 and juntas["encontradas"] == 4 and len(estado["ultimas"]) == 4
    assert json.loads(buscar_fotos(personas=["Juliana", "Sara"], alguna=True))["encontradas"] == 4
    assert estado["ultimas"] == [buena, "5" * 32, otra_ciudad, vieja]  # cualquiera de las dos, por fecha
    assert json.loads(buscar_fotos(lugar="Cartagena"))["encontradas"] == 3
    assert estado["ultimas"] == [buena, sin_ella, vieja]  # de la más nueva a la más vieja
    assert json.loads(buscar_fotos(desde="2025-01-01"))["encontradas"] == 4  # solo fechas: por el índice
    assert "Pedro" in buscar_fotos(personas=["Pedro"]) and estado["ultimas"] == []
    with pytest.raises(ValueError):
        buscar_fotos(desde="24/12/2025")
    with pytest.raises(ValueError):
        crear_album(nombre="Nada")  # sin búsqueda previa no hay fotos

    buscar_fotos(personas=["Juliana"], lugar="Cartagena")
    assert "2 fotos" in crear_album(nombre="Cartagena con Juli")
    assert "Ya existe el álbum «Cartagena con Juli»" in crear_album(nombre="Otra vez")
    assert len(llamar("GET /albumes")[1]["albumes"]) == 1
    album = llamar("GET /albumes/{id}", foto=estado["album"]["id"])[1]
    assert album["nombre"] == "Cartagena con Juli" and {f["id"] for f in album["fotos"]} == {vieja, buena}


def test_asistente_handler(aws, monkeypatch):
    import asistente

    class AgenteFalso:
        def __init__(self, tools, messages, **_):
            self.buscar, self.album = tools[2], tools[3]
            AgenteFalso.mensajes = messages

        def __call__(self, pregunta):
            self.buscar(lugar="Cartagena")
            return "Encontré una foto en Cartagena."

    monkeypatch.setattr(asistente, "Agent", AgenteFalso)
    monkeypatch.setattr(asistente, "BedrockModel", lambda **_: None)
    foto_lista("1" * 32, "2025-12-24", "Cartagena")

    def preguntar(cuerpo):
        r = asistente.lambda_handler({"body": json.dumps(cuerpo), "requestContext": {"authorizer": {"jwt": {"claims": {"sub": USUARIO}}}}}, None)
        return r["statusCode"], json.loads(r["body"])

    assert preguntar({"pregunta": "   "})[0] == 400
    assert preguntar({"pregunta": "x" * 301})[0] == 400
    status, r = preguntar({"pregunta": "¿fotos en Cartagena?"})
    assert status == 200 and r["respuesta"] == "Encontré una foto en Cartagena."
    assert [f["id"] for f in r["fotos"]] == ["1" * 32] and r["fotos"][0]["miniatura"].startswith("https://") and "album" not in r

    # Segundo turno: llega lo hablado y las fotos que se ven, y "haz un álbum con esas" las usa sin buscar de nuevo
    monkeypatch.setattr(AgenteFalso, "__call__", lambda self, p: self.album(nombre="Cartagena"))
    turno = {"pregunta": "¿fotos en Cartagena?", "respuesta": r["respuesta"]}
    status, r2 = preguntar({"pregunta": "sí, haz un álbum con esas", "historial": [turno], "fotos": ["1" * 32]})
    assert status == 200 and r2["album"]["nombre"] == "Cartagena" and [f["id"] for f in r2["fotos"]] == ["1" * 32]
    assert AgenteFalso.mensajes == [{"role": "user", "content": [{"text": "¿fotos en Cartagena?"}]},
                                    {"role": "assistant", "content": [{"text": r["respuesta"]}]}]
    assert preguntar({"pregunta": "sí", "historial": [{"pregunta": "x"}]})[0] == 400
    assert preguntar({"pregunta": "sí", "historial": [turno] * 4})[0] == 400
    assert preguntar({"pregunta": "sí", "fotos": ["../x"]})[0] == 400

    monkeypatch.setattr(AgenteFalso, "__call__", lambda self, p: 1 / 0)
    assert preguntar({"pregunta": "hola"})[0] == 502  # Bedrock caído: error claro, sin traza al usuario
    for _ in range(asistente.PREGUNTAS_DIA - 3):
        preguntar({"pregunta": "hola"})
    assert preguntar({"pregunta": "hola"})[0] == 429


def test_recuerdos(aws, llave_cdn, monkeypatch):
    import recuerdos
    monkeypatch.setattr(recuerdos, "REMITENTE", "revela@correo.test")
    monkeypatch.setattr(recuerdos, "POOL", "us-east-1_test")
    assert llamar("GET /recuerdos")[1]["activo"] is False
    assert llamar("PUT /recuerdos")[1] == {"activo": True}
    api.tabla.put_item(Item={"PK": "RECUERDOS", "SK": "otro-usuario"})  # suscrito sin fotos: no recibe nada

    hace_un_anio, hace_tres, otro_dia, este_anio = "1" * 32, "2" * 32, "3" * 32, "4" * 32
    foto_lista(hace_un_anio, "2025-12-05")
    foto_lista(hace_tres, "2023-12-05")
    foto_lista(otro_dia, "2025-12-06")
    foto_lista(este_anio, "2026-12-05")

    # En la web: los recuerdos del día (o de otra fecha, para la demo)
    web = llamar("GET /recuerdos", params={"fecha": "2026-12-05"})[1]
    assert web["activo"] and web["recuerdos"][0]["titulo"] == "Hace 1 año y 3 años"
    assert web["recuerdos"][0]["subtitulo"] == "2 fotos del 5 de diciembre"
    assert [f["id"] for f in web["recuerdos"][0]["fotos"]] == [hace_un_anio, hace_tres]
    assert llamar("GET /recuerdos", params={"fecha": "2026-03-01"})[1]["recuerdos"] == []  # pocas fotos para otras colecciones
    assert llamar("GET /recuerdos", params={"fecha": "ayer"})[0] == 400

    def correo(usuario):
        assert usuario == USUARIO  # a "otro-usuario" no se le busca el correo porque no tiene recuerdos
        return "persona@correo.test"
    monkeypatch.setattr(recuerdos, "correo_de", correo)
    enviados = []
    monkeypatch.setattr(recuerdos.ses, "send_email", lambda **k: enviados.append(k))

    llamar("PUT /albumes/{id}", {"agregar": [hace_un_anio, hace_tres]}, foto=llamar("POST /albumes", {"nombre": "Diciembre"})[1]["id"])
    r = recuerdos.lambda_handler({"fecha": "2026-12-05"}, None)  # otro álbum con las mismas fotos no cuenta como el de recuerdos
    assert r == {"fecha": "2026-12-05", "usuarios": 2, "albumes": 1, "correos": 1}
    album = next(a for a in llamar("GET /albumes")[1]["albumes"] if a["nombre"] != "Diciembre")
    assert album["nombre"] == "Recuerdos del 5 de diciembre" and album["fotos"] == 2
    asunto = enviados[0]["Message"]["Subject"]["Data"]
    assert asunto == "Hace 1 año y 3 años: 2 fotos del 5 de diciembre" and enviados[0]["Destination"]["ToAddresses"] == ["persona@correo.test"]
    cuerpo = enviados[0]["Message"]["Body"]["Html"]["Data"]
    assert cuerpo.count("https://cdn.test/miniaturas/") == 2 and "https://cdn.test/?recuerdos=2026-12-05" in cuerpo

    # Si vuelve a correr el mismo día no repite el álbum (el correo sí sale)
    assert recuerdos.lambda_handler({"fecha": "2026-12-05"}, None)["albumes"] == 0
    assert len(llamar("GET /albumes")[1]["albumes"]) == 2
    assert recuerdos.lambda_handler({"fecha": "2026-03-01"}, None)["correos"] == 0  # día sin recuerdos

    assert llamar("DELETE /recuerdos")[1] == {"activo": False}
    assert recuerdos.lambda_handler({"fecha": "2026-12-05"}, None)["usuarios"] == 1


def test_recuerdos_correo_de_cognito(monkeypatch):
    import recuerdos
    monkeypatch.setattr(recuerdos, "POOL", "us-east-1_test")
    with Stubber(recuerdos.cognito) as c:
        pedido = {"UserPoolId": "us-east-1_test", "Filter": 'sub = "abc"', "Limit": 1}
        c.add_response("list_users", {"Users": [{"Attributes": [{"Name": "email", "Value": "a@b.co"}, {"Name": "email_verified", "Value": "true"}]}]}, pedido)
        c.add_response("list_users", {"Users": [{"Attributes": [{"Name": "email", "Value": "a@b.co"}, {"Name": "email_verified", "Value": "false"}]}]}, pedido)
        assert recuerdos.correo_de("abc") == "a@b.co"
        assert recuerdos.correo_de("abc") is None  # sin verificar no se le escribe


def test_colecciones_de_recuerdos(aws):
    from datetime import datetime
    juli = "a" * 32
    api.tabla.put_item(Item={"PK": f"U#{USUARIO}", "SK": f"PERSONA#{juli}", "nombre": "Juliana"})
    fotos = {}
    for n, (fecha, ciudad) in enumerate([("2026-11-14", "Rionegro")] * 3 + [("2026-11-12", "Rionegro")] * 2 + [("2026-03-08", "Cali")] * 3):
        fotos[n] = f"{n:032x}"
        foto_lista(fotos[n], fecha, ciudad, juli if n < 4 else None)
        api.tabla.update_item(Key={"PK": f"U#{USUARIO}", "SK": f"F#{fotos[n]}"}, UpdateExpression="SET etiquetas = :e, personas = :p",
                              ExpressionAttributeValues={":e": ["Autofoto", "Persona"] if n % 2 else ["Persona"], ":p": [juli] if n < 4 else []})
    foto_lista("f" * 32, "2026-11-20", "Rionegro")  # del "futuro" respecto a la fecha simulada: no cuenta

    hoy = datetime(2026, 11, 16)
    lista = {c["id"]: c for c in api.colecciones(USUARIO, hoy)}
    assert set(lista) == {"reciente", "dia", "persona", "lugar", "tema"}  # sin fotos de años anteriores no hay aniversario
    assert (lista["reciente"]["titulo"], lista["reciente"]["subtitulo"], len(lista["reciente"]["fotos"])) == ("Rememorando el sábado", "14 de noviembre", 3)
    assert lista["dia"]["subtitulo"] == "8 de marzo de 2026" and len(lista["dia"]["fotos"]) == 3
    assert (lista["persona"]["titulo"], lista["persona"]["subtitulo"]) == ("Con Juliana", "4 fotos")
    assert lista["lugar"]["titulo"] in ("En Rionegro", "En Cali") and lista["tema"]["titulo"] == "Selfies"
    assert api.colecciones(USUARIO, hoy) == api.colecciones(USUARIO, hoy)  # el mismo día, las mismas
    assert "aniversario" in {c["id"] for c in api.colecciones(USUARIO, datetime(2027, 3, 8))}


def test_importar_de_google_fotos(aws, monkeypatch):
    import urllib.error
    pedidos = []

    class Respuesta(io.BytesIO):
        def __init__(self, datos, tipo):
            super().__init__(datos)
            self.headers = __import__("email.message", fromlist=["Message"]).Message()
            self.headers["Content-Type"] = tipo

        def __exit__(self, *_):
            return False

    def abrir(pedido, timeout):
        pedidos.append(pedido)
        if "/redirige" in pedido.full_url:
            raise urllib.error.HTTPError(pedido.full_url, 302, "Found", {}, None)
        return Respuesta(jpeg(gps=False), "image/heif" if "/heic" in pedido.full_url else "image/jpeg")
    monkeypatch.setattr(api._descargador, "open", abrir)

    base = "https://lh3.googleusercontent.com/ppa/AbC-123_x"
    status, r = llamar("POST /importar", {"token": "ya29.token", "url": base, "nombre": "PXL_2024.jpg"})
    assert status == 201
    assert pedidos[0].full_url == base + "=d" and pedidos[0].get_header("Authorization") == "Bearer ya29.token"
    item = api.tabla.get_item(Key={"PK": f"U#{USUARIO}", "SK": f"F#{r['foto']}"})["Item"]
    assert item["estado"] == "PROCESANDO" and item["nombre"] == "PXL_2024.jpg" and "gps_subida" not in item
    assert boto3.client("s3").head_object(Bucket="revela-test", Key=item["original"])["ContentType"] == "image/jpeg"

    # El token de Google nunca va a otro host, ni siguiendo una redirección
    for url in ("https://evil.test/x", "https://lh3.googleusercontent.com.evil.test/x", base + "?x=1", None):
        assert llamar("POST /importar", {"token": "t", "url": url})[0] == 400
    assert llamar("POST /importar", {"url": base})[0] == 400
    assert llamar("POST /importar", {"token": "t", "url": "https://lh3.googleusercontent.com/redirige"})[0] == 400
    assert llamar("POST /importar", {"token": "t", "url": "https://lh3.googleusercontent.com/heic"})[0] == 415
    assert len(pedidos) == 3  # las URLs inválidas ni se intentan
