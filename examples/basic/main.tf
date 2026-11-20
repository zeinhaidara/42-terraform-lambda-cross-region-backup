# Example root: volumes in eu-west-2, copies in us-east-1. Never applied.
terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.73.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "2.8.1"
    }
  }
}

provider "aws" {
  alias  = "source"
  region = "eu-west-2"
}

provider "aws" {
  alias  = "destination"
  region = "us-east-1"
}

module "backup" {
  source = "../.."

  providers = {
    aws.source      = aws.source
    aws.destination = aws.destination
  }

  schedule_expression = "rate(6 hours)"
  retention_days      = 14
  dry_run             = true # flip to false once the dry-run logs look right

  tags = { project = "dr", owner = "platform@example.com" }
}

output "destination_kms_key_arn" {
  value = module.backup.destination_kms_key_arn
}
