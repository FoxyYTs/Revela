variable "prefijo" {
  type = string
}

variable "correo" {
  description = "Recibe las alarmas (hay que confirmar la suscripción de SNS) y los avisos del presupuesto"
  type        = string
}

variable "maquina_estados" {
  type = string
}

variable "cola_errores" {
  type = string
}

variable "funciones" {
  type = list(string)
}

variable "api_id" {
  type = string
}

variable "presupuesto_usd" {
  type    = number
  default = 10
}

data "aws_region" "actual" {}

locals {
  region = data.aws_region.actual.region
  sfn    = ["AWS/States", "%s", "StateMachineArn", var.maquina_estados]
  # Gráfica de líneas de 5 minutos; x/y/ancho/alto en la grilla de 24 columnas del tablero
  grafica = { type = "metric", width = 12, height = 6 }
}

# ---------- Tablero ----------

resource "aws_cloudwatch_dashboard" "revela" {
  dashboard_name = var.prefijo
  dashboard_body = jsonencode({
    widgets = [
      merge(local.grafica, { x = 0, y = 0, properties = {
        title   = "Fotos analizadas (Step Functions)", region = local.region, stat = "Sum", period = 300,
        metrics = [for m in ["ExecutionsStarted", "ExecutionsSucceeded", "ExecutionsFailed"] : [local.sfn[0], m, local.sfn[2], local.sfn[3]]]
      } }),
      merge(local.grafica, { x = 12, y = 0, properties = {
        title = "Segundos por foto (p50 / p90)", region = local.region, period = 300,
        metrics = [
          [local.sfn[0], "ExecutionTime", local.sfn[2], local.sfn[3], { stat = "p50", label = "p50" }],
          [local.sfn[0], "ExecutionTime", local.sfn[2], local.sfn[3], { stat = "p90", label = "p90" }],
        ]
        yAxis = { left = { label = "ms" } }
      } }),
      merge(local.grafica, { x = 0, y = 6, properties = {
        title = "Lambdas: invocaciones, errores y throttles (concurrencia de la cuenta = 10)", region = local.region, stat = "Sum", period = 300,
        # setproduct y no flatten: flatten también aplanaría cada métrica, y CloudWatch exige una lista por métrica
        metrics = [for par in setproduct(concat(var.funciones, ["${var.prefijo}-api"]), ["Invocations", "Errors", "Throttles"]) :
        ["AWS/Lambda", par[1], "FunctionName", par[0]]]
      } }),
      merge(local.grafica, { x = 12, y = 6, properties = {
        title   = "API: peticiones, 4xx y 5xx", region = local.region, stat = "Sum", period = 300,
        metrics = [for m in ["Count", "4xx", "5xx"] : ["AWS/ApiGateway", m, "ApiId", var.api_id]]
      } }),
      merge(local.grafica, { x = 0, y = 12, properties = {
        title = "Cola de fotos y cola de errores (DLQ)", region = local.region, stat = "Maximum", period = 300,
        metrics = [
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", "${var.prefijo}-fotos"],
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", var.cola_errores],
        ]
      } }),
      # Rekognition y Bedrock no tienen recursos propios: estas métricas son de toda la cuenta (compartida con el curso)
      merge(local.grafica, { x = 12, y = 12, properties = {
        title = "IA de toda la cuenta: Rekognition y Bedrock", region = local.region, period = 300,
        metrics = [
          [{ expression = "SEARCH('{AWS/Rekognition,Operation} MetricName=\"SuccessfulRequestCount\"', 'Sum', 300)", id = "rek" }],
          [{ expression = "SEARCH('{AWS/Bedrock,ModelId} MetricName=\"Invocations\"', 'Sum', 300)", id = "bed" }],
        ]
      } }),
    ]
  })
}

# ---------- Alarmas por correo ----------

resource "aws_sns_topic" "alertas" {
  name = "${var.prefijo}-alertas"
}

resource "aws_sns_topic_subscription" "correo" {
  topic_arn = aws_sns_topic.alertas.arn
  protocol  = "email"
  endpoint  = var.correo
}

locals {
  alarmas = {
    "analisis-fallido" = { descripcion = "Una foto terminó en error en Step Functions", namespace = "AWS/States", metrica = "ExecutionsFailed", stat = "Sum", dims = { StateMachineArn = var.maquina_estados } }
    "cola-de-errores"  = { descripcion = "Hay fotos en la DLQ: Pipes no pudo entregarlas", namespace = "AWS/SQS", metrica = "ApproximateNumberOfMessagesVisible", stat = "Maximum", dims = { QueueName = var.cola_errores } }
    "api-5xx"          = { descripcion = "La API respondió errores 5xx", namespace = "AWS/ApiGateway", metrica = "5xx", stat = "Sum", dims = { ApiId = var.api_id } }
  }
}

resource "aws_cloudwatch_metric_alarm" "revela" {
  for_each            = local.alarmas
  alarm_name          = "${var.prefijo}-${each.key}"
  alarm_description   = each.value.descripcion
  namespace           = each.value.namespace
  metric_name         = each.value.metrica
  dimensions          = each.value.dims
  statistic           = each.value.stat
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching" # sin tráfico no hay datos: eso no es una alarma
  alarm_actions       = [aws_sns_topic.alertas.arn]
}

# ---------- Presupuesto ----------

# La etiqueta Proyecto (default_tags del provider) tiene que estar activa en Billing para poder filtrar por ella.
# Es un ajuste de toda la cuenta y AWS tarda hasta 24 h en empezar a contar
resource "aws_ce_cost_allocation_tag" "proyecto" {
  tag_key = "Proyecto"
  status  = "Active"
}

# shortcut: Rekognition, Bedrock y Location cobran por llamada y no llevan etiqueta, así que este presupuesto solo ve
# S3, DynamoDB, Lambda, CloudFront, etc. El gasto de IA lo acotan los cupos por usuario (50 fotos y 30 búsquedas de caras
# al día) y el throttling de la API; un presupuesto de toda la cuenta saltaría por el uso del resto del curso
resource "aws_budgets_budget" "revela" {
  name         = var.prefijo
  budget_type  = "COST"
  limit_amount = tostring(var.presupuesto_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = ["user:Proyecto$revela"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.correo]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.correo]
  }

  depends_on = [aws_ce_cost_allocation_tag.proyecto]
}

output "tablero" {
  value = "https://${local.region}.console.aws.amazon.com/cloudwatch/home?region=${local.region}#dashboards/dashboard/${var.prefijo}"
}
