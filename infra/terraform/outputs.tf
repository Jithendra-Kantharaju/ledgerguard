
output "aws_region" {
  value = var.aws_region
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "frontend_bucket" {
  value = aws_s3_bucket.frontend.bucket
}

output "invoice_bucket" {
  value = aws_s3_bucket.invoices.bucket
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.frontend.id
}

output "frontend_url" {
  value = "https://${local.app_domain}"
}

output "api_url" {
  value = "https://${local.api_domain}"
}

output "cognito_user_pool_id" {
  value = aws_cognito_user_pool.main.id
}

output "cognito_client_id" {
  value = aws_cognito_user_pool_client.web.id
}

output "cognito_domain" {
  value = (
    "${aws_cognito_user_pool_domain.main.domain}.auth.${var.aws_region}.amazoncognito.com"
  )
}

output "github_deploy_role_arn" {
  value = aws_iam_role.github_deploy.arn
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "ecs_security_group_id" {
  value = aws_security_group.ecs.id
}

output "openai_secret_arn" {
  value = aws_secretsmanager_secret.openai.arn
}

output "quickbooks_secret_arn" {
  value = aws_secretsmanager_secret.quickbooks.arn
}

output "rds_endpoint" {
  value = aws_db_instance.main.endpoint
}
