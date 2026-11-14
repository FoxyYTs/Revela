locals {
  prefijo = "jadg-revela"
  # Orígenes que pueden llamar la API y subir a S3: la web en CloudFront y la página servida en local
  origenes = ["https://${module.web.dominio}", "http://localhost:8000"]
}

# Un solo zip con las tres Lambdas; Pillow va aparte en una capa (solo la usa procesar)
data "archive_file" "lambdas" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas"
  excludes    = ["conftest.py", "test_flujo.py", "__pycache__/**", ".pytest_cache/**"]
  output_path = "${path.module}/build/lambdas.zip"
}

module "auth" {
  source  = "./modules/auth"
  prefijo = local.prefijo
}

module "datos" {
  source   = "./modules/datos"
  prefijo  = local.prefijo
  origenes = local.origenes
}

module "pipeline" {
  source      = "./modules/pipeline"
  prefijo     = local.prefijo
  bucket      = module.datos.bucket
  bucket_arn  = module.datos.bucket_arn
  tabla       = module.datos.tabla
  tabla_arn   = module.datos.tabla_arn
  zip         = data.archive_file.lambdas.output_path
  zip_hash    = data.archive_file.lambdas.output_base64sha256
  script_capa = "${path.module}/../scripts/capa_pillow.sh"
}

module "api" {
  source       = "./modules/api"
  prefijo      = local.prefijo
  bucket       = module.datos.bucket
  bucket_arn   = module.datos.bucket_arn
  tabla        = module.datos.tabla
  tabla_arn    = module.datos.tabla_arn
  zip          = data.archive_file.lambdas.output_path
  zip_hash     = data.archive_file.lambdas.output_base64sha256
  issuer       = module.auth.issuer
  client_id    = module.auth.client_id
  origenes     = local.origenes
  mapa_key     = module.web.mapa_key
  mapa_key_arn = module.web.mapa_key_arn
}

module "web" {
  source       = "./modules/web"
  prefijo      = local.prefijo
  html         = "${path.module}/../web/index.html"
  bucket_fotos = module.datos.bucket
  config = {
    api      = module.api.url
    region   = "us-east-1"
    clientId = module.auth.client_id
  }
}
