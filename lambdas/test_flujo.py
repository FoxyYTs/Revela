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
                                                          "QualityFilter": "LOW", "Image": {"S3Object": {"Bucket": "revela-test", "Name": f"fotos/{USUARIO}/{antes}.jpg"}}})
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
