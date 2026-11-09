terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "eu-west-1"
}

variable "project" {
  description = "Project name, used for naming and tagging"
  type        = string
  default     = "tap-to-token"
}

variable "environment" {
  description = "Environment name"
  type        = string
  default     = "demo"
}

provider "aws" {
  region = var.region

  # Every resource gets these tags automatically.
  # This is what makes cost-per-transaction reporting possible later.
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Purpose     = "portfolio-simulation"
    }
  }
}
