data "archive_file" "lambda" {
  type        = "zip"
  source_file = "${path.module}/lambda/backup.py"
  output_path = "${path.root}/.build/${var.name}.zip"
}

resource "aws_iam_role" "lambda" {
  provider = aws.source

  name = "${var.name}-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "lambda" {
  provider = aws.source

  name   = "${var.name}-least-privilege"
  role   = aws_iam_role.lambda.id
  policy = jsonencode(local.lambda_policy)
}

locals {
  dest_snapshots = "arn:aws:ec2:${data.aws_region.destination.region}::snapshot/*"

  lambda_policy = {
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "Describe"
        Effect   = "Allow"
        Action   = ["ec2:DescribeVolumes", "ec2:DescribeSnapshots"]
        Resource = "*"
      },
      {
        # CopySnapshot is authorised in the destination region against the source snapshot.
        Sid      = "CopySnapshots"
        Effect   = "Allow"
        Action   = "ec2:CopySnapshot"
        Resource = "arn:aws:ec2:*::snapshot/*"
      },
      {
        # Tags only while creating the copy.
        Sid       = "TagCopiesOnCreate"
        Effect    = "Allow"
        Action    = "ec2:CreateTags"
        Resource  = local.dest_snapshots
        Condition = { StringEquals = { "ec2:CreateAction" = "CopySnapshot" } }
      },
      {
        # Retention may delete only the copies this function made.
        Sid       = "DeleteOwnCopies"
        Effect    = "Allow"
        Action    = "ec2:DeleteSnapshot"
        Resource  = local.dest_snapshots
        Condition = { StringEquals = { "aws:ResourceTag/managed-by" = "cross-region-backup" } }
      },
      {
        Sid      = "UseDestinationKey"
        Effect   = "Allow"
        Action   = local.key_use_actions
        Resource = aws_kms_key.backup.arn
      },
      {
        Sid      = "OwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.lambda.arn}:*"
      },
      ], length(var.source_kms_key_arns) == 0 ? [] : [{
        Sid      = "ReadEncryptedSourceSnapshots"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:DescribeKey", "kms:CreateGrant"]
        Resource = var.source_kms_key_arns
    }])
  }
}

resource "aws_cloudwatch_log_group" "lambda" {
  provider = aws.source

  name              = "/aws/lambda/${var.name}"
  retention_in_days = 30
  tags              = var.tags
}

resource "aws_lambda_function" "backup" {
  provider = aws.source

  function_name    = var.name
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "backup.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 300

  environment {
    variables = {
      DEST_REGION        = data.aws_region.destination.region
      DEST_KMS_KEY_ARN   = aws_kms_key.backup.arn
      BACKUP_TAG_KEY     = var.backup_tag_key
      BACKUP_TAG_VALUE   = var.backup_tag_value
      RETENTION_DAYS     = tostring(var.retention_days)
      MAX_COPIES_PER_RUN = tostring(var.max_copies_per_run)
      DRY_RUN            = tostring(var.dry_run)
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
  tags       = var.tags
}

resource "aws_cloudwatch_event_rule" "schedule" {
  provider = aws.source

  name                = "${var.name}-schedule"
  schedule_expression = var.schedule_expression
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "lambda" {
  provider = aws.source

  rule = aws_cloudwatch_event_rule.schedule.name
  arn  = aws_lambda_function.backup.arn
}

resource "aws_lambda_permission" "events" {
  provider = aws.source

  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.backup.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}
