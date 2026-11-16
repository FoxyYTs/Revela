"""Asistente de Revela: un agente de Strands con Amazon Nova que responde pedidos en lenguaje natural
("las fotos con Juliana en Cartagena del año pasado") combinando las búsquedas de la API.

POST /asistente {pregunta} → {respuesta, fotos, album?}

Las herramientas se crean en cada petición encerrando al usuario: el modelo nunca ve ni elige de quién son las fotos,
y tampoco maneja ids (la última búsqueda queda en `ultimas` y de ahí salen las fotos y el álbum).
"""
import base64
import json
import logging
import os
from datetime import datetime

from boto3.dynamodb.conditions import Key
from strands import Agent, tool
from strands.models import BedrockModel

import api
from api import ErrorCliente, normalizar, responder

log = logging.getLogger()
log.setLevel(logging.INFO)

MODELO = os.environ.get("MODELO", "us.amazon.nova-2-lite-v1:0")
PREGUNTAS_DIA = 30  # cada pregunta son 2-4 llamadas a Nova
MAX_PREGUNTA = 300
MAX_FOTOS = 200  # lo que cabe en un álbum
AL_MODELO = 15  # resúmenes que ve el modelo para poder comentar las fotos; el resto solo se cuenta

INSTRUCCION = """Eres el asistente de Revela, una galería de fotos. Hoy es {hoy} (hora de Colombia).
Ayudas a encontrar fotos del usuario con tus herramientas:
- buscar_fotos combina filtros: texto (lo que se ve o se lee en la foto), personas (nombres), lugar (ciudad,
  departamento o país) y un rango de fechas AAAA-MM-DD. Convierte fechas relativas ("el año pasado", "en diciembre")
  en ese rango. Pon cada parte del pedido en su filtro, no todo en texto.
- Si un nombre no coincide con nadie, usa personas() para ver los nombres que existen.
- lugares() lista las ciudades donde hay fotos.
- crear_album guarda en un álbum las fotos de la última búsqueda; úsalo solo si el usuario pide un álbum.
Las fotos que encuentres se le muestran al usuario debajo de tu respuesta: no las enumeres ni inventes fotos.
Responde en español, en una a tres frases, diciendo qué encontraste o por qué no hubo resultados."""


def herramientas(usuario, estado):
    pk = f"U#{usuario}"

    def ids_de(prefijo):
        return [i["foto"] for i in api.consultar(pk, prefijo, ScanIndexForward=False, ProjectionExpression="foto")]

    @tool
    def personas() -> str:
        """Nombres de las personas reconocidas en las fotos del usuario."""
        nombres = sorted({i["nombre"] for i in api.consultar(pk, "PERSONA#") if i.get("nombre")})
        return json.dumps(nombres, ensure_ascii=False) if nombres else "No hay personas con nombre."

    @tool
    def lugares() -> str:
        """Ciudades donde el usuario tiene fotos y cuántas hay en cada una."""
        conteo = {}
        for f in api.lugares(usuario)[1]["fotos"]:
            ciudad = (f["lugar"] or "").split(",")[0].strip()
            if ciudad:
                conteo[ciudad] = conteo.get(ciudad, 0) + 1
        return json.dumps(conteo, ensure_ascii=False) if conteo else "Ninguna foto tiene ubicación."

    @tool
    def buscar_fotos(texto: str | None = None, personas: list[str] | None = None, lugar: str | None = None,
                     desde: str | None = None, hasta: str | None = None) -> str:
        """Busca fotos del usuario; todos los filtros que se den deben cumplirse a la vez.

        Args:
            texto: lo que se ve o se lee en la foto, por ejemplo "perro" o "gente comiendo en la playa".
            personas: nombres de personas que deben aparecer en la foto.
            lugar: ciudad, departamento o país.
            desde: primer día del rango, AAAA-MM-DD.
            hasta: último día del rango, AAAA-MM-DD.
        """
        for fecha in (desde, hasta):
            if fecha:
                datetime.strptime(fecha, "%Y-%m-%d")  # ValueError le llega al modelo para que la corrija
        listas = []
        if personas:
            por_nombre = {}
            for i in api.consultar(pk, "PERSONA#"):
                por_nombre.setdefault(normalizar(i.get("nombre", "")), []).append(i["SK"][8:])
            for nombre in personas:
                ids = por_nombre.get(normalizar(nombre))
                if not ids:
                    estado["ultimas"] = []
                    return f"No hay ninguna persona llamada «{nombre}»."
                listas.append([f for p in ids for f in ids_de(f"P#{p}#")])
        if lugar:
            listas.append(ids_de(f"L#{normalizar(lugar)}#"))
        if texto and normalizar(texto):
            listas.append([f["id"] for f in api.buscar(usuario, normalizar(texto)[:200], texto[:200])])
        if not listas:
            if not (desde or hasta):
                raise ValueError("Da al menos un filtro")
            # Solo fechas: el índice por fecha ya trae el rango ordenado
            r = api.tabla.query(IndexName="por-fecha", ScanIndexForward=False, Limit=MAX_FOTOS,
                                KeyConditionExpression=Key("PK").eq(pk) & Key("fecha_orden").between(desde or "0", (hasta or "9") + "~"))
            listas.append([i["SK"][2:] for i in r["Items"]])
        comunes = set.intersection(*map(set, listas))
        ids = [i for i in dict.fromkeys(listas[0]) if i in comunes][:MAX_FOTOS]
        items = [i for i in api.leer_fotos(pk, ids).values() if i["estado"] == "LISTA"
                 and (desde or "0") <= i["fecha_orden"][:10] <= (hasta or "9")]
        estado["ultimas"] = [i["SK"][2:] for i in items]
        vistas = [{"fecha": i["fecha_orden"][:10], "lugar": (i.get("lugar") or {}).get("nombre"), "descripcion": i.get("descripcion")}
                  for i in items[:AL_MODELO]]
        return json.dumps({"encontradas": len(items), "fotos": vistas}, ensure_ascii=False)

    @tool
    def crear_album(nombre: str) -> str:
        """Crea un álbum con las fotos de la última búsqueda.

        Args:
            nombre: nombre del álbum, de hasta 60 caracteres.
        """
        if not estado.get("ultimas"):
            raise ValueError("Primero busca fotos: el álbum se arma con la última búsqueda")
        _, album = api.crear_album(usuario, {"nombre": nombre})
        api.editar_album(usuario, album["id"], {"agregar": estado["ultimas"]})
        estado["album"] = {"id": album["id"], "nombre": album["nombre"]}
        return f"Álbum «{album['nombre']}» creado con {len(estado['ultimas'])} fotos."

    return [personas, lugares, buscar_fotos, crear_album]


def preguntar(usuario, pregunta):
    estado = {"ultimas": []}
    agente = Agent(model=BedrockModel(model_id=MODELO, temperature=0.2), callback_handler=None,
                   system_prompt=INSTRUCCION.format(hoy=datetime.now(api.COLOMBIA).strftime("%Y-%m-%d")),
                   tools=herramientas(usuario, estado))
    respuesta = str(agente(pregunta)).strip()
    fotos = [api.resumen(i) for i in api.leer_fotos(f"U#{usuario}", estado["ultimas"][:100]).values()]
    return {"respuesta": respuesta, "fotos": fotos, **({"album": estado["album"]} if "album" in estado else {})}


def lambda_handler(event, _context):
    usuario = event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    try:
        cuerpo = event.get("body") or "{}"
        try:
            cuerpo = json.loads(base64.b64decode(cuerpo) if event.get("isBase64Encoded") else cuerpo)
        except ValueError:
            raise ErrorCliente(400, "JSON inválido") from None
        pregunta = " ".join(str(cuerpo.get("pregunta") or "").split()) if isinstance(cuerpo, dict) else ""
        if not pregunta or len(pregunta) > MAX_PREGUNTA:
            raise ErrorCliente(400, f"Escribe una pregunta de hasta {MAX_PREGUNTA} caracteres")
        api.descontar_cupo(usuario, "preguntas", PREGUNTAS_DIA, f"Llegaste al límite de {PREGUNTAS_DIA} preguntas por día")
        try:
            return responder(200, preguntar(usuario, pregunta))
        except Exception:  # noqa: BLE001 — Bedrock caído o sin cupo: el resto de Revela sigue funcionando
            log.exception("El asistente falló")
            return responder(502, {"error": "El asistente no está disponible; prueba con el buscador"})
    except ErrorCliente as e:
        return responder(e.status, {"error": str(e)})
