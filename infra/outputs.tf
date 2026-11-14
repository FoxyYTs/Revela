output "web_url" {
  value = "https://${module.web.dominio}"
}

output "api_url" {
  value = module.api.url
}

output "user_pool_id" {
  value = module.auth.user_pool_id
}

output "client_id" {
  value = module.auth.client_id
}

output "bucket_fotos" {
  value = module.datos.bucket
}

output "maquina_estados" {
  value = module.pipeline.maquina_estados
}

# Para probar la página en local: cp de este valor a web/config.js y python -m http.server 8000
output "config_js" {
  value = module.web.config_js
}
