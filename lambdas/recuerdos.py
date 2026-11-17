"""Recuerdos: EventBridge Scheduler la llama cada día a las 7:00 (Colombia). Por cada usuario suscrito busca fotos
tomadas este mismo día en años anteriores, arma un álbum con ellas y le manda un correo con SES.

Evento opcional {"fecha": "AAAA-MM-DD"} para probar otro día (aws lambda invoke).
SES está en sandbox: solo llegan los correos verificados; a los demás se les arma el álbum igual.
"""
import html
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

import api

log = logging.getLogger()
log.setLevel(logging.INFO)

REMITENTE = os.environ.get("REMITENTE", "")
POOL = os.environ.get("POOL", "")
EN_CORREO = 6  # miniaturas en el correo; el resto está en el álbum
VIGENCIA_CORREO = timedelta(days=7)  # las miniaturas del correo dejan de cargar a la semana
ses = boto3.client("ses")
cognito = boto3.client("cognito-idp")


def correo_de(usuario):
    r = cognito.list_users(UserPoolId=POOL, Filter=f'sub = "{usuario}"', Limit=1)  # el sub viene de la tabla, no del usuario
    atributos = {a["Name"]: a["Value"] for u in r["Users"] for a in u["Attributes"]}
    return atributos.get("email") if atributos.get("email_verified") == "true" else None


def enviar(destino, hoy, fotos, album):
    titulo = api.titulo_recuerdos(hoy, fotos)
    vence = datetime.now(timezone.utc) + VIGENCIA_CORREO
    miniaturas = "".join(f'<img src="{html.escape(api.firmar_cdn(f["miniatura"], vence))}" alt="" width="180" '
                         f'style="border-radius:8px;margin:4px">' for f in fotos[:EN_CORREO])
    enlace = f"https://{api.CDN}/?recuerdos={hoy:%Y-%m-%d}"  # abre estos recuerdos en la web
    cuerpo = (f'<div style="font-family:sans-serif;background:#141211;color:#eee;padding:24px">'
              f'<h1 style="font-family:serif">{html.escape(titulo)}</h1><div>{miniaturas}</div>'
              f'<p>Las guardamos en tu álbum «{html.escape(album)}».</p>'
              f'<p><a href="{enlace}" style="color:#f08a5d">Ver los recuerdos en Revela</a></p>'
              f'<p style="color:#999;font-size:12px">Recibes esto porque activaste los recuerdos; puedes apagarlos en '
              f'los ajustes de Revela.</p></div>')
    texto = f"{titulo}\nLas guardamos en tu álbum «{album}»: {enlace}"
    ses.send_email(Source=f"Revela <{REMITENTE}>", Destination={"ToAddresses": [destino]},
                   Message={"Subject": {"Data": titulo, "Charset": "UTF-8"},
                            "Body": {"Html": {"Data": cuerpo, "Charset": "UTF-8"}, "Text": {"Data": texto, "Charset": "UTF-8"}}})


def lambda_handler(event, _context):
    hoy = datetime.strptime(event["fecha"], "%Y-%m-%d") if (event or {}).get("fecha") else datetime.now(api.COLOMBIA)
    resumen = {"fecha": f"{hoy:%Y-%m-%d}", "usuarios": 0, "albumes": 0, "correos": 0}
    for suscrito in api.consultar("RECUERDOS", ""):
        usuario = suscrito["SK"]
        resumen["usuarios"] += 1
        fotos = api.del_dia(usuario, hoy)
        if not fotos:
            continue
        ids = [f["SK"][2:] for f in fotos][:api.MAX_ALBUM]
        nombre = f"Recuerdos del {hoy.day} de {api.MESES[hoy.month - 1]}"
        # Si ya corrió hoy no lo repite; con el nombre, porque otro álbum del usuario puede tener las mismas fotos
        if not api.album_igual(usuario, ids, nombre):
            _, nuevo = api.crear_album(usuario, {"nombre": nombre})
            api.editar_album(usuario, nuevo["id"], {"agregar": ids})
            resumen["albumes"] += 1
        try:
            destino = correo_de(usuario)
            if destino:
                enviar(destino, hoy, fotos, nombre)
                resumen["correos"] += 1
        except ClientError as e:  # sandbox: destinatario sin verificar (MessageRejected o AccessDenied); sigue con los demás
            log.warning("No se pudo enviar un recuerdo (%s); en sandbox el destinatario debe estar verificado", e.response["Error"]["Code"])  # el mensaje trae el correo
    log.info("Recuerdos: %s", resumen)
    return resumen
