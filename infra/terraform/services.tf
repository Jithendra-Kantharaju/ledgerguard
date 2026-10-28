
# ============================================================
# LedgerGuard AWS Services
# ECR, ECS Fargate, IAM, CloudWatch and GitHub Actions OIDC
# ============================================================

# ------------------------------------------------------------
# ECR repository
# ------------------------------------------------------------

resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

# ------------------------------------------------------------
# ECS cluster
# ------------------------------------------------------------

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

# ------------------------------------------------------------
# CloudWatch log groups
# ------------------------------------------------------------

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.name}/api"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${local.name}/worker"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "migrate" {
  name              = "/ecs/${local.name}/migrate"
  retention_in_days = 14
}

# ------------------------------------------------------------
# ECS execution role
# Used for ECR pull, CloudWatch logs and secret injection.
# ------------------------------------------------------------

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type = "Service"

      identifiers = [
        "ecs-tasks.amazonaws.com"
      ]
    }
  }
}

resource "aws_iam_role" "execution" {
  name = "${local.name}-ecs-execution"

  assume_role_policy = (
    data.aws_iam_policy_document.ecs_assume.json
  )
}

resource "aws_iam_role_policy_attachment" "execution" {
  role = aws_iam_role.execution.name

  policy_arn = (
    "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
  )
}

resource "aws_iam_role_policy" "execution_secrets" {
  name = "read-runtime-secrets"
  role = aws_iam_role.execution.id

  policy = jsonencode({
    Version = "2012-10-17"

    Statement = [
      {
        Effect = "Allow"

        Action = [
          "secretsmanager:GetSecretValue"
        ]

        Resource = [
          aws_secretsmanager_secret.database.arn,
          aws_secretsmanager_secret.openai.arn,
          aws_secretsmanager_secret.quickbooks.arn
        ]
      }
    ]
  })
}

# ------------------------------------------------------------
# ECS application task role
# Used for private S3 invoice storage.
# ------------------------------------------------------------

resource "aws_iam_role" "application" {
  name = "${local.name}-app-task"

  assume_role_policy = (
    data.aws_iam_policy_document.ecs_assume.json
  )
}

resource "aws_iam_role_policy" "s3_documents" {
  name = "invoice-s3-access"
  role = aws_iam_role.application.id

  policy = jsonencode({
    Version = "2012-10-17"

    Statement = [
      {
        Effect = "Allow"

        Action = [
          "s3:GetObject",
          "s3:PutObject"
        ]

        Resource = [
          "${aws_s3_bucket.invoices.arn}/*"
        ]
      }
    ]
  })
}

# ------------------------------------------------------------
# Common application configuration
# ------------------------------------------------------------

locals {
  image = (
    "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
  )

  common_environment = [
    {
      name  = "STORAGE_BACKEND"
      value = "s3"
    },
    {
      name  = "S3_BUCKET"
      value = aws_s3_bucket.invoices.bucket
    },
    {
      name  = "AUTH_MODE"
      value = "cognito"
    },
    {
      name  = "COGNITO_USER_POOL_ID"
      value = aws_cognito_user_pool.main.id
    },
    {
      name  = "COGNITO_CLIENT_ID"
      value = aws_cognito_user_pool_client.web.id
    },
    {
      name  = "AWS_REGION"
      value = var.aws_region
    },
    {
      name  = "CORS_ORIGINS"
      value = "https://${local.app_domain}"
    },
    {
      name  = "AP_PROVIDER"
      value = var.ap_provider
    },
    {
      name  = "SHADOW_MODE"
      value = tostring(var.shadow_mode)
    },
    {
      name  = "QBO_TENANT_SLUG"
      value = var.qbo_tenant_slug
    },
    {
      name  = "QBO_VENDOR_ID"
      value = var.qbo_vendor_id
    },
    {
      name  = "QBO_VENDOR_NAME"
      value = var.qbo_vendor_name
    },
    {
      name  = "QBO_EXPENSE_ACCOUNT_ID"
      value = var.qbo_expense_account_id
    }
  ]

  app_database_secret = {
    name = "DATABASE_URL"

    valueFrom = (
      "${aws_secretsmanager_secret.database.arn}:DATABASE_URL::"
    )
  }

  openai_secret = {
    name = "OPENAI_API_KEY"

    valueFrom = (
      aws_secretsmanager_secret.openai.arn
    )
  }

  quickbooks_secret = {
    name = "QBO_ACCESS_TOKEN"

    valueFrom = (
      aws_secretsmanager_secret.quickbooks.arn
    )
  }
}

# ------------------------------------------------------------
# FastAPI task definition
# ------------------------------------------------------------

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"

  cpu    = "512"
  memory = "1024"

  execution_role_arn = aws_iam_role.execution.arn
  task_role_arn      = aws_iam_role.application.arn

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = local.image
      essential = true

      command = [
        "uvicorn",
        "app.api.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000"
      ]

      portMappings = [
        {
          containerPort = 8000
          hostPort      = 8000
          protocol      = "tcp"
        }
      ]

      environment = concat(
        local.common_environment,
        [
          {
            name  = "EXTRACTOR"
            value = "openai"
          }
        ]
      )

      secrets = [
        local.app_database_secret
      ]

      logConfiguration = {
        logDriver = "awslogs"

        options = {
          awslogs-group = (
            aws_cloudwatch_log_group.api.name
          )

          awslogs-region = var.aws_region

          awslogs-stream-prefix = "api"
        }
      }
    }
  ])
}

# ------------------------------------------------------------
# LangGraph worker task definition
# ------------------------------------------------------------

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"

  cpu    = "1024"
  memory = "2048"

  execution_role_arn = aws_iam_role.execution.arn
  task_role_arn      = aws_iam_role.application.arn

  container_definitions = jsonencode([
    {
      name      = "worker"
      image     = local.image
      essential = true

      command = [
        "python",
        "-m",
        "app.worker.main"
      ]

      environment = concat(
        local.common_environment,
        [
          {
            name  = "EXTRACTOR"
            value = "openai"
          },
          {
            name  = "SECONDARY_EXTRACTOR"
            value = "rules_alt"
          },
          {
            name  = "OCR_BACKEND"
            value = "tesseract"
          },
          {
            name  = "WORKER_ID"
            value = "ecs-worker"
          }
        ]
      )

      secrets = [
        local.app_database_secret,
        local.openai_secret,
        local.quickbooks_secret
      ]

      logConfiguration = {
        logDriver = "awslogs"

        options = {
          awslogs-group = (
            aws_cloudwatch_log_group.worker.name
          )

          awslogs-region = var.aws_region

          awslogs-stream-prefix = "worker"
        }
      }
    }
  ])
}

# ------------------------------------------------------------
# Database migration task definition
# One-off privileged migration task, not a persistent service.
# ------------------------------------------------------------

resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"

  cpu    = "512"
  memory = "1024"

  execution_role_arn = aws_iam_role.execution.arn

  container_definitions = jsonencode([
    {
      name      = "migrate"
      image     = local.image
      essential = true

      command = [
        "sh",
        "-c",
        "python -m scripts.init_db && python -m scripts.migrate_cloud"
      ]

      secrets = [
        {
          name = "ADMIN_DATABASE_URL"

          valueFrom = (
            "${aws_secretsmanager_secret.database.arn}:ADMIN_DATABASE_URL::"
          )
        },
        {
          name = "APP_DB_PASSWORD"

          valueFrom = (
            "${aws_secretsmanager_secret.database.arn}:APP_DB_PASSWORD::"
          )
        }
      ]

      logConfiguration = {
        logDriver = "awslogs"

        options = {
          awslogs-group = (
            aws_cloudwatch_log_group.migrate.name
          )

          awslogs-region = var.aws_region

          awslogs-stream-prefix = "migrate"
        }
      }
    }
  ])
}

# ------------------------------------------------------------
# ECS API service
# ------------------------------------------------------------

resource "aws_ecs_service" "api" {
  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn

  desired_count = var.enable_services ? 1 : 0

  launch_type = "FARGATE"

  health_check_grace_period_seconds = 120

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets = aws_subnet.private[*].id

    security_groups = [
      aws_security_group.ecs.id
    ]

    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  lifecycle {
    ignore_changes = [
      task_definition
    ]
  }

  depends_on = [
    aws_lb_listener.https
  ]
}

# ------------------------------------------------------------
# ECS worker service
# ------------------------------------------------------------

resource "aws_ecs_service" "worker" {
  name            = "${local.name}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn

  desired_count = var.enable_services ? 1 : 0

  launch_type = "FARGATE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets = aws_subnet.private[*].id

    security_groups = [
      aws_security_group.ecs.id
    ]

    assign_public_ip = false
  }

  lifecycle {
    ignore_changes = [
      task_definition
    ]
  }
}

# ------------------------------------------------------------
# SNS alert topic
# ------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.notification_email
}

# ------------------------------------------------------------
# CloudWatch alarm: API target 5xx
# ------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "alb_errors" {
  alarm_name = "${local.name}-alb-5xx"

  namespace   = "AWS/ApplicationELB"
  metric_name = "HTTPCode_Target_5XX_Count"

  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"

  dimensions = {
    LoadBalancer = aws_lb.api.arn_suffix
  }

  treat_missing_data = "notBreaching"

  alarm_actions = [
    aws_sns_topic.alerts.arn
  ]
}

# ------------------------------------------------------------
# GitHub Actions OIDC provider
#
# The GitHub repository was created after July 15, 2026.
# Its default OIDC subject uses immutable identity IDs.
# ------------------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"

  client_id_list = [
    "sts.amazonaws.com"
  ]
}

# ------------------------------------------------------------
# GitHub Actions deployment IAM role
#
# Authorized repository:
# Jithendra-Kantharaju/ledgerguard
#
# Immutable owner ID: 193730490
# Immutable repo ID: 1426526327
#
# Protected environment: aws-dev
#
# IMPORTANT:
# Protect the aws-dev GitHub environment and restrict it
# to the ledgerguard-production-hardening branch.
# ------------------------------------------------------------

resource "aws_iam_role" "github_deploy" {
  name = "${local.name}-github-deploy"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"

    Statement = [
      {
        Effect = "Allow"

        Principal = {
          Federated = (
            aws_iam_openid_connect_provider.github.arn
          )
        }

        Action = "sts:AssumeRoleWithWebIdentity"

        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = (
              "sts.amazonaws.com"
            )

            "token.actions.githubusercontent.com:sub" = (
              "repo:Jithendra-Kantharaju@193730490/ledgerguard@1426526327:environment:aws-dev"
            )
          }
        }
      }
    ]
  })
}

# ------------------------------------------------------------
# GitHub Actions deployment IAM permissions
# ECR push, ECS deployment, frontend S3 sync and CloudFront.
# ------------------------------------------------------------

resource "aws_iam_role_policy" "github_deploy" {
  name = "deploy-ecs-and-frontend"
  role = aws_iam_role.github_deploy.id

  policy = jsonencode({
    Version = "2012-10-17"

    Statement = [
      # ECR authentication
      {
        Effect = "Allow"

        Action = [
          "ecr:GetAuthorizationToken"
        ]

        Resource = "*"
      },

      # Push and inspect application images
      {
        Effect = "Allow"

        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:CompleteLayerUpload",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
          "ecr:BatchGetImage"
        ]

        Resource = aws_ecr_repository.app.arn
      },

      # Register updated ECS task definitions
      {
        Effect = "Allow"

        Action = [
          "ecs:RegisterTaskDefinition"
        ]

        Resource = "*"
      },

      # Read task definitions
      {
        Effect = "Allow"

        Action = [
          "ecs:DescribeTaskDefinition"
        ]

        Resource = "*"
      },

      # Deploy application services
      {
        Effect = "Allow"

        Action = [
          "ecs:UpdateService",
          "ecs:DescribeServices"
        ]

        Resource = [
          aws_ecs_service.api.id,
          aws_ecs_service.worker.id
        ]
      },

      # Permit ECS to use only application roles
      {
        Effect = "Allow"

        Action = [
          "iam:PassRole"
        ]

        Resource = [
          aws_iam_role.execution.arn,
          aws_iam_role.application.arn
        ]
      },

      # List frontend deployment bucket
      {
        Effect = "Allow"

        Action = [
          "s3:ListBucket"
        ]

        Resource = (
          aws_s3_bucket.frontend.arn
        )
      },

      # Upload and delete React build artifacts
      {
        Effect = "Allow"

        Action = [
          "s3:PutObject",
          "s3:DeleteObject"
        ]

        Resource = (
          "${aws_s3_bucket.frontend.arn}/*"
        )
      },

      # Invalidate frontend cache after deployment
      {
        Effect = "Allow"

        Action = [
          "cloudfront:CreateInvalidation"
        ]

        Resource = (
          aws_cloudfront_distribution.frontend.arn
        )
      }
    ]
  })
}
