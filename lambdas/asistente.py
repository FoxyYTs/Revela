"""Asistente de Revela: un agente de Strands con Amazon Nova que responde pedidos en lenguaje natural
("las fotos con Juliana en Cartagena del año pasado") combinando las búsquedas de la API.

POST /asistente {pregunta, historial?, fotos?} → {respuesta, fotos, album?}
La Lambda no guarda la conversación: la página manda las últimas preguntas con sus respuestas y los ids de las fotos
que está mostrando, para que "sí, haz un álbum con esas" tenga contexto.

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
MAX_HISTORIAL = 3  # turnos anteriores que se le pasan al modelo
MAX_RESPUESTA = 1000
AL_MODELO = 15  # resúmenes que ve el modelo para poder comentar las fotos; el resto solo se cuenta

INSTRUCCION = """Eres el asistente de Revela, una galería de fotos. Hoy es {hoy} (hora de Colombia).
Ayudas a encontrar fotos del usuario con tus herramientas:
- buscar_fotos combina filtros: texto (lo que se ve o se lee en la foto), personas (nombres), lugar (ciudad,
  departamento o país) y un rango de fechas AAAA-MM-DD. Convierte fechas relativas ("el año pasado", "en diciembre")
  en ese rango. Pon cada parte del pedido en su filtro, no todo en texto.
- "Fotos con Ana y Luis" o "juntas" son fotos donde salen todas (alguna=false); "un álbum de Ana, Luis y Eva" o
  "las fotos de cada una" son fotos donde sale cualquiera de ellas (alguna=true).
- Si un nombre no coincide con nadie, usa personas() para ver los nombres que existen.
- lugares() lista las ciudades donde hay fotos.
- crear_album guarda en un álbum las fotos de la última búsqueda; úsalo solo si el usuario pide un álbum.
Las fotos que encuentres se le muestran al usuario debajo de tu respuesta: no las enumeres ni inventes fotos.
Si el usuario se refiere a "esas fotos", son las de la última búsqueda: no vuelvas a buscar.
Si una búsqueda no da resultados, intenta tú mismo lo razonable (revisar los nombres, quitar un filtro) antes de
responder. Haz una pregunta solo si de verdad falta un dato, y si el usuario acepta lo que propusiste, hazlo ya.
Responde en español y en texto plano (sin markdown), en una a tres frases, diciendo qué encontraste o por qué no hubo resultados."""


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
                     desde: str | None = None, hasta: str | None = None, alguna: bool = False) -> str:
        """Busca fotos del usuario; todos los filtros que se den deben cumplirse a la vez.

        Args:
            texto: lo que se ve o se lee en la foto, por ejemplo "perro" o "gente comiendo en la playa".
            personas: nombres de personas que deben aparecer en la foto (todas juntas, salvo con alguna=true).
            lugar: ciudad, departamento o país.
            desde: primer día del rango, AAAA-MM-DD.
            hasta: último día del rango, AAAA-MM-DD.
            alguna: true si basta con que salga una de las personas, por ejemplo "un álbum de Ana, Luis y Eva";
                false si tienen que salir juntas en la misma foto.
        """
        for fecha in (desde, hasta):
            if fecha:
                datetime.strptime(fecha, "%Y-%m-%d")  # ValueError le llega al modelo para que la corrija
        listas, filtros = [], []
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
                filtros.append(f"persona {nombre}")
            if alguna and len(listas) > 1:
                # Una sola lista con las fotos de cualquiera, de la más nueva a la más vieja (fecha_orden está en el SK)
                fechas = {i["foto"]: i["SK"].split("#", 2)[2] for i in api.consultar(pk, "P#", ProjectionExpression="foto, SK")}
                listas = [sorted({f for lista in listas for f in lista}, key=fechas.get, reverse=True)]
                filtros = ["alguna de " + ", ".join(personas)]
        if lugar:
            listas.append(ids_de(f"L#{normalizar(lugar)}#"))
            filtros.append(f"lugar {lugar}")
        if texto and normalizar(texto):
            listas.append([f["id"] for f in api.buscar(usuario, normalizar(texto)[:200], texto[:200])])
            filtros.append(f"texto {texto}")
        if not listas:
            if not (desde or hasta):
                raise ValueError("Da al menos un filtro")
            # Solo fechas: el índice por fecha ya trae el rango ordenado
            r = api.tabla.query(IndexName="por-fecha", ScanIndexForward=False, Limit=MAX_FOTOS,
                                KeyConditionExpression=Key("PK").eq(pk) & Key("fecha_orden").between(desde or "0", (hasta or "9") + "~"))
            listas.append([i["SK"][2:] for i in r["Items"]])
            filtros.append("fechas")
        comunes = set.intersection(*map(set, listas))
        ids = [i for i in dict.fromkeys(listas[0]) if i in comunes][:MAX_FOTOS]
        items = [i for i in api.leer_fotos(pk, ids).values() if i["estado"] == "LISTA"
                 and (desde or "0") <= i["fecha_orden"][:10] <= (hasta or "9")]
        estado["ultimas"] = [i["SK"][2:] for i in items]
        vistas = [{"fecha": i["fecha_orden"][:10], "lugar": (i.get("lugar") or {}).get("nombre"), "descripcion": i.get("descripcion")}
                  for i in items[:AL_MODELO]]
        resultado = {"encontradas": len(items), "fotos": vistas}
        if not items and (len(listas) > 1 or (desde or hasta) and filtros[-1:] != ["fechas"]):
            # Nova Lite casi no reintenta solo: decirle qué filtro sobra le permite proponer algo concreto
            resultado["cada_filtro_por_separado"] = {f: len(set(l)) for f, l in zip(filtros, listas)}
        return json.dumps(resultado, ensure_ascii=False)

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


def leer_historial(valor):
    """[{pregunta, respuesta}] → mensajes de Strands; solo texto, así que no puede colar llamadas a herramientas."""
    if not isinstance(valor, list) or len(valor) > MAX_HISTORIAL or not all(
            isinstance(t, dict) and isinstance(t.get("pregunta"), str) and isinstance(t.get("respuesta"), str)
            and 0 < len(t["pregunta"]) <= MAX_PREGUNTA and 0 < len(t["respuesta"]) <= MAX_RESPUESTA for t in valor):
        raise ErrorCliente(400, "Historial inválido")
    return [{"role": rol, "content": [{"text": t[campo]}]} for t in valor
            for rol, campo in (("user", "pregunta"), ("assistant", "respuesta"))]


def preguntar(usuario, pregunta, historial=(), fotos=()):
    estado = {"ultimas": list(fotos)}  # crear_album filtra que sean propias y estén listas
    agente = Agent(model=BedrockModel(model_id=MODELO, temperature=0.2), callback_handler=None, messages=list(historial),
                   system_prompt=INSTRUCCION.format(hoy=datetime.now(api.COLOMBIA).strftime("%Y-%m-%d")),
                   tools=herramientas(usuario, estado))
    respuesta = str(agente(pregunta)).replace("**", "").strip()  # la página muestra texto plano
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
        historial = leer_historial(cuerpo.get("historial", []))
        fotos = api.ids_validos(cuerpo.get("fotos", []))
        api.descontar_cupo(usuario, "preguntas", PREGUNTAS_DIA, f"Llegaste al límite de {PREGUNTAS_DIA} preguntas por día")
        try:
            return responder(200, preguntar(usuario, pregunta, historial, fotos))
        except Exception:  # noqa: BLE001 — Bedrock caído o sin cupo: el resto de Revela sigue funcionando
            log.exception("El asistente falló")
            return responder(502, {"error": "El asistente no está disponible; prueba con el buscador"})
    except ErrorCliente as e:
        return responder(e.status, {"error": str(e)})
