variable "prefijo" {
  type = string
}

variable "html" {
  description = "Ruta del index.html"
  type        = string
}

variable "bucket_fotos" {
  description = "Bucket de donde el navegador carga las fotos (URLs prefirmadas) y al que sube"
  type        = string
}

variable "fotos_arn" {
  type = string
}

variable "fotos_dominio" {
  description = "Dominio regional del bucket de fotos: segundo origen de CloudFront para los álbumes compartidos"
  type        = string
}

variable "config" {
  description = "Valores que la página recibe en config.js (URL de la API, Cognito)"
  type        = map(string)
}

# Bucket privado: solo CloudFront lo lee, con Origin Access Control
resource "aws_s3_bucket" "web" {
  bucket        = "${var.prefijo}-web"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "web" {
  bucket                  = aws_s3_bucket.web.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

locals {
  config_js = "window.CONFIG = ${jsonencode(var.config)};\n"
}

resource "aws_s3_object" "index" {
  bucket       = aws_s3_bucket.web.id
  key          = "index.html"
  source       = var.html
  etag         = filemd5(var.html)
  content_type = "text/html; charset=utf-8"
}

resource "aws_s3_object" "app" {
  bucket       = aws_s3_bucket.web.id
  key          = "app.js"
  source       = "${dirname(var.html)}/app.js"
  etag         = filemd5("${dirname(var.html)}/app.js")
  content_type = "application/javascript; charset=utf-8"
}

# Página pública de los álbumes compartidos
resource "aws_s3_object" "album" {
  for_each     = { "album.html" = "text/html; charset=utf-8", "album.js" = "application/javascript; charset=utf-8" }
  bucket       = aws_s3_bucket.web.id
  key          = each.key
  source       = "${dirname(var.html)}/${each.key}"
  etag         = filemd5("${dirname(var.html)}/${each.key}")
  content_type = each.value
}

resource "aws_s3_object" "config" {
  bucket       = aws_s3_bucket.web.id
  key          = "config.js"
  content      = local.config_js
  content_type = "application/javascript"
}

resource "aws_cloudfront_origin_access_control" "web" {
  name                              = "${var.prefijo}-web"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

locals {
  s3_fotos = "https://${var.bucket_fotos}.s3.amazonaws.com https://${var.bucket_fotos}.s3.us-east-1.amazonaws.com"
  # Solo se ejecuta código de la propia web y de cdnjs (MapLibre, con SRI); la página guarda tokens de sesión
  csp = join("; ", [
    "default-src 'self'",
    "script-src 'self' https://cdnjs.cloudflare.com",
    "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://fonts.googleapis.com",
    "font-src https://fonts.gstatic.com",
    "img-src 'self' data: blob: ${local.s3_fotos}",
    "connect-src 'self' https://*.execute-api.us-east-1.amazonaws.com https://cognito-idp.us-east-1.amazonaws.com https://maps.geo.us-east-1.amazonaws.com ${local.s3_fotos}",
    "worker-src blob:",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
  ])
}

# Las mismas cabeceras de Managed-SecurityHeadersPolicy más la CSP
resource "aws_cloudfront_response_headers_policy" "web" {
  name = "${var.prefijo}-web"

  security_headers_config {
    content_security_policy {
      content_security_policy = local.csp
      override                = true
    }
    content_type_options {
      override = true
    }
    frame_options {
      frame_option = "DENY"
      override     = true
    }
    # strict-origin-when-cross-origin manda el dominio como Referer: la API key del mapa depende de eso
    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      override                   = true
    }
    xss_protection {
      mode_block = true
      protection = true
      override   = true
    }
  }
}

resource "aws_cloudfront_distribution" "web" {
  enabled             = true
  comment             = "${var.prefijo} web"
  default_root_object = "index.html"
  price_class         = "PriceClass_100"

  origin {
    origin_id                = "web"
    domain_name              = aws_s3_bucket.web.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.web.id
  }

  # Álbumes compartidos: las fotos solo salen por aquí y con URL firmada por la API (key group); sin firma, 403
  origin {
    origin_id                = "fotos"
    domain_name              = var.fotos_dominio
    origin_access_control_id = aws_cloudfront_origin_access_control.web.id
  }

  dynamic "ordered_cache_behavior" {
    for_each = ["miniaturas/*", "publicas/*"] # nunca fotos/: conserva el EXIF con el GPS
    content {
      path_pattern           = ordered_cache_behavior.value
      target_origin_id       = "fotos"
      viewer_protocol_policy = "redirect-to-https"
      allowed_methods        = ["GET", "HEAD"]
      cached_methods         = ["GET", "HEAD"]
      trusted_key_groups     = [aws_cloudfront_key_group.firma.id]
      cache_policy_id        = "658327ea-f89d-4fab-a63d-7e88639e58f6" # Managed-CachingOptimized
    }
  }

  default_cache_behavior {
    target_origin_id       = "web"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    # shortcut: sin caché (Managed-CachingDisabled) para no invalidar en cada cambio; pasar a CachingOptimized al cerrar la página
    cache_policy_id            = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
    response_headers_policy_id = aws_cloudfront_response_headers_policy.web.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

resource "aws_s3_bucket_policy" "web" {
  bucket = aws_s3_bucket.web.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.web.arn}/*"
      Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.web.arn } }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.web]
}

resource "aws_s3_bucket_policy" "fotos" {
  bucket = split(":::", var.fotos_arn)[1]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = ["${var.fotos_arn}/miniaturas/*", "${var.fotos_arn}/publicas/*"]
      Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.web.arn } }
    }]
  })
}

# Llave para firmar las URLs de los álbumes compartidos: la pública va al key group de CloudFront y la privada a SSM,
# de donde la lee la API. shortcut: la privada también queda en el tfstate (bucket privado y cifrado); generarla fuera
# de Terraform si el estado se comparte con más gente
resource "tls_private_key" "firma" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "aws_ssm_parameter" "llave" {
  name  = "/${var.prefijo}/cloudfront-llave"
  type  = "SecureString"
  value = tls_private_key.firma.private_key_pem
}

resource "aws_cloudfront_public_key" "firma" {
  name        = "${var.prefijo}-albumes"
  encoded_key = tls_private_key.firma.public_key_pem
}

resource "aws_cloudfront_key_group" "firma" {
  name  = "${var.prefijo}-albumes"
  items = [aws_cloudfront_public_key.firma.id]
}

# API key de Amazon Location para los mosaicos del mapa. El provider de AWS no tiene recurso para las keys v2,
# así que va en un stack de CloudFormation: Terraform igual la crea y la destruye
resource "aws_cloudformation_stack" "mapa" {
  name = "${var.prefijo}-mapa"
  template_body = jsonencode({
    Resources = {
      Clave = {
        Type = "AWS::Location::APIKey"
        Properties = {
          KeyName     = "${var.prefijo}-mapa"
          Description = "Mosaicos del mapa de Revela; solo desde la web"
          NoExpiry    = true
          ForceUpdate = true
          ForceDelete = true
          Restrictions = {
            AllowActions   = ["geo-maps:GetTile", "geo-maps:GetStaticMap"]
            AllowResources = ["arn:aws:geo-maps:${data.aws_region.actual.region}::provider/default"]
            # La key viaja al navegador: estos orígenes son lo único que impide usarla desde otro sitio
            AllowReferers = ["https://${aws_cloudfront_distribution.web.domain_name}/*", "http://localhost:8000/*"]
          }
        }
      }
    }
    Outputs = {
      Arn = { Value = { "Fn::GetAtt" = ["Clave", "Arn"] } }
    }
  })
}

data "aws_region" "actual" {}

output "mapa_key" {
  value = "${var.prefijo}-mapa"
}

output "mapa_key_arn" {
  value = aws_cloudformation_stack.mapa.outputs["Arn"]
}

output "firma_id" {
  value = aws_cloudfront_public_key.firma.id
}

output "llave" {
  value = aws_ssm_parameter.llave.name
}

output "llave_arn" {
  value = aws_ssm_parameter.llave.arn
}

output "dominio" {
  value = aws_cloudfront_distribution.web.domain_name
}

output "csp" {
  value = local.csp
}

output "config_js" {
  value = local.config_js
}
