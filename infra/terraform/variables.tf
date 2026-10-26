
variable "project" {
  type    = string
  default = "ledgerguard"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "aws_region" {
  type    = string
  default = "us-east-2"
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "domain_name" {
  type        = string
  description = "Registered domain, e.g. example.com"
}

variable "hosted_zone_id" {
  type        = string
  description = "Existing Route 53 public hosted zone ID"
}

variable "github_repository" {
  type        = string
  description = "GitHub owner/repository"
}

variable "github_branch" {
  type    = string
  default = "ledgerguard-production-hardening"
}

variable "cognito_domain_prefix" {
  type        = string
  description = "Globally unique Cognito hosted-login prefix"
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "enable_services" {
  type        = bool
  default     = false
  description = "Enable API and worker after DB migration and secrets"
}

variable "image_tag" {
  type    = string
  default = "bootstrap"
}

variable "shadow_mode" {
  type    = bool
  default = true
}

variable "ap_provider" {
  type    = string
  default = "quickbooks"
}

variable "qbo_tenant_slug" {
  type    = string
  default = "acme"
}

variable "qbo_vendor_id" {
  type    = string
  default = ""
}

variable "qbo_vendor_name" {
  type    = string
  default = ""
}

variable "qbo_expense_account_id" {
  type    = string
  default = ""
}

variable "notification_email" {
  type        = string
  description = "Email address for AWS operational alerts"
}

variable "multi_az" {
  type    = bool
  default = false
}

variable "deletion_protection" {
  type    = bool
  default = false
}

locals {
  name       = "${var.project}-${var.environment}"
  app_domain = "app.${var.domain_name}"
  api_domain = "api.${var.domain_name}"
}
