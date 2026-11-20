terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
      # source: where volumes and the Lambda live; destination: where copies go.
      configuration_aliases = [aws.source, aws.destination]
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.8, < 3.0"
    }
  }
}
