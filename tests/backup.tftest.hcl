# Mocked providers only. Nothing is created in AWS.
mock_provider "aws" {
  alias = "source"

  mock_data "aws_region" {
    defaults = { region = "eu-west-2" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/ebs-cross-region-backup-lambda" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:eu-west-2:123456789012:log-group:/aws/lambda/ebs-cross-region-backup" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:eu-west-2:123456789012:function:ebs-cross-region-backup" }
  }
  mock_resource "aws_cloudwatch_event_rule" {
    defaults = { arn = "arn:aws:events:eu-west-2:123456789012:rule/ebs-cross-region-backup-schedule" }
  }
}

mock_provider "aws" {
  alias = "destination"

  mock_data "aws_region" {
    defaults = { region = "us-east-1" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/11111111-2222-3333-4444-555555555555" }
  }
}

mock_provider "archive" {}

run "wiring_and_least_privilege" {
  command = apply

  assert {
    condition     = aws_lambda_function.backup.environment[0].variables["DEST_REGION"] == "us-east-1"
    error_message = "Lambda must copy to the destination region"
  }

  assert {
    condition     = aws_lambda_function.backup.environment[0].variables["DEST_KMS_KEY_ARN"] == aws_kms_key.backup.arn
    error_message = "copies must use the destination key"
  }

  assert {
    condition     = aws_lambda_function.backup.environment[0].variables["DRY_RUN"] == "true"
    error_message = "dry run must be the default"
  }

  assert {
    condition     = aws_kms_key.backup.enable_key_rotation
    error_message = "key rotation must be on"
  }

  assert {
    condition     = jsondecode(aws_kms_key.backup.policy).Statement[1].Condition.StringEquals["kms:ViaService"] == "ec2.us-east-1.amazonaws.com"
    error_message = "the function may use the key only through EC2 in the destination region"
  }

  assert {
    condition     = jsondecode(aws_kms_key.backup.policy).Statement[1].Principal.AWS == aws_iam_role.lambda.arn
    error_message = "only the function role gets key use in the key policy"
  }

  assert {
    condition = one([
      for s in jsondecode(aws_iam_role_policy.lambda.policy).Statement : s if s.Sid == "DeleteOwnCopies"
    ]).Condition.StringEquals["aws:ResourceTag/managed-by"] == "cross-region-backup"
    error_message = "delete must be limited to copies the function made"
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.lambda.policy).Statement :
      !contains(flatten([s.Action]), "*") && !contains(flatten([s.Action]), "ec2:*")
    ])
    error_message = "no wildcard actions"
  }

  assert {
    condition     = length(jsondecode(aws_iam_role_policy.lambda.policy).Statement) == 6
    error_message = "no source key statement unless source_kms_key_arns is set"
  }

  assert {
    condition     = aws_cloudwatch_event_target.lambda.arn == aws_lambda_function.backup.arn
    error_message = "schedule must target the function"
  }
}

run "source_key_statement_added_when_needed" {
  command = apply

  variables {
    source_kms_key_arns = ["arn:aws:kms:eu-west-2:123456789012:key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]
  }

  assert {
    condition     = length(jsondecode(aws_iam_role_policy.lambda.policy).Statement) == 7
    error_message = "source key statement missing"
  }
}

run "same_region_rejected" {
  command = plan

  override_data {
    target = data.aws_region.destination
    values = { region = "eu-west-2" }
  }

  expect_failures = [aws_kms_key.backup]
}

run "zero_retention_rejected" {
  command = plan

  variables {
    retention_days = 0
  }

  expect_failures = [var.retention_days]
}

run "bad_schedule_rejected" {
  command = plan

  variables {
    schedule_expression = "every 6 hours"
  }

  expect_failures = [var.schedule_expression]
}

run "bad_source_key_rejected" {
  command = plan

  variables {
    source_kms_key_arns = ["not-an-arn"]
  }

  expect_failures = [var.source_kms_key_arns]
}
