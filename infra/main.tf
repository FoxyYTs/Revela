locals {
  prefijo = "jyj-revela"
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
  vectores    = module.datos.vectores
  indice_arn  = module.datos.indice_arn
  zip         = data.archive_file.lambdas.output_path
  zip_hash    = data.archive_file.lambdas.output_base64sha256
  script_capa = "${path.module}/../scripts/capa.sh"
}

module "api" {
  source         = "./modules/api"
  prefijo        = local.prefijo
  bucket         = module.datos.bucket
  bucket_arn     = module.datos.bucket_arn
  tabla          = module.datos.tabla
  tabla_arn      = module.datos.tabla_arn
  vectores       = module.datos.vectores
  indice_arn     = module.datos.indice_arn
  zip            = data.archive_file.lambdas.output_path
  zip_hash       = data.archive_file.lambdas.output_base64sha256
  issuer         = module.auth.issuer
  client_id      = module.auth.client_id
  origenes       = local.origenes
  mapa_key       = module.web.mapa_key
  mapa_key_arn   = module.web.mapa_key_arn
  cdn            = module.web.dominio
  firma_id       = module.web.firma_id
  llave          = module.web.llave
  llave_arn      = module.web.llave_arn
  script_capa    = "${path.module}/../scripts/capa.sh"
  correo         = var.correo_alertas
  correos_prueba = var.correos_prueba
  pool_id        = module.auth.user_pool_id
}

variable "correo_alertas" {
  description = "Correo para alarmas y presupuesto; va en terraform.tfvars (no se sube al repo)"
  type        = string
  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.correo_alertas))
    error_message = "correo_alertas debe ser un correo."
  }
}

variable "correos_prueba" {
  description = "Cuentas de Revela que recibirán recuerdos mientras SES esté en sandbox; van en terraform.tfvars"
  type        = list(string)
  default     = []
}

module "operacion" {
  source          = "./modules/operacion"
  prefijo         = local.prefijo
  correo          = var.correo_alertas
  maquina_estados = module.pipeline.maquina_estados
  cola_errores    = module.pipeline.cola_errores
  funciones       = module.pipeline.funciones
  api_id          = module.api.api_id
}

module "web" {
  source        = "./modules/web"
  prefijo       = local.prefijo
  html          = "${path.module}/../web/index.html"
  bucket_fotos  = module.datos.bucket
  fotos_arn     = module.datos.bucket_arn
  fotos_dominio = module.datos.bucket_dominio
  config = {
    api      = module.api.url
    region   = "us-east-1"
    clientId = module.auth.client_id
  }
}
