"""Recorre el flujo completo en moto: subida → procesar → guardar → API.
Rekognition no se simula: se pasan respuestas con la forma que entrega Step Functions."""
import io
import json

import boto3
import pytest
from botocore.stub import Stubber
from PIL import ExifTags, Image, UnidentifiedImageError

import api
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

    assert llamar("DELETE /fotos/{id}", foto=foto) == (204, None)
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
    status, subida = llamar("POST /subidas", {"tipo": "image/jpeg"})
    key = subida["campos"]["key"]
    boto3.client("s3").put_object(Bucket="revela-test", Key=key, Body=jpeg())
    imagen = procesar.lambda_handler({"bucket": "revela-test", "key": key}, None)
    guardar.lambda_handler({"bucket": "revela-test", "key": imagen["revisada"], "etiquetas": PERRO, "imagen": imagen}, None)
    assert llamar("GET /fotos/{id}", foto=subida["foto"])[1]["etiquetas"] == ["Dog", "Beach"]


def test_reprocesar_reemplaza_indices(aws, traductor):
    status, subida = llamar("POST /subidas", {"tipo": "image/jpeg"})
    key = subida["campos"]["key"]
    boto3.client("s3").put_object(Bucket="revela-test", Key=key, Body=jpeg())
    imagen = procesar.lambda_handler({"bucket": "revela-test", "key": key}, None)
    for etiquetas, traduccion in ((PERRO, nova(["Perro", "Playa"], cerca=True)), ([{"Name": "Cat", "Confidence": 90.0}], nova(["Gato"]))):
        traductor.add_response("converse", respuesta_nova(traduccion))
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
