variable "prefijo" {
  type = string
}

variable "bucket" {
  type = string
}

variable "bucket_arn" {
  type = string
}

variable "tabla" {
  type = string
}

variable "tabla_arn" {
  type = string
}

variable "vectores" {
  type = string
}

variable "indice_arn" {
  type = string
}

variable "zip" {
  type = string
}

variable "zip_hash" {
  type = string
}

variable "issuer" {
  type = string
}

variable "client_id" {
  type = string
}

variable "mapa_key" {
  type = string
}

variable "mapa_key_arn" {
  type = string
}

variable "cdn" {
  description = "Dominio de CloudFront: sirve la página del álbum compartido y sus fotos firmadas"
  type        = string
}

variable "firma_id" {
  type = string
}

variable "llave" {
  type = string
}

variable "llave_arn" {
  type = string
}

variable "script_capa" {
  type = string
}

variable "origenes" {
  type = list(string)
}

variable "correo" {
  description = "Remitente de los recuerdos (y destinatario mientras SES esté en sandbox)"
  type        = string
}

variable "correos_prueba" {
  description = "Correos que pueden recibir recuerdos mientras SES esté en sandbox (cada uno recibe un correo de verificación)"
  type        = list(string)
}

variable "pool_id" {
  type = string
}

locals {
  modelo = "us.amazon.nova-2-lite-v1:0"
  # El asistente importa api.py, así que necesita el mismo entorno
  entorno = {
    TABLA       = var.tabla
    BUCKET      = var.bucket
    SUBIDAS_DIA = "50"
    MAPA_KEY    = var.mapa_key
    VECTORES    = var.vectores
    CARAS       = "${var.prefijo}-caras-"
    CDN         = var.cdn
    FIRMA_ID    = var.firma_id
    LLAVE       = var.llave
  }
}

data "aws_iam_policy_document" "assume_lambda" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_caller_identity" "actual" {}

resource "aws_iam_role" "api" {
  name               = "${var.prefijo}-api-role"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

# X-Ray: cada Lambda manda su segmento; API Gateway HTTP no participa (X-Ray solo existe en las APIs REST)
resource "aws_iam_role_policy_attachment" "api_xray" {
  role       = aws_iam_role.api.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy_attachment" "api_logs" {
  role       = aws_iam_role.api.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "api" {
  name = "fotos-del-usuario"
  role = aws_iam_role.api.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:BatchGetItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem"]
        Resource = [var.tabla_arn, "${var.tabla_arn}/index/por-fecha"]
      },
      # Las URLs prefirmadas valen con los permisos del rol que las firma
      { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"], Resource = "${var.bucket_arn}/originales/*" },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:DeleteObject"], Resource = ["${var.bucket_arn}/miniaturas/*", "${var.bucket_arn}/fotos/*", "${var.bucket_arn}/publicas/*"] },
      { Effect = "Allow", Action = "geo:DescribeKey", Resource = var.mapa_key_arn },
      # Llave privada para firmar URLs de CloudFront (cifrada con la llave administrada aws/ssm)
      { Effect = "Allow", Action = "ssm:GetParameter", Resource = var.llave_arn },
      # Buscar por significado: vector del texto con Nova y consulta filtrada por usuario (el filtro exige GetVectors)
      { Effect = "Allow", Action = "bedrock:InvokeModel", Resource = "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-2-multimodal-embeddings-v1:0" },
      # Asistente: Nova 2 Lite por el perfil us., que reparte las llamadas entre tres regiones
      {
        Effect = "Allow", Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"] # Strands usa ConverseStream
        Resource = concat(["arn:aws:bedrock:us-east-1:${data.aws_caller_identity.actual.account_id}:inference-profile/${local.modelo}"],
        [for r in ["us-east-1", "us-east-2", "us-west-2"] : "arn:aws:bedrock:${r}::foundation-model/amazon.nova-2-lite-v1:0"])
      },
      { Effect = "Allow", Action = ["s3vectors:QueryVectors", "s3vectors:GetVectors", "s3vectors:DeleteVectors"], Resource = var.indice_arn },
      # Personas: una colección por usuario que se crea al dar el consentimiento y se borra al retirarlo
      { Effect = "Allow", Action = ["rekognition:CreateCollection", "rekognition:DeleteCollection", "rekognition:DeleteFaces", "rekognition:IndexFaces", "rekognition:SearchUsers", "rekognition:CreateUser", "rekognition:AssociateFaces", "rekognition:ListFaces", "rekognition:DisassociateFaces", "rekognition:DeleteUser"], Resource = "arn:aws:rekognition:us-east-1:${data.aws_caller_identity.actual.account_id}:collection/${var.prefijo}-caras-*" },
    ]
  })
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${var.prefijo}-api"
  retention_in_days = 7
}

# rsa (puro Python) para firmar las URLs de CloudFront con botocore.signers.CloudFrontSigner
data "external" "capa_firmas" {
  program = ["bash", var.script_capa, "firmas", "rsa==4.9.1"]
}

resource "aws_lambda_layer_version" "firmas" {
  layer_name          = "${var.prefijo}-firmas"
  filename            = data.external.capa_firmas.result.path
  source_code_hash    = filebase64sha256(data.external.capa_firmas.result.path)
  compatible_runtimes = ["python3.12"]
}

resource "aws_lambda_function" "api" {
  function_name = "${var.prefijo}-api"
  role          = aws_iam_role.api.arn
  runtime       = "python3.12"
  handler       = "api.lambda_handler"
  tracing_config {
    mode = "Active"
  }
  layers           = [aws_lambda_layer_version.firmas.arn]
  filename         = var.zip
  source_code_hash = var.zip_hash
  timeout          = 29 # "Buscar caras" hace hasta 3 llamadas a Rekognition por cara; API Gateway corta a los 30 s
  memory_size      = 256

  environment {
    variables = local.entorno
  }

  depends_on = [aws_cloudwatch_log_group.api, aws_iam_role_policy_attachment.api_logs]
}

# Strands Agents (con sus dependencias: pydantic, opentelemetry, mcp…) ~72 MB; solo la usa el asistente
data "external" "capa_strands" {
  program = ["bash", var.script_capa, "strands", "strands-agents==1.65.0"]
}

resource "aws_lambda_layer_version" "strands" {
  layer_name          = "${var.prefijo}-strands"
  filename            = data.external.capa_strands.result.path
  source_code_hash    = filebase64sha256(data.external.capa_strands.result.path)
  compatible_runtimes = ["python3.12"]
}

resource "aws_cloudwatch_log_group" "asistente" {
  name              = "/aws/lambda/${var.prefijo}-asistente"
  retention_in_days = 7
}

# Agente aparte de la API: su capa pesada y su arranque en frío no frenan las demás rutas
resource "aws_lambda_function" "asistente" {
  function_name = "${var.prefijo}-asistente"
  role          = aws_iam_role.api.arn
  runtime       = "python3.12"
  handler       = "asistente.lambda_handler"
  tracing_config {
    mode = "Active"
  }
  layers           = [aws_lambda_layer_version.firmas.arn, aws_lambda_layer_version.strands.arn]
  filename         = var.zip
  source_code_hash = var.zip_hash
  timeout          = 29 # API Gateway corta a los 30 s
  memory_size      = 512

  environment {
    variables = merge(local.entorno, { MODELO = local.modelo })
  }

  depends_on = [aws_cloudwatch_log_group.asistente, aws_iam_role_policy_attachment.api_logs]
}

resource "aws_apigatewayv2_api" "this" {
  name          = "${var.prefijo}-api"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = var.origenes
    allow_methods = ["GET", "POST", "PUT", "DELETE"]
    allow_headers = ["authorization", "content-type"]
    max_age       = 3600
  }
}

# API Gateway valida el token de Cognito antes de llamar la Lambda; la Lambda solo lee el "sub"
resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.this.id
  name             = "cognito"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    issuer   = var.issuer
    audience = [var.client_id]
  }
}

resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "rutas" {
  for_each = toset(["POST /subidas", "GET /fotos", "GET /fotos/{id}", "DELETE /fotos/{id}", "GET /etiquetas", "GET /lugares", "GET /mapa",
    "GET /personas", "PUT /personas", "DELETE /personas", "PUT /personas/{id}", "POST /fotos/{id}/caras", "POST /personas/{id}/unir",
    "GET /albumes", "POST /albumes", "GET /albumes/{id}", "PUT /albumes/{id}", "DELETE /albumes/{id}",
  "POST /albumes/{id}/enlace", "DELETE /albumes/{id}/enlace", "GET /recuerdos", "PUT /recuerdos", "DELETE /recuerdos"])
  api_id             = aws_apigatewayv2_api.this.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_integration" "asistente" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.asistente.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "asistente" {
  api_id             = aws_apigatewayv2_api.this.id
  route_key          = "POST /asistente"
  target             = "integrations/${aws_apigatewayv2_integration.asistente.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_lambda_permission" "asistente" {
  statement_id  = "AllowAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.asistente.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}

# Única ruta sin autorizador: el álbum compartido se abre sin cuenta (el token de 32 caracteres es la credencial)
resource "aws_apigatewayv2_route" "compartidos" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "GET /compartidos/{token}"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = "$default"
  auto_deploy = true

  # Tope de gasto: cada subida dispara 3 llamadas a Rekognition
  default_route_settings {
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}

output "api_id" {
  value = aws_apigatewayv2_api.this.id
}

output "funcion" {
  value = aws_lambda_function.api.function_name
}

output "url" {
  value = aws_apigatewayv2_api.this.api_endpoint
}

# ---------- Recuerdos: cada mañana, fotos de este día en años anteriores → álbum + correo ----------

# SES está en sandbox: al crearla, AWS manda un correo de verificación; sin confirmarlo no sale ningún recuerdo
resource "aws_ses_email_identity" "remitente" {
  email = var.correo
}

resource "aws_ses_email_identity" "prueba" {
  for_each = toset(var.correos_prueba)
  email    = each.value
}

resource "aws_iam_role" "recuerdos" {
  name               = "${var.prefijo}-recuerdos-role"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

resource "aws_iam_role_policy_attachment" "recuerdos_logs" {
  role       = aws_iam_role.recuerdos.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "recuerdos_xray" {
  role       = aws_iam_role.recuerdos.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy" "recuerdos" {
  name = "recuerdos"
  role = aws_iam_role.recuerdos.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:BatchGetItem"], Resource = [var.tabla_arn, "${var.tabla_arn}/index/por-fecha"] },
      { Effect = "Allow", Action = "ssm:GetParameter", Resource = var.llave_arn },
      # En sandbox SES también autoriza contra la identidad de cada destinatario, no solo la del remitente
      { Effect = "Allow", Action = "ses:SendEmail", Resource = concat([aws_ses_email_identity.remitente.arn], [for i in aws_ses_email_identity.prueba : i.arn]) },
      { Effect = "Allow", Action = "cognito-idp:ListUsers", Resource = "arn:aws:cognito-idp:us-east-1:${data.aws_caller_identity.actual.account_id}:userpool/${var.pool_id}" },
    ]
  })
}

resource "aws_cloudwatch_log_group" "recuerdos" {
  name              = "/aws/lambda/${var.prefijo}-recuerdos"
  retention_in_days = 7
}

resource "aws_lambda_function" "recuerdos" {
  function_name = "${var.prefijo}-recuerdos"
  role          = aws_iam_role.recuerdos.arn
  runtime       = "python3.12"
  handler       = "recuerdos.lambda_handler"
  tracing_config {
    mode = "Active"
  }
  layers           = [aws_lambda_layer_version.firmas.arn] # firma las miniaturas del correo por 7 días
  filename         = var.zip
  source_code_hash = var.zip_hash
  timeout          = 60
  memory_size      = 256

  environment {
    variables = merge(local.entorno, { REMITENTE = var.correo, POOL = var.pool_id })
  }

  depends_on = [aws_cloudwatch_log_group.recuerdos, aws_iam_role_policy_attachment.recuerdos_logs]
}

data "aws_iam_policy_document" "assume_scheduler" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "programador" {
  name               = "${var.prefijo}-programador-role"
  assume_role_policy = data.aws_iam_policy_document.assume_scheduler.json
}

resource "aws_iam_role_policy" "programador" {
  name = "invocar-recuerdos"
  role = aws_iam_role.programador.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.recuerdos.arn }]
  })
}

resource "aws_scheduler_schedule" "recuerdos" {
  name                         = "${var.prefijo}-recuerdos"
  schedule_expression          = "cron(0 7 * * ? *)"
  schedule_expression_timezone = "America/Bogota"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.recuerdos.arn
    role_arn = aws_iam_role.programador.arn
    input    = "{}"
  }
}

output "funcion_recuerdos" {
  value = aws_lambda_function.recuerdos.function_name
}
