data "aws_caller_identity" "current" {
  provider = aws.destination
}

data "aws_region" "destination" {
  provider = aws.destination
}

data "aws_region" "source" {
  provider = aws.source
}

# Key in the destination region that encrypts every copy.
resource "aws_kms_key" "backup" {
  provider = aws.destination

  description             = "${var.name}: encrypts cross-region EBS snapshot copies"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = jsonencode(local.key_policy)
  tags                    = var.tags

  lifecycle {
    precondition {
      condition     = data.aws_region.source.region != data.aws_region.destination.region
      error_message = "The source and destination providers must point at different regions."
    }
  }
}

resource "aws_kms_alias" "backup" {
  provider = aws.destination

  name          = "alias/${var.name}"
  target_key_id = aws_kms_key.backup.key_id
}

locals {
  key_policy = {
    Version = "2012-10-17"
    Statement = [
      {
        # Account administrators manage the key through IAM, as with any key.
        Sid       = "AccountAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        # The backup function may use the key, but only through EC2 in the destination region.
        Sid       = "BackupFunctionViaEc2"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.lambda.arn }
        Action    = local.key_use_actions
        Resource  = "*"
        Condition = {
          StringEquals = { "kms:ViaService" = "ec2.${data.aws_region.destination.region}.amazonaws.com" }
        }
      },
    ]
  }

  key_use_actions = [
    "kms:Encrypt",
    "kms:Decrypt",
    "kms:ReEncrypt*",
    "kms:GenerateDataKey*",
    "kms:DescribeKey",
    "kms:CreateGrant",
  ]
}
