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

variable "script_capa" {
  description = "Script que construye el zip de la capa de Pillow y devuelve su ruta"
  type        = string
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

# ---------- S3 → SQS ----------

resource "aws_sqs_queue" "dlq" {
  name                      = "${var.prefijo}-fotos-dlq"
  message_retention_seconds = 345600 # 4 días para revisar qué falló
}

# Absorbe picos de subidas (la demo con QR) sin perder eventos
resource "aws_sqs_queue" "fotos" {
  name                       = "${var.prefijo}-fotos"
  visibility_timeout_seconds = 60
  message_retention_seconds  = 86400

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue_policy" "fotos" {
  queue_url = aws_sqs_queue.fotos.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "s3.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.fotos.arn
      Condition = { ArnEquals = { "aws:SourceArn" = var.bucket_arn } }
    }]
  })
}

# Solo originales/: las miniaturas que escribe procesar no disparan otro análisis
resource "aws_s3_bucket_notification" "fotos" {
  bucket = var.bucket

  queue {
    queue_arn     = aws_sqs_queue.fotos.arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = "originales/"
  }

  depends_on = [aws_sqs_queue_policy.fotos]
}

# ---------- Lambdas del flujo ----------

data "external" "capa_pillow" {
  program = ["bash", var.script_capa, "pillow", "pillow==11.3.0"]
}

resource "aws_lambda_layer_version" "pillow" {
  layer_name          = "${var.prefijo}-pillow"
  filename            = data.external.capa_pillow.result.path
  source_code_hash    = filebase64sha256(data.external.capa_pillow.result.path)
  compatible_runtimes = ["python3.12"]
}

resource "aws_iam_role" "procesar" {
  name               = "${var.prefijo}-procesar-role"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

resource "aws_iam_role_policy_attachment" "procesar_logs" {
  role       = aws_iam_role.procesar.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "procesar" {
  name = "leer-originales-escribir-miniaturas"
  role = aws_iam_role.procesar.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject", "s3:DeleteObject"], Resource = "${var.bucket_arn}/originales/*" },
      { Effect = "Allow", Action = "s3:PutObject", Resource = ["${var.bucket_arn}/miniaturas/*", "${var.bucket_arn}/fotos/*"] },
    ]
  })
}

resource "aws_cloudwatch_log_group" "procesar" {
  name              = "/aws/lambda/${var.prefijo}-procesar"
  retention_in_days = 7
}

resource "aws_lambda_function" "procesar" {
  function_name    = "${var.prefijo}-procesar"
  role             = aws_iam_role.procesar.arn
  runtime          = "python3.12"
  handler          = "procesar.lambda_handler"
  filename         = var.zip
  source_code_hash = var.zip_hash
  layers           = [aws_lambda_layer_version.pillow.arn]
  timeout          = 60
  memory_size      = 1024 # decodificar una foto de 12 MP ocupa ~150 MB y más memoria = más CPU

  depends_on = [aws_cloudwatch_log_group.procesar, aws_iam_role_policy_attachment.procesar_logs]
}

data "aws_caller_identity" "actual" {}

locals {
  # Nova Lite (ve imágenes) por inference profile "us.": hay que permitir el profile y el modelo en cada región a la que enruta
  modelo = "amazon.nova-lite-v1:0"
}

resource "aws_iam_role" "guardar" {
  name               = "${var.prefijo}-guardar-role"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

resource "aws_iam_role_policy_attachment" "guardar_logs" {
  role       = aws_iam_role.guardar.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "guardar" {
  name = "tabla-nova-limpiar"
  role = aws_iam_role.guardar.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:BatchWriteItem"], Resource = var.tabla_arn },
      { Effect = "Allow", Action = "s3:DeleteObject", Resource = ["${var.bucket_arn}/miniaturas/*", "${var.bucket_arn}/fotos/*"] },
      { Effect = "Allow", Action = "s3:GetObject", Resource = "${var.bucket_arn}/miniaturas/*" }, # Nova describe la miniatura
      # Personas (solo usuarios que lo activaron): Rekognition lee fotos/ con los permisos de este rol
      { Effect = "Allow", Action = "s3:GetObject", Resource = "${var.bucket_arn}/fotos/*" },
      {
        Effect   = "Allow"
        Action   = ["rekognition:IndexFaces", "rekognition:DeleteFaces", "rekognition:SearchUsers", "rekognition:CreateUser", "rekognition:AssociateFaces"]
        Resource = "arn:aws:rekognition:us-east-1:${data.aws_caller_identity.actual.account_id}:collection/${var.prefijo}-caras-*"
      },
      { Effect = "Allow", Action = "geo-places:ReverseGeocode", Resource = "arn:aws:geo-places:us-east-1::provider/default" },
      {
        Effect = "Allow"
        Action = "bedrock:InvokeModel"
        Resource = concat(
          ["arn:aws:bedrock:us-east-1:${data.aws_caller_identity.actual.account_id}:inference-profile/us.${local.modelo}"],
          [for r in ["us-east-1", "us-east-2", "us-west-2"] : "arn:aws:bedrock:${r}::foundation-model/${local.modelo}"],
          ["arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-2-multimodal-embeddings-v1:0"]
        )
      },
      { Effect = "Allow", Action = "s3vectors:PutVectors", Resource = var.indice_arn },
    ]
  })
}

resource "aws_cloudwatch_log_group" "guardar" {
  name              = "/aws/lambda/${var.prefijo}-guardar"
  retention_in_days = 7
}

resource "aws_lambda_function" "guardar" {
  function_name    = "${var.prefijo}-guardar"
  role             = aws_iam_role.guardar.arn
  runtime          = "python3.12"
  handler          = "guardar.lambda_handler"
  filename         = var.zip
  source_code_hash = var.zip_hash
  timeout          = 60 # Nova + hasta 10 caras con 3 llamadas a Rekognition cada una
  memory_size      = 256

  environment {
    variables = {
      TABLA             = var.tabla
      UMBRAL_MODERACION = "80"
      MODELO            = "us.${local.modelo}"
      VECTORES          = var.vectores
      CARAS             = "${var.prefijo}-caras-"
    }
  }

  depends_on = [aws_cloudwatch_log_group.guardar, aws_iam_role_policy_attachment.guardar_logs]
}

# ---------- Step Functions ----------

locals {
  # La cuenta comparte 10 ejecuciones simultáneas de Lambda entre todo el curso: si se llenan, se reintenta con espera
  reintento_lambda = jsonencode({
    ErrorEquals     = ["Lambda.TooManyRequestsException", "Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException"]
    IntervalSeconds = 2
    MaxAttempts     = 6
    BackoffRate     = 2
    JitterStrategy  = "FULL"
  })
  reintento_rekognition = jsonencode({
    ErrorEquals     = ["Rekognition.ThrottlingException", "Rekognition.ProvisionedThroughputExceededException", "Rekognition.InternalServerErrorException"]
    IntervalSeconds = 1
    MaxAttempts     = 5
    BackoffRate     = 2
    JitterStrategy  = "FULL"
  })
}

data "aws_iam_policy_document" "assume_sfn" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "flujo" {
  name               = "${var.prefijo}-flujo-role"
  assume_role_policy = data.aws_iam_policy_document.assume_sfn.json
}

resource "aws_iam_role_policy" "flujo" {
  name = "analizar-foto"
  role = aws_iam_role.flujo.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "lambda:InvokeFunction", Resource = [aws_lambda_function.procesar.arn, aws_lambda_function.guardar.arn] },
      # Rekognition lee la imagen de S3 con los permisos de quien lo llama
      { Effect = "Allow", Action = ["rekognition:DetectLabels", "rekognition:DetectText", "rekognition:DetectModerationLabels"], Resource = "*" },
      { Effect = "Allow", Action = "s3:GetObject", Resource = "${var.bucket_arn}/fotos/*" },
      { Effect = "Allow", Action = "dynamodb:UpdateItem", Resource = var.tabla_arn },
    ]
  })
}

resource "aws_sfn_state_machine" "flujo" {
  name     = "${var.prefijo}-analizar"
  role_arn = aws_iam_role.flujo.arn
  definition = templatefile("${path.module}/flujo.asl.json", {
    procesar              = aws_lambda_function.procesar.arn
    guardar               = aws_lambda_function.guardar.arn
    tabla                 = var.tabla
    reintento_lambda      = local.reintento_lambda
    reintento_rekognition = local.reintento_rekognition
  })
}

# ---------- EventBridge Pipes: SQS → Step Functions sin Lambda intermedia ----------

data "aws_iam_policy_document" "assume_pipes" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["pipes.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "pipe" {
  name               = "${var.prefijo}-pipe-role"
  assume_role_policy = data.aws_iam_policy_document.assume_pipes.json
}

resource "aws_iam_role_policy" "pipe" {
  name = "cola-a-flujo"
  role = aws_iam_role.pipe.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], Resource = aws_sqs_queue.fotos.arn },
      { Effect = "Allow", Action = "states:StartExecution", Resource = aws_sfn_state_machine.flujo.arn },
    ]
  })
}

resource "aws_pipes_pipe" "fotos" {
  name     = "${var.prefijo}-fotos"
  role_arn = aws_iam_role.pipe.arn
  source   = aws_sqs_queue.fotos.arn
  target   = aws_sfn_state_machine.flujo.arn

  source_parameters {
    sqs_queue_parameters {
      batch_size = 1 # una ejecución por foto
    }

    # Descarta el s3:TestEvent que S3 manda al configurar la notificación
    filter_criteria {
      filter {
        pattern = jsonencode({ body = { Records = { eventName = [{ prefix = "ObjectCreated:" }] } } })
      }
    }
  }

  target_parameters {
    step_function_state_machine_parameters {
      invocation_type = "FIRE_AND_FORGET"
    }
  }

  depends_on = [aws_iam_role_policy.pipe]
}

output "maquina_estados" {
  value = aws_sfn_state_machine.flujo.arn
}
