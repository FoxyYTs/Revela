variable "prefijo" {
  type = string
}

data "aws_region" "actual" {}

# Cada persona entra con su correo; Cognito le envía el código de verificación
resource "aws_cognito_user_pool" "this" {
  name                     = "${var.prefijo}-usuarios"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = "INACTIVE" # terraform destroy al terminar el curso

  password_policy {
    minimum_length                   = 8
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = false
    require_uppercase                = false
    temporary_password_validity_days = 7
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  verification_message_template {
    default_email_option = "CONFIRM_WITH_CODE"
    email_subject        = "Tu código de Revela"
    email_message        = "Tu código para activar Revela es {####}"
  }
}

# Cliente público (página sin backend propio): sin secreto
resource "aws_cognito_user_pool_client" "web" {
  name                          = "${var.prefijo}-web"
  user_pool_id                  = aws_cognito_user_pool.this.id
  generate_secret               = false
  explicit_auth_flows           = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true
  access_token_validity         = 1
  id_token_validity             = 1
  refresh_token_validity        = 7

  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }
}

output "user_pool_id" {
  value = aws_cognito_user_pool.this.id
}

output "client_id" {
  value = aws_cognito_user_pool_client.web.id
}

output "issuer" {
  value = "https://cognito-idp.${data.aws_region.actual.region}.amazonaws.com/${aws_cognito_user_pool.this.id}"
}
