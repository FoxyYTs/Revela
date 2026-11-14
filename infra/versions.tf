terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
  }

  required_version = ">= 1.10" # use_lockfile en el backend S3

  # Reutiliza el bucket de estado del reto (bootstrap ya hecho), con otra key
  backend "s3" {
    bucket       = "jyj-reto-tfstate-497502378363"
    key          = "revela/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "us-east-1"

  default_tags {
    tags = { Proyecto = "revela", Dueno = "jadg" }
  }
}
