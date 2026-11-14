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
                                                                          4. Lambda guardar (Nova Micro traduce) ──► DynamoDB
Web: S3 privado ◄── CloudFront + OAC
```

| Carpeta | Contenido |
|---|---|
| `lambdas/` | `api.py`, `procesar.py`, `guardar.py` y `test_flujo.py` (moto) |
| `web/index.html` | Página sin build; recibe la configuración en `config.js` |
| `infra/` | Terraform: módulos `auth`, `datos`, `pipeline`, `api`, `web` (49 recursos) |
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

- Amazon Translate no está habilitado en la cuenta → las etiquetas se traducen con Nova Micro; si falla, quedan en inglés.
- Concurrencia de Lambda de la cuenta = 10 compartida: Step Functions reintenta con espera si se llena.
- Cada foto se analiza una sola vez (paso `Reclamar`, escritura condicional en DynamoDB) y `procesar` copia a `fotos/`
  los bytes que leyó: Rekognition y la app usan esa copia, así que reusar la URL de subida no repite el gasto
  ni permite sobrescribir una foto ya moderada.
- Las fotos en cuarentena nunca salen con URL; las miniaturas no llevan EXIF (no exponen el GPS).
- La API limita a 10 peticiones/s (ráfaga 20) en total y cada usuario puede pedir 50 subidas por día (`SUBIDAS_DIA`,
  contador atómico en DynamoDB) para acotar el gasto en Rekognition y Nova.
