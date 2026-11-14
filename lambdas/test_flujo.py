"""Recorre el flujo completo en moto: subida → procesar → guardar → API.
Rekognition no se simula: se pasan respuestas con la forma que entrega Step Functions."""
import io
import json

import boto3
import pytest
from botocore.stub import Stubber
from PIL import ExifTags, Image

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


def subir_y_analizar(traductor, etiquetas=PERRO, textos=None, moderacion=None, traduccion='["Perro", "Playa"]', imagen=None):
    status, subida = llamar("POST /subidas", {"nombre": "vacaciones.jpg", "tipo": "image/jpeg"})
    assert status == 201
    key = subida["campos"]["key"]
    boto3.client("s3").put_object(Bucket="revela-test", Key=key, Body=imagen or jpeg())

    if etiquetas:
        traductor.add_response("converse", respuesta_nova(traduccion))
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
    foto, resultado = subir_y_analizar(traductor, textos=["FELIZ NAVIDAD", "Café"], traduccion='["PERRO", "pLaya"]')
    assert resultado == {"foto": foto, "estado": "LISTA"}

    status, det = llamar("GET /fotos/{id}", foto=foto)
    assert status == 200
    assert det["etiquetas"] == ["Perro", "Playa"]
    assert det["camara"] == "Google Pixel 8"
    assert det["fecha_toma"] == "2025-12-24T20:15:00" and det["fecha"] == "2025-12-24T20:15:00"
    assert det["gps"] == {"lat": 6.251, "lon": -75.567}
    assert (det["ancho"], det["alto"]) == (800, 1200)  # aplicó la orientación EXIF
    assert det["texto"] == "FELIZ NAVIDAD\nCafé"
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
    assert tabla.scan()["Items"] == []
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


def test_cuarentena_no_indexa_ni_entrega_urls(aws, traductor):
    moderacion = [{"Name": "Graphic Violence", "ParentName": "Violence", "Confidence": 93.0},
                  {"Name": "Swimwear or Underwear", "ParentName": "", "Confidence": 99.0}]
    foto, resultado = subir_y_analizar(traductor, moderacion=moderacion)
    assert resultado["estado"] == "CUARENTENA"
    det = llamar("GET /fotos/{id}", foto=foto)[1]
    assert det["moderacion"] == ["Graphic Violence"]
    assert "original" not in det and "miniatura" not in det
    assert llamar("GET /fotos", params={"q": "perro"})[1]["fotos"] == []


def test_traje_de_bano_o_confianza_baja_no_bloquean(aws, traductor):
    moderacion = [{"Name": "Swimwear or Underwear", "ParentName": "", "Confidence": 99.0},
                  {"Name": "Explicit", "ParentName": "", "Confidence": 55.0}]
    assert subir_y_analizar(traductor, moderacion=moderacion)[1]["estado"] == "LISTA"


def test_un_solo_elemento_y_sin_etiquetas(aws, traductor):
    # JSONata entrega un escalar con un solo resultado y nada sin resultados
    foto, _ = subir_y_analizar(traductor, etiquetas={"Name": "Dog", "Confidence": 99.0}, textos="HOLA", traduccion='["Perro"]')
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
    for etiquetas, traduccion in ((PERRO, '```json\n["Perro", "Playa"]\n```'), ([{"Name": "Cat", "Confidence": 90.0}], '["Gato"]')):
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
    assert boto3.resource("dynamodb").Table("revela-test").scan()["Items"] == []
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
    with pytest.raises(Exception):
        procesar.lambda_handler({"bucket": "revela-test", "key": f"originales/{USUARIO}/x.jpg"}, None)
