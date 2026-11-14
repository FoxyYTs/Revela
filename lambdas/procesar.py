"""Paso del flujo de Step Functions: miniatura + metadatos EXIF de una foto.

Entrada: {"bucket": ..., "key": "originales/<usuario>/<foto>.<ext>"}
Salida:  {"ancho", "alto", "fecha_toma", "camara", "gps", "miniatura"}
"""
import io
import logging
from datetime import datetime

import boto3
from PIL import ExifTags, Image, ImageOps

log = logging.getLogger()
log.setLevel(logging.INFO)

s3 = boto3.client("s3")

LADO_MINIATURA = 480
Image.MAX_IMAGE_PIXELS = 60_000_000  # ~60 MP; más grande se trata como imagen maliciosa (bomba de descompresión)


def _grados(valor, ref):
    g, m, s = (float(x) for x in valor)
    decimal = g + m / 60 + s / 3600
    return -decimal if ref in ("S", "W") else decimal


def leer_exif(img):
    exif = img.getexif()
    datos = exif.get_ifd(ExifTags.IFD.Exif)

    fecha = None
    crudo = datos.get(ExifTags.Base.DateTimeOriginal) or exif.get(ExifTags.Base.DateTime)
    if crudo:
        try:
            fecha = datetime.strptime(str(crudo).strip("\x00 "), "%Y:%m:%d %H:%M:%S").isoformat()
        except ValueError:
            log.info("Fecha EXIF ilegible: %r", crudo)

    camara = " ".join(str(exif.get(t, "")).strip("\x00 ") for t in (ExifTags.Base.Make, ExifTags.Base.Model)).strip() or None

    gps = None
    info = exif.get_ifd(ExifTags.IFD.GPSInfo)
    try:
        lat = _grados(info[ExifTags.GPS.GPSLatitude], info.get(ExifTags.GPS.GPSLatitudeRef))
        lon = _grados(info[ExifTags.GPS.GPSLongitude], info.get(ExifTags.GPS.GPSLongitudeRef))
        if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0, 0):
            gps = {"lat": round(lat, 6), "lon": round(lon, 6)}
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        pass

    return fecha, camara, gps


def lambda_handler(event, _context):
    bucket, key = event["bucket"], event["key"]
    original = s3.get_object(Bucket=bucket, Key=key)["Body"].read()

    img = Image.open(io.BytesIO(original))
    fecha, camara, gps = leer_exif(img)

    # Respeta la orientación de la cámara; el EXIF se descarta en la miniatura (no expone el GPS)
    img = ImageOps.exif_transpose(img)
    ancho, alto = img.size
    img.thumbnail((LADO_MINIATURA, LADO_MINIATURA))
    salida = io.BytesIO()
    img.convert("RGB").save(salida, "JPEG", quality=82, optimize=True)

    miniatura = "miniaturas/" + key.split("/", 1)[1].rsplit(".", 1)[0] + ".jpg"
    s3.put_object(Bucket=bucket, Key=miniatura, Body=salida.getvalue(), ContentType="image/jpeg")

    return {"ancho": ancho, "alto": alto, "fecha_toma": fecha, "camara": camara, "gps": gps, "miniatura": miniatura}
