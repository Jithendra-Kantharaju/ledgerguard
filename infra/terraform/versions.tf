terraform {
  required_version = ">= 1.10.0"

  backend "s3" {
    bucket       = "ledgerguard-jithendra"
    key          = "ledgerguard/dev/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.90, < 7.0"
    }

    random = {
      source  = "hashicorp/random"
      version = ">= 3.6, < 4.0"
    }
  }
}

# Application infrastructure region comes from terraform.tfvars.
provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "Terraform"
    }
  }
}

# CloudFront requires its ACM certificate in us-east-1.
provider "aws" {
  alias  = "use1"
  region = "us-east-1"
}