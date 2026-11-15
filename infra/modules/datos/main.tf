variable "prefijo" {
  type = string
}

variable "origenes" {
  description = "Orígenes web que pueden subir fotos con POST prefirmado"
  type        = list(string)
}

# originales/<usuario>/<foto>.jpg y miniaturas/<usuario>/<foto>.jpg; nada es público
resource "aws_s3_bucket" "fotos" {
  bucket        = "${var.prefijo}-fotos"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "fotos" {
  bucket                  = aws_s3_bucket.fotos.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_cors_configuration" "fotos" {
  bucket = aws_s3_bucket.fotos.id

  cors_rule {
    allowed_methods = ["POST"]
    allowed_origins = var.origenes
    allowed_headers = ["*"]
    max_age_seconds = 3600
  }
}

resource "aws_dynamodb_table" "fotos" {
  name         = "${var.prefijo}-fotos"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  attribute {
    name = "fecha_orden"
    type = "S"
  }

  # Galería por fecha; solo las fotos tienen fecha_orden, los índices de etiquetas no entran (índice disperso)
  global_secondary_index {
    name            = "por-fecha"
    projection_type = "ALL"

    key_schema {
      attribute_name = "PK"
      key_type       = "HASH"
    }

    key_schema {
      attribute_name = "fecha_orden"
      key_type       = "RANGE"
    }
  }

  # Subidas que se pidieron y nunca llegaron
  ttl {
    attribute_name = "expira"
    enabled        = true
  }
}

# Vectores de las miniaturas (Nova Multimodal Embeddings) para la búsqueda por significado; "usuario" es filtrable por defecto
resource "aws_s3vectors_vector_bucket" "vectores" {
  vector_bucket_name = "${var.prefijo}-vectores"
  force_destroy      = true
}

resource "aws_s3vectors_index" "fotos" {
  vector_bucket_name = aws_s3vectors_vector_bucket.vectores.vector_bucket_name
  index_name         = "fotos"
  data_type          = "float32"
  dimension          = 1024
  distance_metric    = "cosine"
}

output "vectores" {
  value = aws_s3vectors_vector_bucket.vectores.vector_bucket_name
}

output "indice_arn" {
  value = aws_s3vectors_index.fotos.index_arn
}

output "bucket" {
  value = aws_s3_bucket.fotos.bucket
}

output "bucket_dominio" {
  value = aws_s3_bucket.fotos.bucket_regional_domain_name
}

output "bucket_arn" {
  value = aws_s3_bucket.fotos.arn
}

output "tabla" {
  value = aws_dynamodb_table.fotos.name
}

output "tabla_arn" {
  value = aws_dynamodb_table.fotos.arn
}
