variable "prefijo" {
  type = string
}

variable "html" {
  description = "Ruta del index.html"
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

  default_cache_behavior {
    target_origin_id       = "web"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    # shortcut: sin caché (Managed-CachingDisabled) para no invalidar en cada cambio; pasar a CachingOptimized al cerrar la página
    cache_policy_id            = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
    response_headers_policy_id = "67f7725c-6f97-4210-82d7-5512b31e9d03" # Managed-SecurityHeadersPolicy
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

output "dominio" {
  value = aws_cloudfront_distribution.web.domain_name
}

output "config_js" {
  value = local.config_js
}
