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

variable "origenes" {
  type = list(string)
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

resource "aws_iam_role" "api" {
  name               = "${var.prefijo}-api-role"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
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
      { Effect = "Allow", Action = ["s3:GetObject", "s3:DeleteObject"], Resource = ["${var.bucket_arn}/miniaturas/*", "${var.bucket_arn}/fotos/*"] },
    ]
  })
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${var.prefijo}-api"
  retention_in_days = 7
}

resource "aws_lambda_function" "api" {
  function_name    = "${var.prefijo}-api"
  role             = aws_iam_role.api.arn
  runtime          = "python3.12"
  handler          = "api.lambda_handler"
  filename         = var.zip
  source_code_hash = var.zip_hash
  timeout          = 10
  memory_size      = 256

  environment {
    variables = {
      TABLA       = var.tabla
      BUCKET      = var.bucket
      SUBIDAS_DIA = "50"
    }
  }

  depends_on = [aws_cloudwatch_log_group.api, aws_iam_role_policy_attachment.api_logs]
}

resource "aws_apigatewayv2_api" "this" {
  name          = "${var.prefijo}-api"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = var.origenes
    allow_methods = ["GET", "POST", "DELETE"]
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
  for_each           = toset(["POST /subidas", "GET /fotos", "GET /fotos/{id}", "DELETE /fotos/{id}", "GET /etiquetas"])
  api_id             = aws_apigatewayv2_api.this.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
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

output "url" {
  value = aws_apigatewayv2_api.this.api_endpoint
}
