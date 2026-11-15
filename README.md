# Revela — galería de fotos inteligente en AWS

Trabajo final de Computación en la Nube (2026-2). Nivel 1: cuentas con Cognito, subida directa a S3,
análisis en paralelo con Step Functions (etiquetas, OCR, moderación, miniatura + EXIF) y búsqueda en DynamoDB.

```
Navegador ──Cognito (login)──► API Gateway HTTP (JWT) ──► Lambda api ──► DynamoDB
    │                                                        │
    └──POST prefirmado──► S3 originales/ ──► SQS ──► EventBridge Pipes ──► Step Functions
                                                                          1. Reclamar (DynamoDB, una vez por foto)
                                                                          2. Lambda procesar (miniatura + EXIF, copia a fotos/)
                                                                          3. En paralelo sobre fotos/:
                                                                             ├─ Rekognition DetectLabels
                                                                             ├─ Rekognition DetectText
                                                                             └─ Rekognition DetectModerationLabels
                                                                          4. Lambda guardar (Nova Lite: pie de foto + traducción;
                                                                             Amazon Location: dirección del GPS) ──► DynamoDB
                                                                             Nova Multimodal Embeddings ──► S3 Vectors
                                                                             Rekognition IndexFaces + Users (si activó personas)
Web: S3 privado ◄── CloudFront + OAC          Mapa: MapLibre + mosaicos de Amazon Location (API key solo para la web)
```

| Carpeta | Contenido |
|---|---|
| `lambdas/` | `api.py`, `procesar.py`, `guardar.py`, `caras.py` y `test_flujo.py` (moto) |
| `web/` | `index.html` + `app.js`, sin build; la configuración llega en `config.js` (la escribe Terraform) |
| `infra/` | Terraform: módulos `auth`, `datos`, `pipeline`, `api`, `web` (58 recursos) |
| `scripts/capa_pillow.sh` | Construye la capa de Pillow; Terraform la llama solo |

## Pruebas

```bash
uv venv .venv -p 3.12 && uv pip install -p .venv boto3 "moto[s3,dynamodb]" pillow pytest
cd lambdas && ../.venv/bin/pytest -q
```

## Despliegue

Necesita `uv` y `zip` (para la capa de Pillow). Estado en `jyj-reto-tfstate-497502378363`, key `revela/`.

```bash
cd infra
terraform init
terraform apply
terraform output web_url
```

Probar la página en local contra la API real: `terraform output -raw config_js > ../web/config.js`
y `cd ../web && python -m http.server 8000` (localhost:8000 está permitido en CORS).

Destruir: `terraform destroy` (los buckets tienen `force_destroy`).

## Notas

- Nova Lite mira la miniatura (480 px) y en una sola llamada escribe el pie de foto y traduce las etiquetas
  (Amazon Translate no está habilitado en la cuenta). Si falla, la foto queda sin pie y con etiquetas en inglés.
  Las fotos en cuarentena no se le envían.
- Lugares: el GPS sale del EXIF; Android e iOS lo borran al compartir desde el celular, así que la página manda la
  ubicación del navegador al subir (casilla "Mi ubicación") y la foto queda marcada como "ubicación al subir".
  Amazon Location pone el nombre (ciudad, departamento, país) con `IntendedUse=Storage` porque se guarda, y se puede
  buscar por él. La API key del mapa va en un stack de CloudFormation (el provider no tiene recurso para keys v2)
  y solo sirve para mosaicos desde el dominio de la web.
- Búsqueda por significado: `guardar` vectoriza la miniatura con Nova Multimodal Embeddings (1024 dimensiones,
  multilingüe) y la guarda en S3 Vectors con el usuario como metadato filtrable. Al buscar, la API vectoriza el texto
  y pide las 30 más cercanas del usuario con distancia coseno ≤ 0,72 (medido: "aula con tablero" 0,38 contra una foto
  de un salón, "perro en la playa" 0,87). Primero van las coincidencias exactas por etiqueta, texto o lugar, después
  las parecidas. Si Bedrock o S3 Vectors fallan, quedan solo las exactas.
- Personas (opcional, datos biométricos): solo para quien lo activa en "👤 Personas", que explica qué se guarda.
  La API crea una colección de Rekognition **por usuario** (`jyj-revela-caras-<sub>`), así una cara nunca se compara
  con fotos de otra cuenta. `guardar` indexa las caras de las fotos nuevas y las agrupa con Rekognition Users
  (`SearchUsers` ≥ 90 %: persona conocida → `AssociateFaces`, si no `CreateUser`). En DynamoDB queda el índice
  `P#<persona>#<fecha>` con la caja de la cara (la portada se recorta en el navegador) y `PERSONA#<id>` con el nombre.
  La portada de cada persona se puede elegir entre sus fotos, y dos personas que Rekognition separó (una foto
  editada, otra edad) se unen: sus caras pasan al mismo usuario de Rekognition (`AssociateFaces` con umbral 0). "🔄 Buscar caras" en el detalle repite la búsqueda
  con `QualityFilter=LOW` (caras pequeñas o borrosas, o fotos subidas antes de activar). La lógica está en
  `lambdas/caras.py`, compartida por `guardar` y la API. Desactivar borra la colección y esos registros. Las colecciones no son de Terraform: después de la exposición
  correr `scripts/borrar_caras.sh`.
- Concurrencia de Lambda de la cuenta = 10 compartida: Step Functions reintenta con espera si se llena.
- Cada foto se analiza una sola vez (paso `Reclamar`, escritura condicional en DynamoDB) y `procesar` copia a `fotos/`
  los bytes que leyó: Rekognition y la app usan esa copia, así que reusar la URL de subida no repite el gasto
  ni permite sobrescribir una foto ya moderada.
- CloudFront manda una CSP: solo corre código de la propia web y de cdnjs (MapLibre con SRI), y la página solo
  habla con la API, Cognito, S3 y Amazon Location. Por eso el JavaScript va en `app.js` y no en línea.
- Una subida que falla en S3 se borra desde la página; si aun así queda una foto en PROCESANDO más de 10 minutos,
  la API la oculta (o la muestra como error si el análisis alcanzó a empezar) hasta que el TTL la borre.
- Las fotos en cuarentena nunca salen con URL; las miniaturas no llevan EXIF (no exponen el GPS).
- La API limita a 10 peticiones/s (ráfaga 20) en total y cada usuario puede pedir 50 subidas por día (`SUBIDAS_DIA`,
  contador atómico en DynamoDB) para acotar el gasto en Rekognition y Nova.
